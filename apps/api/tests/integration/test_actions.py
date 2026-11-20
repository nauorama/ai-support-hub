"""Actions (P7-05): approved Jira issues in `act`, the urgent Slack alert in `handoff_prep`, and
the operator's own action, end to end through the API against Postgres and the real
`support-tools` server (subprocess over streamable HTTP, mock data only).

The model is scripted (no recording, no key): each test lists the answers of its run in order
(triage, research turns, write, verify; or triage and the handoff summary), so a run that makes
another model call fails loudly. The search returns nothing; the writer cites an account fact.
"""

import dataclasses
import json
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hub.config import Settings
from hub.db.session import connect
from hub.deps import Container, build_pipeline
from hub.domain.enums import LlmCallMode
from hub.llm.gateway import Gateway
from hub.llm.recorder import DbCallRecorder
from hub.llm.schemas import ProviderResponse, Usage
from hub.llm.schemas import ToolCall as ModelToolCall
from hub.main import create_app

from ._api import (
    ACCESS_CODE,
    OPERATOR_PASSWORD,
    SESSION_SECRET,
    Urls,
    add_customer,
    add_operator,
    key,
    own_database,
    run_sql,
)
from .test_tool_calls import NoSearch, Scripted, mcp_server  # noqa: F401 - fixture

ROOT = Path(__file__).resolve().parents[4]
DATABASE = "hub_actions_test"
OPERATOR = "marta@tallybridge.test"
OPERATOR2 = "jan@tallybridge.test"
DISPUTE = "I want to dispute card payment TX-000001, the merchant never delivered the order."
ACTION = {
    "type": "jira_create_issue",
    "issue_type": "dispute",
    "summary": "Card payment dispute for TX-000001",
    "description": "Customer says the merchant did not deliver. Check TX-000001.",
}


def _triage(**changes: Any) -> dict[str, Any]:  # noqa: ANN401 - any field value
    values: dict[str, Any] = {
        "intent": "dispute_chargeback",
        "secondary_intent": None,
        "urgency": "normal",
        "wants_human": False,
        "reports_unauthorised_activity": False,
        "is_complaint_or_legal": False,
        "asks_for_action": "dispute",
        "injection_suspected": False,
        "summary": "Disputes a card payment.",
    }
    return values | changes


def _json(value: dict[str, Any]) -> ProviderResponse:
    return ProviderResponse(
        content=json.dumps(value), usage=Usage(input_tokens=500, output_tokens=50)
    )


def _tool_turn(name: str, arguments: dict[str, Any]) -> ProviderResponse:
    return ProviderResponse(
        tool_calls=[ModelToolCall(id="c1", name=name, arguments=json.dumps(arguments))],
        usage=Usage(input_tokens=500, output_tokens=20),
    )


def _dispute_run() -> list[ProviderResponse]:
    """Triage, research (one core call, then done), a draft with a prepared Jira issue that
    cites the account fact, a verification that passes."""
    answer = "We found payment TX-000001 on your account [1]. Our team will review your dispute."
    return [
        _json(_triage()),
        _tool_turn("get_transaction", {"reference": "TX-000001"}),
        ProviderResponse(content="done", usage=Usage(input_tokens=600, output_tokens=2)),
        _json(
            {
                "can_answer": True,
                "missing_information": None,
                "answer": answer,
                "citations": [{"marker": 1, "source_id": "A1"}],
                "proposed_actions": [ACTION],
            }
        ),
        _json(
            {
                "claims": [
                    {
                        "claim": "Payment TX-000001 is on the account.",
                        "supported": True,
                        "source_id": "A1",
                    }
                ]
            }
        ),
    ]


def _handoff_run(**triage: Any) -> list[ProviderResponse]:  # noqa: ANN401 - any field value
    return [
        _json(_triage(**triage)),
        _json({"problem": "Card problem.", "found": "Nothing yet.", "missing": "Everything."}),
    ]


