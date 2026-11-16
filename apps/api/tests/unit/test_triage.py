"""Triage (P3-03): 20 hand-written example tickets, answered from recorded Haiku responses.

The expected labels in `tests/fixtures/triage/examples.jsonl` were written before the first real
call and are not tuned to the model. Where the model disagrees, the owner decides (PR of P3-03);
such an example is listed in `DISAGREES` with what the model answered, and the test checks that
the disagreement is still exactly that.
"""

import uuid
from typing import get_args

import pytest

from hub.domain.enums import LlmCallContext, LlmCallMode, LlmCallStatus, LlmNode
from hub.domain.triage import Intent, TriageResult
from hub.graph.nodes.triage import triage, triage_messages
from hub.llm.gateway import FIXTURES_DIR, FixtureProvider, Gateway
from hub.llm.prompts import PromptError, active_prompt, parse_prompt
from hub.llm.schemas import StructuredOutputError

from ._llm import ROOT, MemoryRecorder, ScriptedProvider, make_gateway, ok, real_models
from ._triage_requests import GENERATED, Example, load_examples, render, request_specs

HAIKU = "anthropic/claude-haiku-5-5"
SONNET = "anthropic/claude-sonnet-5-5"


EXAMPLES = load_examples()
PROMPT = active_prompt(ROOT / "prompts", real_models(), "triage")

# Example id -> {field: what the recorded model answer says}, from the first real recording
# (2026-11-16, Haiku 5.5, triage/v1). The owner decides whether the label or the prompt is
# wrong; labels are never changed only to make this test pass.
DISAGREES: dict[str, dict[str, object]] = {
    # Account takeover with a €900 outgoing transfer: the label keeps secondary intent null;
    # Haiku answers `payment_pending` (which is for incoming money). Known disagreement, kept
    # by owner decision (P3-03 review round 1): no triage/v2 for it.
    "triage_14": {"secondary_intent": "payment_pending"},
    # "I need a bank statement as a PDF": labelled `normal` by the prompt's rule (`low` is for a
    # general question; this is a request, like triage_11/13/17). Haiku answers `low`. Known
    # disagreement, kept by owner decision (P3-03 review round 2): the prompt does not say what
    # urgency a request with no money stuck gets; a later prompt version may.
    "triage_16": {"urgency": "low"},
}


def _fake_gateway(recorder: MemoryRecorder) -> Gateway:
    fake = make_gateway(ScriptedProvider({}), recorder)
    return Gateway(
        models=fake.models,
        pricing=fake.pricing,
        provider=FixtureProvider(FIXTURES_DIR),
        recorder=recorder,
        mode=LlmCallMode.FAKE,
        budget=None,
    )


def test_the_request_files_match_the_examples_and_the_active_prompt() -> None:
    """A prompt or example change needs `make llm-requests` and then `make record-llm`."""
    specs = request_specs()
    requests = FIXTURES_DIR / "requests"
    on_disk = {path.name for path in requests.iterdir() if GENERATED.fullmatch(path.name)}
    assert on_disk == set(specs), "run `make llm-requests`"
    for name, spec in specs.items():
        assert (requests / name).read_text(encoding="utf-8") == render(spec), (
            f"{name} is out of date: run `make llm-requests` and `make record-llm`"
        )


def test_the_examples_cover_every_intent_and_the_hard_cases() -> None:
    assert len(EXAMPLES) == 20
    assert len({example.id for example in EXAMPLES}) == 20
    assert {example.expected.intent for example in EXAMPLES} == set(get_args(Intent))
    expected = [example.expected for example in EXAMPLES]
    assert any(item.secondary_intent for item in expected)
    assert any(item.wants_human for item in expected)
    assert any(item.reports_unauthorised_activity for item in expected)
    assert any(item.is_complaint_or_legal for item in expected)
    assert any(item.injection_suspected for item in expected)
    assert {item.asks_for_action for item in expected} >= {"none", "unfreeze", "limit_increase"}


@pytest.mark.parametrize("example", EXAMPLES, ids=lambda example: example.id)
async def test_an_example_ticket_gets_the_expected_intent_and_flags(example: Example) -> None:
    recorder = MemoryRecorder()
    result = await triage(_fake_gateway(recorder), PROMPT, example.ticket)

    assert isinstance(result, TriageResult)
    expected = example.expected.model_dump()
    got = {field: getattr(result, field) for field in expected}
    wrong = {field: got[field] for field in expected if got[field] != expected[field]}
    assert wrong == DISAGREES.get(example.id, {}), f"{example.id}: model disagrees on {wrong}"
    assert 0 < len(result.summary) <= 300

    # One `llm_calls` row, with the prompt version.
    [row] = recorder.rows
    assert (row.node, row.context, row.status) == (
        LlmNode.TRIAGE,
        LlmCallContext.ONLINE,
        LlmCallStatus.OK,
    )
    assert row.prompt_version == "triage/v1"
    assert row.model == HAIKU


async def test_triage_records_its_run_and_context() -> None:
    recorder = MemoryRecorder()
    run_id = uuid.uuid4()
    await triage(
        _fake_gateway(recorder),
        PROMPT,
        EXAMPLES[0].ticket,
        run_id=run_id,
        context=LlmCallContext.EVAL,
    )
    [row] = recorder.rows
    assert (row.run_id, row.context, row.prompt_version) == (
        run_id,
        LlmCallContext.EVAL,
        "triage/v1",
    )


async def test_the_fallback_model_also_answers_the_first_example() -> None:
    """triage_01 is recorded on Sonnet too (with thinking `between_tools`), so fake mode covers
    the fallback path and the strict schema is confirmed on both models."""
    [_, fallback] = _fake_gateway(MemoryRecorder()).chat_requests(
        "triage", triage_messages(PROMPT, EXAMPLES[0].ticket), TriageResult
    )
    assert fallback.model == SONNET
    response = await FixtureProvider(FIXTURES_DIR).complete(fallback)
    assert TriageResult.model_validate_json(response.content or "").intent == "payment_pending"


async def test_two_invalid_answers_raise_and_no_result_is_guessed() -> None:
    provider = ScriptedProvider({HAIKU: [ok("not json"), ok('{"intent": "fees_fx"}')]})
    recorder = MemoryRecorder()
    with pytest.raises(StructuredOutputError, match="TriageResult"):
        await triage(make_gateway(provider, recorder), PROMPT, "What are your fees?")
    assert [row.status for row in recorder.rows] == [LlmCallStatus.INVALID_OUTPUT] * 2
    assert {row.prompt_version for row in recorder.rows} == {"triage/v1"}


def test_triage_refuses_a_prompt_of_another_node() -> None:
    other = parse_prompt(
        "---\nversion: v1\nrole: write\nchanged: 2026-11-16\nwhy: x\n---\n"
        "# System\n\ns\n\n# User\n\n{{ticket}}\n",
        "write",
        "v1",
    )
    with pytest.raises(PromptError, match="not a triage prompt"):
        triage_messages(other, "hi")
