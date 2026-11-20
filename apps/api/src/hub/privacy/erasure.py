"""Erasure of one customer's data (security §6, FR-22, P8-05): `DELETE /admin/customers/{id}/data`.

Deleted: the customer's messages, drafts, memory and feedback (and, by the API after the commit,
the checkpoints of their tickets). The text of their tool calls is cut to the argument names.
Anonymised, kept for statistics: the `customers` row (name and email replaced, `anonymised_at`
set) and the ticket rows (the operator summary removed; status, intent, route and times stay).
Runs and `llm_calls` stay: they hold codes, numbers and costs only. A run still waiting for an
operator ends `failed`: its draft and its checkpoint are gone, so nothing can resume it.

The audit log is not changed: it refers to the customer only by the internal UUID, which after
the erasure points to no person, so the hash chain still verifies (security §5).
`external_ref` stays: `make seed` recognises an anonymised customer by it and does not bring
the person back (operations §1).

Safe to repeat: a second erasure finds nothing more to delete and reports zeros.
"""

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from hub.audit import record
from hub.db.models import Customer, Run, Ticket
from hub.domain.enums import ActorType, RunStatus
from hub.privacy._content import delete_content, redact_tool_calls, run_ids

ERASED_NAME = "Erased customer"
ERASED_EMAIL = ""
# `runs.error` holds an error class (data model); this one says why the run ended.
ERASED_RUN_ERROR = "CustomerErased"
_OPEN_RUNS = (RunStatus.RUNNING, RunStatus.INTERRUPTED)


@dataclass(frozen=True)
class ErasureReport:
    customer_id: uuid.UUID
    anonymised_at: datetime
    tickets_anonymised: int
    messages_deleted: int
    drafts_deleted: int
    memory_deleted: int
    feedback_deleted: int
    tool_calls_redacted: int
    runs_ended: int
    # Not in the API answer: the checkpoint threads the API deletes after the commit.
    ticket_ids: tuple[uuid.UUID, ...] = field(default=())


async def erase_customer(
    session: AsyncSession, customer_id: uuid.UUID, *, operator_id: uuid.UUID
) -> ErasureReport | None:
    """Erase inside the caller's transaction; None for an unknown customer."""
    customer = await session.get(Customer, customer_id, with_for_update=True)
    if customer is None:
        return None
    tickets = list(
        (
            await session.execute(
                select(Ticket).where(Ticket.customer_id == customer_id).order_by(Ticket.id)
            )
        ).scalars()
    )
    ticket_ids = [ticket.id for ticket in tickets]
    runs = await run_ids(session, ticket_ids)

    deleted = await delete_content(session, ticket_ids)
    redacted = await redact_tool_calls(session, runs)
    runs_ended = await _end_open_runs(session, runs)
    for ticket in tickets:
        if ticket.summary is not None:
            ticket.summary = None  # through the ORM: `tickets` takes no bulk statement
    customer.display_name = ERASED_NAME
    customer.email = ERASED_EMAIL
    if customer.anonymised_at is None:
        customer.anonymised_at = datetime.now(UTC)
    await session.flush()

    report = ErasureReport(
        customer_id=customer.id,
        anonymised_at=customer.anonymised_at,
        tickets_anonymised=len(tickets),
        messages_deleted=deleted.messages,
        drafts_deleted=deleted.drafts,
        memory_deleted=deleted.memory,
        feedback_deleted=deleted.feedback,
        tool_calls_redacted=redacted,
        runs_ended=runs_ended,
        ticket_ids=tuple(ticket_ids),
    )
    await record(
        session,
        actor_type=ActorType.OPERATOR,
        actor_id=operator_id,
        action="customer.erased",
        entity_type="customer",
        entity_id=customer.id,
        details={
            "tickets_anonymised": report.tickets_anonymised,
            "messages_deleted": report.messages_deleted,
            "drafts_deleted": report.drafts_deleted,
            "memory_deleted": report.memory_deleted,
            "feedback_deleted": report.feedback_deleted,
            "tool_calls_redacted": report.tool_calls_redacted,
            "runs_ended": report.runs_ended,
        },
    )
    return report


async def _end_open_runs(session: AsyncSession, runs: list[uuid.UUID]) -> int:
    if not runs:
        return 0
    open_runs = (
        await session.execute(select(Run).where(Run.id.in_(runs), Run.status.in_(_OPEN_RUNS)))
    ).scalars()
    now = datetime.now(UTC)
    ended = 0
    for run in open_runs:
        run.status = RunStatus.FAILED
        run.error = ERASED_RUN_ERROR
        run.finished_at = now
        ended += 1
    return ended
