"""Structured output (P3-02, ADR-0004): strict provider schema, answer check, one repair call.

No network, no API key: the provider is scripted (`_llm.ScriptedProvider`).
"""

import json
from typing import Any, Literal

import pytest
from pydantic import BaseModel, Field

from hub.domain.enums import LlmCallStatus
from hub.llm.schemas import (
    LlmUnavailableError,
    OutboundCardDataError,
    ProviderError,
    ProviderResponse,
    StructuredOutputError,
    Usage,
)
from hub.llm.structured import (
    MAX_ERRORS_SHOWN,
    Invalid,
    SchemaNotSupportedError,
    check_answer,
    provider_schema,
    repair_messages,
)

from ._llm import CALL, MemoryRecorder, ScriptedProvider, make_gateway, ok, real_pricing

HAIKU = "anthropic/claude-haiku-5-5"
LUNA = "openai/gpt-6-luna"
VISA = "4111 1111 1111 1111"  # public test card number
BAD_REQUEST = ProviderError("BadRequestError", transient=False, status_code=400)
RATE_LIMIT = ProviderError("RateLimitError", transient=True, status_code=429)

Intent = Literal["fees_fx", "limits", "other"]


class Triage(BaseModel):
    """Shaped like TriageResult (ai-pipeline §5)."""

    intent: Intent
    secondary_intent: Intent | None
    urgency: Literal["low", "normal", "high"]
    wants_human: bool
    summary: str = Field(max_length=300, description="for the operator")


class Citation(BaseModel):
    marker: int = Field(ge=1)
    source_id: str


class Draft(BaseModel):
    """Shaped like DraftAnswer (ai-pipeline §5)."""

    can_answer: bool
    missing_information: str | None = None
    answer: str
    citations: list[Citation] = Field(max_length=10)


VALID = json.dumps(
    {
        "intent": "fees_fx",
        "secondary_intent": None,
        "urgency": "normal",
        "wants_human": False,
        "summary": "Asks about the FX fee.",
    }
)
WRONG_ENUM = json.dumps({**json.loads(VALID), "urgency": "panic"})
MISSING_FIELD = json.dumps({"intent": "fees_fx"})


def messages(text: str = "How much is the FX fee?") -> list[dict[str, str]]:
    return [
        {"role": "system", "content": "You classify tickets."},
        {"role": "user", "content": text},
    ]


# --- provider schema ----------------------------------------------------------------------------


def _nodes(value: object) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    if isinstance(value, dict):
        found.append(value)
        for item in value.values():
            found.extend(_nodes(item))
    elif isinstance(value, list):
        for item in value:
            found.extend(_nodes(item))
    return found


def test_a_flat_schema_becomes_strict() -> None:
    schema = provider_schema(Triage)
    assert schema["additionalProperties"] is False
    assert schema["required"] == list(Triage.model_fields)
    assert "title" not in schema
    properties = schema["properties"]
    # A nullable field stays nullable and becomes required: the model writes null.
    assert properties["secondary_intent"]["anyOf"][-1] == {"type": "null"}
    assert properties["urgency"] == {"type": "string", "enum": ["low", "normal", "high"]}
    # A limit the provider may refuse moves into the description; Pydantic still enforces it.
    assert properties["summary"] == {
        "type": "string",
        "description": "for the operator Constraint: at most 300 characters.",
    }


def test_nested_models_lists_defaults_and_limits() -> None:
    schema = provider_schema(Draft)
    citation = schema["$defs"]["Citation"]
    assert citation["additionalProperties"] is False
    assert citation["required"] == ["marker", "source_id"]
    assert citation["properties"]["marker"] == {
        "type": "integer",
        "description": "Constraint: at least 1.",
    }
    assert schema["required"] == ["can_answer", "missing_information", "answer", "citations"]
    assert schema["properties"]["citations"]["description"] == "Constraint: at most 10 items."
    for node in _nodes(schema):
        assert not {"title", "default", "maxLength", "minimum", "maxItems"} & set(node)


def test_a_property_named_like_a_keyword_is_kept() -> None:
    class Odd(BaseModel):
        title: str
        default: int
        pattern: str

    schema = provider_schema(Odd)
    assert schema["required"] == ["title", "default", "pattern"]
    assert set(schema["properties"]) == {"title", "default", "pattern"}


def test_the_schema_is_deterministic() -> None:
    assert json.dumps(provider_schema(Draft)) == json.dumps(provider_schema(Draft))


class OpenMap(BaseModel):
    scores: dict[str, float]


class Untyped(BaseModel):
    data: dict[str, Any]


class Choice(BaseModel):
    kind: Literal["a"]


class Other(BaseModel):
    kind: Literal["b"]


class Tagged(BaseModel):
    item: Choice | Other = Field(discriminator="kind")


