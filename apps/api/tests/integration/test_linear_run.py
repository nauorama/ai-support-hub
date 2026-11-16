"""The phase-3 run through the API against Postgres, with recorded model answers (P3-05).

`POST /tickets` starts `intake → triage → write`; every run ends with the ticket in
`pending_approval` and a draft, or, on any error, `handed_off` with reason `system_error`. The
model answers come from `tests/fixtures/llm` (fake mode): the tickets are the ones in
`tests/fixtures/run/tickets.jsonl`, whose requests `make llm-requests` wrote with the same KB.
"""

import dataclasses
import json
import uuid
from collections.abc import Callable, Iterator
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from hub.api import tickets as tickets_module
from hub.audit import ChainCheck, verify_chain
from hub.config import Settings
from hub.db.session import connect
from hub.deps import Container, build_pipeline
from hub.domain.enums import TicketStatus
from hub.graph import linear
from hub.graph.linear import GRAPH_VERSION, RUN_FAILED_DETAIL, RunOutcome, run_ticket
from hub.kb.full_text import load_external_sources, sources_from_files
from hub.kb.loader import load_kb, read_kb
from hub.llm.schemas import ProviderRequest, ProviderResponse, Usage
from hub.main import create_app

from ._api import (
    ACCESS_CODE,
    SESSION_SECRET,
    Urls,
    add_customer,
    add_operator,
    customer_client,
    key,
    operator_client,
    own_database,
    run_sql,
)
from ._compose import ROOT

DATABASE = "hub_linear_run_test"
KB_DIR = ROOT / "data" / "kb"
TICKETS = {
    json.loads(line)["id"]: json.loads(line)["ticket"]
    for line in (ROOT / "apps/api/tests/fixtures/run/tickets.jsonl").read_text().splitlines()
    if line.strip()
}
SEPA, NO_ANSWER = TICKETS["run_01"], TICKETS["run_02"]
NOT_RECORDED = "This exact text has no recorded model answer, so the run must fail."


@pytest.fixture(scope="module")
def run_db(postgres_url: str) -> Iterator[Urls]:
    with own_database(postgres_url, DATABASE) as urls:
        _load_kb(urls)
        yield urls


def _load_kb(urls: Urls) -> None:
    import asyncio

    async def load() -> None:
        engine = create_async_engine(urls[1])
        async with AsyncSession(engine) as session, session.begin():
            await load_kb(session, read_kb(KB_DIR))
        await engine.dispose()

    asyncio.run(load())


def _settings(urls: Urls) -> Settings:
    return Settings(
        _env_file=None,
        app_env="test",
        database_url=urls[1],
        session_secret=SESSION_SECRET,
        demo_access_code=ACCESS_CODE,
        llm_mode="fake",
        models_config=ROOT / "config" / "models.yaml",
        prompts_dir=ROOT / "prompts",
    )


@pytest.fixture
def app(run_db: Urls) -> Iterator[FastAPI]:
    run_sql(
        run_db,
        "TRUNCATE audit_log, llm_calls, drafts, runs, feedback, messages, tickets, customers,"
        " operators RESTART IDENTITY CASCADE",
    )
    add_customer(run_db)
    settings = _settings(run_db)
    database = connect(settings.database_url.get_secret_value())
    container = Container(
        settings=settings, database=database, pipeline=build_pipeline(settings, database)
    )
    application = create_app(container)
    with TestClient(application):  # runs the lifespan: waits for runs at the end
        yield application


def _events(text: str) -> list[tuple[str, Any]]:
    events: list[tuple[str, Any]] = []
    for block in text.strip().split("\n\n"):
        lines = dict(line.split(": ", 1) for line in block.splitlines() if ": " in line)
        if "event" in lines:
            events.append((lines["event"], json.loads(lines["data"])))
    return events


def _post(customer: TestClient, text: str) -> tuple[uuid.UUID, list[tuple[str, Any]]]:
    response = customer.post("/api/v1/tickets", json={"message": text}, headers=key())
    assert response.status_code == 200, response.text
    events = _events(response.text)
    return uuid.UUID(events[0][1]["ticket_id"]), events


def _one(urls: Urls, query: str, *params: Any) -> tuple[Any, ...]:  # noqa: ANN401
    [row] = run_sql(urls, query, *params)
    return row


