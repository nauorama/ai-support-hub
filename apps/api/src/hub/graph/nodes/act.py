"""act node (docs/ai-pipeline.md §3): runs the actions the operator approved.

P6-01: nothing to run. The operator's decision reaches the run with P6-03 (`interrupt()` and
`Command(resume=…)`), and the Jira actions through MCP with P7.
"""

from hub.graph.context import GraphContext
from hub.graph.state import TicketState


async def run(state: TicketState, ctx: GraphContext) -> TicketState:
    return TicketState()
