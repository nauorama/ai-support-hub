"""The phase-3 run (P3-05): pure helpers and the recording requests. The run against a database
is tested in tests/integration/test_linear_run.py."""

import asyncio
import uuid
from types import SimpleNamespace
from typing import Any, cast

import pytest

from hub.db.models import Message
from hub.domain.draft import Citation, DraftAnswer, ProposedAction
from hub.domain.enums import MessageRole
from hub.domain.triage import TriageResult
from hub.graph import linear
from hub.graph.linear import (
    VARIANT,
    OutputMask,
    RunTasks,
    draft_from_answer,
    drop_markers,
    history_turns,
    run_failed_problem,
)
from hub.graph.nodes.intake import ChatTurn, intake
from hub.graph.nodes.triage import audit_details
from hub.guardrails.masking import MaskResult
from hub.kb.full_text import KbSource
from hub.llm.gateway import FIXTURES_DIR

from ._run_requests import GENERATED, load_tickets, render, request_specs

SOURCES = [
    KbSource("withdrawal-times", "How long does a withdrawal take?", "/help/withdrawal-times", ()),
    KbSource("withdrawal-fees", "Withdrawal fees", "/help/withdrawal-fees", ()),
]


def test_the_request_files_match_the_run_tickets_prompts_and_kb() -> None:
    """A prompt, ticket or KB change needs `make llm-requests` and then `make record-llm`."""
    specs = request_specs()
    requests = FIXTURES_DIR / "requests"
    on_disk = {path.name for path in requests.iterdir() if GENERATED.fullmatch(path.name)}
    assert on_disk == set(specs), "run `make llm-requests`"
    for name, spec in specs.items():
        assert (requests / name).read_text(encoding="utf-8") == render(spec), (
            f"{name} is out of date: run `make llm-requests` and `make record-llm`"
        )


def test_the_run_tickets_cover_an_answer_and_no_answer() -> None:
    tickets = load_tickets()
    assert [ticket.id for ticket in tickets] == ["run_01", "run_02"]


def _answer(**changes: Any) -> DraftAnswer:  # noqa: ANN401 - any field value
    values: dict[str, Any] = {
        "can_answer": True,
        "missing_information": None,
        "answer": "It takes 1 business day [1]. Write to <EMAIL_1> if not [2].",
        "citations": [
            Citation(marker=1, source_id="withdrawal-times"),
            Citation(marker=2, source_id="internal-escalation-procedures"),
        ],
        "proposed_actions": [],
    }
    return DraftAnswer(**(values | changes))


def test_a_draft_keeps_known_citations_and_counts_unknown_ones() -> None:
    values, unknown = draft_from_answer(_answer(), SOURCES, OutputMask(intake("ana@example.com")))

    assert unknown == 1
    assert values["citations"] == [
        {
            "marker": 1,
            "source_id": "withdrawal-times",
            "doc_id": "withdrawal-times",
            "title": "How long does a withdrawal take?",
            "url": "/help/withdrawal-times",
        }
    ]
    assert values["evidence"] == {
        "variant": VARIANT,
        "dropped_markers": [2],
        "documents": [
            {
                "doc_id": "withdrawal-times",
                "title": "How long does a withdrawal take?",
                "url": "/help/withdrawal-times",
            }
        ],
    }


def test_a_draft_gets_the_runs_placeholders_restored() -> None:
    values, _ = draft_from_answer(_answer(), SOURCES, OutputMask(intake("ana@example.com")))
    # [2] cites an internal document: the citation is dropped, and so is its marker (N2).
    assert values["body"] == "It takes 1 business day [1]. Write to ana@example.com if not."
    assert values["evidence"]["dropped_markers"] == [2]


def test_a_draft_that_cannot_answer_has_an_empty_body_and_the_missing_information() -> None:
    answer = _answer(
        can_answer=False,
        answer="some text the model should not have written",
        missing_information="Interest on balances (<EMAIL_1>) is not in the KB.",
        citations=[],
    )
    values, unknown = draft_from_answer(answer, SOURCES, OutputMask(intake("ana@example.com")))

    assert (values["can_answer"], values["body"], unknown) == (False, "", 0)
    assert (
        values["missing_information"] == "Interest on balances (ana@example.com) is not in the KB."
    )


def test_proposed_actions_get_ids_for_the_operator() -> None:
    action = ProposedAction(
        type="jira_create_issue",
        issue_type="dispute",
        summary="Dispute for <EMAIL_1>",
        description="Check the payment.",
    )
    values, _ = draft_from_answer(
        _answer(proposed_actions=[action, action]), SOURCES, OutputMask(intake("ana@example.com"))
    )
    assert [item["id"] for item in values["proposed_actions"]] == ["a1", "a2"]
    assert values["proposed_actions"][0] == {
        "id": "a1",
        "type": "jira_create_issue",
        "params": {
            "issue_type": "dispute",
            "summary": "Dispute for ana@example.com",
            "description": "Check the payment.",
        },
    }


