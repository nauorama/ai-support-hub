"""Bulk statements on `tickets` are refused (P2-07, must-do 3 from the P2-06 review).

The mapper events that guard the ticket status do not see statements run with
`Session.execute`; a `do_orm_execute` listener refuses every INSERT, UPDATE and DELETE on
`tickets` there, so a status cannot change without `TicketRepository.change_status`.
"""

from datetime import UTC, datetime

import pytest
from sqlalchemy import delete, insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from hub.db.models import BulkTicketWriteRefusedError, Customer, StatusChangeRefusedError, Ticket
from hub.db.repositories import CustomerRepository, TicketRepository
from hub.domain.enums import CustomerTier, TicketStatus


async def _ticket(session: AsyncSession) -> Ticket:
    customer = await CustomerRepository(session).add(
        Customer(
            external_ref="CUS-0901",
            display_name="Bulk Test",
            email="bulk@example.com",
            tier=CustomerTier.VERIFIED,
            country="SK",
        )
    )
    return await TicketRepository(session).create(
        customer_id=customer.id, ai_disclosed_at=datetime.now(UTC)
    )


@pytest.mark.parametrize(
    "statement",
    [
        pytest.param(update(Ticket).values(status=TicketStatus.CLOSED), id="orm-update-status"),
        pytest.param(
            update(Ticket.__table__).values(status="closed"),  # type: ignore[arg-type]
            id="core-update-status",
        ),
        pytest.param(update(Ticket).values(summary="changed"), id="orm-update-other-column"),
        pytest.param(
            select(Ticket).from_statement(
                update(Ticket).values(status=TicketStatus.CLOSED).returning(Ticket)
            ),
            id="from-statement",
        ),
        pytest.param(delete(Ticket), id="delete"),
    ],
)
async def test_bulk_statements_on_tickets_are_refused(
    app_session: AsyncSession, statement: object
) -> None:
    ticket = await _ticket(app_session)
    with pytest.raises(BulkTicketWriteRefusedError):
        await app_session.execute(statement)  # type: ignore[call-overload]
    # Nothing reached the database.
    await app_session.refresh(ticket)
    assert ticket.status == TicketStatus.PROCESSING
    assert ticket.version == 1


async def test_bulk_insert_of_a_ticket_is_refused(app_session: AsyncSession) -> None:
    ticket = await _ticket(app_session)
    with pytest.raises(BulkTicketWriteRefusedError):
        await app_session.execute(
            insert(Ticket).values(
                customer_id=ticket.customer_id,
                status=TicketStatus.CLOSED,
                ai_disclosed_at=datetime.now(UTC),
            )
        )


async def test_the_refusal_is_a_status_change_refusal(app_session: AsyncSession) -> None:
    # Code that catches StatusChangeRefusedError also catches the bulk case.
    assert issubclass(BulkTicketWriteRefusedError, StatusChangeRefusedError)


async def test_reads_and_the_checked_path_still_work(app_session: AsyncSession) -> None:
    ticket = await _ticket(app_session)
    found = (await app_session.execute(select(Ticket).where(Ticket.id == ticket.id))).scalar_one()
    assert found is ticket
    # The row lock that serialises message writes is a SELECT, not refused.
    await app_session.execute(select(Ticket.id).where(Ticket.id == ticket.id).with_for_update())
    await TicketRepository(app_session).change_status(ticket, TicketStatus.HANDED_OFF)
    assert ticket.status == TicketStatus.HANDED_OFF
