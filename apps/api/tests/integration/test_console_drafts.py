"""Console review of a draft against Postgres as the app role (P3-05b): approve, edit, reject;
labels, edit ratio, `draft_cannot_answer`, idempotency, conflicts, roles and audit records.

The app here has no pipeline, so a new ticket starts no run; each test writes the run and the
draft as the migrator (standing in for the linear run of P3-05a, whose own integration test
approves a real fake-mode draft end to end).
"""

import asyncio
import json
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from hub.audit import ChainCheck, verify_chain
from hub.main import create_app

from . import _api
from ._api import Urls, add_customer, add_operator, customer_client, key, new_ticket, run_sql

DATABASE = "hub_console_drafts_test"
# Public test card number (Visa test range); never a real card.
TEST_CARD = "4111 1111 1111 1111"
RAW_DIGITS = "4111111111111111"
EMAILS = {
    "operator": "marta@tallybridge.test",
    "operator2": "jan@tallybridge.test",
    "lead": "lena@tallybridge.test",
    "compliance": "olga@tallybridge.test",
}
DRAFT_BODY = (
    "A SEPA withdrawal usually arrives within 1 business day [1]. "
    "If it was returned, the money comes back to your balance [2]."
)
CITATIONS = [
    {
        "marker": 1,
        "source_id": "withdrawal-times",
        "doc_id": "withdrawal-times",
        "title": "How long does a withdrawal take?",
        "url": "/help/withdrawal-times",
    },
    {
        "marker": 2,
        "source_id": "withdrawal-failed-or-returned",
        "doc_id": "withdrawal-failed-or-returned",
        "title": "Failed or returned withdrawals",
        "url": "/help/withdrawal-failed-or-returned",
    },
]
ACTIONS = [
    {
        "id": "a1",
        "type": "jira_create_issue",
        "params": {"issue_type": "bug", "summary": "Check payout", "description": "Look at it"},
    }
]


@pytest.fixture(scope="module")
def urls(postgres_url: str) -> Iterator[Urls]:
    with _api.own_database(postgres_url, DATABASE) as found:
        yield found


@pytest.fixture
def operators(urls: Urls) -> dict[str, uuid.UUID]:
    _api.empty_tables(urls)
    add_customer(urls, "CUS-0001", "Ana Novak")
    names = {"operator2": "Jan Novák", "lead": "Lena Lead", "compliance": "Olga Check"}
    return {
        who: add_operator(urls, email, who.rstrip("2"), name=names.get(who, "Marta Kováčová"))
        for who, email in EMAILS.items()
    }


@pytest.fixture
def app(urls: Urls, operators: dict[str, uuid.UUID]) -> Iterator[FastAPI]:
    app = create_app(_api.container(urls))
    with TestClient(app):
        yield app


@pytest.fixture
def staff(app: FastAPI) -> dict[str, TestClient]:
    return {who: _api.operator_client(app, email) for who, email in EMAILS.items()}


@pytest.fixture
def customer(app: FastAPI) -> TestClient:
    return customer_client(app)


