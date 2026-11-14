"""Repositories: the queries the API and later the workflow need, one class per table.

A repository works inside the caller's session and never commits: the caller decides where the
transaction ends (validate → mask → save → audit is one transaction in P2-06). Repositories
flush, so database errors (constraints, stale versions) show up at the call that caused them.

Message bodies and feedback comments must already be masked (G1); masking is the caller's step,
before anything reaches this module.
"""

import uuid
from collections.abc import Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.exc import StaleDataError

from hub.db.models import (
    Customer,
    Feedback,
    Message,
    Operator,
    Setting,
    Ticket,
    approve_status_change,
)
from hub.domain.enums import FeedbackRating, MessageRole, SettingKey, TicketStatus
from hub.domain.ticket_status import INITIAL_STATUS, check_transition


class ConcurrentUpdateError(Exception):
    """Another transaction changed the row first (optimistic lock on `tickets.version`)."""


class CustomerRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, customer: Customer) -> Customer:
        self._session.add(customer)
        await self._session.flush()
        return customer

    async def get(self, customer_id: uuid.UUID) -> Customer | None:
        return await self._session.get(Customer, customer_id)

    async def get_by_external_ref(self, external_ref: str) -> Customer | None:
        result = await self._session.execute(
            select(Customer).where(Customer.external_ref == external_ref)
        )
        return result.scalar_one_or_none()


class OperatorRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, operator: Operator) -> Operator:
        self._session.add(operator)
        await self._session.flush()
        return operator

    async def get(self, operator_id: uuid.UUID) -> Operator | None:
        return await self._session.get(Operator, operator_id)

    async def get_by_email(self, email: str) -> Operator | None:
        result = await self._session.execute(select(Operator).where(Operator.email == email))
        return result.scalar_one_or_none()


class TicketRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(self, *, customer_id: uuid.UUID, ai_disclosed_at: datetime) -> Ticket:
        ticket = Ticket(
            customer_id=customer_id, status=INITIAL_STATUS, ai_disclosed_at=ai_disclosed_at
        )
        self._session.add(ticket)
        await self._session.flush()
        return ticket

    async def get(self, ticket_id: uuid.UUID) -> Ticket | None:
        return await self._session.get(Ticket, ticket_id)

    async def list_for_customer(
        self, customer_id: uuid.UUID, *, limit: int, before: uuid.UUID | None = None
    ) -> Sequence[Ticket]:
        """Newest first. Ordered by the UUID v7 id, which grows with creation time, so a page
        cursor is just the last id (`before`)."""
        statement = select(Ticket).where(Ticket.customer_id == customer_id)
        if before is not None:
            statement = statement.where(Ticket.id < before)
        result = await self._session.execute(statement.order_by(Ticket.id.desc()).limit(limit))
        return result.scalars().all()

    async def get_for_customer(self, ticket_id: uuid.UUID, customer_id: uuid.UUID) -> Ticket | None:
        """The ticket only when it belongs to this customer (customer isolation)."""
        result = await self._session.execute(
            select(Ticket).where(Ticket.id == ticket_id, Ticket.customer_id == customer_id)
        )
        return result.scalar_one_or_none()

    async def change_status(self, ticket: Ticket, target: TicketStatus) -> Ticket:
        """Move the ticket to `target`. Raises `InvalidTransitionError` before any SQL when the
        state diagram forbids it, `ConcurrentUpdateError` when the stored version moved on.

        The only way to change a status: a flush with any other status change raises
        `StatusChangeRefusedError` (`hub.db.models`)."""
        ticket.status = check_transition(ticket.status, target)
        # connection() begins a transaction when none is open, so the approval below always
        # belongs to one and is dropped with it.
        await self._session.connection()
        approve_status_change(self._session.sync_session, ticket.id, target)
        await self.save(ticket)
        return ticket

    async def save(self, ticket: Ticket) -> None:
        """Flush pending changes (of the ticket and anything else in the session); the version
        check runs here. A changed status is refused unless it came from `change_status`."""
        # Read before the flush: a failed flush expires the object, and reading it then raises
        # PendingRollbackError instead of the error below.
        ticket_id = ticket.id
        try:
            await self._session.flush()
        except StaleDataError as exc:
            raise ConcurrentUpdateError(f"ticket {ticket_id} was changed by someone else") from exc


class MessageRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(
        self,
        *,
        ticket_id: uuid.UUID,
        role: MessageRole,
        body: str,
        is_ai_generated: bool = False,
        author_operator_id: uuid.UUID | None = None,
        run_id: uuid.UUID | None = None,
        citations: list[Any] | None = None,
        masked_entities: dict[str, int] | None = None,
        idempotency_key: str | None = None,
    ) -> Message:
        message = Message(
            ticket_id=ticket_id,
            role=role,
            body=body,
            is_ai_generated=is_ai_generated,
            author_operator_id=author_operator_id,
            run_id=run_id,
            citations=citations or [],
            masked_entities=masked_entities or {},
            idempotency_key=idempotency_key,
        )
        self._session.add(message)
        await self._session.flush()
        return message

    async def get_by_idempotency_key(self, key: str) -> Message | None:
        result = await self._session.execute(select(Message).where(Message.idempotency_key == key))
        return result.scalar_one_or_none()

    async def list_for_ticket(self, ticket_id: uuid.UUID) -> Sequence[Message]:
        # created_at is the transaction start, equal for messages of one transaction; the
        # UUID v7 id breaks the tie in creation order.
        result = await self._session.execute(
            select(Message)
            .where(Message.ticket_id == ticket_id)
            .order_by(Message.created_at, Message.id)
        )
        return result.scalars().all()

    async def list_after(self, ticket_id: uuid.UUID, after: Message) -> Sequence[Message]:
        """Messages of the ticket created after `after`, in the order of `list_for_ticket`."""
        result = await self._session.execute(
            select(Message)
            .where(
                Message.ticket_id == ticket_id,
                tuple_(Message.created_at, Message.id) > tuple_(after.created_at, after.id),
            )
            .order_by(Message.created_at, Message.id)
        )
        return result.scalars().all()

    async def get_in_ticket(
        self, ticket_id: uuid.UUID, message_id: uuid.UUID, *, for_update: bool = False
    ) -> Message | None:
        """`for_update` locks the row until the transaction ends (serialises writers that
        depend on this message, e.g. one feedback per message)."""
        statement = select(Message).where(Message.id == message_id, Message.ticket_id == ticket_id)
        if for_update:
            statement = statement.with_for_update()
        result = await self._session.execute(statement)
        return result.scalar_one_or_none()


class SettingsRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, key: SettingKey) -> Any:  # noqa: ANN401 - JSON value of any shape
        setting = await self._session.get(Setting, key)
        return None if setting is None else setting.value

    async def get_all(self) -> dict[SettingKey, Any]:
        result = await self._session.execute(select(Setting))
        return {setting.key: setting.value for setting in result.scalars()}

    async def set(self, key: SettingKey, value: Any) -> None:  # noqa: ANN401 - JSON value
        setting = await self._session.get(Setting, key)
        if setting is None:
            self._session.add(Setting(key=key, value=value))
        else:
            setting.value = value
        await self._session.flush()


class FeedbackRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(
        self,
        *,
        ticket_id: uuid.UUID,
        message_id: uuid.UUID,
        rating: FeedbackRating,
        comment: str | None = None,
    ) -> Feedback:
        feedback = Feedback(
            ticket_id=ticket_id, message_id=message_id, rating=rating, comment=comment
        )
        self._session.add(feedback)
        await self._session.flush()
        return feedback

    async def get_for_message(self, message_id: uuid.UUID) -> Feedback | None:
        """The first feedback on a message (the API stores at most one)."""
        result = await self._session.execute(
            select(Feedback)
            .where(Feedback.message_id == message_id)
            .order_by(Feedback.created_at, Feedback.id)
            .limit(1)
        )
        return result.scalar_one_or_none()
