"""policy_gate node (docs/ai-pipeline.md §3, §6): code, before research.

A run whose intake or triage failed goes straight to `handoff_prep` (row 2: `system_error`, or
`budget_exceeded` after a G8 budget stop), so no search and no model call are spent on it.
Otherwise the hard rules of `config/policy.yaml` (row 1, `hub.graph.routing.gate`): when any
rule matches, the ticket is handed off with every matching reason, in row-1 order (P6-02).
`route.decided` is written by `handoff_prep`, which moves the ticket.
"""

from hub.graph.context import GraphContext
from hub.graph.routing import gate
from hub.graph.state import TicketState


async def run(state: TicketState, ctx: GraphContext) -> TicketState:
    decision = gate(
        ctx.pipeline.policy,
        errors=state.get("errors", []),
        triage=state.get("triage"),
        message_masked=state.get("message_masked", ""),
        flags=state.get("input_flags"),
    )
    if decision.route is None:
        return TicketState()
    return TicketState(route=decision.route, route_reasons=decision.reasons)
