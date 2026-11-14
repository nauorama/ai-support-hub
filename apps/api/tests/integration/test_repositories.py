"""Repositories against the migrated database, as the app role. Every test runs inside one
outer transaction that is rolled back (fixture `app_session`)."""

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import exc, text
from sqlalchemy.ext.asyncio import AsyncSession

from hub.db.models import Customer, Operator, StatusChangeRefusedError, Ticket
from hub.db.repositories import (
    ConcurrentUpdateError,
    CustomerRepository,
    FeedbackRepository,
    MessageRepository,
    OperatorRepository,
    SettingsRepository,
    TicketRepository,
)
from hub.domain.enums import (
    CustomerTier,
    FeedbackRating,
    MessageRole,
    OperatorRole,
    SettingKey,
    TicketStatus,
)
from hub.domain.ticket_status import InvalidTransitionError

NOT_A_HASH = "not-a-real-argon2-hash"
NOW = datetime(2026, 11, 13, 10, 0, tzinfo=UTC)


async def _customer(session: AsyncSession, ref: str = "CUS-0001") -> Customer:
    return await CustomerRepository(session).add(
        Customer(
            external_ref=ref,
            display_name="Test Customer",
            email=f"{ref.lower()}@example.com",
            tier=CustomerTier.VERIFIED,
            country="DE",
        )
    )


async def test_customer_by_id_and_external_ref(app_session: AsyncSession) -> None:
    customer = await _customer(app_session)
    repo = CustomerRepository(app_session)
    assert customer.id.version == 7
    assert await repo.get(customer.id) is customer
    assert await repo.get_by_external_ref("CUS-0001") is customer
    assert await repo.get_by_external_ref("CUS-9999") is None


async def test_operator_by_email(app_session: AsyncSession) -> None:
    repo = OperatorRepository(app_session)
    operator = await repo.add(
        Operator(
            email="lead@example.com", name="Lead", password_hash=NOT_A_HASH, role=OperatorRole.LEAD
        )
    )
    assert operator.is_active
    assert await repo.get(operator.id) is operator
    assert await repo.get_by_email("lead@example.com") is operator
    assert await repo.get_by_email("nobody@example.com") is None


async def test_new_ticket_starts_in_processing_with_version_1(app_session: AsyncSession) -> None:
    customer = await _customer(app_session)
    ticket = await TicketRepository(app_session).create(
        customer_id=customer.id, ai_disclosed_at=NOW
    )
    assert ticket.status is TicketStatus.PROCESSING
    assert ticket.version == 1
    assert ticket.route_reasons == []
    assert ticket.created_at is not None


async def test_allowed_status_change_bumps_the_version(app_session: AsyncSession) -> None:
    customer = await _customer(app_session)
    repo = TicketRepository(app_session)
    ticket = await repo.create(customer_id=customer.id, ai_disclosed_at=NOW)
    await repo.change_status(ticket, TicketStatus.PENDING_APPROVAL)
    await repo.change_status(ticket, TicketStatus.ANSWERED)
    assert ticket.version == 3
    stored = await app_session.execute(
        text("SELECT status, version FROM tickets WHERE id = :id"), {"id": ticket.id}
    )
    assert stored.one() == ("answered", 3)


async def test_forbidden_status_change_raises_and_writes_nothing(
    app_session: AsyncSession,
) -> None:
    customer = await _customer(app_session)
    repo = TicketRepository(app_session)
    ticket = await repo.create(customer_id=customer.id, ai_disclosed_at=NOW)
    with pytest.raises(InvalidTransitionError):
        await repo.change_status(ticket, TicketStatus.CLOSED)
    assert ticket.status is TicketStatus.PROCESSING
    assert ticket.version == 1