@pytest.mark.parametrize(
    ("model", "match"),
    [
        pytest.param(OpenMap, "open object", id="dict-of-floats"),
        pytest.param(Untyped, "open object", id="dict-any"),
        pytest.param(Tagged, "oneOf", id="discriminated-union"),
    ],
)
def test_schemas_strict_mode_cannot_express_are_refused(model: type[BaseModel], match: str) -> None:
    with pytest.raises(SchemaNotSupportedError, match=match):
        provider_schema(model)


# --- answer check and repair request ------------------------------------------------------------


def test_a_valid_answer_is_parsed() -> None:
    result = check_answer(Triage, VALID)
    assert isinstance(result, Triage)
    assert result.intent == "fees_fx"


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        pytest.param(None, "(answer): the answer was empty", id="none"),
        pytest.param("   ", "(answer): the answer was empty", id="blank"),
        pytest.param("The intent is fees.", "(answer): Invalid JSON", id="prose"),
        pytest.param('{"intent": "fees_fx"', "(answer): Invalid JSON", id="cut-off"),
        pytest.param(WRONG_ENUM, "urgency: Input should be 'low', 'normal' or 'high'", id="enum"),
        pytest.param(MISSING_FIELD, "urgency: Field required", id="missing"),
        pytest.param(
            json.dumps({**json.loads(VALID), "summary": "x" * 301}),
            "summary: String should have at most 300 characters",
            id="too-long",
        ),
    ],
)
def test_invalid_answers_say_why(content: str | None, expected: str) -> None:
    result = check_answer(Triage, content)
    assert isinstance(result, Invalid)
    assert any(error.startswith(expected) for error in result.errors), result.errors


def test_errors_never_echo_the_answer() -> None:
    customer_text = "Ms Example wants a refund"
    result = check_answer(Triage, json.dumps({**json.loads(VALID), "urgency": customer_text}))
    assert isinstance(result, Invalid)
    assert customer_text not in " ".join(result.errors)
    assert result.summary == "ValidationError (1)"


def test_the_repair_request_holds_the_answer_and_the_errors() -> None:
    original = messages()
    invalid = Invalid(("urgency: Field required",))
    repaired = repair_messages(original, MISSING_FIELD, invalid)
    assert repaired[:2] == original
    assert repaired[2] == {"role": "assistant", "content": MISSING_FIELD}
    assert repaired[3]["role"] == "user"
    assert "- urgency: Field required" in repaired[3]["content"]
    assert original == messages()  # the caller's list is not changed


@pytest.mark.parametrize("content", [None, "", "  "])
def test_an_empty_answer_is_not_sent_back(content: str | None) -> None:
    repaired = repair_messages(messages(), content, Invalid(("(answer): empty",)))
    assert [message["role"] for message in repaired] == ["system", "user", "user"]


def test_long_error_lists_are_cut() -> None:
    errors = tuple(f"field_{n}: Field required" for n in range(MAX_ERRORS_SHOWN + 3))
    text = repair_messages(messages(), "{}", Invalid(errors))[-1]["content"]
    assert f"field_{MAX_ERRORS_SHOWN - 1}" in text
    assert f"field_{MAX_ERRORS_SHOWN}:" not in text
    assert "- and 3 more" in text


# --- complete_structured ------------------------------------------------------------------------


async def test_a_valid_answer_needs_one_call() -> None:
    provider = ScriptedProvider({HAIKU: [ok(VALID)]})
    recorder = MemoryRecorder()
    result = await make_gateway(provider, recorder).complete_structured(
        "triage", messages(), Triage, call=CALL
    )
    assert result.value == Triage.model_validate_json(VALID)
    assert (result.model, result.repaired, result.fallback_used) == (HAIKU, False, False)
    assert recorder.statuses() == [(HAIKU, LlmCallStatus.OK)]
    [request] = provider.requests
    assert request.json_schema == provider_schema(Triage)
    assert request.schema_name == "Triage"


async def test_an_invalid_answer_and_a_valid_repair_succeed() -> None:
    provider = ScriptedProvider({HAIKU: [ok(WRONG_ENUM), ok(VALID, input_tokens=150)]})
    recorder = MemoryRecorder()
    result = await make_gateway(provider, recorder).complete_structured(
        "triage", messages(), Triage, call=CALL
    )
    assert result.value.urgency == "normal"
    assert result.repaired is True
    assert recorder.statuses() == [
        (HAIKU, LlmCallStatus.INVALID_OUTPUT),
        (HAIKU, LlmCallStatus.OK),
    ]
    invalid_row, ok_row = recorder.rows
    assert invalid_row.error == "ValidationError (1)"
    assert invalid_row.prompt_version == ok_row.prompt_version == "v1"
    assert invalid_row.cost_usd > 0  # an invalid answer was still paid for

    # Tokens and cost of both calls are summed.
    assert result.usage == Usage(input_tokens=250, output_tokens=40)
    pricing = real_pricing()
    assert result.cost_usd == pricing.cost(
        HAIKU, Usage(input_tokens=100, output_tokens=20)
    ) + pricing.cost(HAIKU, Usage(input_tokens=150, output_tokens=20))

    first, repair = provider.requests
    assert repair.messages[: len(first.messages)] == first.messages
    assert repair.messages[-2] == {"role": "assistant", "content": WRONG_ENUM}
    assert "urgency: Input should be" in repair.messages[-1]["content"]
    assert repair.json_schema == first.json_schema


