"""Recovery of runs stuck in `running` (P6-03; design §10, api.md `POST /admin/runs/recover`).

A run is stuck when its process died (a restart, a deploy, an instance that went away) while it
worked: the run row says `running`, but nobody runs it. The state is checkpointed after every
node, so recovery continues the run from its last checkpoint; a run that died before its first
checkpoint starts again from its trigger message with the same run id. The nodes are safe to run
again (ADR-0008): `human_review` stores a run's draft once, a ticket moves only from
`processing`, and a handoff notice is sent only when the ticket was handed off by that node.

**When a run counts as stuck (owner decision, P6-03).** It started longer ago than any run can
take (`STUCK_AFTER_S`: the sum of every node's timeout plus a margin), and this process does not
run it (`Pipeline.live`). A run on the other instance (Cloud Run, at most 2) has ended or failed
by then, so a recovery never runs a live run a second time. design §10 named 90 s; a slow run
(model retries and fallbacks) can take longer than that.

Two ways in: `POST /admin/runs/recover` (all stuck runs) and the next read of the ticket (the
customer's ticket and messages, the console ticket), which starts the recovery as a task of its
own and does not wait for it. A recovery that fails is the run's last resort: the run is
`failed` and the ticket goes to a human (`system_error`). Every recovery writes `run.recovered`.
"""

import logging
import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from hub.audit import record
from hub.db.models import Run, Ticket
from hub.domain.enums import ActorType, RunStatus
from hub.graph.build import NODE_TIMEOUTS_S, compile_graph
from hub.graph.context import GraphContext
from hub.graph.pipeline import Pipeline
from hub.graph.run import RunIncompleteError, ignore, outcome_status, settle_failure, thread
from hub.graph.state import TicketState, run_input

logger = logging.getLogger(__name__)

# Longer than any run: every node at its timeout, one after the other, and a margin for the
# database steps between them. P6-04 (retry loop) must keep this above its longest path.
STUCK_AFTER_S = sum(NODE_TIMEOUTS_S.values()) + 60.0


@dataclass(frozen=True)
class StuckRun:
    run_id: uuid.UUID
    ticket_id: uuid.UUID
    message_id: uuid.UUID | None
    customer_id: uuid.UUID


@dataclass(frozen=True)
class Recovery:
    """Who asked: an admin (`operator`) or the next read of a ticket (`system`)."""

    actor_type: ActorType = ActorType.SYSTEM
    actor_id: uuid.UUID | None = None


@dataclass(frozen=True)
class RecoverReport:
    found: int
    resumed: int
    failed: int


async def stuck_runs(
    session: AsyncSession, pipeline: Pipeline, *, ticket_id: uuid.UUID | None = None
) -> list[StuckRun]:
    """Runs in `running` that started more than `STUCK_AFTER_S` ago (database time) and are not
    live in this process; oldest first."""
    statement = (
        select(Run.id, Run.ticket_id, Run.trigger_message_id, Ticket.customer_id)
        .join(Ticket, Ticket.id == Run.ticket_id)
        .where(
            Run.status == RunStatus.RUNNING,
            Run.started_at < func.now() - timedelta(seconds=STUCK_AFTER_S),
        )
        .order_by(Run.started_at, Run.id)
    )
    if ticket_id is not None:
        statement = statement.where(Run.ticket_id == ticket_id)
    rows = (await session.execute(statement)).all()
    return [StuckRun(*row) for row in rows if row[0] not in pipeline.live]


async def recover_runs(
    pipeline: Pipeline, recovery: Recovery, *, ticket_id: uuid.UUID | None = None
) -> RecoverReport:
    """Recover every stuck run (of one ticket, with `ticket_id`), one after the other."""
    async with pipeline.database.sessions() as session:
        found = await stuck_runs(session, pipeline, ticket_id=ticket_id)
    resumed = failed = 0
    for stuck in found:
        ended = await recover_run(pipeline, stuck, recovery)
        if ended is None:
            continue  # taken by another recovery in this process meanwhile
        resumed += ended
        failed += not ended
    return RecoverReport(found=len(found), resumed=resumed, failed=failed)


async def recover_run(pipeline: Pipeline, stuck: StuckRun, recovery: Recovery) -> bool | None:
    """Continue one stuck run. True when it ended without a failure, False when it failed (and
    was handed to a human), None when this process already works on it."""
    if not pipeline.live.claim(stuck.run_id):
        return None
    try:
        graph = await pipeline.checkpoints.graph(compile_graph)
        snapshot = await graph.aget_state(thread(stuck.ticket_id))
        values: dict[str, Any] = snapshot.values
        start: TicketState | None
        if values.get("run_id") == str(stuck.run_id):
            if not snapshot.next:
                # The graph ended but the run row was not finished: nothing to continue.
                raise RunIncompleteError
            start, how = None, "checkpoint"  # None: continue from the last checkpoint
        else:
            if stuck.message_id is None:  # the trigger message was erased
                raise RunIncompleteError
            start, how = (
                run_input(
                    ticket_id=str(stuck.ticket_id),
                    run_id=str(stuck.run_id),
                    message_id=str(stuck.message_id),
                    customer_ref=str(stuck.customer_id),
                ),
                "restart",
            )
        async with pipeline.database.sessions.begin() as session:
            await record(
                session,
                actor_type=recovery.actor_type,
                actor_id=recovery.actor_id,
                action="run.recovered",
                entity_type="run",
                entity_id=stuck.run_id,
                details={"ticket_id": stuck.ticket_id, "from": how},
            )
        logger.info("run recovered", extra={"run_id": str(stuck.run_id), "from": how})
        final = await graph.ainvoke(
            start,
            config=thread(stuck.ticket_id),
            context=GraphContext(pipeline=pipeline, emit=ignore),
        )
        await outcome_status(pipeline, stuck.ticket_id, final)
        return not final.get("errors")
    except Exception as exc:
        await settle_failure(pipeline, stuck.ticket_id, stuck.run_id, exc)
        return False
    finally:
        pipeline.live.release(stuck.run_id)


async def recover_ticket_later(pipeline: Pipeline, ticket_id: uuid.UUID) -> None:
    """The lazy path: recover the ticket's stuck runs. Run as a task of its own; never raises."""
    try:
        await recover_runs(pipeline, Recovery(), ticket_id=ticket_id)
    except Exception as exc:
        logger.warning("lazy recovery failed", extra={"error": type(exc).__name__})
