"""Helpers for API integration tests that run the app against a database of their own.

A plain module, not conftest: pytest advises against importing from conftest files.
"""

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import psycopg
from fastapi import FastAPI
from fastapi.testclient import TestClient
from psycopg import sql
from uuid_utils.compat import uuid7

from hub.auth.passwords import hash_password
from hub.config import Settings
from hub.db.session import connect
from hub.deps import Container
from hub.observability.readiness import libpq_url

from ._db import app_url, as_user, migrate

ACCESS_CODE = "demo-code-for-tests"
SESSION_SECRET = "session-secret-for-tests-only-0123456789"  # noqa: S105 - test value
OPERATOR_PASSWORD = "operator-password-for-tests"  # noqa: S105 - test value
# Hashed once: Argon2 takes tens of milliseconds per hash.
OPERATOR_HASH = hash_password(OPERATOR_PASSWORD)
TABLES = "audit_log, feedback, llm_calls, drafts, runs, messages, tickets, customers, operators"

Urls = tuple[str, str]
"""(migrator URL, app URL) of one database."""


@contextmanager
def own_database(postgres_url: str, name: str) -> Iterator[Urls]:
    with psycopg.connect(libpq_url(postgres_url), autocommit=True) as connection:
        connection.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(name)))
        connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    migrator = as_user(postgres_url, "hub", "hub", database=name)
    migrate(migrator)
    try:
        yield migrator, app_url(postgres_url, database=name)
    finally:
        with psycopg.connect(libpq_url(postgres_url), autocommit=True) as connection:
            connection.execute(
                sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name))
            )


def run_sql(urls: Urls, query: str, *params: Any) -> list[tuple[Any, ...]]:  # noqa: ANN401
    """Run SQL as the migrator (the test's view of the database, outside the app)."""
    with psycopg.connect(libpq_url(urls[0]), autocommit=True) as connection:
        cursor = connection.execute(query.encode(), params)
        return cursor.fetchall() if cursor.description else []


def empty_tables(urls: Urls) -> None:
    run_sql(urls, f"TRUNCATE {TABLES} RESTART IDENTITY")


def container(urls: Urls) -> Container:
    settings = Settings(
        _env_file=None,
        app_env="test",
        database_url=urls[1],
        session_secret=SESSION_SECRET,
        demo_access_code=ACCESS_CODE,
    )
    return Container(settings=settings, database=connect(urls[1]))


def client(app: FastAPI) -> TestClient:
    """A client with a cookie jar of its own. https: the session cookies are `Secure`."""
    return TestClient(app, base_url="https://testserver", raise_server_exceptions=False)


def add_customer(urls: Urls, ref: str = "CUS-0001", name: str = "Ana Novak") -> uuid.UUID:
    customer_id = uuid.UUID(str(uuid7()))
    run_sql(
        urls,
        "INSERT INTO customers (id, external_ref, display_name, email, tier, country)"
        " VALUES (%s, %s, %s, %s, 'verified', 'SK')",
        customer_id,
        ref,
        name,
        f"{ref.lower()}@example.com",
    )
    return customer_id


def add_operator(
    urls: Urls,
    email: str,
    role: str = "operator",
    *,
    name: str = "Marta Kováčová",
    active: bool = True,
) -> uuid.UUID:
    operator_id = uuid.UUID(str(uuid7()))
    run_sql(
        urls,
        "INSERT INTO operators (id, email, name, password_hash, role, is_active)"
        " VALUES (%s, %s, %s, %s, %s, %s)",
        operator_id,
        email,
        name,
        OPERATOR_HASH,
        role,
        active,
    )
    return operator_id


def operator_client(app: FastAPI, email: str, password: str = OPERATOR_PASSWORD) -> TestClient:
    """A logged-in operator whose client sends the CSRF token on every request."""
    operator = client(app)
    response = operator.post("/api/v1/auth/login", json={"email": email, "password": password})
    assert response.status_code == 200, response.text
    operator.headers["X-CSRF-Token"] = response.json()["csrf_token"]
    return operator


def customer_client(app: FastAPI, ref: str = "CUS-0001") -> TestClient:
    customer = client(app)
    response = customer.post(
        "/api/v1/demo/session", json={"customer_ref": ref, "access_code": ACCESS_CODE}
    )
    assert response.status_code == 200, response.text
    return customer


def key() -> dict[str, str]:
    return {"Idempotency-Key": str(uuid.uuid4())}


def new_ticket(customer: TestClient, text: str = "Where is my SEPA withdrawal?") -> uuid.UUID:
    response = customer.post("/api/v1/tickets", json={"message": text}, headers=key())
    assert response.status_code == 200, response.text
    for block in response.text.strip().split("\n\n"):
        if block.startswith("event: ticket.created"):
            data = block.split("data: ", 1)[1]
            return uuid.UUID(data.split('"ticket_id":"', 1)[1].split('"', 1)[0])
    raise AssertionError("no ticket.created event")
