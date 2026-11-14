"""Console API of phase 2 against Postgres as the app role (P2-07): queue, ticket, claim,
release, reply, close; roles, CSRF, conflicts and audit records."""

import asyncio
import base64
import json
import time
import uuid
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from typing import Any

import psycopg
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from hub.audit import ChainCheck, verify_chain
from hub.db.models import Message, Ticket
from hub.db.repositories import MessageRepository, TicketRepository
from hub.main import create_app
from hub.observability.readiness import libpq_url

from . import _api
from ._api import Urls, add_customer, add_operator, customer_client, key, new_ticket, run_sql

DATABASE = "hub_console_api_test"
# Public test card number (Visa test range); never a real card.
TEST_CARD = "4111 1111 1111 1111"
RAW_DIGITS = "4111111111111111"
EMAILS = {
    "operator": "marta@tallybridge.test",
    "operator2": "jan@tallybridge.test",
    "lead": "lena@tallybridge.test",
    "compliance": "olga@tallybridge.test",
    "admin": "adam@tallybridge.test",
}


@pytest.fixture(scope="module")
def urls(postgres_url: str) -> Iterator[Urls]:
    with _api.own_database(postgres_url, DATABASE) as found:
        yield found


@pytest.fixture
def operators(urls: Urls) -> dict[str, uuid.UUID]:
    _api.empty_tables(urls)
    add_customer(urls, "CUS-0001", "Ana Novak")
    add_customer(urls, "CUS-0002", "Peter Horvat")
    names = {"operator2": "Jan Novák", "lead": "Lena Lead"}
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


def _ticket(client: TestClient, ticket_id: uuid.UUID) -> dict[str, Any]:
    response = client.get(f"/api/v1/console/tickets/{ticket_id}")
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


def _post(
    client: TestClient,
    ticket_id: uuid.UUID,
    action: str,
    **extra: Any,  # noqa: ANN401
) -> Any:  # noqa: ANN401
    version = _ticket(client, ticket_id)["version"]
    return client.post(
        f"/api/v1/console/tickets/{ticket_id}/{action}",
        json={"ticket_version": version, **extra.pop("json", {})},
        **extra,
    )


def _actions(urls: Urls) -> list[str]:
    return [row[0] for row in run_sql(urls, "SELECT action FROM audit_log ORDER BY id")]


def _chain(urls: Urls) -> ChainCheck:
    async def check() -> ChainCheck:
        engine = create_async_engine(urls[1])
        try:
            async with AsyncSession(engine) as session:
                return await verify_chain(session)
        finally:
            await engine.dispose()

    return asyncio.run(check())


def _set(urls: Urls, ticket_id: uuid.UUID, assignments: str, *params: Any) -> None:  # noqa: ANN401
    # Test setup outside the app (as the migrator): stands in for triage and routing (phase 6).
    query = f"UPDATE tickets SET {assignments} WHERE id = %s"  # noqa: S608 - fixed test text
    run_sql(urls, query, *params, ticket_id)


# --- the done-when line: an operator reply reaches the customer -------------------------------


def test_operator_reply_reaches_the_customer_through_get_messages(
    app: FastAPI, staff: dict[str, TestClient], urls: Urls
) -> None:
    customer = customer_client(app)
    ticket_id = new_ticket(customer)
    first = customer.get(f"/api/v1/tickets/{ticket_id}/messages").json()["items"]
    assert [m["role"] for m in first] == ["customer"]

    # A new ticket has no run in phase 2: it is in the queue, and a claim hands it off.
    marta = staff["operator"]
    queue = marta.get("/api/v1/console/tickets").json()["items"]
    assert [(row["id"], row["status"]) for row in queue] == [(str(ticket_id), "processing")]
    claimed = _post(marta, ticket_id, "claim")
    assert claimed.status_code == 200, claimed.text
    assert claimed.json()["status"] == "handed_off"
    assert claimed.json()["route_reasons"] == ["operator_takeover"]
    assert claimed.json()["assigned_operator"]["name"] == "Marta Kováčová"

    reply = _post(
        marta, ticket_id, "reply", json={"body": "Hello Ana, it arrives today."}, headers=key()
    )
    assert reply.status_code == 200, reply.text
    assert reply.json()["author_label"] == "Marta · Support team"

    after = customer.get(f"/api/v1/tickets/{ticket_id}/messages", params={"after": first[0]["id"]})
    [message] = after.json()["items"]
    assert message["role"] == "operator"
    assert message["body"] == "Hello Ana, it arrives today."
    assert message["author_label"] == "Marta · Support team"
    assert message["is_ai_generated"] is False
    assert customer.get(f"/api/v1/tickets/{ticket_id}").json()["status"] == "answered"

    detail = _ticket(marta, ticket_id)
    assert detail["status"] == "answered"
    assert detail["resolution"] == "human"
    assert detail["first_response_at"] is not None
    assert [m["role"] for m in detail["messages"]] == ["customer", "operator"]
    # The staff fixture logged everyone in first (auth.login rows).
    assert [action for action in _actions(urls) if not action.startswith("auth.")] == [
        "ticket.created",
        "route.decided",
        "ticket.claimed",
        "message.sent",
    ]
    assert _chain(urls).ok


