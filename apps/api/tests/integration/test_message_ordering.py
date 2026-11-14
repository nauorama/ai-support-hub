"""Polling with `?after=` sees every message, also when two transactions overlap (P2-07,
must-do 1 from the P2-06 review).

Before the fix, `messages.created_at` was the transaction start (`now()`). A transaction that
began earlier but saved its message later sorted that message before an anchor the client
already had, and `?after=` never returned it. Now every message write locks the ticket row and
then takes `clock_timestamp()` (`MessageRepository.lock_ticket`).
"""

import asyncio
import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine

from hub.db.models import Message
from hub.db.repositories import MessageRepository, TicketRepository
from hub.domain.enums import MessageRole

from . import _api
from ._api import Urls

DATABASE = "hub_message_ordering_test"


@pytest.fixture(scope="module")
def urls(postgres_url: str) -> Iterator[Urls]:
    with _api.own_database(postgres_url, DATABASE) as found:
        yield found


@pytest.fixture
async def engine(urls: Urls) -> AsyncIterator[AsyncEngine]:
    _api.empty_tables(urls)
    engine = create_async_engine(urls[1])
    yield engine
    await engine.dispose()


async def _ticket_with_first_message(engine: AsyncEngine, urls: Urls) -> tuple[uuid.UUID, Message]:
    customer_id = _api.add_customer(urls)
    async with AsyncSession(engine, expire_on_commit=False) as session:
        ticket = await TicketRepository(session).create(
            customer_id=customer_id, ai_disclosed_at=datetime.now(UTC)
        )
        first = await MessageRepository(session).add(
            ticket_id=ticket.id, role=MessageRole.CUSTOMER, body="Where is my money?"
        )
        await session.commit()
        return ticket.id, first


async def _poll(engine: AsyncEngine, ticket_id: uuid.UUID, after: Message) -> list[uuid.UUID]:
    async with AsyncSession(engine) as session:
        return [m.id for m in await MessageRepository(session).list_after(ticket_id, after)]


async def test_a_transaction_that_started_earlier_but_saved_later_is_not_missed(
    engine: AsyncEngine, urls: Urls
) -> None:
    ticket_id, first = await _ticket_with_first_message(engine, urls)
    async with (
        AsyncSession(engine, expire_on_commit=False) as early,
        AsyncSession(engine, expire_on_commit=False) as late,
    ):
        # The operator's transaction starts first (now() is fixed from here on) ...
        await early.execute(text("SELECT 1"))
        await asyncio.sleep(0.05)
        # ... the customer's message is saved and committed in between ...
        customer = await MessageRepository(late).add(
            ticket_id=ticket_id, role=MessageRole.CUSTOMER, body="Hello?"
        )
        await late.commit()
        # ... and the chat polls and takes the customer message as its anchor.
        assert await _poll(engine, ticket_id, first) == [customer.id]

        operator = await MessageRepository(early).add(
            ticket_id=ticket_id, role=MessageRole.OPERATOR, body="It arrives today."
        )
        transaction_start = (await early.execute(text("SELECT now()"))).scalar_one()
        await early.commit()

    # With now() the reply would sort before the anchor; with the lock and clock_timestamp()
    # it sorts after it, and the next poll returns it.
    assert transaction_start < customer.created_at < operator.created_at
    assert await _poll(engine, ticket_id, customer) == [operator.id]


async def test_a_second_writer_waits_for_the_first_to_commit(
    engine: AsyncEngine, urls: Urls
) -> None:
    ticket_id, first = await _ticket_with_first_message(engine, urls)
    async with (
        AsyncSession(engine, expire_on_commit=False) as first_writer,
        AsyncSession(engine, expire_on_commit=False) as second_writer,
    ):
        held = await MessageRepository(first_writer).add(
            ticket_id=ticket_id, role=MessageRole.OPERATOR, body="First."
        )

        async def write_second() -> Message:
            message = await MessageRepository(second_writer).add(
                ticket_id=ticket_id, role=MessageRole.CUSTOMER, body="Second."
            )
            await second_writer.commit()
            return message

        second = asyncio.create_task(write_second())
        await asyncio.sleep(0.3)
        # Blocked on the ticket row lock of the first writer.
        assert not second.done()
        await first_writer.commit()
        saved = await asyncio.wait_for(second, timeout=10)

    assert held.created_at < saved.created_at
    assert await _poll(engine, ticket_id, first) == [held.id, saved.id]
    assert await _poll(engine, ticket_id, held) == [saved.id]