async def test_two_invalid_answers_are_a_typed_error() -> None:
    provider = ScriptedProvider({HAIKU: [ok(WRONG_ENUM), ok("not json")]})
    recorder = MemoryRecorder()
    with pytest.raises(StructuredOutputError, match="Triage") as caught:
        await make_gateway(provider, recorder).complete_structured(
            "triage", messages(), Triage, call=CALL
        )
    assert (caught.value.schema_name, caught.value.errors) == ("Triage", 1)
    assert "not json" not in str(caught.value)
    assert recorder.statuses() == [
        (HAIKU, LlmCallStatus.INVALID_OUTPUT),
        (HAIKU, LlmCallStatus.INVALID_OUTPUT),
    ]
    assert len(provider.requests) == 2  # exactly one repair call, never a third


async def test_an_invalid_answer_is_not_retried_or_sent_to_the_fallback() -> None:
    # Only the repair call follows; Luna has no script, so any call to it would fail the test.
    provider = ScriptedProvider({HAIKU: [ProviderResponse(content=None), ok(VALID)]})
    result = await make_gateway(provider).complete_structured(
        "triage", messages(), Triage, call=CALL
    )
    assert (result.model, result.fallback_used) == (HAIKU, False)
    # The empty answer is left out of the repair request.
    assert [m["role"] for m in provider.requests[1].messages] == ["system", "user", "user"]


async def test_the_repair_goes_to_the_fallback_when_the_fallback_answered() -> None:
    provider = ScriptedProvider({HAIKU: [BAD_REQUEST], LUNA: [ok(MISSING_FIELD), ok(VALID)]})
    recorder = MemoryRecorder()
    result = await make_gateway(provider, recorder).complete_structured(
        "triage", messages(), Triage, call=CALL
    )
    assert (result.model, result.fallback_used, result.repaired) == (LUNA, True, True)
    assert recorder.statuses() == [
        (HAIKU, LlmCallStatus.ERROR),
        (LUNA, LlmCallStatus.INVALID_OUTPUT),
        (LUNA, LlmCallStatus.OK),
    ]
    assert [request.model for request in provider.requests] == [HAIKU, LUNA, LUNA]
    assert provider.requests[2].params == {"reasoning_effort": "none"}


async def test_a_failing_repair_call_gets_retries_then_the_fallback() -> None:
    provider = ScriptedProvider(
        {HAIKU: [ok(WRONG_ENUM), RATE_LIMIT, RATE_LIMIT, RATE_LIMIT], LUNA: [ok(VALID)]}
    )
    recorder = MemoryRecorder()
    result = await make_gateway(provider, recorder).complete_structured(
        "triage", messages(), Triage, call=CALL
    )
    assert (result.model, result.fallback_used, result.repaired) == (LUNA, True, True)
    assert recorder.statuses() == [
        (HAIKU, LlmCallStatus.INVALID_OUTPUT),
        (HAIKU, LlmCallStatus.RETRY),
        (HAIKU, LlmCallStatus.RETRY),
        (HAIKU, LlmCallStatus.ERROR),
        (LUNA, LlmCallStatus.OK),
    ]
    # The fallback gets the repair conversation, not the original one.
    assert provider.requests[-1].messages[-2] == {"role": "assistant", "content": WRONG_ENUM}


async def test_a_repair_with_no_model_left_is_unavailable() -> None:
    provider = ScriptedProvider({HAIKU: [BAD_REQUEST], LUNA: [ok(WRONG_ENUM), BAD_REQUEST]})
    with pytest.raises(LlmUnavailableError):
        await make_gateway(provider).complete_structured("triage", messages(), Triage, call=CALL)


async def test_card_data_in_the_invalid_answer_blocks_the_repair() -> None:
    leaky = json.dumps({**json.loads(VALID), "urgency": f"card {VISA}"})
    provider = ScriptedProvider({HAIKU: [ok(leaky)]})
    recorder = MemoryRecorder()
    with pytest.raises(OutboundCardDataError):
        await make_gateway(provider, recorder).complete_structured(
            "triage", messages(), Triage, call=CALL
        )
    assert len(provider.requests) == 1
    assert recorder.statuses()[-1] == (HAIKU, LlmCallStatus.ERROR)
    assert recorder.rows[-1].error == "blocked: G2"
    assert len(recorder.blocked) == 1


async def test_complete_with_a_schema_does_not_validate() -> None:
    # `complete` is the low-level call: it passes the schema on and returns the raw text.
    provider = ScriptedProvider({HAIKU: [ok("not json")]})
    recorder = MemoryRecorder()
    completion = await make_gateway(provider, recorder).complete(
        "triage", messages(), schema=Triage, call=CALL
    )
    assert completion.content == "not json"
    assert recorder.statuses() == [(HAIKU, LlmCallStatus.OK)]
