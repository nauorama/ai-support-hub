"""decide node (docs/ai-pipeline.md §3, §6): code.

P6-01 applies two rows of the decision table: a failed node hands the ticket off (row 2:
`system_error`, or `budget_exceeded` after a G8 budget stop); everything else goes to operator
approval (row 7: `auto_reply_off`). The other rows, the rule version of confidence and the
verification routing come with P6-02, so no run is auto-replied yet.
"""

from hub.graph.context import GraphContext, failure_reasons
from hub.graph.state import TicketState

APPROVAL_REASONS = ["auto_reply_off"]


async def run(state: TicketState, ctx: GraphContext) -> TicketState:
    errors = state.get("errors", [])
    if errors or state.get("draft") is None:
        return TicketState(route="handoff", route_reasons=failure_reasons(errors))
    return TicketState(route="approval", route_reasons=list(APPROVAL_REASONS))
