"""The Hub's tool client against the real `support-tools` server and Postgres (P7-03, gate 7).

P7-04: the research limit counted from `tool_calls` across clients, the scope built from the
tickets row and checked against it, intake's profile with the restricted flag, and the research
loop on a failed withdrawal with a scripted model.

The server runs as a subprocess over streamable HTTP with a bearer token, as in production
(`python -m support_tools`, mock data only, a temporary mock store). apps/api never imports
support_tools: the two talk only over MCP (design §8). Rows are written by
`DbToolCallRecorder` as the app role, in a database of its own.
"""

import asyncio
import json
import os
import secrets
import socket
import subprocess
import sys
import time
import uuid
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import httpx2
import psycopg
import pytest
from psycopg import sql
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from hub.audit import verify_chain
from hub.db.models import AuditLog, LlmCall, ToolCall
from hub.db.session import Database, connect
from hub.domain.enums import LlmCallContext, LlmCallMode, ToolCallStatus, Urgency
from hub.domain.memory import CustomerContext
from hub.domain.triage import TriageResult
from hub.graph.context import GraphContext
from hub.graph.nodes import intake, policy_gate, research
from hub.graph.state import InputFlags, TicketState, run_input
from hub.graph.tools import tool_scope
from hub.guardrails.injection import InjectionResult
from hub.guardrails.policy import load_policy
from hub.kb.retriever import KbChunk, SearchMode, SearchVisibility
from hub.llm.config import load_models
from hub.llm.gateway import Gateway
from hub.llm.pricing import load_pricing
from hub.llm.prompts import active_prompt
from hub.llm.recorder import DbCallRecorder
from hub.llm.schemas import ProviderRequest, ProviderResponse, Usage
from hub.llm.schemas import ToolCall as ModelToolCall
from hub.observability.readiness import libpq_url
from hub.tools.mcp_client import (
    CALLED_ACTION,
    DENIED_ACTION,
    DbToolCallRecorder,
    LocalResult,
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
THIRD_RUN_ID = uuid.UUID("0193a0c2-7d5e-7a01-8b02-00000000d006")
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
        for run_id in (RUN_ID, SECOND_RUN_ID, THIRD_RUN_ID):
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
    # Its own run: a denied call counts toward the run's limit of one `act` call (P7-04).
    unapproved = await client.session(_scope(ToolNode.ACT, THIRD_RUN_ID)).call(
        "jira_create_issue", ISSUE
    )
    assert unapproved.error == "approval_required"
    retried = await client.session(_scope(ToolNode.ACT, THIRD_RUN_ID)).call(
        "jira_create_issue", ISSUE
    )
    assert retried.error == "run_limit"
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
            node=ToolNode.ACT,
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


async def test_an_unknown_operator_and_a_card_as_tool_name_are_denied(
    client: ToolClient, database: Database
) -> None:
    approval = Approval(
        tool="jira_create_issue",
        ticket_id=TICKET_ID,
        basis=ApprovalBasis.OPERATOR_REVIEW,
        operator_id=uuid.UUID("0193a0c2-7d5e-7a01-8b02-00000000d0ff"),
        arguments=ISSUE,
    )
    issue = await client.session(_scope(ToolNode.ACT)).call(
        "jira_create_issue", ISSUE, approval=approval
    )
    assert issue.error == "unknown_operator"
    card = "4111 1111 1111 1111"  # public test card number
    named = await client.session(_scope(ToolNode.RESEARCH)).call(card, caller=Caller.MODEL)
    assert named.tool == "unknown"
    rows = await _rows(database)
    assert [(row.tool, row.status) for row in rows] == [
        ("jira_create_issue", ToolCallStatus.DENIED),
        ("unknown", ToolCallStatus.DENIED),
    ]
    async with database.sessions() as session:
        stored = await session.scalars(select(AuditLog.details))
        assert "4111" not in str(list(stored))


# --- P7-04 ------------------------------------------------------------------------------------

ROOT = Path(__file__).resolve().parents[4]
HAIKU = "anthropic/claude-haiku-5-5"


def _add_ticket(database_url: str, customer_ref: str) -> tuple[uuid.UUID, uuid.UUID]:
    """A customer of the core mock's seed with a ticket and a running run."""
    customer_id, ticket_id, run_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    with psycopg.connect(libpq_url(database_url), autocommit=True) as connection:
        connection.execute(
            "INSERT INTO customers (id, external_ref, display_name, email, tier, country)"
            " VALUES (%s, %s, 'Test Customer', %s, 'verified', 'PT')",
            (customer_id, customer_ref, f"{customer_ref.lower()}@example.com"),
        )
        connection.execute(
            "INSERT INTO tickets (id, customer_id, channel, status, ai_disclosed_at)"
            " VALUES (%s, %s, 'chat', 'processing', now())",
            (ticket_id, customer_id),
        )
        connection.execute(
            "INSERT INTO runs (id, ticket_id, status, graph_version)"
            " VALUES (%s, %s, 'running', 't')",
            (run_id, ticket_id),
        )
    return ticket_id, run_id


def _state(ticket_id: uuid.UUID, run_id: uuid.UUID, text: str) -> TicketState:
    # `customer_ref` here is the customer's id, as the run sets it: the tool scope never uses it.
    state = run_input(
        ticket_id=str(ticket_id), run_id=str(run_id), message_id=str(run_id), customer_ref="x"
    )
    state.update(
        TicketState(
            message_masked=text,
            message_for_llm=text,
            history_for_llm="(no earlier messages)",
            input_flags=InputFlags(
                injection=InjectionResult(score=0.0, patterns=[]),
                pii_placeholders={},
                language="en",
            ),
        )
    )
    return state


async def test_the_research_limit_is_counted_from_tool_calls_across_clients(
    mcp_server: tuple[str, str], database: Database
) -> None:
    url, token = mcp_server

    def fresh() -> ToolClient:
        # A new client per "process": nothing is shared but the database.
        return ToolClient(McpTransport(url, token), DbToolCallRecorder(database))

    async def search(arguments: dict[str, Any]) -> LocalResult:
        return LocalResult(result={"chunk_ids": ["fees#0"]}, summary="fees#0")

    first = fresh().session(_scope(ToolNode.RESEARCH), local={"search_kb": search})
    assert (await first.call("search_kb", {"query": "fees"}, caller=Caller.MODEL)).result
    await first.call("refund_customer", caller=Caller.MODEL)  # denied, still counted
    await first.call("get_cards", caller=Caller.MODEL)
    # A resume or a recovered run: a new session, a new client.
    second = fresh().session(_scope(ToolNode.RESEARCH))
    assert await second.calls_used() == 3
    assert (await second.call("get_account", caller=Caller.MODEL)).status is ToolCallStatus.OK
    assert (await second.call("get_cards", caller=Caller.MODEL)).status is ToolCallStatus.OK
    sixth = await second.call("get_cards", caller=Caller.MODEL)
    assert sixth.error == "run_limit"
    # Intake's call in the same run and research in another run have their own counts.
    assert (await fresh().session(_scope(ToolNode.INTAKE)).call("core_get_customer")).result
    other_run = fresh().session(_scope(ToolNode.RESEARCH, SECOND_RUN_ID))
    assert (await other_run.call("get_cards", caller=Caller.MODEL)).result

    rows = await _rows(database)
    assert [(row.node, row.tool, row.status) for row in rows] == [
        (ToolNode.RESEARCH, "search_kb", ToolCallStatus.OK),
        (ToolNode.RESEARCH, "unknown", ToolCallStatus.DENIED),
        (ToolNode.RESEARCH, "core_get_cards", ToolCallStatus.OK),
        (ToolNode.RESEARCH, "core_get_customer", ToolCallStatus.OK),
        (ToolNode.RESEARCH, "core_get_cards", ToolCallStatus.OK),
        (ToolNode.RESEARCH, "core_get_cards", ToolCallStatus.DENIED),
        (ToolNode.INTAKE, "core_get_customer", ToolCallStatus.OK),
        (ToolNode.RESEARCH, "core_get_cards", ToolCallStatus.OK),
    ]
    assert rows[0].arguments == {"query": "fees"}
    assert rows[0].result_summary == "fees#0"


async def test_a_scope_with_another_customers_reference_is_refused(
    client: ToolClient, database: Database
) -> None:
    # TICKET_ID belongs to CUS-0001; CUS-0002 exists in the core mock.
    forged = ToolScope(RUN_ID, TICKET_ID, "CUS-0002", ToolNode.RESEARCH)
    outcome = await client.session(forged).call("get_transactions", caller=Caller.MODEL)
    assert outcome.status is ToolCallStatus.DENIED
    assert outcome.error == "invalid_identity"
    [row] = await _rows(database)
    assert row.arguments == {"names": []}
    assert "CUS-0002" not in str(row.arguments)


async def test_the_scope_is_built_from_the_tickets_row(
    tools_db: tuple[str, str], database: Database
) -> None:
    ticket_id, run_id = _add_ticket(tools_db[0], "CUS-0016")
    pipeline = cast(Any, SimpleNamespace(database=database))
    state = _state(ticket_id, run_id, "Hi")
    state["triage"] = _triage(urgency="high")
    scope = await tool_scope(pipeline, state, ToolNode.RESEARCH)
    assert scope == ToolScope(run_id, ticket_id, "CUS-0016", ToolNode.RESEARCH, Urgency.HIGH)


def _triage(**changes: Any) -> TriageResult:  # noqa: ANN401 - any field value
    values: dict[str, Any] = {
        "intent": "withdrawal_issue",
        "secondary_intent": None,
        "urgency": "normal",
        "wants_human": False,
        "reports_unauthorised_activity": False,
        "is_complaint_or_legal": False,
        "asks_for_action": "none",
        "injection_suspected": False,
        "summary": "Asks where a withdrawal is.",
    }
    return TriageResult(**(values | changes))


async def _emit(name: str, data: dict[str, Any]) -> None:
    return None


@pytest.mark.parametrize(("customer", "restricted"), [("CUS-0007", True), ("CUS-0016", False)])
async def test_intake_loads_the_profile_and_a_restricted_customer_is_handed_off(
    client: ToolClient,
    tools_db: tuple[str, str],
    database: Database,
    customer: str,
    restricted: bool,
) -> None:
    ticket_id, run_id = _add_ticket(tools_db[0], customer)
    policy = load_policy(ROOT / "config" / "policy.yaml")
    pipeline = cast(Any, SimpleNamespace(database=database, tools=client, policy=policy))
    ctx = GraphContext(pipeline=pipeline, emit=_emit)
    state = _state(ticket_id, run_id, "Where is my withdrawal?")
    profile = await intake.load_profile(state, ctx)
    assert profile is not None
    assert profile.restricted is restricted
    assert "reason" not in profile.model_dump()
    state["customer_context"] = CustomerContext(profile=profile)
    state["triage"] = _triage()
    gate = await policy_gate.run(state, ctx)
    if restricted:
        assert gate["route"] == "handoff"
        assert gate["route_reasons"] == ["restricted_account"]
    else:
        assert gate == {}
    [row] = await _rows(database)
    assert (row.node, row.tool, row.status) == (ToolNode.INTAKE, "core_get_customer", "ok")
    assert row.arguments == {"customer_ref": customer}


@dataclass
class Scripted:
    """A model that answers each request with the next response, and keeps the requests."""

    responses: list[ProviderResponse]
    requests: list[ProviderRequest] = field(default_factory=list)

    async def complete(self, request: ProviderRequest) -> ProviderResponse:
        self.requests.append(request)
        return self.responses.pop(0)

    async def embed(self, request: ProviderRequest) -> ProviderResponse:  # pragma: no cover
        raise AssertionError("no embedding here")


class NoSearch:
    async def search(
        self, query: str, visibility: SearchVisibility, top_k: int = 6, mode: SearchMode = "hybrid"
    ) -> list[KbChunk]:
        return []


async def test_the_research_loop_finds_a_failed_withdrawal(
    client: ToolClient, tools_db: tuple[str, str], database: Database
) -> None:
    """ "Where is my withdrawal?" for CUS-0016 (seed: TX-000161, a failed SEPA withdrawal with
    error code ERR-2010): the loop calls the real tools, and the evidence holds the status and
    the error code as account facts; the model call is recorded in `llm_calls`."""
    ticket_id, run_id = _add_ticket(tools_db[0], "CUS-0016")
    models = load_models(ROOT / "config" / "models.yaml")

    def turn(*calls: tuple[str, dict[str, Any]]) -> ProviderResponse:
        return ProviderResponse(
            tool_calls=[
                ModelToolCall(id=f"c{n}", name=name, arguments=json.dumps(arguments))
                for n, (name, arguments) in enumerate(calls)
            ],
            usage=Usage(input_tokens=900, output_tokens=30),
        )

    model = Scripted(
        [
            turn(("get_transactions", {"type": "withdrawal_sepa", "status": "failed"})),
            turn(("get_transaction", {"reference": "TX-000161"})),
            ProviderResponse(content="done", usage=Usage(input_tokens=1500, output_tokens=2)),
        ]
    )
    gateway = Gateway(
        models=models,
        pricing=load_pricing(ROOT / "config" / "pricing.yaml"),
        provider=model,
        recorder=DbCallRecorder(database),
        mode=LlmCallMode.FAKE,
        budget=None,
    )
    pipeline = cast(
        Any,
        SimpleNamespace(
            database=database,
            tools=client,
            gateway=gateway,
            research_prompt=active_prompt(ROOT / "prompts", models, "research"),
            retriever=NoSearch(),
            variant="rag_hybrid",
            context=LlmCallContext.ONLINE,
            verifies=True,
            researches=True,
        ),
    )
    ctx = GraphContext(pipeline=pipeline, emit=_emit)
    state = _state(ticket_id, run_id, "Where is my withdrawal?")
    update = await research.with_tools(state, ctx, research.evidence(()))
    evidence = update["evidence"]
    assert evidence is not None
    assert not evidence.degraded
    assert evidence.tool_calls == 2
    texts = [fact.text for fact in evidence.account_facts]
    assert any("TX-000161" in t and "status failed, error code ERR-2010" in t for t in texts)
    assert all(fact.fact_id == f"A{n}" for n, fact in enumerate(evidence.account_facts, 1))
    sent = json.dumps([r.messages for r in model.requests])
    assert "restrict" not in sent
    rows = await _rows(database)
    assert [(row.node, row.tool, row.status) for row in rows] == [
        (ToolNode.RESEARCH, "core_list_transactions", ToolCallStatus.OK),
        (ToolNode.RESEARCH, "core_get_transaction", ToolCallStatus.OK),
    ]
    async with database.sessions() as session:
        calls = (await session.scalars(select(LlmCall).where(LlmCall.run_id == run_id))).all()
    assert [(c.node, c.prompt_version) for c in calls] == [("research", "research/v1")] * 3


async def test_a_failed_profile_load_hands_the_ticket_off(
    mcp_server: tuple[str, str], tools_db: tuple[str, str], database: Database
) -> None:
    """Fail-closed (owner decision, PR #41): restricted status unknown is not safe."""
    ticket_id, run_id = _add_ticket(tools_db[0], "CUS-0016")
    broken = ToolClient(McpTransport(mcp_server[0], "wrong"), DbToolCallRecorder(database))
    policy = load_policy(ROOT / "config" / "policy.yaml")
    pipeline = cast(Any, SimpleNamespace(database=database, tools=broken, policy=policy))
    ctx = GraphContext(pipeline=pipeline, emit=_emit)
    state = _state(ticket_id, run_id, "Where is my withdrawal?")
    with psycopg.connect(libpq_url(tools_db[0]), autocommit=True) as connection:
        connection.execute(
            "INSERT INTO messages (id, ticket_id, role, body) VALUES (%s, %s, 'customer', %s)",
            (run_id, ticket_id, "Where is my withdrawal?"),
        )
    update = await intake.run(state, ctx)
    assert update["errors"] == [intake.PROFILE_UNAVAILABLE]
    assert update["customer_context"] is not None
    assert update["customer_context"].profile is None
    state.update(update)
    gate = await policy_gate.run(state, ctx)
    assert (gate["route"], gate["route_reasons"]) == ("handoff", ["system_error"])
    [row] = await _rows(database)
    assert (row.node, row.status) == (ToolNode.INTAKE, ToolCallStatus.ERROR)