def _with_draft(
    urls: Urls,
    customer: TestClient,
    *,
    can_answer: bool = True,
    body: str = DRAFT_BODY,
    citations: list[dict[str, Any]] | None = None,
    actions: list[dict[str, Any]] | None = None,
) -> tuple[uuid.UUID, uuid.UUID]:
    """A ticket whose run left a pending draft: what the linear run of P3-05a writes."""
    ticket_id = new_ticket(customer, "Where is my SEPA withdrawal of 250 EUR?")
    run_id, draft_id = uuid.uuid4(), uuid.uuid4()
    run_sql(
        urls,
        "INSERT INTO runs (id, ticket_id, status, route, route_reasons, graph_version,"
        " prompt_versions, models, total_cost_usd, input_tokens, output_tokens, finished_at)"
        " VALUES (%s, %s, 'completed', 'approval', '[\"auto_reply_off\"]', 'linear-v1',"
        ' \'{"triage": "v1", "write": "v1"}\','
        ' \'{"write": "anthropic/claude-haiku-5-5"}\', 0.003947, 30417, 430, now())',
        run_id,
        ticket_id,
    )
    run_sql(
        urls,
        "INSERT INTO drafts (id, run_id, ticket_id, can_answer, missing_information, body,"
        " citations, evidence, proposed_actions, status)"
        " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'pending')",
        draft_id,
        run_id,
        ticket_id,
        can_answer,
        None if can_answer else "The KB says nothing about interest.",
        body if can_answer else "",
        json.dumps(CITATIONS if citations is None else citations),
        json.dumps({"variant": "b0_full_kb", "documents": [], "dropped_markers": []}),
        json.dumps(actions or []),
    )
    run_sql(
        urls,
        "UPDATE tickets SET status = 'pending_approval', last_route = 'approval',"
        " route_reasons = '[\"auto_reply_off\"]', version = version + 1 WHERE id = %s",
        ticket_id,
    )
    return ticket_id, draft_id


def _version(client: TestClient, ticket_id: uuid.UUID) -> int:
    response = client.get(f"/api/v1/console/tickets/{ticket_id}")
    assert response.status_code == 200, response.text
    version: int = response.json()["version"]
    return version


def _review(
    client: TestClient,
    ticket_id: uuid.UUID,
    draft_id: uuid.UUID,
    action: str,
    headers: dict[str, str] | None = None,
    version: int | None = None,
    **body: Any,  # noqa: ANN401
) -> Any:  # noqa: ANN401
    return client.post(
        f"/api/v1/console/drafts/{draft_id}/{action}",
        json={
            "ticket_version": _version(client, ticket_id) if version is None else version,
            **body,
        },
        headers=key() if headers is None else headers,
    )


def _customer_messages(customer: TestClient, ticket_id: uuid.UUID) -> list[dict[str, Any]]:
    response = customer.get(f"/api/v1/tickets/{ticket_id}/messages")
    assert response.status_code == 200, response.text
    items: list[dict[str, Any]] = response.json()["items"]
    return items


def _one(urls: Urls, query: str, *params: Any) -> tuple[Any, ...]:  # noqa: ANN401
    [row] = run_sql(urls, query, *params)
    return row


def _audit(urls: Urls, action: str) -> list[dict[str, Any]]:
    rows = run_sql(urls, "SELECT details FROM audit_log WHERE action = %s ORDER BY id", action)
    return [details for (details,) in rows]


def _chain(urls: Urls) -> ChainCheck:
    async def check() -> ChainCheck:
        engine = create_async_engine(urls[1])
        try:
            async with AsyncSession(engine) as session:
                return await verify_chain(session)
        finally:
            await engine.dispose()

    return asyncio.run(check())


# --- the done-when line: approve sends the message with the reviewed-by label ------------------


