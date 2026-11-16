"""The real provider (LiteLLM) against a local capture server, and the fake-mode fixtures.

The capture server listens on 127.0.0.1 only and answers like the provider APIs, so these tests
check the HTTP request LiteLLM 1.108.6 really builds for every role — model id, thinking and
effort settings, no temperature, structured output, API key — without network or a real key.
A LiteLLM upgrade that changes any of it fails here (owner decision, P3-01).
"""

import json
import subprocess
import sys
import threading
from collections.abc import Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Literal, NoReturn

import litellm
import pytest
from litellm import exceptions as litellm_errors
from pydantic import BaseModel, Field

from hub.config import Settings
from hub.domain.enums import LlmCallContext, LlmCallMode, LlmNode
from hub.llm.gateway import (
    FIXTURES_DIR,
    FixtureProvider,
    LiteLLMProvider,
    _provider_error,
    _usage,
    write_fixture,
)
from hub.llm.record import RequestSpec, load_specs, spec_requests
from hub.llm.schemas import (
    CallContext,
    FixtureMissingError,
    OutboundCardDataError,
    OutboundIbanError,
    ProviderError,
    ProviderRequest,
    ProviderResponse,
    Usage,
)
from hub.llm.structured import provider_schema

from ._llm import MemoryRecorder, ScriptedProvider, make_gateway

ANTHROPIC_KEY = "test-anthropic-key"  # a placeholder; never a real key


class Verdict(BaseModel):
    intent: str


@dataclass
class Captured:
    path: str
    headers: dict[str, str]
    body: dict[str, Any]


@dataclass
class CaptureServer:
    base: str
    seen: list[Captured] = field(default_factory=list)


def _answer(body: dict[str, Any]) -> dict[str, Any]:
    """An Anthropic Messages API answer: a tool call when tools were sent, else JSON text."""
    content: list[dict[str, Any]] = [{"type": "text", "text": '{"intent": "fees_fx"}'}]
    if body.get("tools"):
        content = [
            {"type": "tool_use", "id": "toolu_1", "name": "kb_search", "input": {"query": "fees"}}
        ]
    return {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "model": body["model"],
        "content": content,
        "stop_reason": "tool_use" if body.get("tools") else "end_turn",
        "usage": {
            "input_tokens": 10,
            "output_tokens": 5,
            "cache_read_input_tokens": 3,
            "cache_creation_input_tokens": 2,
        },
    }


@pytest.fixture(scope="module")
def capture() -> Iterator[CaptureServer]:
    server_state = CaptureServer(base="")

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            body = json.loads(self.rfile.read(int(self.headers["content-length"])))
            server_state.seen.append(
                Captured(self.path, {k.lower(): v for k, v in self.headers.items()}, body)
            )
            data = json.dumps(_answer(body)).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args: object) -> None:
            return None

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server_state.base = f"http://127.0.0.1:{server.server_address[1]}"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server_state
    server.shutdown()


def _provider(capture: CaptureServer) -> LiteLLMProvider:
    settings = Settings(_env_file=None, anthropic_api_key=ANTHROPIC_KEY)
    return LiteLLMProvider(settings, api_bases={"anthropic": capture.base})


async def _send(
    capture: CaptureServer, request: ProviderRequest
) -> tuple[ProviderResponse, Captured]:
    capture.seen.clear()
    provider = _provider(capture)
    response = await provider.complete(request)
    [captured] = capture.seen
    return response, captured


MESSAGES = [
    {"role": "system", "content": "You classify tickets."},
    {"role": "user", "content": "<ticket>How much is the FX fee?</ticket>"},
]
TOOL = {
    "type": "function",
    "function": {
        "name": "kb_search",
        "description": "Search the knowledge base.",
        "parameters": {"type": "object", "properties": {"query": {"type": "string"}}},
    },
}

# role, model index (0 model, 1 fallback) -> what the wire body must contain. Every chat role runs
# on Haiku 5.5 with thinking off and falls back to Sonnet 5.5 (ADR-0018).
_HAIKU_WIRE = {"model": "claude-haiku-5-5", "thinking": {"type": "disabled"}}
_SONNET_WIRE = {
    "model": "claude-sonnet-5-5",
    "thinking": {"type": "between_tools"},
    "output_config": {"effort": "low"},
}
_MAX_TOKENS = {"triage": 400, "research": 800, "write": 900, "verify": 700, "judge": 800}
EXPECTED: dict[tuple[str, int], dict[str, Any]] = {
    (role, index): {**wire, "max_tokens": max_tokens}
    for role, max_tokens in _MAX_TOKENS.items()
    for index, wire in enumerate((_HAIKU_WIRE, _SONNET_WIRE))
}


