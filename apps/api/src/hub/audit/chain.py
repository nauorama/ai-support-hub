"""Hash chain of the audit log (docs/data-model.md `audit_log`, docs/security.md §5).

``hash = sha256(prev_hash + canonical_json(row))``. The first row's ``prev_hash`` is
``GENESIS_HASH``. Pure functions; the database side is in ``hub.audit.log``.

The canonical form must give the same text for the values ``record`` writes and for the values
Postgres returns from ``jsonb`` and ``timestamptz``, otherwise ``verify_chain`` would report rows
nobody changed. Two places where they differ, both handled here:

- ``jsonb`` stores the decimal text that ``json.dumps`` writes for a float (its shortest
  ``repr``) as ``numeric`` and prints it without exponent notation. The float ``1e23`` is stored
  as ``1e+23`` and comes back as the integer ``100000000000000000000000``, which is not
  ``int(1e23)`` (``99999999999999991611392``, the float's exact binary value). A float with a
  whole value is therefore hashed as the integer of its decimal text. Other floats are smaller
  than 2**53, their decimal text has few digits, and they come back as the same float.
- ``timestamptz`` comes back in the session time zone. The timestamp is hashed in UTC.

Masking (G1) covers every text field of the row and every string and number in ``details``. Only
``uuid.UUID`` objects are kept as they are: about 1 in 100 UUIDs contains a Luhn-valid run of 13+
digits across its dashes, and masking them would break the ids the log is searched by. A UUID
object is made by code, never typed by a customer. A string is always masked, even one shaped
like a UUID, because such a string can hold a card number ("41111111-1111-1111-abcd-…"); the
request id makes the same choice (P2-01).
"""

import hashlib
import json
import math
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal
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


type Id = str | uuid.UUID
type JsonValue = dict[str, JsonValue] | list[JsonValue] | str | int | float | bool | None


class AuditValueError(ValueError):
    """A value that cannot be stored (Postgres rejects it), cannot be hashed the same way twice,
    or would lose data when masked."""


def mask_details(details: Mapping[str, object]) -> dict[str, JsonValue]:
    """A copy of ``details`` with every string (keys too) and number passed through masking (G1).

    A number that masking would change (a card number given as an integer or a float) becomes its
    masked text. A ``uuid.UUID`` becomes its text, unmasked. Only JSON types and UUIDs are
    accepted, so nothing reaches ``jsonb`` that comes back different.
    """
    return _mask_mapping(details)


def mask_id(value: Id) -> str:
    """An id field (``actor_id``, ``entity_id``): a ``uuid.UUID`` as its text, a string masked."""
    if isinstance(value, uuid.UUID):
        return str(value)
    return mask_field(value)


def mask_field(text: str) -> str:
    """One text field of the row, masked (G1). Every string is masked, UUID-shaped or not."""
    if "\x00" in text:
        raise AuditValueError("audit strings must not contain NUL (Postgres rejects it)")
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        raise AuditValueError("audit strings must be valid Unicode (no lone surrogates)") from None
    return mask_text(text)


def _mask_mapping(mapping: Mapping[Any, object]) -> dict[str, JsonValue]:
    masked: dict[str, JsonValue] = {}
    for key, item in mapping.items():
        if not isinstance(key, str):
            raise AuditValueError(f"details keys must be strings, got {type(key).__name__}")
        masked_key = mask_field(key)
        if masked_key in masked:
            # Two keys that mask to the same text: one value would silently replace the other.
            raise AuditValueError("two details keys are the same after masking")
        masked[masked_key] = _mask_value(item)
    return masked


def _mask_value(value: object) -> JsonValue:
    if isinstance(value, Mapping):
        return _mask_mapping(value)
    if isinstance(value, list | tuple):
        return [_mask_value(item) for item in value]
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, str):
        return mask_field(value)
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
            raise AuditValueError("details numbers must be finite (jsonb rejects NaN, Infinity)")
        # The decimal text jsonb will store, without exponent: 4111111111111111.0 is a card too.
        text = format(Decimal(repr(value)), "f")
        masked_text = mask_text(text)
        return value if masked_text == text else masked_text
    raise AuditValueError(f"details values must be JSON types or UUIDs, got {type(value).__name__}")


def _normalise(value: object) -> object:
    """Whole floats as the integer of their decimal text, the way they come back from ``jsonb``."""
    if isinstance(value, dict):
        return {key: _normalise(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_normalise(item) for item in value]
    if isinstance(value, float) and value.is_integer():
        return int(Decimal(repr(value)))
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
