"""Scheduled jobs, erasure and the privacy text (P8-05), against Postgres and Qdrant with recorded
model answers (fake mode).

- `POST /admin/jobs/{name}` with the job token: `retention`, `close-stale`, `recover-runs`
  (including a lost resume: the console committed the decision, the resume never ran), `ping`.
- `DELETE /admin/customers/{id}/data`: afterwards no row holds the customer's messages, memory
  or name, and the audit chain still verifies.
"""

import asyncio
import contextlib
import dataclasses
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from hub.api import console as console_api
from hub.api import console_drafts as console_drafts_api
from hub.audit import ChainCheck, verify_chain
from hub.db.repositories import TicketRepository
from hub.db.session import connect
from hub.deps import Container
from hub.graph import recover as recover_module
from hub.main import create_app

from ._api import ACCESS_CODE, Urls, add_customer, add_operator, key, own_database, run_sql
from .test_graph_run import NO_ANSWER, SEPA, _container, _index_kb, _one, _post, _truncate
from .test_human_review import _customer, _detail, _operator, _run_row, _snapshot

DATABASE = "hub_privacy_jobs_test"
OPERATOR = "marta@tallybridge.test"
ADMIN = "admin@tallybridge.test"
CUSTOMER_NAME = "Ana Novak"
JOBS_TOKEN = "jobs-token-for-tests"  # noqa: S105 - test value
BEARER = {"Authorization": f"Bearer {JOBS_TOKEN}"}


@pytest.fixture(scope="module")
def jobs_db(postgres_url: str, qdrant_url: str) -> Iterator[Urls]:
    with own_database(postgres_url, DATABASE) as urls:
        _index_kb(urls, qdrant_url)
        yield urls


@pytest.fixture
def fresh(jobs_db: Urls) -> Urls:
    _truncate(jobs_db)
    run_sql(jobs_db, "TRUNCATE checkpoints, checkpoint_blobs, checkpoint_writes, customer_memory")
    add_customer(jobs_db, name=CUSTOMER_NAME)
    add_operator(jobs_db, OPERATOR)
    add_operator(jobs_db, ADMIN, "admin", name="Ada Admin")
    return jobs_db


def _jobs_container(urls: Urls, qdrant_url: str, **settings: Any) -> Container:  # noqa: ANN401
    base = _container(urls, qdrant_url)
    update = {"jobs_token": SecretStr(JOBS_TOKEN), **settings}
    return dataclasses.replace(base, settings=base.settings.model_copy(update=update))


@contextlib.contextmanager
def _app(urls: Urls, qdrant_url: str, **settings: Any) -> Iterator[TestClient]:  # noqa: ANN401
    application = create_app(_jobs_container(urls, qdrant_url, **settings))
    with TestClient(application, base_url="https://testserver") as entered:
        yield entered


@pytest.fixture
def web(fresh: Urls, qdrant_url: str) -> Iterator[TestClient]:
    with _app(fresh, qdrant_url) as entered:
        yield entered


def _job(web: TestClient, name: str) -> dict[str, Any]:
    response = web.post(f"/api/v1/admin/jobs/{name}", headers=BEARER)
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    assert body["job"] == name
    report: dict[str, Any] = body["report"]
    return report


def _no_resume(monkeypatch: pytest.MonkeyPatch) -> None:
    """The process "stops" between the console's commit and the resume."""

    async def lost(*_args: object) -> None:
        return None

    monkeypatch.setattr(console_drafts_api, "resume_after_commit", lost)
    monkeypatch.setattr(console_api, "resume_after_commit", lost)


def _decided_long_ago(monkeypatch: pytest.MonkeyPatch) -> None:
    # The audit timestamps are hashed and cannot be moved back; the threshold moves instead.
    monkeypatch.setattr(recover_module, "STUCK_AFTER_S", -1.0)