@pytest.fixture(scope="module")
def actions_db(postgres_url: str) -> Iterator[Urls]:
    with own_database(postgres_url, DATABASE) as found:
        yield found


@dataclasses.dataclass
class Stack:
    app: FastAPI
    # One client for the customer and the operators (their cookies differ), entered once: every
    # request runs on the same event loop, as in production. The Postgres checkpointer's lock is
    # bound to the loop it is first contended on, so clients with a loop each fail a resume.
    web: TestClient
    urls: Urls
    model: Scripted
    operator_id: uuid.UUID


@pytest.fixture
def stack(actions_db: Urls, mcp_server: tuple[str, str]) -> Iterator[Stack]:  # noqa: F811
    run_sql(
        actions_db,
        "TRUNCATE audit_log, llm_calls, tool_calls, drafts, runs, feedback, messages, tickets,"
        " customers, operators RESTART IDENTITY CASCADE",
    )
    add_customer(actions_db)
    operator_id = add_operator(actions_db, OPERATOR, "operator", name="Marta Kováčová")
    add_operator(actions_db, OPERATOR2, "operator", name="Jan Novák")
    url, token = mcp_server
    settings = Settings(
        _env_file=None,
        app_env="test",
        database_url=actions_db[1],
        session_secret=SESSION_SECRET,
        demo_access_code=ACCESS_CODE,
        llm_mode="fake",
        models_config=ROOT / "config" / "models.yaml",
        prompts_dir=ROOT / "prompts",
        mcp_server_url=url,
        mcp_server_token=token,
    )
    database = connect(actions_db[1])
    pipeline = build_pipeline(settings, database, NoSearch())
    model = Scripted([])
    gateway = Gateway(
        models=pipeline.gateway.models,
        pricing=pipeline.gateway.pricing,
        provider=model,
        recorder=DbCallRecorder(database),
        mode=LlmCallMode.FAKE,
        budget=None,
    )
    pipeline = dataclasses.replace(pipeline, gateway=gateway)
    app = create_app(Container(settings=settings, database=database, pipeline=pipeline))
    with TestClient(app, base_url="https://testserver", raise_server_exceptions=False) as web:
        yield Stack(app=app, web=web, urls=actions_db, model=model, operator_id=operator_id)


def _operator(stack: Stack, email: str = OPERATOR) -> TestClient:
    response = stack.web.post(
        "/api/v1/auth/login", json={"email": email, "password": OPERATOR_PASSWORD}
    )
    assert response.status_code == 200, response.text
    stack.web.headers["X-CSRF-Token"] = response.json()["csrf_token"]
    return stack.web


def _post(stack: Stack, text: str, answers: list[ProviderResponse]) -> uuid.UUID:
    stack.model.responses.extend(answers)
    session = stack.web.post(
        "/api/v1/demo/session", json={"customer_ref": "CUS-0001", "access_code": ACCESS_CODE}
    )
    assert session.status_code == 200, session.text
    response = stack.web.post("/api/v1/tickets", json={"message": text}, headers=key())
    assert response.status_code == 200, response.text
    assert stack.model.responses == [], "the run did not make every scripted call"
    for block in response.text.strip().split("\n\n"):
        if block.startswith("event: ticket.created"):
            return uuid.UUID(json.loads(block.split("data: ", 1)[1])["ticket_id"])
    raise AssertionError("no ticket.created event")


def _detail(client: TestClient, ticket_id: uuid.UUID) -> dict[str, Any]:
    response = client.get(f"/api/v1/console/tickets/{ticket_id}")
    assert response.status_code == 200, response.text
    detail: dict[str, Any] = response.json()
    return detail


def _approve(client: TestClient, ticket_id: uuid.UUID, actions: list[str]) -> dict[str, Any]:
    detail = _detail(client, ticket_id)
    response = client.post(
        f"/api/v1/console/drafts/{detail['draft']['id']}/approve",
        json={"ticket_version": detail["version"], "approved_actions": actions},
        headers=key(),
    )
    assert response.status_code == 200, response.text
    out: dict[str, Any] = response.json()
    return out