def test_approve_sends_the_draft_with_the_reviewed_by_label(
    staff: dict[str, TestClient],
    customer: TestClient,
    operators: dict[str, uuid.UUID],
    urls: Urls,
) -> None:
    ticket_id, draft_id = _with_draft(urls, customer, actions=ACTIONS)

    response = _review(staff["operator"], ticket_id, draft_id, "approve", approved_actions=["a1"])

    assert response.status_code == 200, response.text
    out = response.json()
    assert out["draft"]["status"] == "approved"
    assert out["draft"]["final_body"] == DRAFT_BODY
    assert out["draft"]["edit_ratio"] == "0.000"
    assert out["draft"]["reviewed_by"] == str(operators["operator"])
    assert out["message"]["role"] == "assistant"
    assert out["message"]["is_ai_generated"] is True
    assert out["message"]["author_label"] == "AI assistant · reviewed by Marta"
    assert out["ticket"]["status"] == "answered"

    [_, sent] = _customer_messages(customer, ticket_id)
    assert sent["author_label"] == "AI assistant · reviewed by Marta"
    assert sent["is_ai_generated"] is True
    assert sent["body"] == DRAFT_BODY
    assert [(c["marker"], c["url"]) for c in sent["citations"]] == [
        (1, "/help/withdrawal-times"),
        (2, "/help/withdrawal-failed-or-returned"),
    ]
    assert "doc_id" not in sent["citations"][0]

    resolution, first_response = _one(
        urls, "SELECT resolution, first_response_at FROM tickets WHERE id = %s", ticket_id
    )
    assert resolution == "ai_approved"
    assert first_response is not None
    [approved] = _audit(urls, "draft.approved")
    assert approved["approved_actions"] == ["a1"]
    assert approved["message_id"] == sent["id"]
    assert _audit(urls, "message.sent") == [{"message_id": sent["id"]}]
    assert _chain(urls).ok

    # The approved message is the AI's answer, so the customer can rate it.
    feedback = customer.post(
        f"/api/v1/tickets/{ticket_id}/feedback", json={"message_id": sent["id"], "rating": "up"}
    )
    assert feedback.status_code == 201, feedback.text


def test_an_unchanged_draft_that_cannot_answer_is_409(
    staff: dict[str, TestClient], customer: TestClient, urls: Urls
) -> None:
    ticket_id, draft_id = _with_draft(urls, customer, can_answer=False)

    response = _review(staff["operator"], ticket_id, draft_id, "approve")

    assert response.status_code == 409
    assert response.json()["code"] == "draft_cannot_answer"
    assert _one(urls, "SELECT status FROM drafts WHERE id = %s", draft_id) == ("pending",)
    assert _one(urls, "SELECT status FROM tickets WHERE id = %s", ticket_id) == (
        "pending_approval",
    )
    assert run_sql(urls, "SELECT 1 FROM messages WHERE role <> 'customer'") == []


# --- edit -----------------------------------------------------------------------------------


def test_edit_sends_the_operators_text_with_the_operator_label(
    staff: dict[str, TestClient], customer: TestClient, urls: Urls
) -> None:
    ticket_id, draft_id = _with_draft(urls, customer)
    edited = (
        f"A SEPA withdrawal usually arrives within 1 business day [1]. Card {TEST_CARD} is fine."
    )

    response = _review(staff["operator"], ticket_id, draft_id, "edit", body=edited)

    assert response.status_code == 200, response.text
    out = response.json()
    masked = (
        "A SEPA withdrawal usually arrives within 1 business day [1]. Card [CARD ****1111] is fine."
    )
    assert out["draft"]["status"] == "edited"
    assert out["draft"]["final_body"] == masked
    assert 0 < float(out["draft"]["edit_ratio"]) < 1
    assert out["draft"]["body"] == DRAFT_BODY  # the AI text is kept for the edit ratio
    assert out["message"]["role"] == "operator"
    assert out["message"]["is_ai_generated"] is False
    assert out["message"]["author_label"] == "Marta · Support team"
    assert out["message"]["masked_entities"] == {"PAN": 1}

    [_, sent] = _customer_messages(customer, ticket_id)
    assert sent["author_label"] == "Marta · Support team"
    assert sent["masked"] is True
    # Marker [2] was deleted, so its citation is not sent.
    assert [c["marker"] for c in sent["citations"]] == [1]
    assert _one(urls, "SELECT resolution FROM tickets WHERE id = %s", ticket_id) == ("ai_edited",)
    assert "guardrail.masked" in [a for (a,) in run_sql(urls, "SELECT action FROM audit_log")]
    for table in ("messages", "drafts", "audit_log"):
        rows = run_sql(urls, f"SELECT count(*) FROM {table} WHERE {table}::text LIKE %s", "%4111%")  # noqa: S608
        assert rows == [(0,)], table

    # An operator's text is not an AI answer: it cannot be rated.
    feedback = customer.post(
        f"/api/v1/tickets/{ticket_id}/feedback", json={"message_id": sent["id"], "rating": "up"}
    )
    assert feedback.status_code == 422
    assert feedback.json()["code"] == "not_ai_message"