def test_operator_reply_with_a_card_number_is_stored_masked(
    app: FastAPI, staff: dict[str, TestClient], urls: Urls
) -> None:
    customer = customer_client(app)
    ticket_id = new_ticket(customer)
    marta = staff["operator"]
    _post(marta, ticket_id, "claim")
    reply = _post(marta, ticket_id, "reply", json={"body": f"Is it {TEST_CARD}?"}, headers=key())
    assert reply.status_code == 200
    assert reply.json()["body"] == "Is it [CARD ****1111]?"
    assert reply.json()["masked_entities"] == {"PAN": 1}
    dump = str(run_sql(urls, "SELECT * FROM messages")) + str(
        run_sql(urls, "SELECT * FROM audit_log")
    )
    assert RAW_DIGITS not in dump
    assert "guardrail.masked" in _actions(urls)


def test_reply_is_idempotent_per_key(
    app: FastAPI, staff: dict[str, TestClient], urls: Urls
) -> None:
    ticket_id = new_ticket(customer_client(app))
    marta = staff["operator"]
    _post(marta, ticket_id, "claim")
    version = _ticket(marta, ticket_id)["version"]
    headers = key()
    body = {"ticket_version": version, "body": "On its way."}
    first = marta.post(f"/api/v1/console/tickets/{ticket_id}/reply", json=body, headers=headers)
    # The repeat carries the version the console had then; it gets the first answer back.
    again = marta.post(f"/api/v1/console/tickets/{ticket_id}/reply", json=body, headers=headers)
    assert first.status_code == again.status_code == 200
    assert first.json()["id"] == again.json()["id"]
    assert run_sql(urls, "SELECT count(*) FROM messages WHERE role = 'operator'") == [(1,)]
    other = marta.post(
        f"/api/v1/console/tickets/{ticket_id}/reply",
        json={**body, "body": "Something else."},
        headers=headers,
    )
    assert other.status_code == 409
    assert other.json()["code"] == "idempotency_key_reused"
    missing = marta.post(f"/api/v1/console/tickets/{ticket_id}/reply", json=body)
    assert missing.status_code == 422


# --- queue ----------------------------------------------------------------------------------


def test_queue_filters_and_order(app: FastAPI, staff: dict[str, TestClient], urls: Urls) -> None:
    ana, peter = customer_client(app, "CUS-0001"), customer_client(app, "CUS-0002")
    oldest = new_ticket(ana)
    high = new_ticket(peter)
    newest = new_ticket(ana)
    _set(
        urls,
        high,
        "urgency = 'high', intent = 'card_lost_stolen', route_reasons = %s::jsonb,"
        " summary = 'Card stolen in Vienna'",
        '["policy_intent"]',
    )
    marta = staff["operator"]
    _post(marta, newest, "claim")

    def ids(**params: Any) -> list[str]:  # noqa: ANN401
        response = marta.get("/api/v1/console/tickets", params=params)
        assert response.status_code == 200, response.text
        return [row["id"] for row in response.json()["items"]]

    # Urgency first, then the longest waiting.
    assert ids() == [str(high), str(oldest), str(newest)]
    assert ids(status="handed_off") == [str(newest)]
    assert ids(status=["processing", "handed_off"]) == [str(high), str(oldest), str(newest)]
    assert ids(intent="card_lost_stolen") == [str(high)]
    assert ids(urgency="high") == [str(high)]
    assert ids(assigned="me") == [str(newest)]
    assert ids(assigned="none") == [str(high), str(oldest)]
    assert (
        staff["operator2"].get("/api/v1/console/tickets", params={"assigned": "me"}).json()["items"]
        == []
    )
    assert ids(route_reason="policy_intent") == [str(high)]
    assert ids(route_reason="operator_takeover") == [str(newest)]
    assert ids(q="vienna") == [str(high)]
    assert ids(q="peter horvat") == [str(high)]
    assert ids(q="CUS-0001") == [str(oldest), str(newest)]
    # LIKE wildcards in the search are taken literally.
    assert ids(q="%") == []
    assert ids(q="_") == []

    row = marta.get("/api/v1/console/tickets", params={"urgency": "high"}).json()["items"][0]
    assert row["customer"]["customer_ref"] == "CUS-0002"
    assert row["customer"]["display_name"] == "Peter Horvat"
    assert row["summary"] == "Card stolen in Vienna"
    assert row["assigned_operator"] is None


