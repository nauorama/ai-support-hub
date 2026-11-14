"""Customer ticket endpoints (docs/api.md §2), phase 2: no AI run yet.

Order inside a message handler (ADR-0010, security G1): validate → mask → save → audit, in one
transaction that is committed before the first byte of the SSE stream. Validation is the
request schema (4,000 characters), which runs before the handler, so an over-long message is
refused before the mask sees it; the body cap in `hub/api/limits.py` runs before that. The raw
text lives only in this request's memory: it is never logged, stored or put in an error.

Until the workflow exists (phase 6), the stream is `ticket.created` → a placeholder system
message → `done`. The placeholder is not stored: it stands in for the run and would otherwise
leave invented history in every ticket.
"""

import json
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Header, Query, Request, Response
from fastapi.sse import EventSourceResponse, format_sse_event
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from hub.api.dependencies import ContainerDep, CustomerDep, SessionDep, client_ip
from hub.api.errors import ProblemError
from hub.api.schemas import (
    FeedbackIn,
    FeedbackOut,
    MessageIn,
    MessageOut,
    Page,
    TicketCreatedEvent,
    TicketOut,
    citations_for_customer,
)
from hub.audit import record
from hub.db.models import Customer, Message, Ticket, new_id
from hub.db.repositories import (
    ConcurrentUpdateError,
    FeedbackRepository,
    MessageRepository,
    OperatorRepository,
    TicketRepository,
)
from hub.deps import Container
from hub.domain.enums import ActorType, MessageRole, Route, TicketStatus
from hub.guardrails.masking import MaskResult, mask

router = APIRouter(prefix="/api/v1/tickets", tags=["tickets"])

PLACEHOLDER_TEXT = (
    "Thanks, we have your message. Our support team will reply in this chat. "
    "You can close this window; the answer will wait for you here."
)
SYSTEM_LABEL = "Tallybridge"
CUSTOMER_LABEL = "You"
AI_LABEL = "AI assistant"
OPERATOR_LABEL = "Support team"
DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 100

# The handlers return the stream themselves, after the commit. `response_class=EventSourceResponse`
# is not used: FastAPI 0.162 then expects the endpoint to be a generator and iterates its result,
# and a generator endpoint would run the masking and saving only once streaming has started,
# too late to answer 404 or 409. The OpenAPI document still names the media type.
SSE_RESPONSES: dict[int | str, dict[str, Any]] = {
    200: {
        "description": "Server-Sent Events (docs/api.md §4)",
        "content": {"text/event-stream": {}},
    }
}

IdempotencyKey = Annotated[uuid.UUID, Header(alias="Idempotency-Key")]


# --- views -----------------------------------------------------------------------------------


def _ticket_out(ticket: Ticket) -> TicketOut:
    return TicketOut(
        id=ticket.id,
        status=ticket.status,
        ai_disclosed_at=ticket.ai_disclosed_at,
        created_at=ticket.created_at,
    )


async def _author_label(session: AsyncSession, message: Message) -> str:
    match message.role:
        case MessageRole.CUSTOMER:
            return CUSTOMER_LABEL
        case MessageRole.ASSISTANT:
            return AI_LABEL
        case MessageRole.SYSTEM:
            return SYSTEM_LABEL
        case MessageRole.OPERATOR:
            # Security §7: the operator's first name and "Support team".
            operator = (
                await OperatorRepository(session).get(message.author_operator_id)
                if message.author_operator_id
                else None
            )
            if operator is None or not operator.name.strip():
                return OPERATOR_LABEL
            return f"{operator.name.split()[0]} · {OPERATOR_LABEL}"


async def _message_out(session: AsyncSession, message: Message) -> MessageOut:
    return MessageOut(
        id=message.id,
        role=message.role,
        author_label=await _author_label(session, message),
        is_ai_generated=message.is_ai_generated,
        body=message.body,
        citations=citations_for_customer(message.citations),
        masked=any(count > 0 for count in message.masked_entities.values()),
        created_at=message.created_at,
    )