def test_history_is_the_messages_before_the_trigger() -> None:
    def message(role: MessageRole, body: str) -> Message:
        return cast(Message, SimpleNamespace(id=uuid.uuid4(), role=role, body=body))

    first = message(MessageRole.CUSTOMER, "first")
    reply = message(MessageRole.OPERATOR, "reply")
    trigger = message(MessageRole.CUSTOMER, "now")
    later = message(MessageRole.CUSTOMER, "later")

    assert history_turns([first, reply, trigger, later], trigger.id) == [
        ChatTurn(role="customer", text="first"),
        ChatTurn(role="operator", text="reply"),
    ]


def test_the_triage_audit_record_has_the_decision_and_never_the_summary() -> None:
    result = TriageResult(
        intent="withdrawal_issue",
        secondary_intent=None,
        urgency="normal",
        wants_human=False,
        reports_unauthorised_activity=False,
        is_complaint_or_legal=False,
        asks_for_action="none",
        injection_suspected=False,
        summary="Customer ana@example.com asks about a SEPA withdrawal of EUR 420.",
    )
    details = audit_details(result, "triage/v1", "anthropic/claude-haiku-5-5")

    assert details["intent"] == "withdrawal_issue"
    assert details["prompt_version"] == "triage/v1"
    assert "summary" not in details
    assert not any("ana@" in str(value) or "420" in str(value) for value in details.values())


def test_the_error_event_is_problem_details_without_internals() -> None:
    problem = run_failed_problem()
    assert problem["code"] == "internal_error"
    assert problem["status"] == 500
    assert set(problem) == {"type", "title", "status", "detail", "code"}


async def test_run_tasks_wait_for_runs_in_flight() -> None:
    tasks = RunTasks()
    done: list[str] = []

    async def job() -> None:
        await asyncio.sleep(0.01)
        done.append("run")

    tasks.start(job())
    await tasks.wait(timeout_s=1)
    assert done == ["run"]
    await tasks.wait(timeout_s=1)  # nothing in flight: returns at once


# --- output masking (B1, AGENTS.md never-do #1) ------------------------------------------------

TEST_CARD = "4111 1111 1111 1111"  # public test card number


def test_every_model_written_string_of_a_draft_is_card_masked() -> None:
    action = ProposedAction(
        type="jira_create_issue",
        issue_type="dispute",
        summary=f"Dispute card {TEST_CARD}",
        description=f"Customer card {TEST_CARD}, check the payment.",
    )
    answer = _answer(
        answer=f"Your card {TEST_CARD} was charged [1].",
        citations=[Citation(marker=1, source_id="withdrawal-times")],
        proposed_actions=[action],
    )
    clean = OutputMask(intake("hello"))
    values, _ = draft_from_answer(answer, SOURCES, clean)

    assert values["body"] == "Your card [CARD ****1111] was charged [1]."
    params = values["proposed_actions"][0]["params"]
    assert params["summary"] == "Dispute card [CARD ****1111]"
    assert params["description"] == "Customer card [CARD ****1111], check the payment."
    assert clean.counts == {"PAN": 3}
    assert "4111 1111" not in str(values)


def test_missing_information_is_card_masked() -> None:
    answer = _answer(
        can_answer=False, answer="", citations=[], missing_information=f"Card {TEST_CARD}?"
    )
    values, _ = draft_from_answer(answer, SOURCES, OutputMask(intake("hello")))
    assert values["missing_information"] == "Card [CARD ****1111]?"


def test_a_restored_placeholder_is_masked_too() -> None:
    """Restoring happens first, so the mask also sees what the placeholders put back."""
    clean = OutputMask(intake("write to ana@example.com"))
    assert clean(f"Sent to <EMAIL_1> about {TEST_CARD}.") == (
        "Sent to ana@example.com about [CARD ****1111]."
    )


def test_a_failed_mask_is_counted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        linear, "mask", lambda text: MaskResult(text="#" * len(text), counts={}, failed=True)
    )
    clean = OutputMask(intake("hello"))
    assert clean("abc") == "###"
    assert clean.counts == {"failed": 1}


@pytest.mark.parametrize(
    ("body", "keep", "expected", "dropped"),
    [
        ("A [1]. B [2].", {1, 2}, "A [1]. B [2].", []),
        ("A [1]. B [2].", {1}, "A [1]. B.", [2]),
        ("A[3] and [3] again.", set(), "A and again.", [3, 3]),
        ("No markers.", {1}, "No markers.", []),
    ],
)
def test_markers_without_a_kept_citation_are_removed(
    body: str, keep: set[int], expected: str, dropped: list[int]
) -> None:
    assert drop_markers(body, keep) == (expected, dropped)
