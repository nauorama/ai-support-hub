"""policy_gate node (docs/ai-pipeline.md §3, §6): code, before research.

P6-01: a run whose intake or triage failed goes straight to `handoff_prep` (row 2 of the
decision table: `system_error`, or `budget_exceeded` after a G8 budget stop), so no search and no
model call are spent on it. The hard rules of `config/policy.yaml` (row 1) come with P6-02.
"""

from hub.graph.context import GraphContext, failure_reasons
from hub.graph.state import TicketState


async def run(state: TicketState, ctx: GraphContext) -> TicketState:
    errors = state.get("errors", [])
    if errors:
        return TicketState(route="handoff", route_reasons=failure_reasons(errors))
    return TicketState()