def _placeholder() -> MessageOut:
    return MessageOut(
        id=new_id(),
        role=MessageRole.SYSTEM,
        author_label=SYSTEM_LABEL,
        is_ai_generated=False,
        body=PLACEHOLDER_TEXT,
        citations=[],
        masked=False,
        created_at=datetime.now(UTC),
    )


# --- SSE ------------------------------------------------------------------------------------


def _event(name: str, data: Any) -> bytes:  # noqa: ANN401 - any JSON-ready value
    return format_sse_event(event=name, data_str=json.dumps(data, separators=(",", ":")))


def _stream(
    ticket_id: uuid.UUID, message: MessageOut, status: TicketStatus | None = None
) -> EventSourceResponse:
    """`status` is sent as `ticket.status` when this request handed the ticket to a human."""
    created = TicketCreatedEvent(ticket_id=ticket_id, message=message).model_dump(mode="json")
    placeholder = _placeholder().model_dump(mode="json")

    async def events() -> AsyncIterator[bytes]:
        yield _event("ticket.created", created)
        yield _event("message", placeholder)
        if status is not None:
            yield _event("ticket.status", {"status": status.value})
        yield _event("done", {})

    # No buffering in proxies, no caching: the events must reach the browser as they are sent.
    return EventSourceResponse(
        events(), headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
    )


# --- shared steps ---------------------------------------------------------------------------


def _check_rate_limits(container: Container, customer: Customer, request: Request) -> None:
    limits = container.rate_limits
    session_key, ip_key = str(customer.id), client_ip(request)
    retry_after = max(
        limits.messages_per_session.retry_after(session_key) or 0,
        limits.messages_per_ip.retry_after(ip_key) or 0,
    )
    if retry_after:
        raise ProblemError(
            429,
            "too_many_requests",
            "Too many messages. Please wait a moment.",
            headers={"Retry-After": str(retry_after)},
        )
    limits.messages_per_session.hit(session_key)
    limits.messages_per_ip.hit(ip_key)


def _scoped_key(customer: Customer, key: uuid.UUID) -> str:
    # Scoped to the customer: another customer sending the same key gets their own message and
    # can never receive this one.
    return f"{customer.id}:{key}"


async def _ticket_or_404(session: AsyncSession, ticket_id: uuid.UUID, customer: Customer) -> Ticket:
    # 404, not 403, for another customer's ticket: its existence is not revealed (api.md §2).
    ticket = await TicketRepository(session).get_for_customer(ticket_id, customer.id)
    if ticket is None:
        raise ProblemError(404, "not_found", "Ticket not found.")
    return ticket


async def _replay(
    session: AsyncSession,
    stored: Message,
    *,
    expected_ticket_id: uuid.UUID | None,
    masked_body: str,
) -> EventSourceResponse:
    """The stream for a repeated `Idempotency-Key`: the first message, not a new one.

    The key must have been used for the same request: same ticket (for a follow-up message),
    the first message of its ticket (for a new ticket), and the same text after masking.
    """
    same_request = stored.role == MessageRole.CUSTOMER and stored.body == masked_body
    if expected_ticket_id is not None:
        same_request = same_request and stored.ticket_id == expected_ticket_id
    else:
        first = (await MessageRepository(session).list_for_ticket(stored.ticket_id))[0]
        same_request = same_request and first.id == stored.id
    if not same_request:
        raise ProblemError(
            409,
            "idempotency_key_reused",
            "This Idempotency-Key was already used for a different request.",
        )
    return _stream(stored.ticket_id, await _message_out(session, stored))