async def test_stale_ticket_raises_concurrent_update(app_session: AsyncSession) -> None:
    customer = await _customer(app_session)
    repo = TicketRepository(app_session)
    ticket = await repo.create(customer_id=customer.id, ai_disclosed_at=NOW)
    # Another writer changed the row after we loaded it.
    await app_session.execute(
        text("UPDATE tickets SET version = version + 1 WHERE id = :id"), {"id": ticket.id}
    )
    with pytest.raises(ConcurrentUpdateError):
        await repo.change_status(ticket, TicketStatus.HANDED_OFF)


async def test_status_set_directly_and_saved_is_refused(app_session: AsyncSession) -> None:
    # P2-02 review: `save()` flushes any change, so it must not bypass the transition check.
    customer = await _customer(app_session)
    repo = TicketRepository(app_session)
    ticket = await repo.create(customer_id=customer.id, ai_disclosed_at=NOW)
    ticket.status = TicketStatus.ANSWERED  # an allowed transition, but not via change_status
    with pytest.raises(StatusChangeRefusedError):
        await repo.save(ticket)


@pytest.mark.parametrize("target", [TicketStatus.ANSWERED, TicketStatus.CLOSED])
async def test_status_set_directly_and_committed_is_refused(
    app_session: AsyncSession, target: TicketStatus
) -> None:
    customer = await _customer(app_session)
    ticket = await TicketRepository(app_session).create(
        customer_id=customer.id, ai_disclosed_at=NOW
    )
    ticket.status = target
    with pytest.raises(StatusChangeRefusedError):
        await app_session.commit()


async def test_new_ticket_with_another_status_is_refused(app_session: AsyncSession) -> None:
    customer = await _customer(app_session)
    app_session.add(
        Ticket(customer_id=customer.id, status=TicketStatus.CLOSED, ai_disclosed_at=NOW)
    )
    with pytest.raises(StatusChangeRefusedError):
        await app_session.flush()


async def test_approval_of_change_status_is_used_once(app_session: AsyncSession) -> None:
    customer = await _customer(app_session)
    repo = TicketRepository(app_session)
    ticket = await repo.create(customer_id=customer.id, ai_disclosed_at=NOW)
    await repo.change_status(ticket, TicketStatus.ANSWERED)
    # The approval was for ANSWERED once; a later direct change is refused again.
    ticket.status = TicketStatus.CLOSED
    with pytest.raises(StatusChangeRefusedError):
        await repo.save(ticket)


async def test_other_ticket_fields_can_still_be_saved(app_session: AsyncSession) -> None:
    customer = await _customer(app_session)
    repo = TicketRepository(app_session)
    ticket = await repo.create(customer_id=customer.id, ai_disclosed_at=NOW)
    ticket.summary = "Withdrawal not arrived"
    await repo.save(ticket)
    assert ticket.version == 2


async def test_tickets_of_a_customer_newest_first_with_cursor(app_session: AsyncSession) -> None:
    owner = await _customer(app_session, "CUS-0001")
    other = await _customer(app_session, "CUS-0002")
    repo = TicketRepository(app_session)
    created = [await repo.create(customer_id=owner.id, ai_disclosed_at=NOW) for _ in range(3)]
    await repo.create(customer_id=other.id, ai_disclosed_at=NOW)
    newest_first = [t.id for t in reversed(created)]
    assert [t.id for t in await repo.list_for_customer(owner.id, limit=10)] == newest_first
    page = await repo.list_for_customer(owner.id, limit=10, before=newest_first[0])
    assert [t.id for t in page] == newest_first[1:]


async def test_messages_after_a_message(app_session: AsyncSession) -> None:
    customer = await _customer(app_session)
    tickets = TicketRepository(app_session)
    ticket = await tickets.create(customer_id=customer.id, ai_disclosed_at=NOW)
    other = await tickets.create(customer_id=customer.id, ai_disclosed_at=NOW)
    repo = MessageRepository(app_session)
    first, second, third = [
        await repo.add(ticket_id=ticket.id, role=MessageRole.CUSTOMER, body=str(n))
        for n in range(3)
    ]
    assert [m.id for m in await repo.list_after(ticket.id, first)] == [second.id, third.id]
    assert await repo.list_after(ticket.id, third) == []
    assert await repo.get_in_ticket(ticket.id, second.id) is second
    assert await repo.get_in_ticket(other.id, second.id) is None


