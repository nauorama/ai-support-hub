"""Least privilege (docs/security.md §4): the app role can read and append to `audit_log` but
never change or remove a row, and it cannot change the schema."""

from collections.abc import AsyncIterator

import psycopg
import pytest
from psycopg import errors

from hub.observability.readiness import libpq_url

from ._db import APP_USER

AUDIT_ROW = (
    "INSERT INTO audit_log (actor_type, action, entity_type, entity_id, prev_hash, hash) "
    "VALUES ('system', 'test.event', 'test', %s, 'genesis', %s) RETURNING id"
)


@pytest.fixture
async def app_connection(
    app_db_url: str,
) -> AsyncIterator[psycopg.AsyncConnection[tuple[object, ...]]]:
    """Autocommit, so each statement stands alone and a refused one does not abort the rest."""
    connection = await psycopg.AsyncConnection.connect(libpq_url(app_db_url), autocommit=True)
    async with connection:
        yield connection


async def _insert_audit_row(
    connection: psycopg.AsyncConnection[tuple[object, ...]], name: str
) -> object:
    cursor = await connection.execute(AUDIT_ROW, (name, f"hash-{name}"))
    row = await cursor.fetchone()
    assert row is not None
    return row[0]


async def test_the_app_connects_as_a_plain_member_of_hub_app(
    app_connection: psycopg.AsyncConnection[tuple[object, ...]],
) -> None:
    cursor = await app_connection.execute(
        "SELECT current_user, rolsuper, rolcreaterole, rolcreatedb, "
        "pg_has_role(current_user, 'hub_app', 'MEMBER') FROM pg_roles WHERE rolname = current_user"
    )
    assert await cursor.fetchone() == (APP_USER, False, False, False, True)


async def test_app_role_can_insert_and_select_audit_log(
    app_connection: psycopg.AsyncConnection[tuple[object, ...]],
) -> None:
    row_id = await _insert_audit_row(app_connection, "insert-select")
    cursor = await app_connection.execute("SELECT action FROM audit_log WHERE id = %s", (row_id,))
    assert await cursor.fetchone() == ("test.event",)


async def test_update_on_audit_log_fails_for_the_app_role(
    app_connection: psycopg.AsyncConnection[tuple[object, ...]],
) -> None:
    row_id = await _insert_audit_row(app_connection, "update")
    with pytest.raises(errors.InsufficientPrivilege, match="permission denied for table audit_log"):
        await app_connection.execute(
            "UPDATE audit_log SET action = 'tampered' WHERE id = %s", (row_id,)
        )
    cursor = await app_connection.execute("SELECT action FROM audit_log WHERE id = %s", (row_id,))
    assert await cursor.fetchone() == ("test.event",)


@pytest.mark.parametrize(
    "statement",
    ["DELETE FROM audit_log WHERE id = %s", "TRUNCATE audit_log"],
    ids=["delete", "truncate"],
)
async def test_removing_audit_rows_fails_for_the_app_role(
    app_connection: psycopg.AsyncConnection[tuple[object, ...]], statement: str
) -> None:
    row_id = await _insert_audit_row(app_connection, f"remove-{statement.split()[0]}")
    params = (row_id,) if "%s" in statement else None
    with pytest.raises(errors.InsufficientPrivilege):
        await app_connection.execute(statement, params)
    cursor = await app_connection.execute("SELECT 1 FROM audit_log WHERE id = %s", (row_id,))
    assert await cursor.fetchone() == (1,)


@pytest.mark.parametrize(
    "statement",
    [
        "CREATE TABLE app_made_table (id int)",
        "ALTER TABLE audit_log ADD COLUMN note text",
        "DROP TABLE audit_log",
    ],
    ids=["create", "alter", "drop"],
)
async def test_app_role_cannot_change_the_schema(
    app_connection: psycopg.AsyncConnection[tuple[object, ...]], statement: str
) -> None:
    with pytest.raises((errors.InsufficientPrivilege, errors.WrongObjectType)):
        await app_connection.execute(statement)


async def test_app_role_cannot_grant_itself_update_on_audit_log(
    app_connection: psycopg.AsyncConnection[tuple[object, ...]],
) -> None:
    # Postgres answers a GRANT without grant option with a warning, not an error; what counts
    # is that nothing was granted.
    await app_connection.execute("GRANT UPDATE ON audit_log TO hub_app")
    cursor = await app_connection.execute(
        "SELECT has_table_privilege(current_user, 'audit_log', 'UPDATE')"
    )
    assert await cursor.fetchone() == (False,)


@pytest.mark.parametrize(
    "table", ["customers", "operators", "tickets", "messages", "settings", "feedback"]
)
async def test_app_role_reads_and_writes_the_other_tables(
    app_connection: psycopg.AsyncConnection[tuple[object, ...]], table: str
) -> None:
    # One privilege per call: with a list, has_table_privilege is true when ANY is held.
    cursor = await app_connection.execute(
        "SELECT bool_and(has_table_privilege(current_user, %s, p)), "
        "has_table_privilege(current_user, %s, 'TRUNCATE') "
        "FROM unnest(ARRAY['SELECT', 'INSERT', 'UPDATE', 'DELETE']) AS p",
        (table, table),
    )
    assert await cursor.fetchone() == (True, False)
