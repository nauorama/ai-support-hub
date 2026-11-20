"""`POST /admin/jobs/{name}` [job token] (api.md §3, security §4, P8-05): the scheduled jobs.

GitHub Actions calls them with `JOBS_TOKEN` as a bearer token (operations §6): no session, no
CSRF token (no cookie is involved), and the token opens no other endpoint. Everything runs
inside the request (Cloud Run gives CPU only during a request, ADR-0007). Every job run writes
`job.ran` (actor `system`, entity the job, the report's numbers).

- `retention`: `hub.privacy.retention`, then the checkpoints it names.
- `close-stale`: `answered` tickets without a customer message for `STALE_AFTER` (72 h) are
  closed one by one through `TicketRepository.change_status` (never a bulk `UPDATE`: the status
  guard refuses one, data model §1), each with `ticket.closed` and then its memory summary, as the
  close endpoint does. A paused run of such a ticket is a lost resume (the answer did not
  resume it); `recover-runs` resumes it with the decision that answered the ticket.
- `recover-runs`: `hub.graph.recover.recover_runs` (stuck runs and lost resumes), with the job
  as the actor instead of an admin.
- `ping`: one query to Postgres and one to Qdrant, so the free services stay awake.
"""

import hmac
import logging
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from hub.api.dependencies import ContainerDep
from hub.api.errors import ProblemError
from hub.api.privacy import delete_checkpoints
from hub.api.resume import write_memory
from hub.audit import record
from hub.db.models import Message, Ticket
from hub.db.repositories import TicketRepository
from hub.db.session import Database
from hub.deps import Container
from hub.domain.enums import ActorType, MessageRole, TicketStatus
from hub.graph.recover import Recovery, recover_runs
from hub.kb.service import index_counts
from hub.privacy import run_retention

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/admin/jobs", tags=["jobs"])

# The audit actor of every job run (security §4: "Every job run writes an audit record").
JOB_ACTOR = "jobs"
# data model §1: `answered → closed` after 72 h without a customer message.
STALE_AFTER = timedelta(hours=72)


class RequireJobToken:
    """The dependency of the job routes: `Authorization: Bearer <JOBS_TOKEN>`, compared in
    constant time. A class, so the route walk (test_console_routes_have_roles) can find it.
    An empty `JOBS_TOKEN` turns the jobs off (503), so no empty token can ever match."""

    async def __call__(
        self, container: ContainerDep, authorization: Annotated[str | None, Header()] = None
    ) -> None:
        expected = container.settings.jobs_token.get_secret_value()
        if not expected:
            raise ProblemError(503, "jobs_unavailable", "Scheduled jobs are not configured.")
        scheme, _, given = (authorization or "").partition(" ")
        if scheme.lower() != "bearer" or not hmac.compare_digest(given.encode(), expected.encode()):
            raise ProblemError(401, "unauthorized", "A valid job token is required.")


type Job = Callable[[Container], Awaitable[dict[str, Any]]]


@router.post("/{name}", dependencies=[Depends(RequireJobToken())])
async def run_job(name: str, container: ContainerDep) -> dict[str, Any]:
    """Run one job and return its report: `{"job": name, "report": {...}}`."""
    job = JOBS.get(name)
    if job is None:
        raise ProblemError(404, "not_found", "No such job.")
    database = container.database
    if database is None:
        raise ProblemError(503, "database_unavailable", "The database is not configured.")
    try:
        report = await job(container)
    except Exception as exc:
        # A failed run is audited too (security §4: every job run), with the error class only.
        await _audit_job(database, name, {"error": type(exc).__name__})
        raise
    await _audit_job(database, name, report)
    return {"job": name, "report": report}


async def _audit_job(database: Database, name: str, details: dict[str, Any]) -> None:
    async with database.sessions.begin() as session:
        await record(
            session,
            actor_type=ActorType.SYSTEM,
            actor_id=JOB_ACTOR,
            action="job.ran",
            entity_type="job",
            entity_id=name,
            details=details,
        )


def _database(container: Container) -> Database:
    if container.database is None:  # pragma: no cover - checked by run_job
        raise ProblemError(503, "database_unavailable", "The database is not configured.")
    return container.database