def _audit(urls: Urls, action: str) -> list[tuple[Any, ...]]:
    return run_sql(
        urls,
        "SELECT actor_type, actor_id, entity_id, details FROM audit_log WHERE action = %s"
        " ORDER BY id",
        action,
    )


# --- job token --------------------------------------------------------------------------------


def test_the_job_token_is_required_and_opens_nothing_else(web: TestClient) -> None:
    assert web.post("/api/v1/admin/jobs/ping").status_code == 401
    wrong = web.post("/api/v1/admin/jobs/ping", headers={"Authorization": "Bearer nope"})
    assert (wrong.status_code, wrong.json()["code"]) == (401, "unauthorized")
    basic = web.post("/api/v1/admin/jobs/ping", headers={"Authorization": f"Basic {JOBS_TOKEN}"})
    assert basic.status_code == 401
    assert web.post("/api/v1/admin/jobs/nope", headers=BEARER).status_code == 404
    # The token is no session: the admin endpoints still answer 401.
    assert web.post("/api/v1/admin/runs/recover", headers=BEARER).status_code == 401
    assert web.get("/api/v1/console/tickets", headers=BEARER).status_code == 401


def test_an_empty_job_token_turns_the_jobs_off(fresh: Urls, qdrant_url: str) -> None:
    with _app(fresh, qdrant_url, jobs_token=SecretStr("")) as web:
        response = web.post("/api/v1/admin/jobs/ping", headers={"Authorization": "Bearer "})
    assert (response.status_code, response.json()["code"]) == (503, "jobs_unavailable")


def test_ping_touches_postgres_and_qdrant_and_is_audited(web: TestClient, fresh: Urls) -> None:
    report = _job(web, "ping")
    assert report["postgres_chunks"] == report["qdrant_points"] > 0
    [(actor_type, actor_id, entity_id, details)] = _audit(fresh, "job.ran")
    assert (actor_type, actor_id, entity_id) == ("system", "jobs", "ping")
    assert details == report


# --- recover-runs: the lost resume ------------------------------------------------------------


def test_recover_runs_resumes_a_lost_approve_with_the_rebuilt_decision(
    web: TestClient, fresh: Urls, monkeypatch: pytest.MonkeyPatch
) -> None:
    """From P6-03: the approve committed (message sent, ticket answered), the resume was lost;
    the job rebuilds the decision from the draft, the ticket and the audit log and resumes."""
    _no_resume(monkeypatch)
    ticket_id, _ = _post(_customer(web), SEPA)
    operator = _operator(web, OPERATOR)
    detail = _detail(operator, ticket_id)
    approved = operator.post(
        f"/api/v1/console/drafts/{detail['draft']['id']}/approve",
        json={"ticket_version": detail["version"]},
        headers=key(),
    )
    assert approved.status_code == 200, approved.text
    message_id = approved.json()["message"]["id"]
    run_id, status, *_ = _run_row(fresh, ticket_id)
    assert status == "interrupted"

    # Decided just now: the console's own resume may still come, so the job leaves it.
    assert _job(web, "recover-runs")["lost_resumes"] == 0
    _decided_long_ago(monkeypatch)
    assert _job(web, "recover-runs") == {
        "found": 1,
        "resumed": 1,
        "failed": 0,
        "lost_resumes": 1,
    }
    _, status, route, finished, error = _run_row(fresh, ticket_id)
    assert (status, route, error) == ("completed", "approval", None)
    assert finished is not None
    values = asyncio.run(_snapshot(fresh, ticket_id)).values
    review = values["review"]
    assert (review.kind, review.message_id, review.body) == ("approve", message_id, None)
    [(operator_id,)] = run_sql(fresh, "SELECT id FROM operators WHERE email = %s", OPERATOR)
    assert review.operator_id == str(operator_id)
    assert values["final_message_id"] == message_id
    [(actor_type, actor_id, entity_id, details)] = _audit(fresh, "run.recovered")
    assert (actor_type, actor_id, entity_id) == ("system", "jobs", str(run_id))
    assert details == {"ticket_id": str(ticket_id), "from": "lost_resume", "kind": "approve"}
    # Nothing left to do on the next run.
    assert _job(web, "recover-runs")["found"] == 0