def _actions(urls: Urls) -> list[str]:
    """Audit actions in order, without the demo login of the customer client."""
    rows = run_sql(urls, "SELECT action FROM audit_log ORDER BY id")
    return [action for (action,) in rows if action != "auth.demo_session"]


# --- a run that ends with a draft ------------------------------------------------------------


def test_an_operator_approves_the_sepa_draft_and_the_customer_sees_it(
    app: FastAPI, run_db: Urls
) -> None:
    """Done when of P3-05: approve sends the message with the "AI assistant · reviewed by" label
    (P3-05b), on a draft the fake-mode run wrote."""
    add_operator(run_db, "marta@tallybridge.test")
    customer = customer_client(app)
    operator = operator_client(app, "marta@tallybridge.test")
    ticket_id, _ = _post(customer, SEPA)
    detail = operator.get(f"/api/v1/console/tickets/{ticket_id}").json()
    assert detail["latest_run"]["prompt_versions"] == {"triage": "v1", "write": "v1"}
    draft = detail["draft"]
    assert draft["status"] == "pending"

    response = operator.post(
        f"/api/v1/console/drafts/{draft['id']}/approve",
        json={"ticket_version": detail["version"]},
        headers=key(),
    )

    assert response.status_code == 200, response.text
    messages = customer.get(f"/api/v1/tickets/{ticket_id}/messages").json()["items"]
    sent = messages[-1]
    assert sent["author_label"] == "AI assistant · reviewed by Marta"
    assert sent["body"] == draft["body"]
    assert "/help/withdrawal-times" in {citation["url"] for citation in sent["citations"]}
    assert customer.get(f"/api/v1/tickets/{ticket_id}").json()["status"] == "answered"


def test_a_sepa_ticket_gets_a_cited_draft_and_waits_for_approval(
    app: FastAPI, run_db: Urls
) -> None:
    ticket_id, events = _post(customer_client(app), SEPA)

    assert [name for name, _ in events] == [
        "ticket.created",
        "run.status",
        "run.status",
        "message",
        "ticket.status",
        "done",
    ]
    assert [data["step"] for name, data in events if name == "run.status"] == [
        "understanding",
        "writing",
    ]
    assert events[4][1] == {"status": "pending_approval"}

    status, intent, urgency, summary, route, reasons = _one(
        run_db,
        "SELECT status, intent, urgency, summary, last_route, route_reasons FROM tickets"
        " WHERE id = %s",
        ticket_id,
    )
    assert (status, intent, route, reasons) == (
        "pending_approval",
        "withdrawal_issue",
        "approval",
        ["auto_reply_off"],
    )
    assert urgency in {"low", "normal"}
    assert summary

    run_id, run_status, run_route, versions, models, graph, cost, tokens_in, finished = _one(
        run_db,
        "SELECT id, status, route, prompt_versions, models, graph_version, total_cost_usd,"
        " input_tokens, finished_at FROM runs WHERE ticket_id = %s",
        ticket_id,
    )
    assert (run_status, run_route, graph) == ("completed", "approval", GRAPH_VERSION)
    assert versions == {"triage": "v1", "write": "v1"}
    assert models == {
        "triage": "anthropic/claude-haiku-5-5",
        "write": "anthropic/claude-haiku-5-5",
    }
    assert finished is not None

    calls = run_sql(
        run_db,
        "SELECT node, prompt_version, status, cost_usd, input_tokens FROM llm_calls"
        " WHERE run_id = %s ORDER BY created_at",
        run_id,
    )
    assert [(node, version, call) for node, version, call, _, _ in calls] == [
        ("triage", "triage/v1", "ok"),
        ("write", "write/v1", "ok"),
    ]
    assert cost == sum((row[3] for row in calls), Decimal(0))
    assert cost > 0
    assert tokens_in == sum(row[4] for row in calls)

    can_answer, body, citations, evidence, draft_status, missing = _one(
        run_db,
        "SELECT can_answer, body, citations, evidence, status, missing_information FROM drafts"
        " WHERE run_id = %s",
        run_id,
    )
    assert (can_answer, draft_status, missing) == (True, "pending", None)
    assert "withdrawal-times" in {citation["doc_id"] for citation in citations}
    assert all(citation["url"] == f"/help/{citation['doc_id']}" for citation in citations)
    assert all(f"[{citation['marker']}]" in body for citation in citations)
    assert evidence["variant"] == "b0_full_kb"