async def retention(container: Container) -> dict[str, Any]:
    settings = container.settings
    async with _database(container).sessions.begin() as session:
        found = await run_retention(
            session,
            ticket_days=settings.retention_ticket_days,
            checkpoint_days=settings.retention_checkpoint_days,
        )
    # After the commit: a failed checkpoint delete is repeated by the next night's run.
    checkpoints = await delete_checkpoints(container, found.checkpoint_ticket_ids)
    return {
        "tickets_deleted": found.tickets_deleted,
        "messages_deleted": found.messages_deleted,
        "drafts_deleted": found.drafts_deleted,
        "runs_deleted": found.runs_deleted,
        "tool_calls_deleted": found.tool_calls_deleted,
        "feedback_deleted": found.feedback_deleted,
        "memory_deleted": found.memory_deleted,
        "checkpoint_threads_deleted": checkpoints,
    }


async def stale_tickets(container: Container) -> list[uuid.UUID]:
    """`answered` tickets whose newest customer message (or creation) is older than 72 h."""
    newest = (
        select(Message.ticket_id, func.max(Message.created_at).label("newest"))
        .where(Message.role == MessageRole.CUSTOMER)
        .group_by(Message.ticket_id)
        .subquery()
    )
    async with _database(container).sessions() as session:
        cutoff = (await session.execute(select(func.now()))).scalar_one() - STALE_AFTER
        rows = await session.execute(
            select(Ticket.id)
            .outerjoin(newest, newest.c.ticket_id == Ticket.id)
            .where(
                Ticket.status == TicketStatus.ANSWERED,
                func.coalesce(newest.c.newest, Ticket.created_at) < cutoff,
            )
            .order_by(Ticket.id)
        )
        return list(rows.scalars())


async def close_one(container: Container, ticket_id: uuid.UUID) -> bool:
    """Close one stale ticket under its row lock, through the checked status path. False when
    it changed meanwhile (the customer wrote, an operator closed it)."""
    async with _database(container).sessions.begin() as session:
        repository = TicketRepository(session)
        ticket = await repository.get(ticket_id)
        if ticket is None:
            return False
        await repository.lock(ticket)
        if ticket.status != TicketStatus.ANSWERED or not await _still_stale(session, ticket):
            return False
        if ticket.resolved_at is None:
            ticket.resolved_at = datetime.now(UTC)
        await repository.change_status(ticket, TicketStatus.CLOSED)
        await record(
            session,
            actor_type=ActorType.SYSTEM,
            actor_id=JOB_ACTOR,
            action="ticket.closed",
            entity_type="ticket",
            entity_id=ticket.id,
            details={"from_status": TicketStatus.ANSWERED.value, "job": "close-stale"},
        )
    return True


async def _still_stale(session: AsyncSession, ticket: Ticket) -> bool:
    """The re-check under the lock: still no customer message for 72 h."""
    newest = (
        await session.execute(
            select(func.max(Message.created_at)).where(
                Message.ticket_id == ticket.id, Message.role == MessageRole.CUSTOMER
            )
        )
    ).scalar_one_or_none()
    now = (await session.execute(select(func.now()))).scalar_one()
    last: datetime = newest if newest is not None else ticket.created_at
    return last < now - STALE_AFTER


async def close_stale(container: Container) -> dict[str, Any]:
    found = await stale_tickets(container)
    closed = 0
    for ticket_id in found:
        if not await close_one(container, ticket_id):
            continue
        closed += 1
        await write_memory(container, ticket_id)  # never raises; its failure is audited
    return {"found": len(found), "closed": closed}


async def recover(container: Container) -> dict[str, Any]:
    if container.pipeline is None:
        raise ProblemError(503, "workflow_unavailable", "The workflow is not configured.")
    report = await recover_runs(
        container.pipeline, Recovery(actor_type=ActorType.SYSTEM, actor_id=JOB_ACTOR)
    )
    return {
        "found": report.found,
        "resumed": report.resumed,
        "failed": report.failed,
        "lost_resumes": report.lost,
    }


async def ping(container: Container) -> dict[str, Any]:
    """A real request to each free service, so neither counts as unused (operations §6): the
    chunk count in Postgres and the point count of the Qdrant collection. 503 when one does not
    answer, so the scheduled workflow fails visibly."""
    if container.kb is None:
        raise ProblemError(503, "kb_unavailable", "The knowledge-base index is not configured.")
    try:
        postgres, qdrant = await index_counts(_database(container), container.kb.index)
    except Exception as exc:
        logger.warning("ping failed", extra={"error": type(exc).__name__})
        raise ProblemError(503, "ping_failed", "A service did not answer.") from None
    return {"postgres_chunks": postgres, "qdrant_points": qdrant}


JOBS: dict[str, Job] = {
    "retention": retention,
    "close-stale": close_stale,
    "recover-runs": recover,
    "ping": ping,
}