async def _audit_message(
    session: AsyncSession,
    *,
    customer_id: uuid.UUID,
    ticket_id: uuid.UUID,
    message_id: uuid.UUID,
    masked: MaskResult,
    action: str,
) -> None:
    # Ids and counts only: no message text in the audit log (security §5).
    await record(
        session,
        actor_type=ActorType.CUSTOMER,
        actor_id=customer_id,
        action=action,
        entity_type="ticket",
        entity_id=ticket_id,
        details={"message_id": message_id},
    )
    await _audit_masking(session, masked, entity_type="message", entity_id=message_id)


async def _audit_masking(
    session: AsyncSession, masked: MaskResult, *, entity_type: str, entity_id: uuid.UUID
) -> None:
    """`guardrail.masked` when something was masked, and also when masking failed: then every
    digit was replaced and the counts are unknown, and the failure must leave a trace."""
    if masked.counts or masked.failed:
        await record(
            session,
            actor_type=ActorType.SYSTEM,
            actor_id=None,
            action="guardrail.masked",
            entity_type=entity_type,
            entity_id=entity_id,
            details={"counts": dict(masked.counts), "failed": masked.failed},
        )


async def _change_status_or_409(
    session: AsyncSession, ticket: Ticket, target: TicketStatus
) -> None:
    try:
        await TicketRepository(session).change_status(ticket, target)
    except ConcurrentUpdateError:
        raise ProblemError(409, "conflict", "The ticket changed. Please try again.") from None


async def _hand_off_after_mask_failure(
    session: AsyncSession, ticket: Ticket, masked: MaskResult
) -> TicketStatus | None:
    """On any error the ticket goes to a human (AGENTS.md): a failed mask hands it off with
    reason `system_error`. A ticket that already waits for a human keeps its status."""
    if not masked.failed or ticket.status not in (TicketStatus.PROCESSING, TicketStatus.ANSWERED):
        return None
    ticket.last_route = Route.HANDOFF
    ticket.route_reasons = ["system_error"]
    await _change_status_or_409(session, ticket, TicketStatus.HANDED_OFF)
    await record(
        session,
        actor_type=ActorType.SYSTEM,
        actor_id=None,
        action="route.decided",
        entity_type="ticket",
        entity_id=ticket.id,
        details={"route": Route.HANDOFF.value, "reasons": ["system_error"]},
    )
    return TicketStatus.HANDED_OFF


async def _save_or_replay(
    session: AsyncSession,
    save: Callable[[], Awaitable[tuple[Message, TicketStatus | None]]],
    scoped_key: str,
    *,
    ticket_id: uuid.UUID | None,
    masked_body: str,
) -> EventSourceResponse:
    """Run `save` (writes and commit), then stream its message. When a concurrent request with
    the same key saved first, the unique constraint on `messages.idempotency_key` fails (at the
    flush of the message or at commit); answer with that request's message instead.

    `ticket_id` is passed as a value: the rollback expires every loaded object."""
    try:
        message, status = await save()
    except IntegrityError:
        await session.rollback()
        stored = await MessageRepository(session).get_by_idempotency_key(scoped_key)
        if stored is None:
            raise
        return await _replay(session, stored, expected_ticket_id=ticket_id, masked_body=masked_body)
    return _stream(message.ticket_id, await _message_out(session, message), status)


def _masked_entities(masked: MaskResult) -> dict[str, int]:
    """Counts by type only, e.g. `{"PAN": 1}` (data model, `messages.masked_entities`)."""
    return {str(kind): count for kind, count in masked.counts.items()}


def _human_owns(ticket: Ticket) -> bool:
    """After the first handoff or approval in a ticket, a human owns it (data model §1)."""
    return ticket.last_route in (Route.APPROVAL, Route.HANDOFF)


# --- endpoints ------------------------------------------------------------------------------