def test_waiting_time_is_the_last_customer_message(
    app: FastAPI, staff: dict[str, TestClient]
) -> None:
    ana = customer_client(app)
    first, second = new_ticket(ana), new_ticket(ana)
    # A new message on the older ticket makes it wait less than the second one.
    response = ana.post(
        f"/api/v1/tickets/{first}/messages", json={"message": "Any news?"}, headers=key()
    )
    assert response.status_code == 200
    items = staff["operator"].get("/api/v1/console/tickets").json()["items"]
    assert [row["id"] for row in items] == [str(second), str(first)]


def test_queue_pages_with_a_cursor(app: FastAPI, staff: dict[str, TestClient], urls: Urls) -> None:
    ana = customer_client(app)
    created = [new_ticket(ana) for _ in range(5)]
    _set(urls, created[3], "urgency = 'normal'")
    marta = staff["operator"]
    seen: list[str] = []
    cursor = None
    while True:
        params: dict[str, Any] = {"limit": 2}
        if cursor:
            params["cursor"] = cursor
        page = marta.get("/api/v1/console/tickets", params=params).json()
        seen += [row["id"] for row in page["items"]]
        cursor = page["next_cursor"]
        if cursor is None:
            break
    expected = [created[3], created[0], created[1], created[2], created[4]]
    assert seen == [str(ticket_id) for ticket_id in expected]
    bad = marta.get("/api/v1/console/tickets", params={"cursor": "not-a-cursor"})
    assert bad.status_code == 422
    assert bad.json()["code"] == "validation_error"


def test_ticket_detail(app: FastAPI, staff: dict[str, TestClient]) -> None:
    ticket_id = new_ticket(customer_client(app), f"My card {TEST_CARD} was declined")
    detail = _ticket(staff["operator"], ticket_id)
    assert detail["id"] == str(ticket_id)
    assert detail["version"] == 1
    assert detail["customer"]["customer_ref"] == "CUS-0001"
    [message] = detail["messages"]
    assert message["body"] == "My card [CARD ****1111] was declined"
    assert message["masked_entities"] == {"PAN": 1}
    assert message["author_label"] == "You"
    # Later phases fill these.
    assert detail["latest_run"] is None
    assert detail["draft"] is None
    assert detail["evidence"] == detail["memory"] == detail["tool_calls"] == []
    assert detail["trace_url"] is None
    assert staff["operator"].get(f"/api/v1/console/tickets/{uuid.uuid4()}").status_code == 404


# --- claim, release, close ------------------------------------------------------------------


