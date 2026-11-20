"""Audit log against Postgres: `record` as the app role, `verify_chain` after changes made by hand.

The tests commit (the chain spans transactions, and concurrent writers need their own), so they
use a database of their own and empty `audit_log` before each test. The shared `hub` database
holds hand-made audit rows from test_db_roles.py that are not part of any chain.
"""

import asyncio
import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any

import psycopg
import pytest
from psycopg import sql
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine

from hub.audit import GENESIS_HASH, ChainCheck, record, verify_chain
from hub.audit.chain import AuditRow, compute_hash
from hub.db.models import AuditLog
from hub.domain.enums import ActorType
from hub.observability.readiness import libpq_url

from ._db import app_url, as_user, migrate

DATABASE = "hub_audit_test"
VISA = "4111111111111111"  # public test card number


@pytest.fixture(scope="module")
def audit_db(postgres_url: str) -> Iterator[tuple[str, str]]:
    """(migrator URL, app URL) of a migrated database of its own."""
    with psycopg.connect(libpq_url(postgres_url), autocommit=True) as connection:
        connection.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(DATABASE)))
        connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(DATABASE)))
    migrator = as_user(postgres_url, "hub", "hub", database=DATABASE)
    migrate(migrator)
    yield migrator, app_url(postgres_url, database=DATABASE)
    with psycopg.connect(libpq_url(postgres_url), autocommit=True) as connection:
        connection.execute(
            sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(DATABASE))
        )


@pytest.fixture
async def app_engine(audit_db: tuple[str, str]) -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(audit_db[1])
    yield engine
    await engine.dispose()


@pytest.fixture(autouse=True)
def empty_audit_log(audit_db: tuple[str, str]) -> None:
    with psycopg.connect(libpq_url(audit_db[0]), autocommit=True) as connection:
        connection.execute("TRUNCATE audit_log RESTART IDENTITY")


def as_migrator(audit_db: tuple[str, str], statement: str, params: tuple[Any, ...] = ()) -> None:
    """A change by hand, as the table owner: the app role cannot UPDATE or DELETE rows."""
    with psycopg.connect(libpq_url(audit_db[0]), autocommit=True) as connection:
        connection.execute(statement.encode(), params)


async def _record(
    engine: AsyncEngine, count: int = 1, details: dict[str, Any] | None = None
) -> list[AuditLog]:
    async with AsyncSession(engine, expire_on_commit=False) as session:
        rows = [
            await record(
                session,
                actor_type=ActorType.OPERATOR,
                actor_id="op-1",
                action="ticket.claimed",
                entity_type="ticket",
                entity_id=f"t-{i}",
                details=details if details is not None else {"n": i, "confidence": 0.83},
            )
            for i in range(count)
        ]
        await session.commit()
    return rows


async def _verify(engine: AsyncEngine) -> ChainCheck:
    async with AsyncSession(engine) as session:
        return await verify_chain(session)


async def test_an_empty_log_verifies(app_engine: AsyncEngine) -> None:
    assert await _verify(app_engine) == ChainCheck(ok=True, checked=0)


async def test_record_builds_a_chain_that_verifies(app_engine: AsyncEngine) -> None:
    first = await _record(app_engine, 3)
    second = await _record(app_engine, 2)  # a later transaction continues the chain
    rows = first + second

    assert rows[0].prev_hash == GENESIS_HASH
    assert [row.prev_hash for row in rows[1:]] == [row.hash for row in rows[:-1]]
    assert await _verify(app_engine) == ChainCheck(ok=True, checked=5)


async def test_details_are_stored_masked(
    app_engine: AsyncEngine, audit_db: tuple[str, str]
) -> None:
    await _record(app_engine, details={"note": f"card {VISA} cvv 123", "n": int(VISA)})

    with psycopg.connect(libpq_url(audit_db[0])) as connection:
        row = connection.execute("SELECT details::text FROM audit_log").fetchone()
    assert row is not None
    assert VISA not in row[0]
    assert "[CARD ****1111]" in row[0]
    assert await _verify(app_engine) == ChainCheck(ok=True, checked=1)


