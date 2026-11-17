"""respond node (docs/ai-pipeline.md §3): the end of every run.

P6-01: the run row gets its outcome (status, route, reasons, models, totals from `llm_calls`):
`failed` when a node failed, `completed` otherwise. Saving an auto-reply (P6-02) and the message
after the operator's decision (P6-03) come later; until then a run never sends a message itself.
"""

from hub.domain.enums import Route, RunStatus
from hub.graph.context import GraphContext, error_class, ids
from hub.graph.nodes.handoff_prep import reasons
from hub.graph.state import TicketState
from hub.graph.store import finish_run


async def run(state: TicketState, ctx: GraphContext) -> TicketState:
    _, run_id = ids(state)
    errors = state.get("errors", [])
    route = state.get("route") or "handoff"
    async with ctx.pipeline.database.sessions.begin() as session:
        await finish_run(
            session,
            run_id,
            status=RunStatus.FAILED if errors else RunStatus.COMPLETED,
            route=Route(route),
            reasons=reasons(state) if route == "handoff" else list(state.get("route_reasons", [])),
            models=dict(state.get("models", {})),
            error=error_class(errors),
        )
    return TicketState()