def test_claim_conflicts_and_lead_override(
    app: FastAPI, staff: dict[str, TestClient], operators: dict[str, uuid.UUID], urls: Urls
) -> None:
    ticket_id = new_ticket(customer_client(app))
    marta, jan, lena = staff["operator"], staff["operator2"], staff["lead"]
    assert _post(marta, ticket_id, "claim").status_code == 200
    # Claiming again is a no-op.
    assert _post(marta, ticket_id, "claim").json()["assigned_operator"]["id"] == str(
        operators["operator"]
    )

    taken = _post(jan, ticket_id, "claim")
    assert taken.status_code == 409
    assert taken.json()["code"] == "conflict"
    forced = _post(jan, ticket_id, "claim", params={"force": "true"})
    assert forced.status_code == 403
    assert _post(jan, ticket_id, "release").status_code == 409

    overridden = _post(lena, ticket_id, "claim", params={"force": "true"})
    assert overridden.status_code == 200
    assert overridden.json()["assigned_operator"]["id"] == str(operators["lead"])
    [details] = [
        row[0]
        for row in run_sql(
            urls,
            "SELECT details FROM audit_log WHERE action = 'ticket.claimed'"
            " ORDER BY id DESC LIMIT 1",
        )
    ]
    assert details == {"previous_operator_id": str(operators["operator"]), "forced": True}

    released = _post(lena, ticket_id, "release")
    assert released.status_code == 200
    assert released.json()["assigned_operator"] is None
    assert "ticket.released" in _actions(urls)
    # Releasing an unassigned ticket changes nothing.
    assert _post(marta, ticket_id, "release").status_code == 200


def test_a_stale_version_is_409(app: FastAPI, staff: dict[str, TestClient]) -> None:
    ticket_id = new_ticket(customer_client(app))
    marta = staff["operator"]
    for action in ("claim", "release", "close", "reply"):
        extra: dict[str, Any] = {"body": "hi"} if action == "reply" else {}
        response = marta.post(
            f"/api/v1/console/tickets/{ticket_id}/{action}",
            json={"ticket_version": 99, **extra},
            headers=key(),
        )
        assert response.status_code == 409, action
        assert response.json()["code"] == "stale_draft", action


def test_reply_needs_a_claim_on_processing_and_refuses_a_closed_ticket(
    app: FastAPI, staff: dict[str, TestClient], urls: Urls
) -> None:
    customer = customer_client(app)
    ticket_id = new_ticket(customer)
    marta = staff["operator"]
    response = _post(marta, ticket_id, "reply", json={"body": "hi"}, headers=key())
    assert response.status_code == 409
    assert response.json()["detail"] == "Claim the ticket first."
    _set(urls, ticket_id, "status = 'pending_approval'")
    response = _post(marta, ticket_id, "reply", json={"body": "hi"}, headers=key())
    assert response.status_code == 409
    _set(urls, ticket_id, "status = 'closed'")
    response = _post(marta, ticket_id, "reply", json={"body": "hi"}, headers=key())
    assert response.json()["code"] == "ticket_closed"
    assert _post(marta, ticket_id, "claim").json()["code"] == "ticket_closed"
    assert run_sql(urls, "SELECT count(*) FROM messages WHERE role = 'operator'") == [(0,)]


def test_reply_on_an_answered_ticket_keeps_it_answered(
    app: FastAPI, staff: dict[str, TestClient]
) -> None:
    ticket_id = new_ticket(customer_client(app))
    marta = staff["operator"]
    _post(marta, ticket_id, "claim")
    _post(marta, ticket_id, "reply", json={"body": "First."}, headers=key())
    second = _post(marta, ticket_id, "reply", json={"body": "Second."}, headers=key())
    assert second.status_code == 200
    detail = _ticket(marta, ticket_id)
    assert detail["status"] == "answered"
    assert [m["body"] for m in detail["messages"]][1:] == ["First.", "Second."]
    # Another operator cannot write into Marta's ticket.
    assert (
        _post(staff["operator2"], ticket_id, "reply", json={"body": "x"}, headers=key()).status_code
        == 409
    )


def test_close(app: FastAPI, staff: dict[str, TestClient], urls: Urls) -> None:
    customer = customer_client(app)
    ticket_id = new_ticket(customer)
    marta = staff["operator"]
    # processing cannot be closed (state diagram); hand it off first with a claim.
    assert _post(marta, ticket_id, "close").status_code == 409
    _post(marta, ticket_id, "claim")
    closed = _post(marta, ticket_id, "close")
    assert closed.status_code == 200
    assert closed.json()["status"] == "closed"
    assert _ticket(marta, ticket_id)["resolved_at"] is not None
    assert _post(marta, ticket_id, "close").json()["code"] == "ticket_closed"
    assert _actions(urls)[-1] == "ticket.closed"
    # The customer's chat sees it closed and must open a new ticket.
    response = customer.post(
        f"/api/v1/tickets/{ticket_id}/messages", json={"message": "hi"}, headers=key()
    )
    assert response.json()["code"] == "ticket_closed"
    assert _chain(urls).ok