async def test_text_fields_are_stored_masked(
    app_engine: AsyncEngine, audit_db: tuple[str, str]
) -> None:
    async with AsyncSession(app_engine) as session:
        await record(
            session,
            actor_type=ActorType.CUSTOMER,
            actor_id=f"ref {VISA}",
            action=f"action {VISA}",
            entity_type=f"type {VISA}",
            entity_id=f"card {VISA}",
        )
        await session.commit()

    with psycopg.connect(libpq_url(audit_db[0])) as connection:
        row = connection.execute(
            "SELECT actor_id, action, entity_type, entity_id FROM audit_log"
        ).fetchone()
    assert row == (
        "ref [CARD ****1111]",
        "action [CARD ****1111]",
        "type [CARD ****1111]",
        "card [CARD ****1111]",
    )
    assert await _verify(app_engine) == ChainCheck(ok=True, checked=1)


async def test_a_uuid_object_is_stored_unchanged(
    app_engine: AsyncEngine, audit_db: tuple[str, str]
) -> None:
    """Text masking turned this id into "0b74d[CARD ****8741]-e5b0ef08c44e" before P8-03."""
    value = uuid.UUID("0b74d664-3640-4097-8741-e5b0ef08c44e")
    async with AsyncSession(app_engine) as session:
        await record(
            session,
            actor_type=ActorType.OPERATOR,
            actor_id=value,
            action="ticket.claimed",
            entity_type="ticket",
            entity_id=value,
            details={"ticket_id": value},
        )
        await session.commit()

    with psycopg.connect(libpq_url(audit_db[0])) as connection:
        row = connection.execute(
            "SELECT actor_id, entity_id, details ->> 'ticket_id' FROM audit_log"
        ).fetchone()
    assert row == (str(value), str(value), str(value))
    assert await _verify(app_engine) == ChainCheck(ok=True, checked=1)


async def test_a_card_in_a_float_is_stored_masked_a_canonical_uuid_string_is_kept(
    app_engine: AsyncEngine, audit_db: tuple[str, str]
) -> None:
    """Round-2 review: a float may not carry a full card number into the log. A string that is
    exactly a canonical UUID is kept since P8-03 (owner decision; a card written in that shape
    is an accepted limitation, security §3); one joined to other text is still masked."""
    shaped = "41111111-1111-1111-abcd-ef0123456789"
    async with AsyncSession(app_engine) as session:
        await record(
            session,
            actor_type=ActorType.CUSTOMER,
            actor_id=shaped,
            action="ticket.created",
            entity_type="ticket",
            entity_id=f"ref{shaped}",
            details={"ref": shaped, "n": float(VISA)},
        )
        await session.commit()

    with psycopg.connect(libpq_url(audit_db[0])) as connection:
        row = connection.execute(
            "SELECT actor_id, entity_id, details::text FROM audit_log"
        ).fetchone()
    assert row is not None
    actor_id, entity_id, details = row
    assert actor_id == shaped
    assert "41111111-1111-1111" not in entity_id
    assert VISA not in details
    assert "[CARD ****1111]" in details
    assert f'"ref": "{shaped}"' in details
    assert await _verify(app_engine) == ChainCheck(ok=True, checked=1)


@pytest.mark.parametrize(
    "change",
    [
        "UPDATE audit_log SET details = '{\"n\": 99}' WHERE id = %s",
        "UPDATE audit_log SET details = details || '{\"extra\": true}' WHERE id = %s",
        "UPDATE audit_log SET action = 'ticket.closed' WHERE id = %s",
        "UPDATE audit_log SET actor_type = 'system' WHERE id = %s",
        "UPDATE audit_log SET actor_id = NULL WHERE id = %s",
        "UPDATE audit_log SET entity_type = 'message' WHERE id = %s",
        "UPDATE audit_log SET entity_id = 't-x' WHERE id = %s",
        "UPDATE audit_log SET ts = ts + interval '1 microsecond' WHERE id = %s",
        "UPDATE audit_log SET hash = repeat('a', 64) WHERE id = %s",
        "UPDATE audit_log SET prev_hash = repeat('a', 64) WHERE id = %s",
    ],
)
async def test_changing_one_stored_row_by_hand_is_reported_at_that_row(
    app_engine: AsyncEngine, audit_db: tuple[str, str], change: str
) -> None:
    rows = await _record(app_engine, 5)
    target = rows[2].id

    as_migrator(audit_db, change, (target,))

    assert await _verify(app_engine) == ChainCheck(ok=False, checked=2, broken_id=target)