def test_recover_runs_resumes_a_lost_first_reply_of_a_handoff(
    web: TestClient, fresh: Urls, monkeypatch: pytest.MonkeyPatch
) -> None:
    _no_resume(monkeypatch)
    ticket_id, _ = _post(_customer(web), NO_ANSWER)
    operator = _operator(web, OPERATOR)
    reply = operator.post(
        f"/api/v1/console/tickets/{ticket_id}/reply",
        json={"ticket_version": _detail(operator, ticket_id)["version"], "body": "On it."},
        headers=key(),
    )
    assert reply.status_code == 200, reply.text
    assert _run_row(fresh, ticket_id)[1] == "interrupted"

    _decided_long_ago(monkeypatch)
    report = _job(web, "recover-runs")
    assert (report["lost_resumes"], report["resumed"]) == (1, 1)
    assert _run_row(fresh, ticket_id)[1] == "completed"
    values = asyncio.run(_snapshot(fresh, ticket_id)).values
    review = values["review"]
    assert (review.kind, review.body, review.message_id) == ("reply", "On it.", reply.json()["id"])


def test_a_run_that_still_waits_is_no_lost_resume(
    web: TestClient, fresh: Urls, monkeypatch: pytest.MonkeyPatch
) -> None:
    _decided_long_ago(monkeypatch)
    pending, _ = _post(_customer(web), SEPA)
    handed_off, _ = _post(web, NO_ANSWER)
    assert _job(web, "recover-runs")["found"] == 0
    assert _run_row(fresh, pending)[1] == "interrupted"
    assert _run_row(fresh, handed_off)[1] == "interrupted"


# --- close-stale ------------------------------------------------------------------------------


def _answered_ticket(web: TestClient, urls: Urls) -> uuid.UUID:
    ticket_id, _ = _post(_customer(web), SEPA)
    operator = _operator(web, OPERATOR)
    detail = _detail(operator, ticket_id)
    response = operator.post(
        f"/api/v1/console/drafts/{detail['draft']['id']}/approve",
        json={"ticket_version": detail["version"]},
        headers=key(),
    )
    assert response.status_code == 200, response.text
    return ticket_id


def _age(urls: Urls, ticket_id: uuid.UUID, interval: str) -> None:
    """Move a ticket and its messages back in time (test setup, as the migrator)."""
    run_sql(
        urls,
        "UPDATE messages SET created_at = created_at - %s::interval WHERE ticket_id = %s",
        interval,
        ticket_id,
    )
    run_sql(
        urls,
        "UPDATE tickets SET created_at = created_at - %s::interval WHERE id = %s",
        interval,
        ticket_id,
    )


def test_close_stale_closes_answered_tickets_after_72_h_through_the_checked_path(
    web: TestClient, fresh: Urls, monkeypatch: pytest.MonkeyPatch
) -> None:
    stale = _answered_ticket(web, fresh)
    recent = _answered_ticket(web, fresh)
    _age(fresh, stale, "73 hours")
    _age(fresh, recent, "71 hours")
    changes: list[str] = []
    original = TicketRepository.change_status

    async def counted(self: TicketRepository, ticket: Any, target: Any) -> Any:  # noqa: ANN401
        changes.append(str(target))
        return await original(self, ticket, target)

    monkeypatch.setattr(TicketRepository, "change_status", counted)

    assert _job(web, "close-stale") == {"found": 1, "closed": 1}
    assert changes == ["closed"]
    status = dict(run_sql(fresh, "SELECT id, status FROM tickets"))
    assert (status[stale], status[recent]) == ("closed", "answered")
    [(actor_type, actor_id, entity_id, details)] = _audit(fresh, "ticket.closed")
    assert (actor_type, actor_id, entity_id) == ("system", "jobs", str(stale))
    assert details == {"from_status": "answered", "job": "close-stale"}
    # The memory summary was attempted as by the close endpoint (fake mode has no recording
    # for it, so `memory.failed`); either way the close stands.
    memory = run_sql(
        fresh,
        "SELECT action FROM audit_log WHERE action IN ('memory.written', 'memory.failed')"
        " AND entity_id = %s",
        str(stale),
    )
    assert len(memory) == 1
    # A second run finds nothing.
    assert _job(web, "close-stale") == {"found": 0, "closed": 0}


