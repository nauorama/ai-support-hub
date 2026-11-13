"""Hash chain of the audit log (docs/data-model.md `audit_log`, docs/security.md §5).

``hash = sha256(prev_hash + canonical_json(row))``. The first row's ``prev_hash`` is
``GENESIS_HASH``. Pure functions; the database side is in ``hub.audit.log``.

The canonical form must give the same text for the values ``record`` writes and for the values
Postgres returns from ``jsonb`` and ``timestamptz``, otherwise ``verify_chain`` would report rows
nobody changed. Two places where they differ, both handled here:

- ``jsonb`` stores numbers as ``numeric`` and prints them without exponent notation, so the float
  ``1e20`` comes back as the integer ``100000000000000000000``. A float with a whole value is
  therefore hashed as an integer. Other floats come back with the same value, and Python writes
  equal floats the same way.
- ``timestamptz`` comes back in the session time zone. The timestamp is hashed in UTC.
"""

import hashlib
import json
import math
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict

from hub.domain.enums import ActorType
from hub.guardrails.masking import mask_text

GENESIS_HASH = "0" * 64

# A card number stored as a number has 13-19 digits; shorter integers are amounts and counts.
_MIN_CARD_DIGITS = 13


class AuditRow(BaseModel):
    """The hashed fields of one audit row: every column except ``id``, ``prev_hash`` and
    ``hash``. ``id`` is assigned by the database on insert; the order of rows is protected by
    ``prev_hash`` instead."""

    model_config = ConfigDict(frozen=True)

    ts: datetime
    actor_type: ActorType
    actor_id: str | None
    action: str
    entity_type: str
    entity_id: str
    details: dict[str, Any]


type JsonValue = dict[str, JsonValue] | list[JsonValue] | str | int | float | bool | None


class AuditDetailsError(ValueError):
    """``details`` hold a value that cannot be stored in ``jsonb`` or hashed the same way twice."""


def mask_details(details: Mapping[str, object]) -> dict[str, JsonValue]:
    """A copy of ``details`` with every string (keys too) passed through card masking (G1).

    An integer that masking would change (a card number given as a number) becomes its masked
    text. Only JSON types are accepted, so nothing reaches ``jsonb`` that comes back different.
    """
    return _mask_mapping(details)


def _mask_mapping(mapping: Mapping[Any, object]) -> dict[str, JsonValue]:
    masked: dict[str, JsonValue] = {}
    for key, item in mapping.items():
        if not isinstance(key, str):
            raise AuditDetailsError(f"details keys must be strings, got {type(key).__name__}")
        masked[_mask_str(key)] = _mask_value(item)
    return masked


def _mask_value(value: object) -> JsonValue:
    if isinstance(value, Mapping):
        return _mask_mapping(value)
    if isinstance(value, list | tuple):
        return [_mask_value(item) for item in value]
    if isinstance(value, str):
        return _mask_str(value)
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        if len(str(abs(value))) < _MIN_CARD_DIGITS:
            return value
        text = str(value)
        masked_text = mask_text(text)
        return value if masked_text == text else masked_text
    if isinstance(value, float):
        if not math.isfinite(value):
            raise AuditDetailsError("details numbers must be finite (jsonb rejects NaN, Infinity)")
        return value
    raise AuditDetailsError(f"details values must be JSON types, got {type(value).__name__}")


def _mask_str(text: str) -> str:
    if "\x00" in text:
        raise AuditDetailsError("details strings must not contain NUL (jsonb rejects \\u0000)")
    return mask_text(text)


def _normalise(value: object) -> object:
    """Whole floats as integers, the way they come back from ``jsonb``."""
    if isinstance(value, dict):
        return {key: _normalise(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_normalise(item) for item in value]
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


def canonical_json(row: AuditRow) -> str:
    """Sorted keys, no spaces, ASCII only, timestamp in UTC with microseconds."""
    if row.ts.tzinfo is None:
        raise ValueError("audit ts must be timezone-aware")
    payload = {
        "ts": row.ts.astimezone(UTC).isoformat(timespec="microseconds"),
        "actor_type": row.actor_type.value,
        "actor_id": row.actor_id,
        "action": row.action,
        "entity_type": row.entity_type,
        "entity_id": row.entity_id,
        "details": _normalise(row.details),
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def compute_hash(prev_hash: str, row: AuditRow) -> str:
    return hashlib.sha256((prev_hash + canonical_json(row)).encode("ascii")).hexdigest()
