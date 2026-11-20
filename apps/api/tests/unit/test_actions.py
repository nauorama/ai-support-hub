"""`act` and the urgent Slack alert without a server (P7-05): what is run, and that a failure
never fails the handoff or loses the answer. The end-to-end runs are in
tests/integration/test_actions.py."""

import uuid
from types import SimpleNamespace
from typing import Any, cast

import pytest

from hub.domain.review import ReviewDecision
from hub.graph.context import GraphContext
from hub.graph.nodes import act, handoff_prep
from hub.graph.state import TicketState

TICKET = uuid.UUID("0193a0c2-7d5e-7a01-8b02-00000000e001")
RUN = uuid.UUID("0193a0c2-7d5e-7a01-8b02-00000000e002")
OPERATOR = "0193a0c2-7d5e-7a01-8b02-00000000e003"
PARAMS = {"issue_type": "dispute", "summary": "Dispute", "description": "Check TX-000001."}
PROPOSED: list[Any] = [
    {"id": "a1", "type": "jira_create_issue", "params": PARAMS},
    {"id": "a2", "type": "jira_create_issue", "params": dict(PARAMS, issue_type="bug")},
    {"id": "a3", "type": "something_else", "params": PARAMS},
    {"id": "a4", "type": "jira_create_issue", "params": "not an object"},
    "not an action",
]


def _review(*ids: str) -> ReviewDecision:
    return ReviewDecision(kind="approve", operator_id=OPERATOR, approved_actions=list(ids))


def _state(review: ReviewDecision | None) -> TicketState:
    state = TicketState(ticket_id=str(TICKET), run_id=str(RUN), errors=[])
    if review is not None:
        state["review"] = review
    return state


async def _emit(name: str, data: dict[str, Any]) -> None:
    return None


def _ctx(**pipeline: Any) -> GraphContext:  # noqa: ANN401 - any attribute
    return GraphContext(pipeline=cast(Any, SimpleNamespace(**pipeline)), emit=_emit)


def test_only_ticked_jira_actions_of_the_stored_draft_are_run() -> None:
    assert act.approved(PROPOSED, _review("a2", "a3", "a4", "a9")) == [PROPOSED[1]]
    assert act.approved(PROPOSED, _review()) == []
    assert act.approved(PROPOSED, _review("a1", "a2")) == PROPOSED[:2]


def test_the_approval_names_the_operator_and_the_stored_parameters() -> None:
    approval = act.approval(TICKET, OPERATOR, PARAMS)
    assert approval.basis == "operator_review"
    assert approval.operator_id == uuid.UUID(OPERATOR)
    assert approval.ticket_id == TICKET
    assert approval.arguments == PARAMS


@pytest.mark.parametrize(
    ("review", "tools"),
    [(None, object()), (_review(), object()), (_review("a1"), None)],
)
async def test_nothing_to_run_does_nothing(review: ReviewDecision | None, tools: object) -> None:
    assert await act.run(_state(review), _ctx(tools=tools)) == {}


class _Broken:
    def __call__(self) -> Any:  # noqa: ANN401
        raise ConnectionError("database down")


async def test_a_failure_is_recorded_and_the_run_goes_on() -> None:
    """ai-pipeline §3: "records the error, continues"; the answer is already sent."""
    ctx = _ctx(tools=object(), database=SimpleNamespace(sessions=_Broken()))
    update = await act.run(_state(_review("a1")), ctx)
    assert update == {"errors": ["act:ConnectionError"]}
    assert act.timed_out(_state(None)) == {"errors": ["act:TimeoutError"]}


def test_the_alert_text_holds_fixed_fields_only() -> None:
    text = handoff_prep.alert_text(TICKET, "card_lost_stolen", ["policy_intent", "fraud_report"])
    assert text.startswith(f"Urgent handoff: ticket {TICKET}, intent card_lost_stolen")
    assert "policy_intent, fraud_report" in text
    assert "intent unknown, reasons none" in handoff_prep.alert_text(TICKET, None, [])


async def test_no_tool_client_sends_no_alert() -> None:
    assert (
        await handoff_prep.urgent_alert(_state(None), _ctx(tools=None), ["fraud_report"]) is False
    )


async def test_a_failed_alert_never_fails_the_handoff() -> None:
    ctx = _ctx(tools=object(), database=SimpleNamespace(sessions=_Broken()))
    assert await handoff_prep.urgent_alert(_state(None), ctx, ["fraud_report"]) is False