def test_an_edit_equal_to_the_draft_is_an_approve(
    staff: dict[str, TestClient], customer: TestClient, urls: Urls
) -> None:
    """The label follows who wrote the text, not the button (owner decision)."""
    ticket_id, draft_id = _with_draft(urls, customer)

    response = _review(staff["operator"], ticket_id, draft_id, "edit", body=DRAFT_BODY)

    assert response.status_code == 200, response.text
    out = response.json()
    assert out["draft"]["status"] == "approved"
    assert out["draft"]["edit_ratio"] == "0.000"
    assert out["message"]["role"] == "assistant"
    assert out["message"]["author_label"] == "AI assistant · reviewed by Marta"
    assert _one(urls, "SELECT resolution FROM tickets WHERE id = %s", ticket_id) == ("ai_approved",)
    assert len(_audit(urls, "draft.approved")) == 1
    assert _audit(urls, "draft.edited") == []


def test_an_edit_with_a_marker_without_a_source_is_422(
    staff: dict[str, TestClient], customer: TestClient, urls: Urls
) -> None:
    ticket_id, draft_id = _with_draft(urls, customer)

    response = _review(
        staff["operator"], ticket_id, draft_id, "edit", body="It arrives in 1 day [1] [3]."
    )

    assert response.status_code == 422
    problem = response.json()
    assert problem["code"] == "validation_error"
    assert problem["errors"] == [
        {
            "loc": ["body", "body"],
            "type": "unknown_citation_marker",
            "msg": "A citation marker has no source in the draft.",
        }
    ]
    assert _one(urls, "SELECT status FROM drafts WHERE id = %s", draft_id) == ("pending",)


def test_a_draft_that_cannot_answer_can_be_edited_and_sent(
    staff: dict[str, TestClient], customer: TestClient, urls: Urls
) -> None:
    ticket_id, draft_id = _with_draft(urls, customer, can_answer=False, citations=[])

    response = _review(
        staff["operator"], ticket_id, draft_id, "edit", body="We do not pay interest on balances."
    )

    assert response.status_code == 200, response.text
    assert response.json()["draft"]["status"] == "edited"
    assert response.json()["draft"]["edit_ratio"] == "1.000"
    assert response.json()["ticket"]["status"] == "answered"


# --- reject ---------------------------------------------------------------------------------


def test_reject_without_a_reply_hands_the_ticket_off(
    staff: dict[str, TestClient], customer: TestClient, urls: Urls
) -> None:
    ticket_id, draft_id = _with_draft(urls, customer)
    headers = key()

    response = _review(
        staff["operator"],
        ticket_id,
        draft_id,
        "reject",
        headers=headers,
        reason="wrong_facts",
        note=f"Customer wrote card {TEST_CARD} somewhere",
    )

    assert response.status_code == 200, response.text
    out = response.json()
    assert out["message"] is None
    assert out["draft"]["status"] == "rejected"
    assert out["draft"]["reject_reason"] == "wrong_facts"
    assert out["draft"]["reject_note"] == "Customer wrote card [CARD ****1111] somewhere"
    assert out["draft"]["final_body"] is None
    assert out["draft"]["edit_ratio"] is None
    assert out["ticket"]["status"] == "handed_off"
    assert len(_customer_messages(customer, ticket_id)) == 1
    [rejected] = _audit(urls, "draft.rejected")
    assert rejected["reject_reason"] == "wrong_facts"
    assert rejected["with_reply"] is False
    assert RAW_DIGITS not in json.dumps(run_sql(urls, "SELECT details FROM audit_log"))

    # A repeat of the same request returns the first result; another reason is a conflict.
    again = _review(
        staff["operator"],
        ticket_id,
        draft_id,
        "reject",
        headers=headers,
        version=1,
        reason="wrong_facts",
    )
    assert again.status_code == 200, again.text
    assert again.json()["draft"] == out["draft"]
    other = _review(staff["operator"], ticket_id, draft_id, "reject", reason="policy")
    assert other.status_code == 409
    assert other.json()["code"] == "conflict"
    assert len(_audit(urls, "draft.rejected")) == 1


