"""Jira mock: stores issues in the local table and logs (ADR-0019; there is no real mode).

Same idempotency key with the same arguments = the same issue (a replay). The same key with other
arguments is refused, and so is a second issue for a ticket under a new key (security T8).
"""

import logging
from collections.abc import Callable
from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict

from support_tools.adapters.mock_store import MockRefusedError, MockStore, arguments_hash

logger = logging.getLogger(__name__)

PROJECT_KEY = "TB"
# A reserved `.example` domain: the mock has no real Jira behind it.
BROWSE_URL = "https://jira.tallybridge.example/browse/"


class IssueType(StrEnum):
    DISPUTE = "dispute"
    FRAUD = "fraud"
    BUG = "bug"
    LIMIT_REQUEST = "limit_request"


class JiraIssue(BaseModel):
    model_config = ConfigDict(frozen=True)

    key: str
    url: str
    # False when this call replayed an earlier one with the same idempotency key.
    created: bool


class JiraIssueStatus(BaseModel):
    model_config = ConfigDict(frozen=True)

    key: str
    status: str
    assignee: str | None


class JiraMock:
    def __init__(self, store: MockStore, now: Callable[[], datetime]) -> None:
        self._store = store
        self._now = now

    def create_issue(
        self,
        issue_type: IssueType,
        summary: str,
        description: str,
        customer_ref: str,
        ticket_id: str,
        idempotency_key: str,
    ) -> JiraIssue:
        arguments: dict[str, Any] = {
            "issue_type": str(issue_type),
            "summary": summary,
            "description": description,
            "customer_ref": customer_ref,
            "ticket_id": ticket_id,
        }
        digest = arguments_hash(arguments)
        with self._store.transaction() as db:
            earlier = db.execute(
                "SELECT number, arguments_hash FROM jira_issues WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
            if earlier is not None:
                if earlier["arguments_hash"] != digest:
                    raise MockRefusedError("idempotency key reused with other arguments")
                return _issue(earlier["number"], created=False)
            if db.execute("SELECT 1 FROM jira_issues WHERE ticket_id = ?", (ticket_id,)).fetchone():
                raise MockRefusedError("this ticket already has a Jira issue")
            cursor = db.execute(
                "INSERT INTO jira_issues (idempotency_key, ticket_id, arguments_hash, issue_type,"
                " summary, description, customer_ref, status, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, 'open', ?)",
                (
                    idempotency_key,
                    ticket_id,
                    digest,
                    str(issue_type),
                    summary,
                    description,
                    customer_ref,
                    self._now().isoformat(),
                ),
            )
            number = cursor.lastrowid
        if number is None:  # sqlite sets it after every INSERT
            raise RuntimeError("no row id after insert")
        issue = _issue(number, created=True)
        # Key, type and ticket only: summary and description may hold customer text.
        logger.info("jira mock: created %s (%s) for ticket %s", issue.key, issue_type, ticket_id)
        return issue

    def get_issue(self, key: str) -> JiraIssueStatus:
        number = _number(key)
        with self._store.transaction() as db:
            row = db.execute(
                "SELECT status FROM jira_issues WHERE number = ?", (number,)
            ).fetchone()
        if row is None:
            raise MockRefusedError("issue not found")
        return JiraIssueStatus(key=key, status=row["status"], assignee=None)


def _issue(number: int, *, created: bool) -> JiraIssue:
    key = f"{PROJECT_KEY}-{number}"
    return JiraIssue(key=key, url=f"{BROWSE_URL}{key}", created=created)


def _number(key: str) -> int:
    prefix = f"{PROJECT_KEY}-"
    if not key.startswith(prefix) or not key[len(prefix) :].isdigit():
        raise MockRefusedError("issue not found")
    return int(key[len(prefix) :])