async def test_ticket_isolation_by_customer(app_session: AsyncSession) -> None:
    owner = await _customer(app_session, "CUS-0001")
    other = await _customer(app_session, "CUS-0002")
    repo = TicketRepository(app_session)
    ticket = await repo.create(customer_id=owner.id, ai_disclosed_at=NOW)
    assert await repo.get_for_customer(ticket.id, owner.id) is ticket
    assert await repo.get_for_customer(ticket.id, other.id) is None
    assert await repo.get(uuid.uuid4()) is None


async def test_messages_in_creation_order_and_by_idempotency_key(
    app_session: AsyncSession,
) -> None:
    customer = await _customer(app_session)
    ticket = await TicketRepository(app_session).create(
        customer_id=customer.id, ai_disclosed_at=NOW
    )
    repo = MessageRepository(app_session)
    first = await repo.add(
        ticket_id=ticket.id,
        role=MessageRole.CUSTOMER,
        body="My card [CARD ****1111] was declined",
        masked_entities={"PAN": 1},
        idempotency_key="key-1",
    )
    second = await repo.add(
        ticket_id=ticket.id, role=MessageRole.SYSTEM, body="placeholder", is_ai_generated=True
    )
    assert [m.id for m in await repo.list_for_ticket(ticket.id)] == [first.id, second.id]
    assert await repo.get_by_idempotency_key("key-1") is first
    assert await repo.get_by_idempotency_key("key-2") is None
    assert first.citations == []
    assert first.masked_entities == {"PAN": 1}


async def test_idempotency_key_is_unique(app_session: AsyncSession) -> None:
    customer = await _customer(app_session)
    ticket = await TicketRepository(app_session).create(
        customer_id=customer.id, ai_disclosed_at=NOW
    )
    repo = MessageRepository(app_session)
    await repo.add(ticket_id=ticket.id, role=MessageRole.CUSTOMER, body="a", idempotency_key="k")
    with pytest.raises(exc.IntegrityError, match="uq_messages_idempotency_key"):
        await repo.add(
            ticket_id=ticket.id, role=MessageRole.CUSTOMER, body="b", idempotency_key="k"
        )


async def test_settings_set_get_and_overwrite(app_session: AsyncSession) -> None:
    repo = SettingsRepository(app_session)
    assert await repo.get(SettingKey.CONFIDENCE_THRESHOLD) is None
    await repo.set(SettingKey.CONFIDENCE_THRESHOLD, 0.8)
    await repo.set(SettingKey.INTENT_ALLOWLIST, ["limits", "fees_fx"])
    await repo.set(SettingKey.CONFIDENCE_THRESHOLD, 0.85)
    assert await repo.get(SettingKey.CONFIDENCE_THRESHOLD) == 0.85
    assert await repo.get_all() == {
        SettingKey.CONFIDENCE_THRESHOLD: 0.85,
        SettingKey.INTENT_ALLOWLIST: ["limits", "fees_fx"],
    }


async def test_feedback_on_a_message(app_session: AsyncSession) -> None:
    customer = await _customer(app_session)
    ticket = await TicketRepository(app_session).create(
        customer_id=customer.id, ai_disclosed_at=NOW
    )
    message = await MessageRepository(app_session).add(
        ticket_id=ticket.id, role=MessageRole.ASSISTANT, body="answer", is_ai_generated=True
    )
    feedback = await FeedbackRepository(app_session).add(
        ticket_id=ticket.id, message_id=message.id, rating=FeedbackRating.DOWN, comment="wrong"
    )
    assert feedback.rating is FeedbackRating.DOWN
    assert feedback.created_at is not None
