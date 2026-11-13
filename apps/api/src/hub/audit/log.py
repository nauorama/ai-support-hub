"""Writing and checking the audit log.

``record`` works inside the caller's session and never commits, like the repositories: the audit
row belongs to the same transaction as the change it describes (validate → mask → save → audit).

Inserts are serialised with a transaction-level advisory lock around "read last hash → insert"
(docs/data-model.md). The lock is held until the caller's transaction ends, so audit writes are
serialised for the whole transaction, not only for the insert; at demo scale that is fine. The
read of the last hash needs a fresh snapshot after the lock is taken, which READ COMMITTED (the
default) gives; under REPEATABLE READ two writers could fork the chain.
"""

from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, ValidationError
from sqlalchemy import Row, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from hub.audit.chain import (
    GENESIS_HASH,
    AuditRow,
    Id,
    compute_hash,
    mask_details,
    mask_field,
    mask_id,
)
from hub.db.models import AuditLog
from hub.domain.enums import ActorType

# Any fixed bigint works; it only has to be the same for every writer. ASCII "audit".
AUDIT_LOCK_KEY = 0x6175646974


class ChainCheck(BaseModel):
    """Result of ``verify_chain``: ``broken_id`` is the first row that does not verify."""

    model_config = ConfigDict(frozen=True)

    ok: bool
    checked: int
    broken_id: int | None = None


async def record(
    session: AsyncSession,
    *,
    actor_type: ActorType,
    actor_id: Id | None,
    action: str,
    entity_type: str,
    entity_id: Id,
    details: dict[str, Any] | None = None,
) -> AuditLog:
    """Append one audit row. Every text field and ``details`` are masked (G1) before they are
    hashed and stored. Pass ids as ``uuid.UUID`` where they are UUIDs: only UUID objects are kept
    unmasked; a string is always masked (``hub.audit.chain``)."""
    row = AuditRow(
        # Set here, not by the server default: the hash covers it and is computed before insert.
        ts=datetime.now(UTC),
        actor_type=actor_type,
        actor_id=None if actor_id is None else mask_id(actor_id),
        action=mask_field(action),
        entity_type=mask_field(entity_type),
        entity_id=mask_id(entity_id),
        details=mask_details(details or {}),
    )
    await session.execute(select(func.pg_advisory_xact_lock(AUDIT_LOCK_KEY)))
    last = await session.scalar(select(AuditLog.hash).order_by(AuditLog.id.desc()).limit(1))
    prev_hash = last if last is not None else GENESIS_HASH
    entry = AuditLog(
        **row.model_dump(),
        prev_hash=prev_hash,
        hash=compute_hash(prev_hash, row),
    )
    session.add(entry)
    await session.flush()
    return entry


async def verify_chain(session: AsyncSession) -> ChainCheck:
    """Recompute the chain in id order; report the first row whose hash or link is wrong.

    An edited row fails its own hash. A deleted row shows at the next row, whose ``prev_hash``
    no longer matches. Deleting the last rows is not detected (docs/security.md §5).
    """
    # Columns, not ORM objects: objects already in the session's identity map would hide what
    # is stored, and a long log would fill the map.
    statement = select(
        AuditLog.id,
        AuditLog.ts,
        AuditLog.actor_type,
        AuditLog.actor_id,
        AuditLog.action,
        AuditLog.entity_type,
        AuditLog.entity_id,
        AuditLog.details,
        AuditLog.prev_hash,
        AuditLog.hash,
    ).order_by(AuditLog.id)
    expected_prev = GENESIS_HASH
    checked = 0
    result = await session.stream(statement)
    async for partition in result.partitions(500):
        for stored in partition:
            if stored.prev_hash != expected_prev or _stored_hash(stored) != stored.hash:
                await result.close()
                return ChainCheck(ok=False, checked=checked, broken_id=stored.id)
            expected_prev = stored.hash
            checked += 1
    return ChainCheck(ok=True, checked=checked)


def _stored_hash(stored: Row[Any]) -> str | None:
    """The hash the stored values should have; ``None`` when they cannot be hashed at all."""
    try:
        row = AuditRow(
            ts=stored.ts,
            actor_type=stored.actor_type,
            actor_id=stored.actor_id,
            action=stored.action,
            entity_type=stored.entity_type,
            entity_id=stored.entity_id,
            details=stored.details,
        )
        return compute_hash(stored.prev_hash, row)
    except (ValidationError, ValueError):
        # A hand-made row (e.g. a naive timestamp written by someone) is a broken row.
        return None
