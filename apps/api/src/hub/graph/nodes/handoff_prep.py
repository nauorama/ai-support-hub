"""handoff_prep node (docs/ai-pipeline.md §3): the ticket goes to a human.

P6-01: the ticket moves to `handed_off` with the route's reasons and `route.decided`, unless an
operator already has it. The customer notice from a fixed template, the operator summary and
the urgent Slack alert come with P6-02 and P7. A failure here is not caught: the run's last
resort (`hub.graph.run`) records it and hands the ticket off.
"""

from hub.graph.context import GraphContext, ids
from hub.graph.state import TicketState
from hub.graph.store import hand_off


async def run(state: TicketState, ctx: GraphContext) -> TicketState:
    pipeline = ctx.pipeline
    ticket_id, run_id = ids(state)
    async with pipeline.database.sessions.begin() as session:
        status = await hand_off(
            session, pipeline, ticket_id, run_id, list(state.get("route_reasons", []))
        )
    return TicketState(ticket_status=status.value)
