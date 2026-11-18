"""respond node (docs/ai-pipeline.md §3): the end of every run.

Auto-reply route (row 9, P6-02): in one transaction the draft is stored as `approved`, the
answer is saved as an `assistant` message (AI-generated, no reviewer), the ticket moves
`processing → answered` with resolution `ai`, and `route.decided` and `message.sent` are written.
The message reaches the stream after the commit (`message` event with its id). When an operator
claimed the ticket while the run worked, the draft is stored as `superseded` and nothing is sent.

Every run: the run row gets its outcome (status, route, reasons, confidence, features, models,
totals from `llm_calls`): `failed` when a node failed, `completed` otherwise. The message after
the operator's decision comes with P6-03.
"""

import uuid
from datetime import UTC, datetime

from hub.audit import record
from hub.db.repositories import MessageRepository, TicketRepository
from hub.domain.enums import (
    ActorType,
    DraftStatus,
    MessageRole,
    Resolution,
    Route,
    RunStatus,
    TicketStatus,
)
from hub.graph.context import GraphContext, error_class, ids
from hub.graph.nodes.handoff_prep import reasons
from hub.graph.nodes.human_review import store_draft
from hub.graph.state import TicketState
from hub.graph.store import audit_route, finish_run, ticket, to_decimal


async def auto_reply(state: TicketState, ctx: GraphContext) -> TicketState:
    pipeline = ctx.pipeline
    ticket_id, run_id = ids(state)
    async with pipeline.database.sessions.begin() as session:
        repository = TicketRepository(session)
        found = await repository.lock(await ticket(session, ticket_id))
        sends = found.status == TicketStatus.PROCESSING
        draft, _ = await store_draft(
            session,
            pipeline,
            state,
            status=DraftStatus.APPROVED if sends else DraftStatus.SUPERSEDED,
        )
        if not sends:
            return TicketState(ticket_status=found.status.value)
        message = await MessageRepository(session).add(
            ticket_id=ticket_id,
            role=MessageRole.ASSISTANT,
            body=draft.body,
            is_ai_generated=True,
            run_id=run_id,
            citations=[dict(citation) for citation in draft.citations],
        )
        draft.final_body = draft.body
        now = datetime.now(UTC)
        if found.first_response_at is None:
            found.first_response_at = now
        found.resolution = Resolution.AI
        found.last_route = Route.AUTO_REPLY
        found.route_reasons = []
        found.confidence = to_decimal(state.get("confidence"))
        await repository.change_status(found, TicketStatus.ANSWERED)
        await audit_route(
            session,
            pipeline,
            ticket_id,
            run_id,
            Route.AUTO_REPLY,
            [],
            state.get("confidence"),
            state.get("features"),
        )
        await record(
            session,
            actor_type=ActorType.AI,
            actor_id=None,
            action="message.sent",
            entity_type="ticket",
            entity_id=ticket_id,
            details={"message_id": message.id, "run_id": run_id, "draft_id": draft.id},
        )
        message_id: uuid.UUID = message.id
        status = found.status
    await ctx.emit("message", {"message_id": str(message_id)})
    return TicketState(ticket_status=status.value, final_message_id=str(message_id))


async def run(state: TicketState, ctx: GraphContext) -> TicketState:
    _, run_id = ids(state)
    errors = state.get("errors", [])
    route = state.get("route") or "handoff"
    update = TicketState()
    if route == "auto_reply" and not errors and state.get("draft") is not None:
        update = await auto_reply(state, ctx)
    async with ctx.pipeline.database.sessions.begin() as session:
        await finish_run(
            session,
            run_id,
            status=RunStatus.FAILED if errors else RunStatus.COMPLETED,
            route=Route(route),
            reasons=reasons(state) if route == "handoff" else list(state.get("route_reasons", [])),
            models=dict(state.get("models", {})),
            error=error_class(errors),
            confidence=state.get("confidence"),
            features=state.get("features"),
        )
    return update
