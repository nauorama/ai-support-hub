"""Jira and Slack mocks: idempotency keys and per-ticket limits (security T8), stored in SQLite."""

import logging
import threading
from pathlib import Path

import pytest

from support_tools.adapters.jira import IssueType, JiraMock
from support_tools.adapters.mock_store import MockRefusedError, MockStore
from support_tools.adapters.slack import SlackMock, Urgency

from .conftest import NOW

TICKET = "0b3f6c1e-2a4d-4c8e-9f10-1a2b3c4d5e6f"
OTHER_TICKET = "1c4a7d2f-3b5e-4d9f-8a21-2b3c4d5e6f70"


def _jira(store: MockStore) -> JiraMock:
    return JiraMock(store, lambda: NOW)


def _create(jira: JiraMock, key: str = "key-0001", ticket: str = TICKET, summary: str = "s") -> str:
    return jira.create_issue(IssueType.DISPUTE, summary, "description", "CUS-0001", ticket, key).key


def test_the_same_idempotency_key_twice_creates_one_issue(store: MockStore) -> None:
    jira = _jira(store)
    first = jira.create_issue(IssueType.DISPUTE, "s", "d", "CUS-0001", TICKET, "key-0001")
    second = jira.create_issue(IssueType.DISPUTE, "s", "d", "CUS-0001", TICKET, "key-0001")
    assert (first.key, first.created) == ("TB-1", True)
    assert (second.key, second.created) == ("TB-1", False)
    assert first.url == "https://jira.tallybridge.example/browse/TB-1"
    with store.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM jira_issues").fetchone()[0] == 1


def test_a_reused_key_with_other_arguments_is_refused(store: MockStore) -> None:
    jira = _jira(store)
    _create(jira)
    with pytest.raises(MockRefusedError, match="reused"):
        _create(jira, summary="other")


def test_a_ticket_gets_at_most_one_issue(store: MockStore) -> None:
    jira = _jira(store)
    _create(jira)
    with pytest.raises(MockRefusedError, match="already has a Jira issue"):
        _create(jira, key="key-0002")
    assert _create(jira, key="key-0003", ticket=OTHER_TICKET) == "TB-2"


def test_an_issue_can_be_read_back_after_a_restart(tmp_path: Path) -> None:
    path = tmp_path / "mock.sqlite"
    store = MockStore(path)
    key = _create(_jira(store))
    store.close()
    second = MockStore(path)
    reopened = _jira(second)
    status = reopened.get_issue(key)
    assert (status.key, status.status, status.assignee) == ("TB-1", "open", None)
    # The replay still finds the first issue after the restart.
    assert _create(reopened) == key
    second.close()


@pytest.mark.parametrize("key", ["TB-2", "XX-1", "TB-x"])
def test_an_unknown_issue_is_not_found(store: MockStore, key: str) -> None:
    jira = _jira(store)
    _create(jira)
    with pytest.raises(MockRefusedError, match="issue not found"):
        jira.get_issue(key)


def test_concurrent_calls_with_one_key_create_one_issue(store: MockStore) -> None:
    jira = _jira(store)
    keys: list[str] = []
    threads = [threading.Thread(target=lambda: keys.append(_create(jira))) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert keys == ["TB-1"] * 8


def test_the_log_names_the_issue_but_not_its_text(
    store: MockStore, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    jira = _jira(store)
    jira.create_issue(
        IssueType.FRAUD, "secret summary", "secret text", "CUS-0001", TICKET, "k-000001"
    )
    assert "TB-1" in caplog.text
    assert "secret" not in caplog.text
    assert "CUS-0001" not in caplog.text


def _slack(store: MockStore) -> SlackMock:
    return SlackMock(store, lambda: NOW)


def test_the_same_key_twice_posts_one_alert(store: MockStore) -> None:
    slack = _slack(store)
    first = slack.post_escalation(TICKET, Urgency.HIGH, "text", "key-0001")
    second = slack.post_escalation(TICKET, Urgency.HIGH, "text", "key-0001")
    assert first.created
    assert not second.created
    assert first.ts == second.ts == f"{int(NOW.timestamp())}.000001"
    with store.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM slack_messages").fetchone()[0] == 1


def test_a_ticket_gets_at_most_one_alert_and_a_key_is_not_reused(store: MockStore) -> None:
    slack = _slack(store)
    slack.post_escalation(TICKET, Urgency.HIGH, "text", "key-0001")
    with pytest.raises(MockRefusedError, match="reused"):
        slack.post_escalation(TICKET, Urgency.HIGH, "other text", "key-0001")
    with pytest.raises(MockRefusedError, match="already has a Slack alert"):
        slack.post_escalation(TICKET, Urgency.HIGH, "text", "key-0002")
    other = slack.post_escalation(OTHER_TICKET, Urgency.NORMAL, "text", "key-0003")
    assert other.ts.endswith(".000002")


def test_the_store_creates_its_folder(tmp_path: Path) -> None:
    MockStore(tmp_path / "a" / "b" / "mock.sqlite").close()
    assert (tmp_path / "a" / "b" / "mock.sqlite").exists()