def test_the_run_writes_its_audit_records_without_ticket_text(app: FastAPI, run_db: Urls) -> None:
    ticket_id, _ = _post(customer_client(app), SEPA)

    assert _actions(run_db) == [
        "ticket.created",
        "intake.done",
        "triage.done",
        "draft.created",
        "route.decided",
    ]
    rows = {
        action: details
        for action, details in run_sql(run_db, "SELECT action, details FROM audit_log")
    }
    assert rows["intake.done"]["injection_score"] == 0.0
    assert rows["intake.done"]["pii_placeholders"] == {}
    triage = rows["triage.done"]
    assert (triage["intent"], triage["prompt_version"]) == ("withdrawal_issue", "triage/v1")
    assert "summary" not in triage
    assert rows["draft.created"]["prompt_version"] == "write/v1"
    assert rows["route.decided"]["reasons"] == ["auto_reply_off"]
    assert rows["route.decided"]["prompt_versions"] == {"triage": "v1", "write": "v1"}
    assert rows["draft.created"]["masked"] == {}
    assert rows["triage.done"]["summary_masked"] == {}
    # The audit log holds ids, codes and numbers, never message text (security §5).
    text = json.dumps(list(rows.values()))
    assert "SEPA" not in text
    assert "withdrew" not in text

    async def check() -> ChainCheck:
        engine = create_async_engine(run_db[1])
        async with AsyncSession(engine) as session:
            result = await verify_chain(session)
        await engine.dispose()
        return result

    import asyncio

    assert asyncio.run(check()).ok
    assert ticket_id


def test_a_question_outside_the_kb_gets_a_draft_that_cannot_answer(
    app: FastAPI, run_db: Urls
) -> None:
    ticket_id, events = _post(customer_client(app), NO_ANSWER)

    assert events[-2] == ("ticket.status", {"status": "pending_approval"})
    can_answer, body, citations, missing = _one(
        run_db,
        "SELECT can_answer, body, citations, missing_information FROM drafts WHERE ticket_id = %s",
        ticket_id,
    )
    assert (can_answer, body, citations) == (False, "", [])
    assert missing


# --- a run that fails ------------------------------------------------------------------------


def test_a_failed_model_call_hands_the_ticket_to_a_human(app: FastAPI, run_db: Urls) -> None:
    """No recording for this text: fake mode raises, the run fails, a human gets the ticket."""
    ticket_id, events = _post(customer_client(app), NOT_RECORDED)

    names = [name for name, _ in events]
    assert names == ["ticket.created", "run.status", "error", "message", "ticket.status", "done"]
    error = dict(events)["error"]
    assert (error["code"], error["status"], error["detail"]) == (
        "internal_error",
        500,
        RUN_FAILED_DETAIL,
    )
    assert events[4][1] == {"status": "handed_off"}

    assert _one(
        run_db, "SELECT status, last_route, route_reasons FROM tickets WHERE id = %s", ticket_id
    ) == ("handed_off", "handoff", ["system_error"])
    run_id, status, error_class, route = _one(
        run_db, "SELECT id, status, error, route FROM runs WHERE ticket_id = %s", ticket_id
    )
    assert (status, error_class, route) == ("failed", "FixtureMissingError", "handoff")
    assert _one(run_db, "SELECT status, error FROM llm_calls WHERE run_id = %s", run_id) == (
        "error",
        "FixtureMissing",
    )
    assert run_sql(run_db, "SELECT 1 FROM drafts WHERE ticket_id = %s", ticket_id) == []
    assert _actions(run_db)[-1] == "route.decided"


def test_an_empty_kb_hands_the_ticket_to_a_human(app: FastAPI, run_db: Urls) -> None:
    run_sql(run_db, "DELETE FROM kb_documents")
    try:
        ticket_id, events = _post(customer_client(app), SEPA)
    finally:
        _load_kb(run_db)
    assert events[-2] == ("ticket.status", {"status": "handed_off"})
    assert _one(run_db, "SELECT error FROM runs WHERE ticket_id = %s", ticket_id) == (
        "KbUnavailableError",
    )