@router.post("", responses=SSE_RESPONSES)
async def create_ticket(
    body: MessageIn,
    request: Request,
    idempotency_key: IdempotencyKey,
    container: ContainerDep,
    customer: CustomerDep,
    session: SessionDep,
) -> EventSourceResponse:
    _check_rate_limits(container, customer, request)
    masked = mask(body.message)
    scoped_key = _scoped_key(customer, idempotency_key)
    messages = MessageRepository(session)

    stored = await messages.get_by_idempotency_key(scoped_key)
    if stored is not None:
        return await _replay(session, stored, expected_ticket_id=None, masked_body=masked.text)

    customer_id = customer.id

    async def save() -> tuple[Message, TicketStatus | None]:
        ticket = await TicketRepository(session).create(
            customer_id=customer_id, ai_disclosed_at=datetime.now(UTC)
        )
        message = await messages.add(
            ticket_id=ticket.id,
            role=MessageRole.CUSTOMER,
            body=masked.text,
            masked_entities=_masked_entities(masked),
            idempotency_key=scoped_key,
        )
        await _audit_message(
            session,
            customer_id=customer_id,
            ticket_id=ticket.id,
            message_id=message.id,
            masked=masked,
            action="ticket.created",
        )
        status = await _hand_off_after_mask_failure(session, ticket, masked)
        await session.commit()
        return message, status

    return await _save_or_replay(session, save, scoped_key, ticket_id=None, masked_body=masked.text)


@router.post("/{ticket_id}/messages", responses=SSE_RESPONSES)
async def add_message(
    ticket_id: uuid.UUID,
    body: MessageIn,
    request: Request,
    idempotency_key: IdempotencyKey,
    container: ContainerDep,
    customer: CustomerDep,
    session: SessionDep,
) -> EventSourceResponse:
    ticket = await _ticket_or_404(session, ticket_id, customer)
    _check_rate_limits(container, customer, request)
    masked = mask(body.message)
    scoped_key = _scoped_key(customer, idempotency_key)
    messages = MessageRepository(session)

    stored = await messages.get_by_idempotency_key(scoped_key)
    if stored is not None:
        return await _replay(session, stored, expected_ticket_id=ticket.id, masked_body=masked.text)
    if ticket.status == TicketStatus.CLOSED:
        raise ProblemError(409, "ticket_closed", "This ticket is closed. Start a new one.")

    customer_id, ticket_id = customer.id, ticket.id

    async def save() -> tuple[Message, TicketStatus | None]:
        message = await messages.add(
            ticket_id=ticket_id,
            role=MessageRole.CUSTOMER,
            body=masked.text,
            masked_entities=_masked_entities(masked),
            idempotency_key=scoped_key,
        )
        status = await _hand_off_after_mask_failure(session, ticket, masked)
        if status is None and ticket.status == TicketStatus.ANSWERED:
            # AI owns → a new run (phase 6); human owns → the operator queue.
            target = TicketStatus.HANDED_OFF if _human_owns(ticket) else TicketStatus.PROCESSING
            await _change_status_or_409(session, ticket, target)
        await _audit_message(
            session,
            customer_id=customer_id,
            ticket_id=ticket_id,
            message_id=message.id,
            masked=masked,
            action="message.received",
        )
        await session.commit()
        return message, status

    return await _save_or_replay(
        session, save, scoped_key, ticket_id=ticket_id, masked_body=masked.text
    )


@router.get("")
async def list_tickets(
    customer: CustomerDep,
    session: SessionDep,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
    cursor: uuid.UUID | None = None,
) -> Page[TicketOut]:
    tickets = await TicketRepository(session).list_for_customer(
        customer.id, limit=limit + 1, before=cursor
    )
    page, more = tickets[:limit], len(tickets) > limit
    return Page[TicketOut](
        items=[_ticket_out(ticket) for ticket in page],
        next_cursor=str(page[-1].id) if more else None,
    )


@router.get("/{ticket_id}")
async def get_ticket(ticket_id: uuid.UUID, customer: CustomerDep, session: SessionDep) -> TicketOut:
    return _ticket_out(await _ticket_or_404(session, ticket_id, customer))


