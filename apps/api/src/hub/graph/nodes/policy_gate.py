"""policy_gate node (docs/ai-pipeline.md §3, §6): code, before research.

A run whose intake or triage failed goes straight to `handoff_prep` (row 2: `system_error`, or
`budget_exceeded` after a G8 budget stop), so no search and no model call are spent on it.
Otherwise the hard rules of `config/policy.yaml` (row 1, `hub.graph.routing.gate`): when any
rule matches, the ticket is handed off with every matching reason, in row-1 order (P6-02).
`route.decided` is written by `handoff_prep`, which moves the ticket. `customer_restricted` reads
the profile's flag that intake loaded with `core_get_customer` (P7-04), never a reason.
"""

from hub.graph.context import GraphContext
from hub.graph.routing import gate
from hub.graph.state import TicketState


def restricted(state: TicketState) -> bool | None:
    """The profile's restricted flag (P7-04): None when intake loaded no profile, so the rule
    `customer_restricted` cannot match on missing data."""
    context = state.get("customer_context")
    profile = context.profile if context is not None else None
    return profile.restricted if profile is not None else None


async def run(state: TicketState, ctx: GraphContext) -> TicketState:
    decision = gate(
        ctx.pipeline.policy,
        errors=state.get("errors", []),
        triage=state.get("triage"),
        message_masked=state.get("message_masked", ""),
        flags=state.get("input_flags"),
        customer_restricted=restricted(state),
    )
    if decision.route is None:
        return TicketState()
    return TicketState(route=decision.route, route_reasons=decision.reasons)
