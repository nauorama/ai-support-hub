"""The Hub's tool client against the real `support-tools` server and Postgres (P7-03, gate 7).

The server runs as a subprocess over streamable HTTP with a bearer token, as in production
(`python -m support_tools`, mock data only, a temporary mock store). apps/api never imports
support_tools: the two talk only over MCP (design §8). Rows are written by
`DbToolCallRecorder` as the app role, in a database of its own.
"""

import asyncio
import os
import secrets
import socket
import subprocess
import sys
import time
import uuid
from collections.abc import AsyncIterator, Iterator

import httpx2
import psycopg
import pytest
from psycopg import sql
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from hub.audit import verify_chain
from hub.db.models import AuditLog, ToolCall
from hub.db.session import Database, connect
from hub.domain.enums import ToolCallStatus
from hub.observability.readiness import libpq_url
from hub.tools.mcp_client import (
    CALLED_ACTION,
    DENIED_ACTION,
    DbToolCallRecorder,
    McpTransport,
    ToolClient,
)
from hub.tools.permissions import Approval, ApprovalBasis, Caller, ToolNode, ToolScope

from ._db import app_url, as_user, migrate

DATABASE = "hub_tool_calls_test"
CUSTOMER_ID = uuid.UUID("0193a0c2-7d5e-7a01-8b02-00000000d001")
TICKET_ID = uuid.UUID("0193a0c2-7d5e-7a01-8b02-00000000d002")
RUN_ID = uuid.UUID("0193a0c2-7d5e-7a01-8b02-00000000d003")
SECOND_RUN_ID = uuid.UUID("0193a0c2-7d5e-7a01-8b02-00000000d004")
OPERATOR_ID = uuid.UUID("0193a0c2-7d5e-7a01-8b02-00000000d005")
# Seed: TX-000001 belongs to CUS-0001, TX-000012 to CUS-0002.
OWN_TX, OTHER_TX = "TX-000001", "TX-000012"
ISSUE = {"issue_type": "dispute", "summary": "Card payment dispute", "description": "Check it."}


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port: int = sock.getsockname()[1]
        return port