def test_a_customer_message_makes_an_old_ticket_fresh_again(web: TestClient, fresh: Urls) -> None:
    ticket_id = _answered_ticket(web, fresh)
    _age(fresh, ticket_id, "100 hours")
    # The operator's answer is old too; only the customer's own messages count.
    run_sql(
        fresh,
        "UPDATE messages SET created_at = now() WHERE ticket_id = %s AND role = 'operator'",
        ticket_id,
    )
    assert _job(web, "close-stale")["closed"] == 1
    later = _answered_ticket(web, fresh)
    _age(fresh, later, "100 hours")
    run_sql(
        fresh,
        "UPDATE messages SET created_at = now() WHERE ticket_id = %s AND role = 'customer'",
        later,
    )
    assert _job(web, "close-stale")["closed"] == 0


# --- retention --------------------------------------------------------------------------------


def _checkpoints(urls: Urls, ticket_id: uuid.UUID) -> int:
    [(count,)] = run_sql(
        urls, "SELECT count(*) FROM checkpoints WHERE thread_id = %s", str(ticket_id)
    )
    return int(count)


def test_retention_deletes_old_tickets_expired_memory_and_old_checkpoints(
    fresh: Urls, qdrant_url: str
) -> None:
    with _app(fresh, qdrant_url, retention_checkpoint_days=0) as web:
        old = _answered_ticket(web, fresh)
        waiting, _ = _post(web, SEPA)
        closed, _ = _post(web, NO_ANSWER)
        operator = _operator(web, OPERATOR)
        response = operator.post(
            f"/api/v1/console/tickets/{closed}/close",
            json={"ticket_version": _detail(operator, closed)["version"]},
        )
        assert response.status_code == 200, response.text
        _age(fresh, old, "731 days")
        customer_id = _one(fresh, "SELECT id FROM customers")[0]
        run_sql(
            fresh,
            "INSERT INTO customer_memory (id, customer_id, ticket_id, kind, content, expires_at)"
            " VALUES (gen_random_uuid(), %s, %s, 'ticket_summary', 'old summary',"
            " now() - interval '1 day')",
            customer_id,
            waiting,
        )
        [(llm_calls,)] = run_sql(fresh, "SELECT count(*) FROM llm_calls")
        assert all(_checkpoints(fresh, t) > 0 for t in (old, waiting, closed))

        report = _job(web, "retention")

    assert report["tickets_deleted"] == 1
    assert report["runs_deleted"] == 1
    assert report["drafts_deleted"] == 1
    assert report["messages_deleted"] >= 2
    assert report["memory_deleted"] == 1
    assert report["checkpoint_threads_deleted"] == 2  # the old ticket and the closed one
    tickets = {row[0] for row in run_sql(fresh, "SELECT id FROM tickets")}
    assert tickets == {waiting, closed}
    assert run_sql(fresh, "SELECT count(*) FROM messages WHERE ticket_id = %s", old) == [(0,)]
    assert run_sql(fresh, "SELECT count(*) FROM customer_memory") == [(0,)]
    # The cost record stays, without its run.
    assert run_sql(fresh, "SELECT count(*) FROM llm_calls") == [(llm_calls,)]
    assert (_checkpoints(fresh, old), _checkpoints(fresh, closed)) == (0, 0)
    assert _checkpoints(fresh, waiting) > 0
    [(_, _, _, details)] = _audit(fresh, "job.ran")
    assert details["tickets_deleted"] == 1