def _side_effects(urls: Urls) -> list[tuple[Any, ...]]:
    return run_sql(
        urls,
        "SELECT node, tool, status, external_ref, approved_by FROM tool_calls"
        " WHERE side_effect ORDER BY created_at",
    )


# --- act: approved Jira issues ------------------------------------------------------------------


def test_an_approved_dispute_issue_is_created_and_shown_on_the_ticket(stack: Stack) -> None:
    """Done when (P7-05): dispute ticket → approval route with a draft and a prepared Jira issue
    → the operator approves → the issue key is in `tool_calls.external_ref` and on the ticket."""
    ticket_id = _post(stack, DISPUTE, _dispute_run())
    operator = _operator(stack)
    before = _detail(operator, ticket_id)
    assert before["status"] == "pending_approval"
    assert "policy_review" in before["route_reasons"]
    [proposed] = before["draft"]["proposed_actions"]
    assert proposed["id"] == "a1"
    assert proposed["params"]["issue_type"] == "dispute"
    # Nothing ran before the approval.
    assert _side_effects(stack.urls) == []

    reviewed = _approve(operator, ticket_id, ["a1"])

    assert reviewed["draft"]["approved_actions"] == ["a1"]
    [(node, tool, status, ref, approved_by)] = _side_effects(stack.urls)
    assert (node, tool, status) == ("act", "jira_create_issue", "ok")
    assert ref.startswith("TB-")
    assert approved_by == stack.operator_id
    after = _detail(operator, ticket_id)
    [shown] = [call for call in after["tool_calls"] if call["side_effect"]]
    assert (shown["tool"], shown["external_ref"]) == ("jira_create_issue", ref)
    assert shown["arguments"]["customer_ref"] == "CUS-0001"
    assert after["latest_run"]["status"] == "completed", after["latest_run"]["error"]
    audit = run_sql(
        stack.urls,
        "SELECT details FROM audit_log WHERE action = 'tool.called'"
        " AND details ->> 'tool' = 'jira_create_issue'",
    )
    assert [details["approved_by"] for (details,) in audit] == [str(stack.operator_id)]


def test_an_approve_without_ticked_actions_creates_no_issue(stack: Stack) -> None:
    ticket_id = _post(stack, DISPUTE, _dispute_run())
    operator = _operator(stack)

    _approve(operator, ticket_id, [])

    assert _side_effects(stack.urls) == []
    assert _detail(operator, ticket_id)["latest_run"]["status"] == "completed"


# --- handoff_prep: the urgent Slack alert -------------------------------------------------------


@pytest.mark.parametrize(
    "triage",
    [
        # Owner decision (P7-05): a fraud report is urgent with triage urgency normal.
        {
            "intent": "card_declined",
            "asks_for_action": "none",
            "reports_unauthorised_activity": True,
        },
        {"intent": "card_lost_stolen", "asks_for_action": "none"},
        {"intent": "fees_fx", "asks_for_action": "none", "wants_human": True, "urgency": "high"},
    ],
)
def test_every_urgent_handoff_notice_also_sends_the_slack_alert(
    stack: Stack, triage: dict[str, Any]
) -> None:
    """From P6-02: the urgent notice promises a reply within 30 (60) minutes; the alert is what
    makes it true. Every `message.sent` notice marked `urgent` has an alert."""
    ticket_id = _post(stack, "Someone used my card, please help.", _handoff_run(**triage))

    [(details,)] = run_sql(
        stack.urls,
        "SELECT details FROM audit_log WHERE action = 'message.sent'"
        " AND details ->> 'kind' = 'handoff_notice'",
    )
    assert details["urgent"] is True
    [(node, tool, status, ref, approved_by)] = _side_effects(stack.urls)
    assert (node, tool, status) == ("handoff_prep", "slack_post_escalation", "ok")
    assert ref
    assert approved_by is None
    [(arguments,)] = run_sql(
        stack.urls, "SELECT arguments FROM tool_calls WHERE tool = 'slack_post_escalation'"
    )
    assert arguments["urgency"] == "high"
    assert arguments["ticket_id"] == str(ticket_id)
    # Fixed fields only: never customer text.
    assert "Someone used my card" not in arguments["text"]
    assert str(ticket_id) in arguments["text"]


