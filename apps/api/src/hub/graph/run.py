"""One run of the workflow graph for one customer message (P6-01; the linear run of P3-05 to
P5-04 moved into `hub.graph.build`).

Called by `POST /tickets` for a new ticket, and by the evaluation runner (`hub_evals.runner`,
with `context = eval`, the evaluation database and its own cost limit as `budget`).

1. `run.status` `understanding` is sent at once; the run row is created and committed before the
   graph starts (`store.start_run`). No run starts when the ticket is no longer the AI's or the
   message is not its own: the stream then ends with the ticket's status.
2. The graph runs with the ticket id as its checkpoint thread (ai-pipeline §1) and this run's
   identity and nothing else as input (`state.run_input`). Nodes send `run.status` as they start.
3. The stream ends with `ticket.status` (the status the last node that changed the ticket left
   it in), with `error` before it when a node failed and the ticket was handed off.

The route decides how the run ends (ai-pipeline §6, P6-02): `pending_approval` with a draft,
`handed_off` with a notice (also after a failed node: `system_error`, or `budget_exceeded` after a
G8 budget stop), or `answered` with an auto-reply when a lead turned it on.

**Last resort.** When the graph itself fails (a node that does not fail into a handoff, the
checkpointer, the database), the run is `failed` and the ticket goes to a human with
`system_error` (`store.fail`). When even that cannot be saved, the ticket is still `processing`:
it waits in the operator queue (a claim hands it off) and `recover-runs` (P6-03) picks the run up.
The stream says so instead of claiming a handoff. Logs and the run row name only the error class:
an error message can quote customer text.

An operator may claim the ticket while the run is going (`processing → handed_off`, reason
`operator_takeover`). Then the run still finishes, but its draft is stored as `superseded` and the
ticket is left as the operator has it.
"""

import logging
import uuid

from hub.domain.enums import TicketStatus
from hub.graph.build import compile_graph
from hub.graph.context import ERROR_REASONS, GraphContext
from hub.graph.pipeline import (
    STEP_UNDERSTANDING,
    Emit,
    Pipeline,
    RunOutcome,
    run_failed_problem,
)
from hub.graph.state import run_input
from hub.graph.store import fail, start_run

logger = logging.getLogger(__name__)


class RunIncompleteError(Exception):
    """The graph ended without a node that settled the ticket's status."""


async def run_ticket(
    pipeline: Pipeline, *, ticket_id: uuid.UUID, message_id: uuid.UUID, emit: Emit
) -> RunOutcome:
    """One run for the customer message `message_id` of a ticket in `processing`. Never raises
    for a failed step: the failure is recorded and the ticket handed off. Emits the stream
    events `run.status`, then `ticket.status` (and `error` before it when the run failed)."""
    run_id: uuid.UUID | None = None
    try:
        await emit("run.status", {"step": STEP_UNDERSTANDING})
        started = await start_run(pipeline, ticket_id, message_id)
        if isinstance(started, TicketStatus):
            # Nothing to do: the ticket is no longer the AI's, or the message is not its own.
            # The stream still ends with the ticket's status (api.md §4).
            await emit("ticket.status", {"status": started.value})
            return RunOutcome(run_id=None, ticket_status=started, failed=False)
        run_id, customer_id = started
        graph = await pipeline.checkpoints.graph(compile_graph)
        final = await graph.ainvoke(
            run_input(
                ticket_id=str(ticket_id),
                run_id=str(run_id),
                message_id=str(message_id),
                customer_ref=str(customer_id),
            ),
            config={"configurable": {"thread_id": str(ticket_id)}},
            context=GraphContext(pipeline=pipeline, emit=emit, last_step=STEP_UNDERSTANDING),
        )
        shown = final.get("ticket_status") if isinstance(final, dict) else None
        if shown is None:
            raise RunIncompleteError
        status = TicketStatus(shown)
        failed = bool(final.get("errors"))
    except Exception as exc:
        error = type(exc).__name__
        logger.warning("run failed", extra={"run_id": str(run_id), "error": error})
        try:
            failed_status: TicketStatus | None = await fail(
                pipeline, ticket_id, run_id, error, {}, list(ERROR_REASONS)
            )
        except Exception as second:
            logger.error(
                "run failure could not be recorded",
                extra={"run_id": str(run_id), "error": type(second).__name__},
            )
            failed_status = None
        await emit("error", run_failed_problem())
        shown_status = failed_status if failed_status is not None else TicketStatus.PROCESSING
        await emit("ticket.status", {"status": shown_status.value})
        return RunOutcome(run_id=run_id, ticket_status=failed_status, failed=True)
    if failed:
        await emit("error", run_failed_problem())
    await emit("ticket.status", {"status": status.value})
    return RunOutcome(run_id=run_id, ticket_status=status, failed=failed)