# --- roles and CSRF -------------------------------------------------------------------------


def test_compliance_reads_but_cannot_change(app: FastAPI, staff: dict[str, TestClient]) -> None:
    ticket_id = new_ticket(customer_client(app))
    olga = staff["compliance"]
    assert olga.get("/api/v1/console/tickets").status_code == 200
    assert _ticket(olga, ticket_id)["id"] == str(ticket_id)
    for action in ("claim", "release", "close"):
        response = _post(olga, ticket_id, action)
        assert response.status_code == 403, action
        assert response.json()["code"] == "forbidden"
    response = _post(olga, ticket_id, "reply", json={"body": "hi"}, headers=key())
    assert response.status_code == 403


def test_admin_can_do_what_an_operator_can(app: FastAPI, staff: dict[str, TestClient]) -> None:
    ticket_id = new_ticket(customer_client(app))
    assert _post(staff["admin"], ticket_id, "claim").status_code == 200


@pytest.mark.parametrize("token", [None, "wrong"])
def test_state_changes_need_the_csrf_token_of_the_session(
    app: FastAPI, staff: dict[str, TestClient], urls: Urls, token: str | None
) -> None:
    ticket_id = new_ticket(customer_client(app))
    marta = staff["operator"]
    version = _ticket(marta, ticket_id)["version"]
    headers = {"X-CSRF-Token": token} if token else {}
    if token is None:
        del marta.headers["X-CSRF-Token"]
    response = marta.post(
        f"/api/v1/console/tickets/{ticket_id}/claim",
        json={"ticket_version": version},
        headers=headers,
    )
    assert response.status_code == 403
    assert response.json()["code"] == "csrf_failed"
    # Another operator's token does not work either.
    marta.headers["X-CSRF-Token"] = staff["operator2"].headers["X-CSRF-Token"]
    response = marta.post(
        f"/api/v1/console/tickets/{ticket_id}/claim", json={"ticket_version": version}
    )
    assert response.status_code == 403
    assert run_sql(urls, "SELECT assigned_operator_id FROM tickets") == [(None,)]
    # Reading needs no token.
    assert marta.get("/api/v1/console/tickets").status_code == 200


# --- races and edge cases -------------------------------------------------------------------


def _another_writer_moves_the_version(monkeypatch: pytest.MonkeyPatch, urls: Urls) -> None:
    """After the handler has read the ticket, another transaction changes it (committed)."""
    real_get = TicketRepository.get

    async def get_then_race(self: TicketRepository, ticket_id: uuid.UUID) -> Ticket | None:
        ticket = await real_get(self, ticket_id)
        run_sql(urls, "UPDATE tickets SET version = version + 1 WHERE id = %s", ticket_id)
        return ticket

    monkeypatch.setattr(TicketRepository, "get", get_then_race)


@pytest.mark.parametrize("action", ["claim", "close"])
def test_a_concurrent_change_at_flush_time_is_409(
    app: FastAPI,
    staff: dict[str, TestClient],
    urls: Urls,
    monkeypatch: pytest.MonkeyPatch,
    action: str,
) -> None:
    ticket_id = new_ticket(customer_client(app))
    marta = staff["operator"]
    if action == "close":
        _post(marta, ticket_id, "claim")
    version = _ticket(marta, ticket_id)["version"]
    _another_writer_moves_the_version(monkeypatch, urls)
    response = marta.post(
        f"/api/v1/console/tickets/{ticket_id}/{action}", json={"ticket_version": version}
    )
    assert response.status_code == 409
    assert response.json()["code"] == "stale_draft"
    assert "ticket.closed" not in _actions(urls)


