"""LLM gateway (P3-01): G2 outbound scan, retries and fallback, timeouts, tools rule, records.

No network, no API key: the provider is scripted (`_llm.ScriptedProvider`).
"""

import uuid
from decimal import Decimal

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import BaseModel, Field

from hub.domain.enums import LlmCallContext, LlmCallStatus, LlmNode
from hub.guardrails.masking import luhn_valid
from hub.llm.config import ConfigError, parse_models
from hub.llm.gateway import BACKOFF_S, RETRIES, scan_outbound
from hub.llm.schemas import (
    CallContext,
    FixtureMissingError,
    LlmUnavailableError,
    OutboundCardDataError,
    ProviderError,
    ProviderRequest,
    ProviderResponse,
    ToolsNotAllowedError,
    Usage,
)

from ._llm import CALL, MemoryRecorder, ScriptedProvider, Sleeps, make_gateway, ok, real_models

HAIKU = "anthropic/claude-haiku-5-5"
SONNET = "anthropic/claude-sonnet-5-5"
LUNA = "openai/gpt-6-luna"
SOL = "openai/gpt-6.1-sol"
EMBEDDING = "openai/text-embedding-3-small"
VISA = "4111 1111 1111 1111"  # public test card number

RATE_LIMIT = ProviderError("RateLimitError", transient=True, status_code=429)
BAD_REQUEST = ProviderError("BadRequestError", transient=False, status_code=400)


def user(text: str) -> list[dict[str, str]]:
    return [
        {"role": "system", "content": "You classify tickets."},
        {"role": "user", "content": text},
    ]


# --- G2: outbound card scan ---------------------------------------------------------------------


class Leaky(BaseModel):
    note: str = Field(description=f"for example {VISA}")


TOOL_WITH_CARD = {
    "type": "function",
    "function": {"name": "lookup", "description": f"card {VISA}", "parameters": {}},
}


@pytest.mark.parametrize(
    ("messages", "schema", "tools", "role"),
    [
        pytest.param(user(f"my card is {VISA}"), None, None, "triage", id="message"),
        pytest.param(user("hello"), Leaky, None, "triage", id="schema"),
        pytest.param(user("hello"), None, [TOOL_WITH_CARD], "research", id="tool"),
        pytest.param(
            [{"role": "user", "content": [{"type": "text", "text": f"pan {VISA}"}]}],
            None,
            None,
            "triage",
            id="content-block",
        ),
        pytest.param(user("my CVV is 123"), None, None, "triage", id="cvv"),
    ],
)
async def test_a_request_with_card_data_is_blocked_and_audited(
    messages: list[dict[str, object]],
    schema: type[BaseModel] | None,
    tools: list[dict[str, object]] | None,
    role: str,
) -> None:
    provider = ScriptedProvider({})
    recorder = MemoryRecorder()
    gateway = make_gateway(provider, recorder)
    run_id = uuid.uuid4()
    call = CallContext(context=LlmCallContext.ONLINE, node=LlmNode.TRIAGE, run_id=run_id)

    with pytest.raises(OutboundCardDataError):
        await gateway.complete(role, messages, schema=schema, tools=tools, call=call)

    assert provider.requests == []  # no model ever saw it
    [(row, entities)] = recorder.blocked
    assert entities
    assert all(count > 0 for count in entities.values())
    assert recorder.rows == [row]
    assert row.status is LlmCallStatus.ERROR
    assert row.error == "blocked: G2"
    assert row.run_id == run_id
    assert (row.input_tokens, row.output_tokens, row.cost_usd) == (0, 0, Decimal(0))
    assert VISA.replace(" ", "") not in repr(recorder.blocked).replace(" ", "")


async def test_embedding_texts_are_scanned_too() -> None:
    provider = ScriptedProvider({})
    recorder = MemoryRecorder()
    call = CallContext(context=LlmCallContext.ONLINE, node=LlmNode.EMBEDDING)
    with pytest.raises(OutboundCardDataError):
        await make_gateway(provider, recorder).embed(["fine", f"card {VISA}"], call=call)
    assert provider.requests == []
    assert recorder.blocked[0][1] == {"PAN": 1}


async def test_masked_card_passes_the_scan() -> None:
    provider = ScriptedProvider({HAIKU: [ok()]})
    result = await make_gateway(provider).complete(
        "triage", user("my card [CARD ****1111] was declined"), call=CALL
    )
    assert result.content == "fine"