def test_a_standard_handoff_sends_no_alert(stack: Stack) -> None:
    ticket_id = _post(
        stack,
        "I would like to talk to a person about fees.",
        _handoff_run(intent="fees_fx", asks_for_action="none", wants_human=True),
    )
    [(details,)] = run_sql(
        stack.urls,
        "SELECT details FROM audit_log WHERE action = 'message.sent'"
        " AND details ->> 'kind' = 'handoff_notice'",
    )
    assert details["urgent"] is False
    assert _side_effects(stack.urls) == []
    assert _detail(_operator(stack), ticket_id)["status"] == "handed_off"


# --- the operator's own action ------------------------------------------------------------------


def test_an_operator_creates_the_fraud_issue_of_a_handoff_once(stack: Stack) -> None:
    """ai-pipeline §6: a fraud handoff has no draft; the operator sends the pre-filled form."""
    ticket_id = _post(
        stack,
        "Someone used my card 4111 1111 1111 1111, it was not me.",
        _handoff_run(
            intent="card_declined", asks_for_action="none", reports_unauthorised_activity=True
        ),
    )
    operator = _operator(stack)
    body = {
        "type": "jira_create_issue",
        "params": {
            "issue_type": "fraud",
            "summary": "Unauthorised card use",
            "description": "Customer wrote card 4111 1111 1111 1111 in the chat.",
        },
    }
    # The alert is the ticket's one Slack call; the issue is its one Jira call.
    created = operator.post(f"/api/v1/console/tickets/{ticket_id}/actions", json=body)
    assert created.status_code == 200, created.text
    out = created.json()
    assert (out["node"], out["tool"], out["status"]) == ("act", "jira_create_issue", "ok")
    assert out["external_ref"].startswith("TB-")
    assert out["approved_by"] == str(stack.operator_id)
    assert "4111 1111 1111 1111" not in json.dumps(out)
    assert "[CARD ****1111]" in out["arguments"]["description"]

    again = operator.post(f"/api/v1/console/tickets/{ticket_id}/actions", json=body)
    assert again.status_code == 409
    assert again.json()["code"] == "action_denied"
    # The run's one `act` call is used (checked before the ticket's one issue, G6).
    assert again.json()["reason"] == "run_limit"
    assert [row[1] for row in _side_effects(stack.urls)] == [
        "slack_post_escalation",
        "jira_create_issue",
    ]


def test_the_operator_action_checks_the_ticket_and_the_request(stack: Stack) -> None:
    ticket_id = _post(stack, DISPUTE, _dispute_run())
    operator = _operator(stack)
    params = {key: value for key, value in ACTION.items() if key != "type"}
    body = {"type": "jira_create_issue", "params": params}
    url = f"/api/v1/console/tickets/{ticket_id}/actions"

    # A draft waits: its own action is approved with the draft.
    waiting = operator.post(url, json=body)
    assert waiting.status_code == 409
    assert waiting.json()["code"] == "conflict"
    # The customer and the ticket never come from the request.
    forged_params = {**params, "customer_ref": "CUS-0002"}
    forged = operator.post(url, json={"type": "jira_create_issue", "params": forged_params})
    assert forged.status_code == 422
    unknown = operator.post(f"/api/v1/console/tickets/{uuid.uuid4()}/actions", json=body)
    assert unknown.status_code == 404
    assert _side_effects(stack.urls) == []