def test_concurrent_duplicate_reply_answers_with_the_stored_message(
    app: FastAPI, staff: dict[str, TestClient], urls: Urls, monkeypatch: pytest.MonkeyPatch
) -> None:
    ticket_id = new_ticket(customer_client(app))
    marta = staff["operator"]
    _post(marta, ticket_id, "claim")
    version = _ticket(marta, ticket_id)["version"]
    headers, body = key(), {"ticket_version": version, "body": "Once."}
    first = marta.post(f"/api/v1/console/tickets/{ticket_id}/reply", json=body, headers=headers)

    # The second request looks up the key before the first one committed: it sees nothing.
    real_lookup = MessageRepository.get_by_idempotency_key
    calls = 0

    async def late_lookup(self: MessageRepository, scoped: str) -> Message | None:
        nonlocal calls
        calls += 1
        return None if calls == 1 else await real_lookup(self, scoped)

    monkeypatch.setattr(MessageRepository, "get_by_idempotency_key", late_lookup)
    # The console still had the old version: the replay must not be refused as stale.
    run_sql(urls, "UPDATE tickets SET version = %s WHERE id = %s", version, ticket_id)
    second = marta.post(f"/api/v1/console/tickets/{ticket_id}/reply", json=body, headers=headers)
    assert calls == 2
    assert second.status_code == 200, second.text
    assert second.json()["id"] == first.json()["id"]
    assert run_sql(urls, "SELECT count(*) FROM messages WHERE role = 'operator'") == [(1,)]
    assert _actions(urls).count("message.sent") == 1


def test_actions_on_an_unknown_ticket_are_404(staff: dict[str, TestClient]) -> None:
    for action in ("claim", "release", "close"):
        response = staff["operator"].post(
            f"/api/v1/console/tickets/{uuid.uuid4()}/{action}", json={"ticket_version": 1}
        )
        assert response.status_code == 404, action


def test_a_cursor_without_a_time_zone_is_refused(staff: dict[str, TestClient]) -> None:
    raw = json.dumps([0, "2026-11-14T10:00:00", str(uuid.uuid4())]).encode()
    cursor = base64.urlsafe_b64encode(raw).decode()
    response = staff["operator"].get("/api/v1/console/tickets", params={"cursor": cursor})
    assert response.status_code == 422


# --- a message must not land in a ticket closed by an overlapping transaction ---------------


@contextmanager
def _close_in_flight(urls: Urls, ticket_id: uuid.UUID) -> Iterator[psycopg.Connection]:
    """An operator's close that has updated the ticket row but not committed yet: it holds the
    row lock, and other transactions still read the old status. The caller commits."""
    with psycopg.connect(libpq_url(urls[0])) as connection:
        connection.execute(
            b"UPDATE tickets SET status = 'closed', version = version + 1 WHERE id = %s",
            (ticket_id,),
        )
        yield connection


def _blocked_until_commit(request: Callable[[], Any], connection: psycopg.Connection) -> Any:  # noqa: ANN401
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(request)
        time.sleep(0.5)
        # The request read the old status and now waits for the row lock of the close.
        assert not future.done()
        connection.commit()
        return future.result(timeout=10)


def test_customer_message_overlapping_a_close_is_409_and_not_stored(
    app: FastAPI, staff: dict[str, TestClient], urls: Urls
) -> None:
    customer = customer_client(app)
    ticket_id = new_ticket(customer)
    _post(staff["operator"], ticket_id, "claim")
    before = run_sql(urls, "SELECT count(*) FROM messages")

    with _close_in_flight(urls, ticket_id) as close:
        response = _blocked_until_commit(
            lambda: customer.post(
                f"/api/v1/tickets/{ticket_id}/messages",
                json={"message": "One more thing"},
                headers=key(),
            ),
            close,
        )

    assert response.status_code == 409
    assert response.json()["code"] == "ticket_closed"
    assert run_sql(urls, "SELECT count(*) FROM messages") == before
    assert "message.received" not in _actions(urls)


def test_operator_reply_overlapping_a_close_is_409_and_not_stored(
    app: FastAPI, staff: dict[str, TestClient], urls: Urls
) -> None:
    ticket_id = new_ticket(customer_client(app))
    marta = staff["operator"]
    _post(marta, ticket_id, "claim")
    _post(marta, ticket_id, "reply", json={"body": "First."}, headers=key())
    # An answered ticket: a second reply would not change the ticket row, so only the check
    # under the lock can see the close.
    version = _ticket(marta, ticket_id)["version"]

    with _close_in_flight(urls, ticket_id) as close:
        response = _blocked_until_commit(
            lambda: marta.post(
                f"/api/v1/console/tickets/{ticket_id}/reply",
                json={"ticket_version": version, "body": "Second."},
                headers=key(),
            ),
            close,
        )

    assert response.status_code == 409
    assert response.json()["code"] == "ticket_closed"
    assert run_sql(urls, "SELECT body FROM messages WHERE role = 'operator'") == [("First.",)]