def test_neighbouring_strings_do_not_join_into_a_card_number() -> None:
    request = ProviderRequest(
        kind="chat",
        model=HAIKU,
        messages=[
            {"role": "user", "content": "4111 1111"},
            {"role": "user", "content": "1111 1111"},
        ],
        timeout_s=1,
    )
    assert scan_outbound(request) == {}


def test_a_masking_failure_blocks(monkeypatch: pytest.MonkeyPatch) -> None:
    from hub.guardrails import masking
    from hub.llm import gateway as gateway_module

    monkeypatch.setattr(
        gateway_module,
        "mask",
        lambda text: masking.MaskResult(text="#", counts={}, failed=True),
    )
    request = ProviderRequest(kind="chat", model=HAIKU, messages=user("hi"), timeout_s=1)
    assert scan_outbound(request)["MASKING_FAILED"] > 0


@st.composite
def luhn_numbers(draw: st.DrawFn) -> str:
    body = draw(st.text(alphabet="0123456789", min_size=12, max_size=15))
    check = next(d for d in "0123456789" if luhn_valid(body + d))
    number = body + check
    separator = draw(st.sampled_from(["", " ", "-"]))
    return separator.join(number[i : i + 4] for i in range(0, len(number), 4))


@settings(max_examples=200, deadline=None)
@given(
    number=luhn_numbers(),
    before=st.text(alphabet=st.characters(categories=["L", "Zs", "P"]), max_size=40),
    after=st.text(alphabet=st.characters(categories=["L", "Zs", "P"]), max_size=40),
)
def test_any_valid_luhn_number_in_any_message_is_found(
    number: str, before: str, after: str
) -> None:
    request = ProviderRequest(
        kind="chat", model=HAIKU, messages=user(f"{before} {number} {after}"), timeout_s=1
    )
    assert scan_outbound(request).get("PAN", 0) >= 1


# --- retries and fallback ------------------------------------------------------------------------


async def test_provider_error_triggers_retries_then_the_fallback() -> None:
    provider = ScriptedProvider({HAIKU: [RATE_LIMIT, RATE_LIMIT, RATE_LIMIT], LUNA: [ok("ok")]})
    recorder = MemoryRecorder()
    sleeps = Sleeps()

    result = await make_gateway(provider, recorder, sleeps).complete(
        "triage", user("hello"), call=CALL
    )

    assert result.fallback_used
    assert result.model == LUNA
    assert result.content == "ok"
    assert [request.model for request in provider.requests] == [HAIKU] * (RETRIES + 1) + [LUNA]
    assert recorder.statuses() == [
        (HAIKU, LlmCallStatus.RETRY),
        (HAIKU, LlmCallStatus.RETRY),
        (HAIKU, LlmCallStatus.ERROR),
        (LUNA, LlmCallStatus.OK),
    ]
    assert sleeps.waits == [BACKOFF_S, BACKOFF_S * 2]
    assert recorder.rows[0].error == "RateLimitError (429)"
    # The fallback gets its own parameters (Luna needs reasoning_effort none).
    assert provider.requests[-1].params == {"reasoning_effort": "none"}


async def test_a_transient_error_then_success_stays_on_the_model() -> None:
    provider = ScriptedProvider({HAIKU: [RATE_LIMIT, ok()]})
    recorder = MemoryRecorder()
    result = await make_gateway(provider, recorder).complete("triage", user("hi"), call=CALL)
    assert not result.fallback_used
    assert recorder.statuses() == [(HAIKU, LlmCallStatus.RETRY), (HAIKU, LlmCallStatus.OK)]


async def test_a_permanent_error_goes_to_the_fallback_without_retries() -> None:
    provider = ScriptedProvider({HAIKU: [BAD_REQUEST], LUNA: [ok()]})
    recorder = MemoryRecorder()
    sleeps = Sleeps()
    result = await make_gateway(provider, recorder, sleeps).complete(
        "triage", user("hi"), call=CALL
    )
    assert result.fallback_used
    assert recorder.statuses() == [(HAIKU, LlmCallStatus.ERROR), (LUNA, LlmCallStatus.OK)]
    assert sleeps.waits == []