def _role_cases() -> list[Any]:
    return [pytest.param(role, index, id=f"{role}-{index}") for role, index in EXPECTED]


@pytest.mark.parametrize(("role", "index"), _role_cases())
async def test_litellm_sends_what_the_role_config_says(
    capture: CaptureServer, role: str, index: int
) -> None:
    gateway = make_gateway(ScriptedProvider({}))
    tools = [TOOL] if role == "research" else None
    schema = None if role == "research" else Verdict
    request = gateway.chat_requests(role, MESSAGES, schema=schema, tools=tools)[index]

    response, captured = await _send(capture, request)
    body = captured.body

    for key, value in EXPECTED[(role, index)].items():
        assert body.get(key) == value, f"{role}[{index}] {key}: {body.get(key)!r}"
    for forbidden in ("temperature", "top_p", "top_k", "api_base", "api_key"):
        assert forbidden not in body
    assert captured.path.endswith("/v1/messages")
    assert captured.headers["x-api-key"] == ANTHROPIC_KEY
    assert body["system"] == [{"type": "text", "text": "You classify tickets."}]
    if schema is not None:
        assert body["output_format"]["schema"] == provider_schema(Verdict)
    # LiteLLM adds cache reads and writes to prompt_tokens (3 + 2 on top of 10).
    assert response.usage == Usage(
        input_tokens=15, output_tokens=5, cached_tokens=3, cache_write_tokens=2
    )
    if tools:
        [sent_tool] = body["tools"]
        assert sent_tool["name" if "name" in sent_tool else "function"]


class _Cite(BaseModel):
    marker: int = Field(ge=1)
    source_id: str


class _Draft(BaseModel):
    """Shaped like DraftAnswer (ai-pipeline §5): nested model, list, nullable field, limits."""

    can_answer: bool
    missing_information: str | None
    answer: str = Field(max_length=1200, description="customer text")
    urgency: Literal["low", "normal", "high"] = "normal"
    citations: list[_Cite]


