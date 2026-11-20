"""An action an operator runs by hand (docs/api.md §3, ai-pipeline §6 "Jira issues"; P7-05).

`POST /console/tickets/{id}/actions` creates a Jira issue for a ticket: a fraud handoff has no
draft to approve, so the console pre-fills the form (type `fraud`, the triage summary) and the
operator sends it. The call goes through `hub.tools` like the `act` node's: allow-list `act`,
the ticket's latest run as the scope, the customer reference from the tickets row (never from
the request), an `Approval` of basis `operator_review` for this operator and exactly these
masked parameters, one issue per ticket (security T8) and the idempotency key
`<tool>:<ticket id>`. The tool client records it in `tool_calls` (the issue key in
`external_ref`) with its audit record.

Checked under the ticket row lock, so two requests for one ticket run one after the other and
the second is denied (`ticket_limit`). Only a ticket a human has: `handed_off` or `answered`
(`processing`: claim it first; `pending_approval`: approve the draft's action; closed: 409).
"""

import uuid

from fastapi import APIRouter
from sqlalchemy import select

from hub.api.console import (
    Writer,
    _check_not_claimed_by_other,
    _check_not_closed,
    _ticket_or_404,
)
from hub.api.console_schemas import ActionIn, ToolCallOut
from hub.api.dependencies import ContainerDep, SessionDep
from hub.api.errors import ProblemError
from hub.db.models import Customer, ToolCall
from hub.db.repositories import RunRepository, TicketRepository
from hub.domain.enums import TicketStatus, ToolCallStatus
from hub.guardrails.masking import mask
from hub.tools.permissions import Approval, ApprovalBasis, ToolNode, ToolScope

router = APIRouter(prefix="/api/v1/console", tags=["console"])

_ACTIONABLE = frozenset({TicketStatus.HANDED_OFF, TicketStatus.ANSWERED})


@router.post("/tickets/{ticket_id}/actions")
async def run_action(
    ticket_id: uuid.UUID,
    body: ActionIn,
    operator: Writer,
    session: SessionDep,
    container: ContainerDep,
) -> ToolCallOut:
    """Create the ticket's Jira issue. `409 action_denied` (with `reason`, e.g. `ticket_limit`)
    when the permissions refuse it, `502 tool_error` when the tool failed; both are recorded."""
    pipeline = container.pipeline
    tools = None if pipeline is None else pipeline.tools
    if tools is None:
        raise ProblemError(503, "tools_unavailable", "Tool actions are not configured.")
    ticket = await _ticket_or_404(session, ticket_id)
    await TicketRepository(session).lock(ticket)
    _check_not_closed(ticket)
    if ticket.status not in _ACTIONABLE:
        detail = (
            "Claim the ticket first."
            if ticket.status == TicketStatus.PROCESSING
            else "This ticket has a draft that waits for review."
        )
        raise ProblemError(409, "conflict", detail)
    _check_not_claimed_by_other(ticket, operator)
    run = await RunRepository(session).latest_for_ticket(ticket.id)
    if run is None:
        raise ProblemError(409, "conflict", "This ticket has no run to record the action in.")
    customer_ref = await session.scalar(
        select(Customer.external_ref).where(Customer.id == ticket.customer_id)
    )
    if customer_ref is None:  # pragma: no cover - a ticket always has its customer
        raise ProblemError(404, "not_found", "Ticket not found.")
    params = body.params
    arguments = {
        "issue_type": params.issue_type,
        "summary": mask(params.summary).text,
        "description": mask(params.description).text,
    }
    scope = ToolScope(
        run_id=run.id, ticket_id=ticket.id, customer_ref=customer_ref, node=ToolNode.ACT
    )
    outcome = await tools.session(scope).call(
        body.type,
        arguments,
        approval=Approval(
            tool=body.type,
            ticket_id=ticket.id,
            basis=ApprovalBasis.OPERATOR_REVIEW,
            operator_id=operator.id,
            arguments=arguments,
        ),
    )
    # Ends the transaction that holds the ticket lock; the call is recorded in its own.
    await session.commit()
    if outcome.status is ToolCallStatus.DENIED:
        raise ProblemError(
            409,
            "action_denied",
            "The action is not allowed for this ticket.",
            extensions={"reason": outcome.error},
        )
    if outcome.status is ToolCallStatus.ERROR:
        raise ProblemError(502, "tool_error", "The tool did not complete the action.")
    recorded = await session.scalar(
        select(ToolCall)
        .where(ToolCall.run_id == run.id, ToolCall.tool == body.type)
        .order_by(ToolCall.created_at.desc(), ToolCall.id.desc())
        .limit(1)
    )
    if recorded is None:  # pragma: no cover - the client records every call
        raise ProblemError(502, "tool_error", "The tool did not complete the action.")
    return ToolCallOut.model_validate(recorded)