# --- erasure ----------------------------------------------------------------------------------

FEEDBACK = "feedback that names the customer"
MEMORY = "Customer asked about a SEPA withdrawal twice"
TOOL_TEXT = f"Dispute for {CUSTOMER_NAME}"


def _rows_containing(urls: Urls, needle: str) -> list[str]:
    """Every row of every table in `public` whose text form contains `needle`; the checkpoint
    tables are checked by thread id instead (their blobs are binary)."""
    tables = [
        name
        for (name,) in run_sql(
            urls,
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
            " AND tablename NOT LIKE 'checkpoint%%'",
        )
    ]
    hits = []
    for table in tables:
        # Table names come from pg_tables, not from input.
        query = f'SELECT count(*) FROM "{table}" t WHERE t::text LIKE %s'  # noqa: S608
        rows = run_sql(urls, query, f"%{needle}%")
        if rows[0][0]:
            hits.append(table)
    return hits


def test_erasure_leaves_no_message_memory_or_name_and_the_chain_verifies(
    web: TestClient, fresh: Urls
) -> None:
    ticket_id = _answered_ticket(web, fresh)
    waiting, _ = _post(web, NO_ANSWER)
    customer_id = _one(fresh, "SELECT id FROM customers")[0]
    ai_message = _customer(web).get(f"/api/v1/tickets/{ticket_id}/messages").json()["items"][-1]
    feedback = web.post(
        f"/api/v1/tickets/{ticket_id}/feedback",
        json={"message_id": ai_message["id"], "rating": "up", "comment": FEEDBACK},
    )
    assert feedback.status_code in (200, 201), feedback.text
    run_sql(
        fresh,
        "INSERT INTO customer_memory (id, customer_id, ticket_id, kind, content)"
        " VALUES (gen_random_uuid(), %s, %s, 'ticket_summary', %s)",
        customer_id,
        ticket_id,
        MEMORY,
    )
    run_id = _run_row(fresh, ticket_id)[0]
    run_sql(
        fresh,
        "INSERT INTO tool_calls (id, run_id, node, tool, arguments, result_summary, status,"
        " side_effect, latency_ms) VALUES (gen_random_uuid(), %s, 'act', 'jira_create_issue',"
        " %s::jsonb, %s, 'ok', true, 5)",
        run_id,
        f'{{"summary": "{TOOL_TEXT}", "issue_type": "dispute"}}',
        TOOL_TEXT,
    )
    for needle in (CUSTOMER_NAME, SEPA, MEMORY, FEEDBACK):
        assert _rows_containing(fresh, needle), needle  # the scan finds them before
    assert _checkpoints(fresh, ticket_id) > 0

    assert (
        _operator(web, OPERATOR).delete(f"/api/v1/admin/customers/{customer_id}/data").status_code
        == 403
    )
    admin = _operator(web, ADMIN)
    assert admin.delete(f"/api/v1/admin/customers/{uuid.uuid4()}/data").status_code == 404
    response = admin.delete(f"/api/v1/admin/customers/{customer_id}/data")
    assert response.status_code == 200, response.text
    report = response.json()
    assert report["customer_id"] == str(customer_id)
    assert report["tickets_anonymised"] == 2
    assert report["messages_deleted"] >= 4
    # The approved draft, and the superseded draft the handoff's run kept for the operator.
    assert (report["drafts_deleted"], report["memory_deleted"]) == (2, 1)
    assert (report["feedback_deleted"], report["tool_calls_redacted"]) == (1, 1)
    assert report["runs_ended"] == 1  # the handoff's run still waited
    assert report["checkpoint_threads_deleted"] == 2

    for needle in (CUSTOMER_NAME, SEPA, NO_ANSWER, MEMORY, FEEDBACK, TOOL_TEXT):
        assert _rows_containing(fresh, needle) == [], needle
    assert (_checkpoints(fresh, ticket_id), _checkpoints(fresh, waiting)) == (0, 0)
    assert run_sql(fresh, "SELECT arguments, result_summary FROM tool_calls") == [
        ({"names": ["issue_type", "summary"]}, None)
    ]
    # Kept for statistics: the ticket rows (anonymised) and the runs.
    assert run_sql(fresh, "SELECT count(*) FROM tickets WHERE summary IS NULL") == [(2,)]
    assert run_sql(fresh, "SELECT status, error FROM runs WHERE ticket_id = %s", waiting) == [
        ("failed", "CustomerErased")
    ]
    [(actor_type, _, entity_id, details)] = _audit(fresh, "customer.erased")
    assert (actor_type, entity_id) == ("operator", str(customer_id))
    assert details["messages_deleted"] == report["messages_deleted"]

    check = asyncio.run(_verify(fresh))
    assert check.ok, check
    assert check.checked > 0
    # The erased customer has no demo session any more, and a repeat finds nothing.
    login = web.post(
        "/api/v1/demo/session", json={"customer_ref": "CUS-0001", "access_code": ACCESS_CODE}
    )
    assert login.status_code == 404
    again = admin.delete(f"/api/v1/admin/customers/{customer_id}/data").json()
    assert (again["messages_deleted"], again["tickets_anonymised"]) == (0, 2)


