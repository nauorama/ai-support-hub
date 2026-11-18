"""The API's side of a paused run (P6-03, ai-pipeline §1, ADR-0008).

- `paused_run` / `resume_after_commit`: the console finds the paused run inside its transaction
  (under the ticket row lock) and resumes it after the commit with the operator's
  `ReviewDecision`. The decision is written first (owner decision), so a failed resume never
  loses the answer: the run stays `interrupted` or becomes `failed`.
- `recover_on_read`: the next read of a ticket in `processing` starts the recovery of its stuck
  run (`hub.graph.recover`) as a task of its own; the read does not wait for it.
"""

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from hub.db.models import Run, Ticket
from hub.db.repositories import RunRepository
from hub.deps import Container
from hub.domain.enums import RunStatus, TicketStatus
from hub.domain.review import ReviewDecision
from hub.graph.recover import recover_ticket_later, stuck_runs
from hub.graph.run import resume_run


async def paused_run(session: AsyncSession, run_id: uuid.UUID | None) -> uuid.UUID | None:
    """`run_id` when that run waits for the operator (`interrupted`), else None."""
    if run_id is None:
        return None
    run = await session.get(Run, run_id)
    return run.id if run is not None and run.status == RunStatus.INTERRUPTED else None


async def latest_paused_run(session: AsyncSession, ticket_id: uuid.UUID) -> uuid.UUID | None:
    """The ticket's latest run when it waits for the operator."""
    run = await RunRepository(session).latest_for_ticket(ticket_id)
    return run.id if run is not None and run.status == RunStatus.INTERRUPTED else None


async def resume_after_commit(
    container: Container,
    ticket_id: uuid.UUID,
    run_id: uuid.UUID | None,
    decision: ReviewDecision,
) -> None:
    """Resume the paused run, inside the request (Cloud Run gives CPU only during a request,
    ADR-0007). Never raises: the decision is already committed."""
    if run_id is None or container.pipeline is None:
        return
    await resume_run(container.pipeline, ticket_id=ticket_id, run_id=run_id, decision=decision)


async def recover_on_read(container: Container, session: AsyncSession, ticket: Ticket) -> None:
    """Start the recovery of a stuck run of this ticket (design §10: "resumed on the next read
    of the ticket"). Only a ticket in `processing` can have one; the check is one query."""
    pipeline = container.pipeline
    if pipeline is None or ticket.status != TicketStatus.PROCESSING:
        return
    if not await stuck_runs(session, pipeline, ticket_id=ticket.id):
        return
    container.runs.start(recover_ticket_later(pipeline, ticket.id))
