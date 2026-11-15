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
from typing import Any, NoReturn

import litellm
import pytest
from litellm import exceptions as litellm_errors
from pydantic import BaseModel

from hub.config import Settings
from hub.domain.enums import LlmCallContext, LlmNode
from hub.llm.gateway import (
    FIXTURES_DIR,
    FixtureProvider,
    LiteLLMProvider,
    _provider_error,
    _usage,
    write_fixture,
)
from hub.llm.record import RequestSpec, load_specs
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

from ._llm import MemoryRecorder, ScriptedProvider, make_gateway

ANTHROPIC_KEY = "test-anthropic-key"  # placeholders; never real keys
OPENAI_KEY = "test-openai-key"


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


def _answer(path: str, body: dict[str, Any]) -> dict[str, Any]:
    if path.endswith("/v1/messages"):
        return {
            "id": "msg_1",
            "type": "message",
            "role": "assistant",
            "model": body["model"],
            "content": [{"type": "text", "text": '{"intent": "fees_fx"}'}],
            "stop_reason": "end_turn",
            "usage": {
                "input_tokens": 10,
                "output_tokens": 5,
                "cache_read_input_tokens": 3,
                "cache_creation_input_tokens": 2,
            },
        }
    if path.endswith("/embeddings"):
        return {
            "object": "list",
            "data": [
                {"object": "embedding", "index": i, "embedding": [0.1, 0.2]}
                for i, _ in enumerate(body["input"])
            ],
            "model": body["model"],
            "usage": {"prompt_tokens": 4, "total_tokens": 4},
        }
    message: dict[str, Any] = {"role": "assistant", "content": "hello"}
    if body.get("tools"):
        message = {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "kb_search", "arguments": '{"query": "fees"}'},
                }
            ],
        }
    return {
        "id": "c1",
        "object": "chat.completion",
        "created": 1,
        "model": body["model"],
        "choices": [{"index": 0, "finish_reason": "stop", "message": message}],
        "usage": {
            "prompt_tokens": 10,
            "completion_tokens": 5,
            "total_tokens": 15,
            "prompt_tokens_details": {"cached_tokens": 4},
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
            data = json.dumps(_answer(self.path, body)).encode()
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
    settings = Settings(_env_file=None, anthropic_api_key=ANTHROPIC_KEY, openai_api_key=OPENAI_KEY)
    return LiteLLMProvider(settings, api_bases={"anthropic": capture.base, "openai": capture.base})


async def _send(
    capture: CaptureServer, request: ProviderRequest
) -> tuple[ProviderResponse, Captured]:
    capture.seen.clear()
    provider = _provider(capture)
    send = provider.embed if request.kind == "embedding" else provider.complete
    response = await send(request)
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

# role, model index (0 model, 1 fallback) -> what the wire body must contain / must not contain.
EXPECTED: dict[tuple[str, int], dict[str, Any]] = {
    ("triage", 0): {
        "model": "claude-haiku-5-5",
        "max_tokens": 400,
        "thinking": {"type": "disabled"},
    },
    ("triage", 1): {
        "model": "gpt-6-luna",
        "max_completion_tokens": 400,
        "reasoning_effort": "none",
    },
    ("research", 0): {
        "model": "claude-sonnet-5-5",
        "max_tokens": 800,
        "thinking": {"type": "between_tools"},
        "output_config": {"effort": "low"},
    },
    ("research", 1): {
        "model": "gpt-6-luna",
        "max_completion_tokens": 800,
        "reasoning_effort": "none",
    },
    ("write", 0): {
        "model": "claude-sonnet-5-5",
        "max_tokens": 900,
        "thinking": {"type": "between_tools"},
        "output_config": {"effort": "low"},
    },
    ("write", 1): {"model": "gpt-6.1-sol", "max_completion_tokens": 900, "reasoning_effort": "low"},
    ("verify", 0): {
        "model": "claude-haiku-5-5",
        "max_tokens": 700,
        "thinking": {"type": "disabled"},
    },
    ("verify", 1): {
        "model": "gpt-6-luna",
        "max_completion_tokens": 700,
        "reasoning_effort": "none",
    },
    ("judge", 0): {"model": "gpt-6.1-sol", "max_completion_tokens": 800, "reasoning_effort": "low"},
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
    if request.model.startswith("anthropic/"):
        assert captured.path.endswith("/v1/messages")
        assert captured.headers["x-api-key"] == ANTHROPIC_KEY
        assert body["system"] == [{"type": "text", "text": "You classify tickets."}]
        if schema is not None:
            assert body["output_format"]["schema"]["properties"] == {
                "intent": {"title": "Intent", "type": "string"}
            }
        # LiteLLM adds cache reads and writes to prompt_tokens (3 + 2 on top of 10).
        assert response.usage == Usage(
            input_tokens=15, output_tokens=5, cached_tokens=3, cache_write_tokens=2
        )
    else:
        assert captured.path.endswith("/chat/completions")
        assert captured.headers["authorization"] == f"Bearer {OPENAI_KEY}"
        assert "max_tokens" not in body
        if schema is not None:
            assert body["response_format"]["type"] == "json_schema"
            assert body["response_format"]["json_schema"]["name"] == "Verdict"
            assert body["response_format"]["json_schema"]["strict"] is True
        assert response.usage == Usage(input_tokens=10, output_tokens=5, cached_tokens=4)
    if tools:
        [sent_tool] = body["tools"]
        assert sent_tool["name" if "name" in sent_tool else "function"]


async def test_tool_calls_come_back_as_tool_calls(capture: CaptureServer) -> None:
    gateway = make_gateway(ScriptedProvider({}))
    request = gateway.chat_requests("research", MESSAGES, tools=[TOOL])[1]
    response, _ = await _send(capture, request)
    [call] = response.tool_calls
    assert (call.id, call.name, json.loads(call.arguments)) == (
        "call_1",
        "kb_search",
        {"query": "fees"},
    )


async def test_embeddings_go_to_the_embeddings_endpoint(capture: CaptureServer) -> None:
    request = make_gateway(ScriptedProvider({})).embedding_request(["a", "b"])
    response, captured = await _send(capture, request)
    assert captured.path.endswith("/embeddings")
    assert captured.body == {"model": "text-embedding-3-small", "input": ["a", "b"]}
    assert response.vectors == [[0.1, 0.2], [0.1, 0.2]]
    assert response.usage == Usage(input_tokens=4)


async def test_a_missing_key_fails_before_any_call(capture: CaptureServer) -> None:
    capture.seen.clear()
    provider = LiteLLMProvider(Settings(_env_file=None), api_bases={"openai": capture.base})
    request = ProviderRequest(
        kind="embedding", model="openai/text-embedding-3-small", texts=["a"], timeout_s=5
    )
    with pytest.raises(ProviderError) as raised:
        await provider.embed(request)
    assert (raised.value.kind, raised.value.transient) == ("MissingApiKey", False)
    assert capture.seen == []


async def test_provider_failures_become_provider_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fail(**kwargs: object) -> NoReturn:
        raise litellm_errors.RateLimitError(
            message="slow down; your prompt was: card 4111 1111 1111 1111",
            llm_provider="openai",
            model="gpt-6-luna",
        )

    monkeypatch.setattr(litellm, "acompletion", fail)
    monkeypatch.setattr(litellm, "aembedding", fail)
    provider = LiteLLMProvider(Settings(_env_file=None, openai_api_key=OPENAI_KEY))
    chat = ProviderRequest(
        kind="chat", model="openai/gpt-6-luna", messages=MESSAGES, max_tokens=5, timeout_s=5
    )
    embed = ProviderRequest(
        kind="embedding", model="openai/text-embedding-3-small", texts=["a"], timeout_s=5
    )
    for call in (provider.complete(chat), provider.embed(embed)):
        with pytest.raises(ProviderError) as raised:
            await call
        # Only class and status: the provider text (which may echo the request) is dropped.
        assert raised.value.summary == "RateLimitError (429)"
        assert raised.value.transient
        assert raised.value.__cause__ is None


@pytest.mark.parametrize(
    ("error", "transient"),
    [
        (litellm_errors.Timeout(message="t", model="m", llm_provider="openai"), True),
        (litellm_errors.APIConnectionError(message="c", llm_provider="openai", model="m"), True),
        (
            litellm_errors.ServiceUnavailableError(message="s", llm_provider="openai", model="m"),
            True,
        ),
        (litellm_errors.InternalServerError(message="i", llm_provider="openai", model="m"), True),
        (litellm_errors.BadGatewayError(message="g", llm_provider="openai", model="m"), True),
        (litellm_errors.BadRequestError(message="b", model="m", llm_provider="openai"), False),
        (litellm_errors.AuthenticationError(message="a", llm_provider="openai", model="m"), False),
        (ValueError("anything else"), False),
    ],
)
def test_which_errors_are_retried(error: Exception, transient: bool) -> None:
    assert _provider_error(error).transient is transient


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
    requests = (
        [fake.embedding_request(spec.texts, spec.role)]
        if spec.texts
        else fake.chat_requests(spec.role, spec.messages, tools=spec.tools)
    )
    for request in requests:
        assert provider.path(request).exists(), f"{spec_path.name}: no fixture for {request.model}"
    fake = type(fake)(
        models=fake.models, pricing=fake.pricing, provider=provider, recorder=recorder
    )
    call = CallContext(context=LlmCallContext.ONLINE, node=LlmNode.TRIAGE, prompt_version="v1")
    if spec.texts:
        result = await fake.embed(spec.texts, call=call, role=spec.role)
        assert len(result.vectors) == len(spec.texts)
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
        data = ({"no_embedding": 1},)
        usage = None

    async def empty(**kwargs: object) -> Empty:
        return Empty()

    monkeypatch.setattr(litellm, "acompletion", empty)
    monkeypatch.setattr(litellm, "aembedding", empty)
    provider = LiteLLMProvider(Settings(_env_file=None, openai_api_key=OPENAI_KEY))
    chat = ProviderRequest(
        kind="chat", model="openai/gpt-6-luna", messages=MESSAGES, max_tokens=5, timeout_s=5
    )
    embed = ProviderRequest(
        kind="embedding", model="openai/text-embedding-3-small", texts=["a"], timeout_s=5
    )
    for call in (provider.complete(chat), provider.embed(embed)):
        with pytest.raises(ProviderError) as raised:
            await call
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