async def _verify(urls: Urls) -> ChainCheck:
    database = connect(urls[1])  # the app role, as `GET /console/audit/verify` will (P10-09)
    try:
        async with database.sessions() as session:
            return await verify_chain(session)
    finally:
        await database.dispose()


# --- privacy text -----------------------------------------------------------------------------


def test_the_privacy_text_is_public(fresh: Urls, qdrant_url: str) -> None:
    with _app(fresh, qdrant_url) as web:
        response = web.get("/api/v1/privacy")
    assert response.status_code == 200
    body = response.json()
    headings = [section["heading"] for section in body["sections"]]
    assert headings == [
        "What the AI sees",
        "Who processes the data",
        "How long we keep it",
        "Talking to a person",
    ]
    text = " ".join(section["text"] for section in body["sections"])
    assert "Anthropic" in text
    assert "Talk to a human" in text


def test_a_lost_resume_without_its_checkpoint_fails_the_run_and_keeps_the_answer(
    web: TestClient, fresh: Urls, monkeypatch: pytest.MonkeyPatch
) -> None:
    _no_resume(monkeypatch)
    ticket_id = _answered_ticket(web, fresh)
    run_sql(fresh, "DELETE FROM checkpoints WHERE thread_id = %s", str(ticket_id))
    _decided_long_ago(monkeypatch)
    report = _job(web, "recover-runs")
    assert (report["lost_resumes"], report["resumed"], report["failed"]) == (1, 0, 1)
    _, status, _, _, error = _run_row(fresh, ticket_id)
    assert (status, error) == ("failed", "RunIncompleteError")
    assert _one(fresh, "SELECT status FROM tickets WHERE id = %s", ticket_id) == ("answered",)


def test_a_failed_job_run_is_audited_with_its_error_class(fresh: Urls, qdrant_url: str) -> None:
    container = dataclasses.replace(_jobs_container(fresh, qdrant_url), pipeline=None)
    with TestClient(create_app(container), base_url="https://testserver") as web:
        response = web.post("/api/v1/admin/jobs/recover-runs", headers=BEARER)
    assert (response.status_code, response.json()["code"]) == (503, "workflow_unavailable")
    [(actor_type, _, entity_id, details)] = _audit(fresh, "job.ran")
    assert (actor_type, entity_id, details) == ("system", "recover-runs", {"error": "ProblemError"})
