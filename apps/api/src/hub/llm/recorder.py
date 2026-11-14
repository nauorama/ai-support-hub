"""Where the gateway writes its `llm_calls` rows and G2 audit records.

Each record is committed in its own transaction: a failed call must stay recorded even when the
caller's transaction rolls back (one row per call, also for failures; P3-01).
"""

from typing import Protocol

from hub.audit import record as audit_record
from hub.db.models import LlmCall
from hub.db.session import Database
from hub.domain.enums import ActorType
from hub.llm.schemas import LlmCallRecord

BLOCKED_ACTION = "guardrail.blocked"


class CallRecorder(Protocol):
    async def record(self, call: LlmCallRecord) -> None:
        """Store one `llm_calls` row."""

    async def record_blocked(self, call: LlmCallRecord, entities: dict[str, int]) -> None:
        """Store the row of a call that G2 blocked, and its audit record, together."""


class DbCallRecorder:
    def __init__(self, database: Database) -> None:
        self._database = database

    async def record(self, call: LlmCallRecord) -> None:
        async with self._database.sessions.begin() as session:
            session.add(LlmCall(**call.model_dump()))

    async def record_blocked(self, call: LlmCallRecord, entities: dict[str, int]) -> None:
        async with self._database.sessions.begin() as session:
            session.add(LlmCall(**call.model_dump()))
            await session.flush()
            await audit_record(
                session,
                actor_type=ActorType.SYSTEM,
                actor_id=None,
                action=BLOCKED_ACTION,
                entity_type="llm_call",
                entity_id=call.id,
                details=blocked_details(call, entities),
            )


def blocked_details(call: LlmCallRecord, entities: dict[str, int]) -> dict[str, object]:
    """Audit details of a G2 block: which guardrail, where, and how many entities of each type.
    Never the request text."""
    return {
        "guardrail": "G2",
        "node": call.node.value,
        "context": call.context.value,
        "model": call.model,
        "run_id": call.run_id,
        "entities": dict(sorted(entities.items())),
    }
