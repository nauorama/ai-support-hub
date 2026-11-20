"""act node (docs/ai-pipeline.md §3): runs the Jira actions the operator approved (P7-05).

The console wrote the review (the message, the draft with its `approved_actions`, the ticket)
before it resumed the run, so `act` runs only what an operator ticked: each approved id of the
run's stored draft becomes an `Approval` (basis `operator_review`, the reviewing operator, the
stored, G1-masked action parameters) for `jira_create_issue` through `hub.tools` (G6: allow-list
`act`, one call per run, one issue per ticket, the idempotency key `<tool>:<ticket id>`). The
tool client records every call in `tool_calls` (the issue key in `external_ref`) with its audit
record. A model never reaches this node: the arguments are the stored draft's, not the state's.

Nothing to run (no review, no approved action, no tool client): the node does nothing.

Failures (ai-pipeline §3, "records the error, continues"; owner decision after the PR #42
review): a Jira call that is refused or fails is recorded by the tool client (`tool_calls` row
with status `denied` or `error`, audited as `tool.denied` or `tool.called`) and shown on the
ticket in the console; the run goes on and ends `completed`, because the answer was already sent,
and the operator can create the issue by hand (`POST /console/tickets/{id}/actions`). Only an
unexpected failure of the node itself (the database, a timeout) is `act:<error class>` in
`errors`, and the run ends `failed`; the ticket is never handed off from here. The console
refuses more ticked actions than the ticket may still create (API §3), so a limit denial here
means a race, not a silent drop.
"""

import logging
import uuid
from typing import Any

from hub.db.repositories import DraftRepository
from hub.domain.enums import ToolCallStatus
from hub.domain.review import ReviewDecision
from hub.graph.context import GraphContext, ids
from hub.graph.state import TicketState
from hub.graph.tools import tool_scope
from hub.tools.mcp_client import ToolOutcome
from hub.tools.permissions import Approval, ApprovalBasis, ToolNode

logger = logging.getLogger(__name__)

JIRA_TOOL = "jira_create_issue"


def approved(proposed: list[Any], review: ReviewDecision) -> list[dict[str, Any]]:
    """The stored proposed actions the operator ticked, in the draft's order; only Jira issues
    (the one type a writer may propose, ai-pipeline §5)."""
    ticked = set(review.approved_actions)
    return [
        action
        for action in proposed
        if isinstance(action, dict)
        and action.get("id") in ticked
        and action.get("type") == JIRA_TOOL
        and isinstance(action.get("params"), dict)
    ]


def failed_with(state: TicketState, error: str) -> TicketState:
    """An unexpected failure of the node: the run ends `failed` (ai-pipeline §1: a step after the
    resume failed); the answer is sent either way, and the ticket stays as the operator left it."""
    return TicketState(errors=[*state.get("errors", []), f"act:{error}"])


def timed_out(state: TicketState) -> TicketState:
    """A timeout is recorded like any other failure of `act`; it never hands the ticket off."""
    return failed_with(state, "TimeoutError")


def approval(ticket_id: uuid.UUID, operator_id: str, params: dict[str, Any]) -> Approval:
    return Approval(
        tool=JIRA_TOOL,
        ticket_id=ticket_id,
        basis=ApprovalBasis.OPERATOR_REVIEW,
        operator_id=uuid.UUID(operator_id),
        arguments=dict(params),
    )


async def run(state: TicketState, ctx: GraphContext) -> TicketState:
    review = state.get("review")
    tools = ctx.pipeline.tools
    if review is None or not review.approved_actions or tools is None:
        return TicketState()
    ticket_id, run_id = ids(state)
    try:
        async with ctx.pipeline.database.sessions() as session:
            draft = await DraftRepository(session).for_run(run_id)
            proposed = list(draft.proposed_actions) if draft is not None else []
        actions = approved(proposed, review)
        session_ = tools.session(await tool_scope(ctx.pipeline, state, ToolNode.ACT))
        outcomes: list[ToolOutcome] = []
        for action in actions:
            outcomes.append(
                await session_.call(
                    JIRA_TOOL,
                    dict(action["params"]),
                    approval=approval(ticket_id, review.operator_id, action["params"]),
                )
            )
    except Exception as exc:  # the node itself failed: recorded, the answer is already sent
        logger.warning("act failed", extra={"run_id": str(run_id), "error": type(exc).__name__})
        return failed_with(state, type(exc).__name__)
    # A refused or failed call is already a `tool_calls` row with its audit record, shown in the
    # console; the run goes on.
    failed = [
        outcome.status.value for outcome in outcomes if outcome.status is not ToolCallStatus.OK
    ]
    if failed:
        logger.warning(
            "approved action not done", extra={"run_id": str(run_id), "statuses": failed}
        )
    return TicketState()