def _nodes(value: object) -> Iterator[dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for item in value.values():
            yield from _nodes(item)
    elif isinstance(value, list):
        for item in value:
            yield from _nodes(item)


@pytest.mark.parametrize("model_index", [0, 1], ids=["haiku", "sonnet"])
async def test_a_nested_schema_reaches_model_and_fallback_strict(
    capture: CaptureServer, model_index: int
) -> None:
    # verify: Haiku 5.5, fallback Sonnet 5.5. LiteLLM must send our strict schema natively
    # (Anthropic `output_format`, not the forced-tool trick).
    request = make_gateway(ScriptedProvider({})).chat_requests("verify", MESSAGES, _Draft)[
        model_index
    ]
    _, captured = await _send(capture, request)
    body = captured.body
    assert "tools" not in body
    assert "tool_choice" not in body
    # LiteLLM inlines the `$defs` for Anthropic, so the strict rules are checked node by node.
    sent = body["output_format"]["schema"]
    objects = [node for node in _nodes(sent) if node.get("type") == "object"]
    assert len(objects) >= 2  # the answer and the citation
    for node in objects:
        assert node["additionalProperties"] is False
        assert node["required"] == list(node["properties"])
    for node in _nodes(sent):
        assert not {"maxLength", "minimum", "title", "default"} & set(node)


async def test_tool_calls_come_back_as_tool_calls(capture: CaptureServer) -> None:
    gateway = make_gateway(ScriptedProvider({}))
    request = gateway.chat_requests("research", MESSAGES, tools=[TOOL])[1]
    response, _ = await _send(capture, request)
    [call] = response.tool_calls
    assert (call.id, call.name, json.loads(call.arguments)) == (
        "toolu_1",
        "kb_search",
        {"query": "fees"},
    )


async def test_a_real_embedding_call_is_not_available_until_p5_01(
    capture: CaptureServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Embeddings come from a local model (ADR-0018), never through LiteLLM.
    async def never(**kwargs: object) -> NoReturn:
        raise AssertionError("no embedding call may go to a provider")

    monkeypatch.setattr(litellm, "aembedding", never)
    capture.seen.clear()
    request = make_gateway(ScriptedProvider({})).embedding_request(["a", "b"])
    with pytest.raises(ProviderError) as raised:
        await _provider(capture).embed(request)
    assert (raised.value.kind, raised.value.transient) == ("LocalEmbeddingNotAvailable", False)
    assert capture.seen == []


async def test_a_missing_key_fails_before_any_call(capture: CaptureServer) -> None:
    capture.seen.clear()
    provider = LiteLLMProvider(Settings(_env_file=None), api_bases={"anthropic": capture.base})
    request = make_gateway(ScriptedProvider({})).chat_requests("triage", MESSAGES)[0]
    with pytest.raises(ProviderError) as raised:
        await provider.complete(request)
    assert (raised.value.kind, raised.value.transient) == ("MissingApiKey", False)
    assert capture.seen == []


async def test_provider_failures_become_provider_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fail(**kwargs: object) -> NoReturn:
        raise litellm_errors.RateLimitError(
            message="slow down; your prompt was: card 4111 1111 1111 1111",
            llm_provider="anthropic",
            model="claude-haiku-5-5",
        )

    monkeypatch.setattr(litellm, "acompletion", fail)
    provider = LiteLLMProvider(Settings(_env_file=None, anthropic_api_key=ANTHROPIC_KEY))
    chat = make_gateway(ScriptedProvider({})).chat_requests("triage", MESSAGES)[0]
    with pytest.raises(ProviderError) as raised:
        await provider.complete(chat)
    # Only class and status: the provider text (which may echo the request) is dropped.
    assert raised.value.summary == "RateLimitError (429)"
    assert raised.value.transient
    assert raised.value.__cause__ is None


@pytest.mark.parametrize(
    ("error", "transient"),
    [
        (litellm_errors.Timeout(message="t", model="m", llm_provider="anthropic"), True),
        (litellm_errors.APIConnectionError(message="c", llm_provider="anthropic", model="m"), True),
        (
            litellm_errors.ServiceUnavailableError(
                message="s", llm_provider="anthropic", model="m"
            ),
            True,
        ),
        (
            litellm_errors.InternalServerError(message="i", llm_provider="anthropic", model="m"),
            True,
        ),
        (litellm_errors.BadGatewayError(message="g", llm_provider="anthropic", model="m"), True),
        (litellm_errors.BadRequestError(message="b", model="m", llm_provider="anthropic"), False),
        (
            litellm_errors.AuthenticationError(message="a", llm_provider="anthropic", model="m"),
            False,
        ),
        (ValueError("anything else"), False),
    ],
)
def test_which_errors_are_retried(error: Exception, transient: bool) -> None:
    assert _provider_error(error).transient is transient


def test_a_provider_error_passes_through_unchanged() -> None:
    error = ProviderError("LocalEmbeddingNotAvailable", transient=False)
    assert _provider_error(error) is error


# --- fake mode -----------------------------------------------------------------------------------


def _request(text: str = "hi") -> ProviderRequest:
    return ProviderRequest(
        kind="chat",
        model="anthropic/claude-haiku-5-5",
        messages=[{"role": "user", "content": text}],
        max_tokens=400,
        timeout_s=30,
    )


async def test_a_written_fixture_is_served_back(tmp_path: Path) -> None:
    response = ProviderResponse(content="recorded", usage=Usage(input_tokens=7, output_tokens=3))
    path = write_fixture(tmp_path, _request(), response)
    assert path.name == f"{_request().fixture_key()}.json"
    assert await FixtureProvider(tmp_path).complete(_request()) == response
    # The timeout is not part of the key: changing it must not orphan recordings.
    assert _request().model_copy(update={"timeout_s": 5}).fixture_key() == _request().fixture_key()


async def test_a_missing_or_broken_fixture_is_an_error(tmp_path: Path) -> None:
    provider = FixtureProvider(tmp_path)
    with pytest.raises(FixtureMissingError, match="make record-llm"):
        await provider.embed(_request())
    provider.path(_request()).write_text("{}", encoding="utf-8")
    with pytest.raises(FixtureMissingError, match="invalid"):
        await provider.complete(_request())


def test_a_request_with_a_raw_iban_is_never_recorded(tmp_path: Path) -> None:
    with pytest.raises(OutboundIbanError):
        write_fixture(tmp_path, _request("iban DE89 3704 0044 0532 0130 00"), ProviderResponse())
    assert list(tmp_path.iterdir()) == []


def test_a_request_with_card_data_is_never_recorded(tmp_path: Path) -> None:
    with pytest.raises(OutboundCardDataError):
        write_fixture(tmp_path, _request("card 4111 1111 1111 1111"), ProviderResponse())
    assert list(tmp_path.iterdir()) == []


def _specs() -> list[tuple[Path, RequestSpec]]:
    return load_specs(FIXTURES_DIR)


def test_there_is_at_least_one_recorded_request() -> None:
    assert _specs()


@pytest.mark.parametrize(
    ("spec_path", "spec"), _specs(), ids=lambda item: getattr(item, "stem", "")
)
async def test_every_request_spec_has_a_fixture_for_each_of_its_models(
    spec_path: Path, spec: RequestSpec
) -> None:
    """Fake mode works end to end through the gateway for every request in the repository."""
    recorder = MemoryRecorder()
    provider = FixtureProvider(FIXTURES_DIR)
    fake = make_gateway(ScriptedProvider({}), recorder)
    requests = spec_requests(fake, spec)
    for request in requests:
        assert provider.path(request).exists(), f"{spec_path.name}: no fixture for {request.model}"
    fake = type(fake)(
        models=fake.models,
        pricing=fake.pricing,
        provider=provider,
        recorder=recorder,
        mode=LlmCallMode.FAKE,
        budget=None,
    )
    call = CallContext(
        context=LlmCallContext.ONLINE, node=LlmNode.TRIAGE, prompt_version=spec.prompt_version
    )
    schema = spec.schema_model
    if spec.texts:
        result = await fake.embed(spec.texts, call=call, role=spec.role)
        assert len(result.vectors) == len(spec.texts)
    elif schema is not None:
        # A recorded structured answer must be valid as recorded: no repair call in fake mode.
        structured = await fake.complete_structured(spec.role, spec.messages, schema, call=call)
        assert not structured.repaired
    else:
        completion = await fake.complete(spec.role, spec.messages, tools=spec.tools, call=call)
        assert completion.content or completion.tool_calls
    assert recorder.rows[-1].cost_usd >= 0


def test_importing_the_gateway_loads_no_env_file_into_the_environment(tmp_path: Path) -> None:
    """LiteLLM's default DEV mode copies the nearest .env into os.environ at import."""
    (tmp_path / ".env").write_text("HUB_SENTINEL_FROM_DOTENV=leaked\n", encoding="utf-8")
    script = (
        "import os, hub.llm.gateway; print(os.environ.get('HUB_SENTINEL_FROM_DOTENV', 'absent'))"
    )
    result = subprocess.run(
        [sys.executable, "-c", script], cwd=tmp_path, capture_output=True, text=True, check=True
    )
    assert result.stdout.strip() == "absent"


def test_a_response_without_usage_counts_zero_tokens() -> None:
    assert _usage(None) == Usage()


class _StatusError(Exception):
    def __init__(self, status_code: object) -> None:
        super().__init__("provider said no")
        self.status_code = status_code


@pytest.mark.parametrize(
    ("status_code", "transient"),
    [(500, True), (502, True), (599, True), (499, False), ("x", False)],
)
def test_any_5xx_status_is_transient(status_code: object, transient: bool) -> None:
    error = _provider_error(_StatusError(status_code))
    assert error.transient is transient
    assert error.status_code == (status_code if isinstance(status_code, int) else None)


async def test_an_unreadable_answer_is_a_provider_error(monkeypatch: pytest.MonkeyPatch) -> None:
    class Empty:
        choices = ()
        usage = None

    async def empty(**kwargs: object) -> Empty:
        return Empty()

    monkeypatch.setattr(litellm, "acompletion", empty)
    provider = LiteLLMProvider(Settings(_env_file=None, anthropic_api_key=ANTHROPIC_KEY))
    chat = make_gateway(ScriptedProvider({})).chat_requests("triage", MESSAGES)[0]
    with pytest.raises(ProviderError) as raised:
        await provider.complete(chat)
    assert (raised.value.kind, raised.value.transient) == ("InvalidResponse", False)


def test_litellm_switches_are_set_and_its_loggers_pinned_at_warning() -> None:
    import logging
    import os

    from hub.llm.gateway import LITELLM_LOGGERS, LITELLM_SWITCHES

    assert LITELLM_SWITCHES == {
        "LITELLM_MODE": "PRODUCTION",
        "LITELLM_LOCAL_MODEL_COST_MAP": "True",
        "LITELLM_LOCAL_ANTHROPIC_BETA_HEADERS": "True",
    }
    assert all(os.environ[name] == value for name, value in LITELLM_SWITCHES.items())
    for name in LITELLM_LOGGERS:
        assert logging.getLogger(name).level == logging.WARNING