async def test_when_everything_fails_the_error_is_typed_and_every_attempt_is_recorded() -> None:
    provider = ScriptedProvider({HAIKU: [RATE_LIMIT] * 3, LUNA: [RATE_LIMIT] * 3})
    recorder = MemoryRecorder()
    with pytest.raises(LlmUnavailableError):
        await make_gateway(provider, recorder).complete("triage", user("hi"), call=CALL)
    assert len(recorder.rows) == 2 * (RETRIES + 1)
    assert [row.status for row in recorder.rows].count(LlmCallStatus.ERROR) == 2


async def test_a_role_without_fallback_fails_after_its_retries() -> None:
    provider = ScriptedProvider({SOL: [RATE_LIMIT] * 3})
    recorder = MemoryRecorder()
    call = CallContext(context=LlmCallContext.EVAL, node=LlmNode.JUDGE)
    with pytest.raises(LlmUnavailableError):
        await make_gateway(provider, recorder).complete("judge", user("grade"), call=call)
    assert [model for model, _ in recorder.statuses()] == [SOL] * 3


async def test_a_hanging_call_times_out_and_counts_as_transient() -> None:
    models = real_models()
    triage = models.roles["triage"].model_copy(update={"timeout_s": 0.01})
    models = models.model_copy(update={"roles": {**models.roles, "triage": triage}})
    provider = ScriptedProvider({HAIKU: [5.0, ok()]})
    recorder = MemoryRecorder()
    result = await make_gateway(provider, recorder, models=models).complete(
        "triage", user("hi"), call=CALL
    )
    assert result.content == "fine"
    assert recorder.rows[0].status is LlmCallStatus.RETRY
    assert recorder.rows[0].error == "Timeout"


async def test_a_missing_fixture_is_raised_not_retried_or_sent_to_the_fallback() -> None:
    provider = ScriptedProvider({HAIKU: [FixtureMissingError("no fixture")]})
    recorder = MemoryRecorder()
    with pytest.raises(FixtureMissingError):
        await make_gateway(provider, recorder).complete("triage", user("hi"), call=CALL)
    assert len(provider.requests) == 1
    # Still one row for the attempt (one row per call, also for failures).
    assert recorder.statuses() == [(HAIKU, LlmCallStatus.ERROR)]
    assert recorder.rows[0].error == "FixtureMissing"


async def test_an_unexpected_provider_failure_is_recorded_and_goes_to_the_fallback() -> None:
    provider = ScriptedProvider({HAIKU: [IndexError("list index out of range")], LUNA: [ok()]})
    recorder = MemoryRecorder()
    sleeps = Sleeps()
    result = await make_gateway(provider, recorder, sleeps).complete(
        "triage", user("hi"), call=CALL
    )
    assert result.fallback_used
    assert recorder.statuses() == [(HAIKU, LlmCallStatus.ERROR), (LUNA, LlmCallStatus.OK)]
    assert recorder.rows[0].error == "IndexError"  # the class only, never the message
    assert sleeps.waits == []


async def test_a_paid_call_that_cannot_be_priced_is_recorded_with_its_tokens() -> None:
    from hub.llm.gateway import Gateway

    from ._llm import real_pricing

    pricing = real_pricing()
    without_haiku = {name: price for name, price in pricing.models.items() if name != HAIKU}
    provider = ScriptedProvider({HAIKU: [ok(input_tokens=300, output_tokens=40)]})
    recorder = MemoryRecorder()
    gateway = Gateway(
        models=real_models(),
        pricing=pricing.model_copy(update={"models": without_haiku}),
        provider=provider,
        recorder=recorder,
    )
    with pytest.raises(ConfigError, match="no price"):
        await gateway.complete("triage", user("hi"), call=CALL)
    [row] = recorder.rows
    assert (row.status, row.error, row.input_tokens, row.output_tokens) == (
        LlmCallStatus.ERROR,
        "PricingError",
        300,
        40,
    )


# --- records -------------------------------------------------------------------------------------


