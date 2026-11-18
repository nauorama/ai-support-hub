"""The local table of the Jira and Slack mocks: a SQLite file (ADR-0019, owner decision P7-02).

The mocks store every created issue and every posted alert, so an idempotency key can be
replayed and the per-ticket limits hold across a server restart (security T8: at most one Jira
issue and one Slack alert per ticket). Unique indexes enforce both in the database, so two
concurrent calls cannot both win. On Cloud Run the file lives as long as the instance, which is
enough for a demo with mocks.
"""

import hashlib
import json
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jira_issues (
    number INTEGER PRIMARY KEY AUTOINCREMENT,
    idempotency_key TEXT NOT NULL UNIQUE,
    ticket_id TEXT NOT NULL UNIQUE,
    arguments_hash TEXT NOT NULL,
    issue_type TEXT NOT NULL,
    summary TEXT NOT NULL,
    description TEXT NOT NULL,
    customer_ref TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS slack_messages (
    number INTEGER PRIMARY KEY AUTOINCREMENT,
    idempotency_key TEXT NOT NULL UNIQUE,
    ticket_id TEXT NOT NULL UNIQUE,
    arguments_hash TEXT NOT NULL,
    urgency TEXT NOT NULL,
    text TEXT NOT NULL,
    ts TEXT NOT NULL UNIQUE,
    posted_at TEXT NOT NULL
);
"""


class MockRefusedError(Exception):
    """A call the mock refuses with a safe message (reused key, per-ticket limit, unknown key)."""


def arguments_hash(arguments: dict[str, Any]) -> str:
    """A stable hash of a call's arguments, to tell a replay from a reused key."""
    canonical = json.dumps(arguments, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode()).hexdigest()


class MockStore:
    """One SQLite connection guarded by a lock; every write is one transaction."""

    def __init__(self, path: Path | str) -> None:
        if isinstance(path, Path):
            path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(str(path), check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock, self._connection:
            self._connection.executescript(_SCHEMA)

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Serialised: the check for a replay and the insert see the same state."""
        with self._lock, self._connection:
            yield self._connection

    def close(self) -> None:
        self._connection.close()