@router.get("/{ticket_id}/messages")
async def list_messages(
    ticket_id: uuid.UUID,
    customer: CustomerDep,
    session: SessionDep,
    after: uuid.UUID | None = None,
) -> Page[MessageOut]:
    ticket = await _ticket_or_404(session, ticket_id, customer)
    messages = MessageRepository(session)
    found: Sequence[Message]
    if after is None:
        found = await messages.list_for_ticket(ticket.id)
    else:
        anchor = await messages.get_in_ticket(ticket.id, after)
        if anchor is None:
            raise ProblemError(404, "not_found", "Message not found in this ticket.")
        found = await messages.list_after(ticket.id, anchor)
    return Page[MessageOut](items=[await _message_out(session, message) for message in found])


@router.post("/{ticket_id}/request-human")
async def request_human(
    ticket_id: uuid.UUID,
    idempotency_key: IdempotencyKey,  # required by the contract; the request is idempotent by state
    customer: CustomerDep,
    session: SessionDep,
) -> TicketOut:
    """Immediate handoff with reason `customer_request`. Repeating it changes nothing: a ticket
    that already waits for a human (`handed_off`, `pending_approval`) is returned as it is."""
    ticket = await _ticket_or_404(session, ticket_id, customer)
    if ticket.status == TicketStatus.CLOSED:
        raise ProblemError(409, "ticket_closed", "This ticket is closed. Start a new one.")
    if ticket.status in (TicketStatus.PROCESSING, TicketStatus.ANSWERED):
        ticket.last_route = Route.HANDOFF
        ticket.route_reasons = ["customer_request"]
        await _change_status_or_409(session, ticket, TicketStatus.HANDED_OFF)
        await record(
            session,
            actor_type=ActorType.CUSTOMER,
            actor_id=customer.id,
            action="route.decided",
            entity_type="ticket",
            entity_id=ticket.id,
            details={"route": Route.HANDOFF.value, "reasons": ["customer_request"]},
        )
        await session.commit()
    return _ticket_out(ticket)


@router.post("/{ticket_id}/feedback", status_code=201)
async def give_feedback(
    ticket_id: uuid.UUID,
    body: FeedbackIn,
    response: Response,
    customer: CustomerDep,
    session: SessionDep,
) -> FeedbackOut:
    """One rating per AI message. A repeated request (a double click) returns the first rating
    with 200 and stores nothing; the message row is locked, so two concurrent requests cannot
    both insert."""
    ticket = await _ticket_or_404(session, ticket_id, customer)
    message = await MessageRepository(session).get_in_ticket(
        ticket.id, body.message_id, for_update=True
    )
    if message is None:
        raise ProblemError(404, "not_found", "Message not found in this ticket.")
    if not message.is_ai_generated:
        raise ProblemError(422, "not_ai_message", "Only answers of the AI assistant can be rated.")
    feedbacks = FeedbackRepository(session)
    existing = await feedbacks.get_for_message(message.id)
    if existing is not None:
        existing_id = existing.id  # read first: the rollback expires every loaded object
        await session.rollback()  # releases the row lock; nothing was written
        response.status_code = 200
        return FeedbackOut(id=existing_id)

    # The comment is customer text like a message: masked before it is stored.
    masked = mask(body.comment) if body.comment else None
    feedback = await feedbacks.add(
        ticket_id=ticket.id,
        message_id=message.id,
        rating=body.rating,
        comment=masked.text if masked else None,
    )
    await record(
        session,
        actor_type=ActorType.CUSTOMER,
        actor_id=customer.id,
        action="feedback.created",
        entity_type="message",
        entity_id=message.id,
        details={"feedback_id": feedback.id, "rating": body.rating.value},
    )
    if masked is not None:
        await _audit_masking(session, masked, entity_type="feedback", entity_id=feedback.id)
    await session.commit()
    return FeedbackOut(id=feedback.id)