async def test_a_successful_call_records_tokens_cost_latency_model_and_prompt_version() -> None:
    usage = {"input_tokens": 1000, "output_tokens": 200, "cached_tokens": 400}
    provider = ScriptedProvider({HAIKU: [ok("fine", **usage)]})
    recorder = MemoryRecorder()
    run_id = uuid.uuid4()
    call = CallContext(
        context=LlmCallContext.ONLINE, node=LlmNode.TRIAGE, run_id=run_id, prompt_version="v3"
    )
    result = await make_gateway(provider, recorder, clock=[10.0, 10.25]).complete(
        "triage", user("hi"), call=call
    )

    [row] = recorder.rows
    # 600 uncached x 0.10 + 400 cached x 0.01 + 200 output x 0.50, per million tokens.
    expected = Decimal("0.000164")
    assert result.cost_usd == row.cost_usd == expected
    assert result.latency_ms == row.latency_ms == 250
    assert (row.input_tokens, row.output_tokens, row.cached_tokens) == (1000, 200, 400)
    assert (row.model, row.prompt_version, row.run_id, row.node, row.context) == (
        HAIKU,
        "v3",
        run_id,
        LlmNode.TRIAGE,
        LlmCallContext.ONLINE,
    )
    assert row.status is LlmCallStatus.OK
    assert row.error is None


async def test_embeddings_are_recorded_and_priced() -> None:
    vectors = [[0.1, 0.2], [0.3, 0.4]]
    response = ProviderResponse(vectors=vectors, usage=Usage(input_tokens=50_000))
    provider = ScriptedProvider({EMBEDDING: [response]})
    recorder = MemoryRecorder()
    call = CallContext(context=LlmCallContext.ONLINE, node=LlmNode.EMBEDDING)
    result = await make_gateway(provider, recorder).embed(["a", "b"], call=call)
    assert result.vectors == vectors
    assert result.cost_usd == Decimal("0.001000")  # 50,000 x 0.02 per million
    assert recorder.rows[0].prompt_version is None


async def test_a_wrong_number_of_vectors_is_an_error() -> None:
    provider = ScriptedProvider({EMBEDDING: [ProviderResponse(vectors=[[0.1]])]})
    call = CallContext(context=LlmCallContext.ONLINE, node=LlmNode.EMBEDDING)
    with pytest.raises(LlmUnavailableError):
        await make_gateway(provider).embed(["a", "b"], call=call)


# --- roles and tools -----------------------------------------------------------------------------

TOOL = {"type": "function", "function": {"name": "kb_search", "parameters": {"type": "object"}}}


@pytest.mark.parametrize("role", ["triage", "write", "verify", "judge"])
async def test_tools_are_refused_for_a_role_without_tools(role: str) -> None:
    provider = ScriptedProvider({})
    with pytest.raises(ToolsNotAllowedError):
        await make_gateway(provider).complete(role, user("hi"), tools=[TOOL], call=CALL)
    assert provider.requests == []


async def test_the_research_role_passes_tools_to_model_and_fallback() -> None:
    provider = ScriptedProvider({SONNET: [BAD_REQUEST], LUNA: [ok()]})
    await make_gateway(provider).complete("research", user("hi"), tools=[TOOL], call=CALL)
    assert [request.tools for request in provider.requests] == [[TOOL], [TOOL]]


async def test_a_schema_is_passed_as_json_schema() -> None:
    class Answer(BaseModel):
        intent: str

    provider = ScriptedProvider({HAIKU: [ok('{"intent": "fees_fx"}')]})
    await make_gateway(provider).complete("triage", user("hi"), schema=Answer, call=CALL)
    [request] = provider.requests
    assert request.schema_name == "Answer"
    assert request.json_schema == Answer.model_json_schema()


async def test_unknown_role_and_wrong_kind_are_config_errors() -> None:
    gateway = make_gateway(ScriptedProvider({}))
    with pytest.raises(ConfigError, match="unknown model role"):
        await gateway.complete("poetry", user("hi"), call=CALL)
    with pytest.raises(ConfigError, match="not a chat role"):
        await gateway.complete("embedding", user("hi"), call=CALL)
    with pytest.raises(ConfigError, match="not a embedding role"):
        await gateway.embed(["x"], call=CALL, role="triage")


def test_config_refuses_tools_on_a_model_that_cannot_call_them() -> None:
    # The owner's rule for P3-01: a role mapped to GPT-6.1 Sol must never be given tools.
    data = {
        "models": {SOL: {"tools": False}, SONNET: {"tools": True}},
        "roles": {
            "write": {
                "model": SONNET,
                "fallback": {"model": SOL},
                "max_tokens": 900,
                "timeout_s": 60,
                "tools": True,
            }
        },
        "prompts": {},
    }
    with pytest.raises(ConfigError, match=r"may receive tools, but openai/gpt-6.1-sol"):
        parse_models(data)