async def test_rewriting_a_row_with_a_correct_new_hash_is_reported_at_the_next_row(
    app_engine: AsyncEngine, audit_db: tuple[str, str]
) -> None:
    """Someone who also recomputes the changed row's hash still breaks the next row's link."""
    rows = await _record(app_engine, 3)
    changed = rows[1]
    new_hash = compute_hash(
        changed.prev_hash,
        AuditRow(
            ts=changed.ts,
            actor_type=changed.actor_type,
            actor_id=changed.actor_id,
            action="ticket.closed",
            entity_type=changed.entity_type,
            entity_id=changed.entity_id,
            details=changed.details,
        ),
    )
    as_migrator(
        audit_db,
        "UPDATE audit_log SET action = 'ticket.closed', hash = %s WHERE id = %s",
        (new_hash, changed.id),
    )

    assert await _verify(app_engine) == ChainCheck(ok=False, checked=2, broken_id=rows[2].id)


async def test_deleting_a_middle_row_is_reported_at_the_next_row(
    app_engine: AsyncEngine, audit_db: tuple[str, str]
) -> None:
    rows = await _record(app_engine, 5)
    as_migrator(audit_db, "DELETE FROM audit_log WHERE id = %s", (rows[2].id,))

    assert await _verify(app_engine) == ChainCheck(ok=False, checked=2, broken_id=rows[3].id)


async def test_deleting_the_first_row_is_reported(
    app_engine: AsyncEngine, audit_db: tuple[str, str]
) -> None:
    rows = await _record(app_engine, 3)
    as_migrator(audit_db, "DELETE FROM audit_log WHERE id = %s", (rows[0].id,))

    assert await _verify(app_engine) == ChainCheck(ok=False, checked=0, broken_id=rows[1].id)


async def test_known_limitation_deleting_the_last_rows_is_not_detected(
    app_engine: AsyncEngine, audit_db: tuple[str, str]
) -> None:
    """A hash chain proves only that the rows it has are unchanged and in order. Removing the
    newest rows leaves a shorter chain that still verifies (docs/security.md §5)."""
    rows = await _record(app_engine, 5)
    as_migrator(audit_db, "DELETE FROM audit_log WHERE id >= %s", (rows[3].id,))

    assert await _verify(app_engine) == ChainCheck(ok=True, checked=3)


async def test_concurrent_writers_keep_one_chain(app_engine: AsyncEngine) -> None:
    """The advisory lock serialises "read last hash → insert"; without it two writers read the
    same last hash and the chain forks."""
    writers, per_writer = 8, 5
    await asyncio.gather(*(_record(app_engine, per_writer) for _ in range(writers)))

    assert await _verify(app_engine) == ChainCheck(ok=True, checked=writers * per_writer)


async def test_a_rolled_back_record_leaves_the_chain_valid(app_engine: AsyncEngine) -> None:
    await _record(app_engine, 2)
    async with AsyncSession(app_engine) as session:
        await record(
            session,
            actor_type=ActorType.SYSTEM,
            actor_id=None,
            action="kb.reindexed",
            entity_type="kb",
            entity_id="all",
        )
        await session.rollback()
    await _record(app_engine, 1)

    assert await _verify(app_engine) == ChainCheck(ok=True, checked=3)


async def test_values_read_back_from_postgres_verify(app_engine: AsyncEngine) -> None:
    """jsonb prints numbers without exponent notation and timestamptz comes back in the session
    time zone; neither may break the chain."""
    await _record(
        app_engine,
        details={
            # Whole floats above 2**53 that are not exactly representable, so int(value) differs
            # from the decimal text jsonb stores (round-1 review). Chosen so that masking keeps
            # them as numbers; 1e23 and 1e300 hold long zero runs, which mask as cards.
            "not_exact": 5.6266053047935685e22,
            "not_exact_2": 7.500827868583425e22,
            "whole": 1.0,
            "small": 1e-7,
            "sum": 0.1 + 0.2,
            "nested": [{"z": 2}],
        },
    )
    async with AsyncSession(app_engine) as session:
        types = await session.scalar(
            text(
                "SELECT string_agg(jsonb_typeof(details -> k), ',') FROM audit_log, "
                "unnest(ARRAY['not_exact', 'not_exact_2']) AS k"
            )
        )
        assert types == "number,number"
        await session.execute(text("SET TIME ZONE 'Asia/Tokyo'"))
        assert await verify_chain(session) == ChainCheck(ok=True, checked=1)