def test_reject_with_a_reply_sends_the_operators_answer(
    staff: dict[str, TestClient], customer: TestClient, urls: Urls
) -> None:
    ticket_id, draft_id = _with_draft(urls, customer)

    response = _review(
        staff["operator"],
        ticket_id,
        draft_id,
        "reject",
        reason="wrong_tone",
        reply_body="Your withdrawal is on its way and should arrive tomorrow.",
    )

    assert response.status_code == 200, response.text
    out = response.json()
    assert out["draft"]["status"] == "rejected"
    assert out["draft"]["final_body"] == "Your withdrawal is on its way and should arrive tomorrow."
    assert out["draft"]["edit_ratio"] is None
    assert out["message"]["author_label"] == "Marta · Support team"
    assert out["message"]["citations"] == []
    assert out["ticket"]["status"] == "answered"
    assert _one(urls, "SELECT resolution FROM tickets WHERE id = %s", ticket_id) == ("human",)
    assert _audit(urls, "draft.rejected")[0]["with_reply"] is True


def test_reject_needs_a_known_reason(
    staff: dict[str, TestClient], customer: TestClient, urls: Urls
) -> None:
    ticket_id, draft_id = _with_draft(urls, customer)
    bodies: list[dict[str, Any]] = [{}, {"reason": "bad"}]
    for body in bodies:
        response = _review(staff["operator"], ticket_id, draft_id, "reject", **body)
        assert response.status_code == 422
        assert response.json()["code"] == "validation_error"


# --- conflicts, actions, idempotency ----------------------------------------------------------


def test_a_stale_version_or_a_reviewed_draft_is_409(
    staff: dict[str, TestClient], customer: TestClient, urls: Urls
) -> None:
    ticket_id, draft_id = _with_draft(urls, customer)
    stale = _version(staff["operator"], ticket_id) - 1

    response = _review(staff["operator"], ticket_id, draft_id, "approve", version=stale)
    assert response.status_code == 409
    assert response.json()["code"] == "stale_draft"

    assert _review(staff["operator"], ticket_id, draft_id, "approve").status_code == 200
    attempts: list[tuple[str, dict[str, Any]]] = [("approve", {}), ("edit", {"body": "Other."})]
    for action, body in attempts:
        again = _review(staff["operator"], ticket_id, draft_id, action, **body)
        assert again.status_code == 409, action
        assert again.json()["code"] == "conflict"
    assert len(_customer_messages(customer, ticket_id)) == 2


def test_another_operators_claim_blocks_the_review_but_not_a_lead(
    staff: dict[str, TestClient], customer: TestClient, urls: Urls
) -> None:
    ticket_id, draft_id = _with_draft(urls, customer)
    claim = staff["operator2"].post(
        f"/api/v1/console/tickets/{ticket_id}/claim",
        json={"ticket_version": _version(staff["operator2"], ticket_id)},
    )
    assert claim.status_code == 200, claim.text

    blocked = _review(staff["operator"], ticket_id, draft_id, "approve")
    assert blocked.status_code == 409
    assert blocked.json()["code"] == "conflict"

    by_lead = _review(staff["lead"], ticket_id, draft_id, "approve")
    assert by_lead.status_code == 200, by_lead.text
    assert by_lead.json()["message"]["author_label"] == "AI assistant · reviewed by Lena"