@pytest.fixture(scope="module")
def mcp_server(tmp_path_factory: pytest.TempPathFactory) -> Iterator[tuple[str, str]]:
    """(URL, token) of a running support-tools server."""
    token = secrets.token_urlsafe(32)
    port = _free_port()
    store = tmp_path_factory.mktemp("mcp") / "mock.sqlite"
    env = {
        "PATH": os.environ.get("PATH", ""),
        "CORE_ANCHOR_DATE": "2026-11-12",
        "MCP_MOCK_DB": str(store),
        "MCP_SERVER_TOKEN": token,
    }
    command = [sys.executable, "-m", "support_tools", "--transport", "http", "--port", str(port)]
    server = subprocess.Popen(
        command, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    url = f"http://127.0.0.1:{port}/mcp"
    try:
        deadline = time.monotonic() + 20
        while True:
            try:
                httpx2.post(url, json={})
                break
            except httpx2.TransportError:
                if time.monotonic() > deadline or server.poll() is not None:
                    raise RuntimeError("the MCP server did not start") from None
                time.sleep(0.1)
        yield url, token
    finally:
        server.terminate()
        server.wait(timeout=10)


@pytest.fixture(scope="module")
def tools_db(postgres_url: str) -> Iterator[tuple[str, str]]:
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
async def database(tools_db: tuple[str, str]) -> AsyncIterator[Database]:
    with psycopg.connect(libpq_url(tools_db[0]), autocommit=True) as connection:
        connection.execute(
            "TRUNCATE tool_calls, audit_log, runs, tickets, customers, operators CASCADE"
        )
        connection.execute(
            "INSERT INTO customers (id, external_ref, display_name, email, tier, country)"
            " VALUES (%s, 'CUS-0001', 'Ana Novak', 'cus-0001@example.com', 'verified', 'SK')",
            (CUSTOMER_ID,),
        )
        connection.execute(
            "INSERT INTO operators (id, email, name, password_hash, role)"
            " VALUES (%s, 'op@example.com', 'Test Operator', 'x', 'operator')",
            (OPERATOR_ID,),
        )
        connection.execute(
            "INSERT INTO tickets (id, customer_id, channel, status, ai_disclosed_at)"
            " VALUES (%s, %s, 'chat', 'processing', now())",
            (TICKET_ID, CUSTOMER_ID),
        )
        for run_id in (RUN_ID, SECOND_RUN_ID):
            connection.execute(
                "INSERT INTO runs (id, ticket_id, status, graph_version)"
                " VALUES (%s, %s, 'running', 'test')",
                (run_id, TICKET_ID),
            )
    db = connect(tools_db[1])
    yield db
    await db.dispose()


@pytest.fixture
def client(mcp_server: tuple[str, str], database: Database) -> ToolClient:
    url, token = mcp_server
    return ToolClient(McpTransport(url, token), DbToolCallRecorder(database))


def _scope(node: ToolNode, run_id: uuid.UUID = RUN_ID) -> ToolScope:
    return ToolScope(run_id=run_id, ticket_id=TICKET_ID, customer_ref="CUS-0001", node=node)


async def _rows(database: Database) -> list[ToolCall]:
    async with database.sessions() as session:
        return list((await session.scalars(select(ToolCall).order_by(ToolCall.created_at))).all())


async def _actions(database: Database) -> list[str]:
    async with database.sessions() as session:
        return list((await session.scalars(select(AuditLog.action).order_by(AuditLog.id))).all())


async def test_no_tool_call_reaches_another_customers_data(
    client: ToolClient, database: Database
) -> None:
    session = client.session(_scope(ToolNode.RESEARCH))
    own = await session.call("get_transaction", {"reference": OWN_TX}, caller=Caller.MODEL)
    other = await session.call("get_transaction", {"reference": OTHER_TX}, caller=Caller.MODEL)
    forged = await session.call(
        "get_transaction",
        {"reference": OTHER_TX, "customer_ref": "CUS-0002"},
        caller=Caller.MODEL,
    )
    listed = await session.call("get_transactions", {"limit": 20}, caller=Caller.MODEL)

    assert own.status is ToolCallStatus.OK
    assert own.result is not None
    assert own.result["reference"] == OWN_TX
    assert other.status is ToolCallStatus.ERROR
    assert other.error is not None
    assert other.error.endswith("transaction not found")
    assert forged.status is ToolCallStatus.DENIED
    assert listed.result is not None
    references = {item["reference"] for item in listed.result["transactions"]}
    assert references
    assert OTHER_TX not in references

    rows = await _rows(database)
    assert [row.status for row in rows] == [
        ToolCallStatus.OK,
        ToolCallStatus.ERROR,
        ToolCallStatus.DENIED,
        ToolCallStatus.OK,
    ]
    assert rows[0].arguments == {"reference": OWN_TX, "customer_ref": "CUS-0001"}
    assert rows[2].arguments == {"names": ["customer_ref", "reference"]}
    assert "CUS-0002" not in str(rows[2].arguments)
    assert await _actions(database) == [CALLED_ACTION, CALLED_ACTION, DENIED_ACTION, CALLED_ACTION]
    async with database.sessions() as db_session:
        denied = await db_session.scalar(select(AuditLog).where(AuditLog.action == DENIED_ACTION))
        assert denied is not None
        assert denied.details["reason"] == "identity_argument"
        assert denied.details["fields"] == ["customer_ref"]
        assert denied.actor_type == "ai"
        assert (await verify_chain(db_session)).ok


async def test_an_approved_jira_issue_is_created_once_per_ticket(
    client: ToolClient, database: Database
) -> None:
    unapproved = await client.session(_scope(ToolNode.ACT)).call("jira_create_issue", ISSUE)
    assert unapproved.error == "approval_required"
    approval = Approval(
        tool="jira_create_issue",
        ticket_id=TICKET_ID,
        basis=ApprovalBasis.OPERATOR_REVIEW,
        operator_id=OPERATOR_ID,
        arguments=ISSUE,
    )
    created = await client.session(_scope(ToolNode.ACT)).call(
        "jira_create_issue", ISSUE, approval=approval
    )
    assert created.status is ToolCallStatus.OK, created.error
    again = await client.session(_scope(ToolNode.ACT, SECOND_RUN_ID)).call(
        "jira_create_issue", ISSUE, approval=approval
    )
    assert again.error == "ticket_limit"

    rows = await _rows(database)
    issue = next(row for row in rows if row.status is ToolCallStatus.OK)
    assert issue.side_effect
    assert issue.approved_by == OPERATOR_ID
    assert issue.external_ref is not None
    assert issue.external_ref.startswith("TB-")
    assert issue.idempotency_key == f"jira_create_issue:{TICKET_ID}"


async def test_the_idempotency_key_is_unique(database: Database) -> None:
    def row() -> ToolCall:
        return ToolCall(
            run_id=RUN_ID,
            tool="jira_create_issue",
            status=ToolCallStatus.OK,
            side_effect=True,
            idempotency_key="same-key",
            latency_ms=1,
        )

    async with database.sessions.begin() as session:
        session.add(row())
    with pytest.raises(IntegrityError):
        async with database.sessions.begin() as session:
            session.add(row())


async def test_a_wrong_token_is_an_error_outcome(
    mcp_server: tuple[str, str], database: Database
) -> None:
    client = ToolClient(McpTransport(mcp_server[0], "wrong"), DbToolCallRecorder(database))
    outcome = await asyncio.wait_for(
        client.session(_scope(ToolNode.INTAKE)).call("core_get_customer"), timeout=30
    )
    assert outcome.status is ToolCallStatus.ERROR
    [row] = await _rows(database)
    assert row.status is ToolCallStatus.ERROR