def _ticket_with_message(
    urls: Urls, text: str, status: str = "processing"
) -> tuple[uuid.UUID, uuid.UUID]:
    """A ticket with one customer message, written outside the app (no run started)."""
    run_sql(
        urls, "TRUNCATE audit_log, llm_calls, drafts, runs, messages, tickets, customers CASCADE"
    )
    customer_id = add_customer(urls)
    ticket_id, message_id = uuid.uuid4(), uuid.uuid4()
    run_sql(
        urls,
        "INSERT INTO tickets (id, customer_id, channel, status, ai_disclosed_at)"
        " VALUES (%s, %s, 'chat', %s, now())",
        ticket_id,
        customer_id,
        status,
    )
    run_sql(
        urls,
        "INSERT INTO messages (id, ticket_id, role, body) VALUES (%s, %s, 'customer', %s)",
        message_id,
        ticket_id,
        text,
    )
    return ticket_id, message_id


@dataclasses.dataclass
class _Scripted:
    """A provider that answers each chat request with the next scripted content."""

    answers: list[str]

    async def complete(self, request: ProviderRequest) -> ProviderResponse:
        return ProviderResponse(
            content=self.answers.pop(0), usage=Usage(input_tokens=10, output_tokens=5)
        )

    async def embed(self, request: ProviderRequest) -> ProviderResponse:
        raise AssertionError("no embeddings in the linear run")


async def _run(
    urls: Urls,
    ticket_id: uuid.UUID,
    message_id: uuid.UUID,
    on_event: Callable[[str, dict[str, Any]], None] = lambda _name, _data: None,
    provider: _Scripted | None = None,
) -> tuple[RunOutcome, list[tuple[str, dict[str, Any]]]]:
    settings = _settings(urls)
    database = connect(settings.database_url.get_secret_value())
    pipeline = build_pipeline(settings, database)
    if provider is not None:
        pipeline = dataclasses.replace(
            pipeline, gateway=dataclasses.replace(pipeline.gateway, provider=provider)
        )
    emitted: list[tuple[str, dict[str, Any]]] = []

    async def emit(name: str, data: dict[str, Any]) -> None:
        emitted.append((name, data))
        on_event(name, data)

    try:
        outcome = await run_ticket(
            pipeline,
            ticket_id=ticket_id,
            message_id=message_id,
            emit=emit,
        )
    finally:
        await database.dispose()
    return outcome, emitted


async def test_a_ticket_claimed_during_the_run_keeps_its_operator(run_db: Urls) -> None:
    """The run still finishes; its draft is `superseded` and the ticket is left alone."""
    ticket_id, message_id = _ticket_with_message(run_db, SEPA)

    def claim(_name: str, data: dict[str, Any]) -> None:
        if data.get("step") == "writing":
            # An operator claims the ticket (the claim endpoint does processing → handed_off).
            run_sql(
                run_db,
                "UPDATE tickets SET status = 'handed_off', last_route = 'handoff',"
                " route_reasons = '[\"operator_takeover\"]', version = version + 1"
                " WHERE id = %s",
                ticket_id,
            )

    outcome, emitted = await _run(run_db, ticket_id, message_id, claim)

    assert not outcome.failed
    assert emitted[-1] == ("ticket.status", {"status": "handed_off"})
    assert _one(run_db, "SELECT status, route_reasons FROM tickets WHERE id = %s", ticket_id) == (
        "handed_off",
        ["operator_takeover"],
    )
    assert _one(run_db, "SELECT status FROM drafts WHERE ticket_id = %s", ticket_id) == (
        "superseded",
    )
    assert "route.decided" not in _actions(run_db)


async def test_a_claim_through_the_console_during_the_run_needs_the_current_version(
    app: FastAPI, run_db: Urls
) -> None:
    """P3-05a known gap: the claim goes through `POST /console/tickets/{id}/claim`. The run's
    triage save moved the version, so a claim with the version from before the run is 409; with
    the current one it hands the ticket off, and the run's draft is `superseded`."""
    ticket_id, message_id = _ticket_with_message(run_db, SEPA)
    add_operator(run_db, "marta@tallybridge.test")
    operator = operator_client(app, "marta@tallybridge.test")
    before = operator.get(f"/api/v1/console/tickets/{ticket_id}").json()["version"]
    answers: list[tuple[int, str]] = []

    def claim(_name: str, data: dict[str, Any]) -> None:
        if data.get("step") != "writing":
            return
        url = f"/api/v1/console/tickets/{ticket_id}/claim"
        stale = operator.post(url, json={"ticket_version": before})
        answers.append((stale.status_code, stale.json()["code"]))
        current = operator.get(f"/api/v1/console/tickets/{ticket_id}").json()["version"]
        assert current > before
        answers.append((operator.post(url, json={"ticket_version": current}).status_code, ""))

    outcome, emitted = await _run(run_db, ticket_id, message_id, claim)

    assert answers == [(409, "stale_draft"), (200, "")]
    assert not outcome.failed
    assert emitted[-1] == ("ticket.status", {"status": "handed_off"})
    assert _one(run_db, "SELECT status, route_reasons FROM tickets WHERE id = %s", ticket_id) == (
        "handed_off",
        ["operator_takeover"],
    )
    assert _one(run_db, "SELECT status FROM drafts WHERE ticket_id = %s", ticket_id) == (
        "superseded",
    )


