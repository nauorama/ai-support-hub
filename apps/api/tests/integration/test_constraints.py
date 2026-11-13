"""The database itself rejects values outside the enums of the data model (`CHECK`
constraints), also when a writer bypasses the models. Each case runs in a savepoint of one
transaction that is rolled back at the end."""

from collections.abc import AsyncIterator

import psycopg
import pytest
from psycopg import errors

from hub.observability.readiness import libpq_url

Connection = psycopg.AsyncConnection[tuple[object, ...]]


# One valid row per table, the base that each bad value is written over. Fixed ids:
# c…1 customer, c…2 operator, c…3 ticket, c…4 message, c…5 feedback.
ROWS = [
    """INSERT INTO customers (id, external_ref, display_name, email, tier, country)
       VALUES ('00000000-0000-7000-8000-0000000000c1', 'CUS-TEST', 'Test Customer',
               'test@example.com', 'starter', 'DE')""",
    """INSERT INTO operators (id, email, name, password_hash, role)
       VALUES ('00000000-0000-7000-8000-0000000000c2', 'op@example.com', 'Test Operator',
               'x', 'operator')""",
    # No `version`: the database default (migration 0003) gives 1.
    """INSERT INTO tickets (id, customer_id, channel, status, ai_disclosed_at)
       VALUES ('00000000-0000-7000-8000-0000000000c3', '00000000-0000-7000-8000-0000000000c1',
               'chat', 'processing', now())""",
    """INSERT INTO messages (id, ticket_id, role, body)
       VALUES ('00000000-0000-7000-8000-0000000000c4', '00000000-0000-7000-8000-0000000000c3',
               'customer', 'hello')""",
    """INSERT INTO feedback (id, ticket_id, message_id, rating)
       VALUES ('00000000-0000-7000-8000-0000000000c5', '00000000-0000-7000-8000-0000000000c3',
               '00000000-0000-7000-8000-0000000000c4', 'up')""",
    "INSERT INTO settings (key, value) VALUES ('auto_reply_enabled', 'false')",
    """INSERT INTO audit_log (actor_type, action, entity_type, entity_id, prev_hash, hash)
       VALUES ('system', 'test.event', 'test', 'x', 'genesis', 'hash-constraints')""",
]

BAD_VALUES = [
    ("customers", "tier", "'gold'"),
    ("operators", "role", "'superuser'"),
    ("tickets", "channel", "'email'"),
    ("tickets", "status", "'open'"),
    ("tickets", "intent", "'refund'"),
    ("tickets", "secondary_intent", "'refund'"),
    ("tickets", "urgency", "'urgent'"),
    ("tickets", "last_route", "'escalate'"),
    ("tickets", "resolution", "'bot'"),
    ("tickets", "confidence", "1.5"),
    ("tickets", "confidence", "-0.1"),
    ("tickets", "route_reasons", "'{}'::jsonb"),
    ("messages", "role", "'admin'"),
    ("messages", "citations", "'{}'::jsonb"),
    ("messages", "masked_entities", "'[]'::jsonb"),
    ("feedback", "rating", "'meh'"),
    ("settings", "key", "'unknown_setting'"),
    ("audit_log", "actor_type", "'robot'"),
    ("audit_log", "details", "'[]'::jsonb"),
]


@pytest.fixture
async def seeded(migrated_db_url: str) -> AsyncIterator[Connection]:
    # As the migrator (owner): the CHECKs hold for every role, and audit_log rows can only be
    # updated by the owner.
    connection = await psycopg.AsyncConnection.connect(libpq_url(migrated_db_url))
    async with connection:
        for statement in ROWS:
            await connection.execute(statement)
        yield connection
        await connection.rollback()


@pytest.mark.parametrize(
    ("table", "column", "value"), BAD_VALUES, ids=[f"{t}.{c}={v}" for t, c, v in BAD_VALUES]
)
async def test_check_constraint_rejects_value(
    seeded: Connection, table: str, column: str, value: str
) -> None:
    # Identifiers and values are constants of this test module, not input.
    statement = f"UPDATE {table} SET {column} = {value}"  # noqa: S608
    with pytest.raises(errors.CheckViolation, match=f"ck_{table}_"):
        async with seeded.transaction():
            await seeded.execute(statement)


async def test_null_passes_the_optional_enum_checks(seeded: Connection) -> None:
    async with seeded.transaction():
        await seeded.execute(
            "UPDATE tickets SET intent = NULL, secondary_intent = NULL, urgency = NULL, "
            "last_route = NULL, resolution = NULL, confidence = NULL"
        )


async def test_valid_values_pass(seeded: Connection) -> None:
    async with seeded.transaction():
        await seeded.execute(
            "UPDATE tickets SET intent = 'withdrawal_issue', urgency = 'high', "
            "last_route = 'approval', resolution = 'ai_edited', confidence = 0.875, "
            "route_reasons = '[\"auto_reply_off\"]'::jsonb"
        )


async def test_plain_sql_ticket_insert_gets_version_1(seeded: Connection) -> None:
    cursor = await seeded.execute("SELECT version FROM tickets")
    assert await cursor.fetchall() == [(1,)]
