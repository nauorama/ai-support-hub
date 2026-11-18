"""Slack mock: stores escalation alerts in the local table and logs (ADR-0019; no real mode).

The same idempotency rules as the Jira mock: a replay returns the first message, a reused key with
other arguments and a second alert for a ticket are refused (security T8).
"""

import logging
from collections.abc import Callable
from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from support_tools.adapters.mock_store import MockRefusedError, MockStore, arguments_hash

logger = logging.getLogger(__name__)


class Urgency(StrEnum):
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"


class SlackMessage(BaseModel):
    model_config = ConfigDict(frozen=True)

    # Slack's message id: seconds since the epoch, a dot and a sequence number.
    ts: str
    created: bool


class SlackMock:
    def __init__(self, store: MockStore, now: Callable[[], datetime]) -> None:
        self._store = store
        self._now = now

    def post_escalation(
        self, ticket_id: str, urgency: Urgency, text: str, idempotency_key: str
    ) -> SlackMessage:
        digest = arguments_hash({"ticket_id": ticket_id, "urgency": str(urgency), "text": text})
        with self._store.transaction() as db:
            earlier = db.execute(
                "SELECT ts, arguments_hash FROM slack_messages WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
            if earlier is not None:
                if earlier["arguments_hash"] != digest:
                    raise MockRefusedError("idempotency key reused with other arguments")
                return SlackMessage(ts=earlier["ts"], created=False)
            if db.execute(
                "SELECT 1 FROM slack_messages WHERE ticket_id = ?", (ticket_id,)
            ).fetchone():
                raise MockRefusedError("this ticket already has a Slack alert")
            now = self._now()
            sequence = db.execute(
                "SELECT COALESCE(MAX(number), 0) + 1 FROM slack_messages"
            ).fetchone()[0]
            ts = f"{int(now.timestamp())}.{sequence:06d}"
            db.execute(
                "INSERT INTO slack_messages (idempotency_key, ticket_id, arguments_hash, urgency,"
                " text, ts, posted_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (idempotency_key, ticket_id, digest, str(urgency), text, ts, now.isoformat()),
            )
        # Ticket and urgency only: the text may hold customer details.
        logger.info("slack mock: escalation %s (%s) for ticket %s", ts, urgency, ticket_id)
        return SlackMessage(ts=ts, created=True)