async def test_no_run_starts_for_a_ticket_a_human_already_has(run_db: Urls) -> None:
    ticket_id, message_id = _ticket_with_message(run_db, SEPA, status="handed_off")

    outcome, emitted = await _run(run_db, ticket_id, message_id)

    assert outcome == RunOutcome(run_id=None, ticket_status=TicketStatus.HANDED_OFF, failed=False)
    # The stream still ends with the ticket's status (api.md §4, review round 1 N3).
    assert emitted == [
        ("run.status", {"step": "understanding"}),
        ("ticket.status", {"status": "handed_off"}),
    ]
    assert run_sql(run_db, "SELECT 1 FROM runs") == []


async def test_no_run_starts_for_a_message_of_another_ticket(run_db: Urls) -> None:
    ticket_id, _ = _ticket_with_message(run_db, SEPA)

    outcome, emitted = await _run(run_db, ticket_id, uuid.uuid4())

    assert outcome.run_id is None
    assert emitted[-1] == ("ticket.status", {"status": "processing"})
    assert run_sql(run_db, "SELECT 1 FROM runs") == []


async def test_a_failure_that_cannot_be_recorded_still_ends_the_stream(
    run_db: Urls, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When even the handoff cannot be written (database down), the stream gets `error` and the
    ticket's real status, `processing` (it waits in the queue); the run stays `running` for
    `recover-runs` (phase 6)."""
    ticket_id, message_id = _ticket_with_message(run_db, NOT_RECORDED)

    async def broken(*_args: object) -> None:
        raise ConnectionError

    monkeypatch.setattr(linear, "_fail", broken)
    outcome, emitted = await _run(run_db, ticket_id, message_id)

    assert (outcome.failed, outcome.ticket_status) == (True, None)
    assert emitted[1:] == [
        ("error", linear.run_failed_problem()),
        ("ticket.status", {"status": "processing"}),
    ]
    assert _one(run_db, "SELECT status FROM runs WHERE ticket_id = %s", ticket_id) == ("running",)


TEST_CARD = "4111 1111 1111 1111"  # public test card number


async def test_card_data_written_by_the_model_is_stored_masked(run_db: Urls) -> None:
    """Review round 1, B1 (AGENTS.md never-do #1): the model only sees masked text, but its
    answers are masked again before anything is stored."""
    ticket_id, message_id = _ticket_with_message(run_db, SEPA)
    triage = {
        "intent": "withdrawal_issue",
        "secondary_intent": None,
        "urgency": "normal",
        "wants_human": False,
        "reports_unauthorised_activity": False,
        "is_complaint_or_legal": False,
        "asks_for_action": "none",
        "injection_suspected": False,
        "summary": f"Customer with card {TEST_CARD} asks about a SEPA withdrawal.",
    }
    draft = {
        "can_answer": True,
        "missing_information": None,
        "answer": f"Your card {TEST_CARD} is not used for SEPA withdrawals [1].",
        "citations": [{"marker": 1, "source_id": "withdrawal-times"}],
        "proposed_actions": [
            {
                "type": "jira_create_issue",
                "issue_type": "dispute",
                "summary": f"Card {TEST_CARD}",
                "description": f"Check card {TEST_CARD}.",
            }
        ],
    }
    provider = _Scripted([json.dumps(triage), json.dumps(draft)])

    outcome, _ = await _run(run_db, ticket_id, message_id, provider=provider)

    assert not outcome.failed
    [(summary,)] = run_sql(run_db, "SELECT summary FROM tickets WHERE id = %s", ticket_id)
    assert summary == "Customer with card [CARD ****1111] asks about a SEPA withdrawal."
    body, actions = _one(
        run_db, "SELECT body, proposed_actions FROM drafts WHERE ticket_id = %s", ticket_id
    )
    assert body == "Your card [CARD ****1111] is not used for SEPA withdrawals [1]."
    assert actions[0]["params"]["summary"] == "Card [CARD ****1111]"
    assert actions[0]["params"]["description"] == "Check card [CARD ****1111]."
    stored = run_sql(
        run_db,
        "SELECT t.summary || d.body || d.proposed_actions::text || "
        "(SELECT string_agg(details::text, ' ') FROM audit_log)"
        " FROM tickets t JOIN drafts d ON d.ticket_id = t.id WHERE t.id = %s",
        ticket_id,
    )
    assert "4111" not in str(stored)
    audit = dict(run_sql(run_db, "SELECT action, details FROM audit_log"))
    assert audit["draft.created"]["masked"] == {"PAN": 3}
    assert audit["triage.done"]["summary_masked"] == {"PAN": 1}


# --- the stream ------------------------------------------------------------------------------


def test_a_run_that_ends_without_a_status_still_ends_the_stream_by_the_contract(
    app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    """api.md §4: placeholder, `ticket.status`, `done` even when the run task ends without a
    status (review round 1, N3)."""

    async def silent(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(tickets_module, "run_ticket", silent)
    _, events = _post(customer_client(app), SEPA)

    assert [name for name, _ in events] == ["ticket.created", "message", "ticket.status", "done"]
    assert events[2][1] == {"status": "processing"}


def test_a_replayed_request_starts_no_second_run(app: FastAPI, run_db: Urls) -> None:
    customer = customer_client(app)
    headers = key()
    first = customer.post("/api/v1/tickets", json={"message": SEPA}, headers=headers)
    second = customer.post("/api/v1/tickets", json={"message": SEPA}, headers=headers)

    assert first.status_code == second.status_code == 200
    assert "run.status" not in second.text
    assert _one(run_db, "SELECT count(*) FROM runs") == (1,)


def test_the_stream_pings_while_the_run_works(
    app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(tickets_module, "PING_INTERVAL_S", 0.0001)
    response = customer_client(app).post("/api/v1/tickets", json={"message": SEPA}, headers=key())
    assert ": ping" in response.text
    assert response.text.rstrip().endswith("data: {}")


# --- the KB text and the schema --------------------------------------------------------------


async def test_the_kb_text_from_postgres_equals_the_one_from_the_files(run_db: Urls) -> None:
    """Recorded write requests are built from the files; a live run reads Postgres. Equal text
    means equal fixture keys, so `make dev` finds the recordings."""
    engine = create_async_engine(run_db[1])
    async with AsyncSession(engine) as session:
        from_db = await load_external_sources(session)
    await engine.dispose()
    assert from_db == sources_from_files(read_kb(KB_DIR))


@pytest.mark.parametrize(
    ("table", "column", "rule"),
    [
        ("llm_calls", "run_id", "SET NULL"),
        ("messages", "run_id", "SET NULL"),
        ("drafts", "run_id", "CASCADE"),
        ("runs", "trigger_message_id", "SET NULL"),
    ],
)
def test_foreign_keys_to_runs_have_the_agreed_delete_rules(
    run_db: Urls, table: str, column: str, rule: str
) -> None:
    [(delete_rule,)] = run_sql(
        run_db,
        "SELECT rc.delete_rule FROM information_schema.referential_constraints rc"
        " JOIN information_schema.key_column_usage k ON k.constraint_name = rc.constraint_name"
        " WHERE k.table_name = %s AND k.column_name = %s",
        table,
        column,
    )
    assert delete_rule == rule


def test_the_compose_api_service_sees_the_fixtures_config_and_prompts() -> None:
    """Fake mode in a compose container needs the recordings at `FIXTURES_DIR`, and the app
    loads config/ and prompts/ at start (P3-05, deferred from P3-01)."""
    from hub.llm.gateway import FIXTURES_DIR

    from ._compose import compose_config

    volumes = {
        (volume["source"], volume["target"]): volume.get("read_only", False)
        for volume in compose_config()["services"]["api"]["volumes"]
    }
    relative = FIXTURES_DIR.relative_to(ROOT)
    expected = {
        (str(ROOT / relative), str(Path("/app") / relative)),
        (str(ROOT / "config"), "/app/config"),
        (str(ROOT / "prompts"), "/app/prompts"),
    }
    assert expected <= set(volumes)
    assert all(volumes[mount] for mount in expected), "mount them read-only"