def test_approved_actions_must_be_proposed_in_the_draft(
    staff: dict[str, TestClient], customer: TestClient, urls: Urls
) -> None:
    ticket_id, draft_id = _with_draft(urls, customer, actions=ACTIONS)
    for actions in (["a2"], ["not-an-id"]):
        response = _review(
            staff["operator"], ticket_id, draft_id, "approve", approved_actions=actions
        )
        assert response.status_code == 422, actions
        assert response.json()["code"] == "validation_error"
    assert _one(urls, "SELECT status FROM drafts WHERE id = %s", draft_id) == ("pending",)


def test_the_same_key_returns_the_first_result_and_another_request_is_409(
    staff: dict[str, TestClient], customer: TestClient, urls: Urls
) -> None:
    ticket_id, draft_id = _with_draft(urls, customer)
    headers = key()
    version = _version(staff["operator"], ticket_id)

    first = _review(
        staff["operator"], ticket_id, draft_id, "edit", headers, version, body="Edited [1]."
    )
    second = _review(
        staff["operator"], ticket_id, draft_id, "edit", headers, version, body="Edited [1]."
    )
    assert first.status_code == second.status_code == 200
    assert second.json()["message"]["id"] == first.json()["message"]["id"]
    assert len(_customer_messages(customer, ticket_id)) == 2

    reused = _review(
        staff["operator"], ticket_id, draft_id, "edit", headers, version, body="Other [1]."
    )
    assert reused.status_code == 409
    assert reused.json()["code"] == "idempotency_key_reused"
    # Another operator's key space: the same key is a new request (and the draft is done).
    other = _review(
        staff["operator2"], ticket_id, draft_id, "edit", headers, version, body="Edited [1]."
    )
    assert other.status_code == 409
    assert other.json()["code"] == "stale_draft"

    missing = staff["operator"].post(
        f"/api/v1/console/drafts/{draft_id}/approve", json={"ticket_version": version}
    )
    assert missing.status_code == 422


# --- roles, CSRF, unknown draft, ticket detail ------------------------------------------------


def test_compliance_cannot_review_and_csrf_is_needed(
    app: FastAPI, staff: dict[str, TestClient], customer: TestClient, urls: Urls
) -> None:
    ticket_id, draft_id = _with_draft(urls, customer)

    assert _review(staff["compliance"], ticket_id, draft_id, "approve").status_code == 403
    without_token = _api.operator_client(app, EMAILS["operator"])
    del without_token.headers["X-CSRF-Token"]
    response = without_token.post(
        f"/api/v1/console/drafts/{draft_id}/approve",
        json={"ticket_version": 2},
        headers=key(),
    )
    assert response.status_code == 403
    assert response.json()["code"] == "csrf_failed"
    unknown = _review(staff["operator"], ticket_id, uuid.uuid4(), "approve")
    assert unknown.status_code == 404
    assert _one(urls, "SELECT status FROM drafts WHERE id = %s", draft_id) == ("pending",)


def test_ticket_detail_shows_the_latest_run_and_its_draft(
    staff: dict[str, TestClient], customer: TestClient, urls: Urls
) -> None:
    ticket_id, draft_id = _with_draft(urls, customer, actions=ACTIONS)

    detail = staff["compliance"].get(f"/api/v1/console/tickets/{ticket_id}").json()

    run = detail["latest_run"]
    assert run["status"] == "completed"
    assert run["route"] == "approval"
    assert run["route_reasons"] == ["auto_reply_off"]
    assert run["prompt_versions"] == {"triage": "v1", "write": "v1"}
    assert run["models"] == {"write": "anthropic/claude-haiku-5-5"}
    assert run["total_cost_usd"] == "0.003947"
    assert (run["input_tokens"], run["output_tokens"]) == (30417, 430)
    draft = detail["draft"]
    assert draft["id"] == str(draft_id)
    assert draft["status"] == "pending"
    assert draft["can_answer"] is True
    assert draft["body"] == DRAFT_BODY
    assert draft["citations"] == CITATIONS
    assert draft["proposed_actions"] == ACTIONS
    assert draft["evidence"]["variant"] == "b0_full_kb"
