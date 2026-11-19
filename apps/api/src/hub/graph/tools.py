"""The tool scope of a node (P7-04, security G6).

The scope is built from the tickets row, never from model output and never from the state's
`customer_ref` (which holds the customer's id): `customer_ref` is the `customers.external_ref`
of the ticket's own customer. The tool client checks it again against the database before a
call that injects it (`hub.tools.mcp_client`, `invalid_identity` otherwise).
"""

import uuid

from sqlalchemy import select

from hub.db.models import Customer, Ticket
from hub.domain.enums import Urgency
from hub.graph.pipeline import Pipeline
from hub.graph.state import TicketState
from hub.tools.permissions import ToolNode, ToolScope


async def customer_ref(pipeline: Pipeline, ticket_id: uuid.UUID) -> str:
    async with pipeline.database.sessions() as session:
        found = await session.scalar(
            select(Customer.external_ref)
            .join(Ticket, Ticket.customer_id == Customer.id)
            .where(Ticket.id == ticket_id)
        )
    if found is None:  # pragma: no cover - tickets are never deleted while a run goes on
        raise LookupError("ticket")
    return found


async def tool_scope(pipeline: Pipeline, state: TicketState, node: ToolNode) -> ToolScope:
    ticket_id = uuid.UUID(state["ticket_id"])
    triage = state.get("triage")
    return ToolScope(
        run_id=uuid.UUID(state["run_id"]),
        ticket_id=ticket_id,
        customer_ref=await customer_ref(pipeline, ticket_id),
        node=node,
        urgency=Urgency(triage.urgency) if triage is not None else None,
    )
