"""Audit hash chain and details masking (hub.audit.chain): pure parts, no database."""

import hashlib
import json
import re
import uuid
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from typing import Any, cast

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from sqlalchemy import Row

from hub.audit.chain import (
    GENESIS_HASH,
    AuditRow,
    AuditValueError,
    canonical_json,
    compute_hash,
    mask_details,
    mask_field,
    mask_id,
)
from hub.audit.log import _stored_hash
from hub.domain.enums import ActorType
from hub.guardrails.masking import luhn_valid, mask_text

VISA = "4111111111111111"  # public test card number
TS = datetime(2026, 11, 13, 10, 0, 0, 123456, tzinfo=UTC)


def _row(**changes: object) -> AuditRow:
    values: dict[str, object] = {
        "ts": TS,
        "actor_type": ActorType.OPERATOR,
        "actor_id": "op-1",
        "action": "ticket.claimed",
        "entity_type": "ticket",
        "entity_id": "t-1",
        "details": {"b": 2, "a": [1, "x"]},
    }
    return AuditRow(**(values | changes))


def test_canonical_json_is_sorted_compact_ascii_and_utc() -> None:
    assert canonical_json(_row(details={"b": 2, "a": "é"})) == (
        '{"action":"ticket.claimed","actor_id":"op-1","actor_type":"operator",'
        '"details":{"a":"\\u00e9","b":2},"entity_id":"t-1","entity_type":"ticket",'
        '"ts":"2026-11-13T10:00:00.123456+00:00"}'
    )


def test_key_order_does_not_change_the_text() -> None:
    first = _row(details={"a": 1, "b": {"y": 1, "x": 2}})
    second = _row(details={"b": {"x": 2, "y": 1}, "a": 1})
    assert canonical_json(first) == canonical_json(second)


def test_the_same_moment_in_another_time_zone_gives_the_same_text() -> None:
    elsewhere = TS.astimezone(timezone(timedelta(hours=2)))
    assert canonical_json(_row(ts=elsewhere)) == canonical_json(_row())


def test_a_naive_timestamp_is_refused() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        canonical_json(_row(ts=datetime(2026, 11, 13, 10, 0)))


@pytest.mark.parametrize(
    ("written", "read_back"),
    [
        # What json.loads gives for the text jsonb prints (numeric, no exponent notation).
        (1.0, 1.0),
        (1e20, 100000000000000000000),
        # Not exactly representable: int(1e23) is 99999999999999991611392, but jsonb stores the
        # decimal text "1e+23" (reviewer finding on P2-04).
        (1e23, 100000000000000000000000),
        (float(2**70), 1180591620717411300000),
        (1e300, 10**300),
        (123.0, 123.0),
        (0.1, 0.1),
        (1e-7, 0.0000001),
        (1.23e-5, 0.0000123),
        (0.1 + 0.2, 0.30000000000000004),
    ],
)
def test_numbers_hash_the_same_as_jsonb_returns_them(written: float, read_back: object) -> None:
    assert canonical_json(_row(details={"n": written})) == canonical_json(
        _row(details={"n": read_back})
    )


def test_hash_is_sha256_of_prev_hash_and_canonical_json() -> None:
    row = _row()
    expected = hashlib.sha256((GENESIS_HASH + canonical_json(row)).encode()).hexdigest()
    assert compute_hash(GENESIS_HASH, row) == expected
    assert re.fullmatch(r"[0-9a-f]{64}", expected)


def test_genesis_is_64_zeros() -> None:
    assert GENESIS_HASH == "0" * 64


@pytest.mark.parametrize(
    "change",
    [
        {"ts": TS + timedelta(microseconds=1)},
        {"actor_type": ActorType.SYSTEM},
        {"actor_id": None},
        {"action": "ticket.closed"},
        {"entity_type": "message"},
        {"entity_id": "t-2"},
        {"details": {"b": 3, "a": [1, "x"]}},
    ],
)
def test_every_hashed_field_changes_the_hash(change: dict[str, Any]) -> None:
    assert compute_hash(GENESIS_HASH, _row(**change)) != compute_hash(GENESIS_HASH, _row())


def test_prev_hash_changes_the_hash() -> None:
    assert compute_hash("a" * 64, _row()) != compute_hash(GENESIS_HASH, _row())


def test_details_strings_and_keys_are_masked_at_any_depth() -> None:
    details = {
        "note": f"card {VISA} cvv 123",
        f"key {VISA}": "x",
        "nested": {"list": [f"{VISA}", ("pin 4821",)]},
    }
    assert mask_details(details) == {
        "note": "card [CARD ****1111] cvv [REDACTED]",
        "key [CARD ****1111]": "x",
        "nested": {"list": ["[CARD ****1111]", ["pin [REDACTED]"]]},
    }


@pytest.mark.parametrize(
    "number",
    [
        int(VISA),
        # Floats: jsonb would store the decimal text "4111111111111111.0" (round-2 review).
        float(VISA),
        4111111111111111.5,
        5555555555554444.0,  # public test card number
    ],
)
def test_a_card_number_given_as_a_number_is_masked(number: float) -> None:
    masked = mask_details({"n": number})["n"]
    assert isinstance(masked, str)
    assert masked.startswith("[CARD ****")


def test_a_long_number_without_a_card_in_it_stays_a_number() -> None:
    # 13+ digits go through masking like text; this one has no Luhn-valid 13-19 digit part.
    assert mask_details({"n": 1_000_000_000_000}) == {"n": 1_000_000_000_000}


@pytest.mark.parametrize(
    "value",
    [
        0,
        -5,
        250,
        999_999_999_999,
        True,
        False,
        None,
        0.83,
        1e-7,
        # A whole float above 2**53 whose 23 digits hold no Luhn-valid 13-19 digit part.
        5.6266053047935685e22,
        "ticket-42",
        "TB-ERR-401",
    ],
)
def test_other_values_are_kept(value: object) -> None:
    assert mask_details({"v": value}) == {"v": value}


def test_mask_details_does_not_change_its_input() -> None:
    details = {"note": VISA}
    mask_details(details)
    assert details == {"note": VISA}


@pytest.mark.parametrize(
    ("details", "message"),
    [
        ({"n": float("nan")}, "finite"),
        ({"n": float("inf")}, "finite"),
        ({"s": "a\x00b"}, "NUL"),
        ({"s": "\ud800"}, "valid Unicode"),
        ({"\ud800": 1}, "valid Unicode"),
        ({VISA: 1, "4111 1111 1111 1111": 2}, "same after masking"),
        ({1: "x"}, "keys must be strings"),
        ({"s": {1, 2}}, "JSON types"),
        ({"d": TS}, "JSON types"),
    ],
)
def test_values_jsonb_cannot_store_are_refused(details: dict[Any, Any], message: str) -> None:
    with pytest.raises(AuditValueError, match=message):
        mask_details(details)


# Text masking found a Luhn-valid run across its dashes ("0b74d[CARD ****8741]-e5b0ef08c44e")
# until P8-03, which keeps a canonical UUID (owner decision, security §3).
UUID_WITH_A_LUHN_RUN = "0b74d664-3640-4097-8741-e5b0ef08c44e"


def test_text_masking_keeps_this_uuid_but_not_a_card_next_to_it() -> None:
    assert mask_text(UUID_WITH_A_LUHN_RUN) == UUID_WITH_A_LUHN_RUN
    assert mask_field(f"{UUID_WITH_A_LUHN_RUN}, card 4111 1111 1111 1111") == (
        f"{UUID_WITH_A_LUHN_RUN}, card [CARD ****1111]"
    )


def test_a_uuid_object_is_kept() -> None:
    value = uuid.UUID(UUID_WITH_A_LUHN_RUN)
    assert mask_id(value) == UUID_WITH_A_LUHN_RUN
    assert mask_details({"ticket": value, "ids": [value]}) == {
        "ticket": UUID_WITH_A_LUHN_RUN,
        "ids": [UUID_WITH_A_LUHN_RUN],
    }


@pytest.mark.parametrize(
    "text",
    [
        UUID_WITH_A_LUHN_RUN,
        UUID_WITH_A_LUHN_RUN.upper(),
        # UUID-shaped strings that hold a full card number (round-2 review): kept since P8-03,
        # an accepted limitation of the owner's decision (security §3).
        "41111111-1111-1111-abcd-ef0123456789",
        "00000000-4111-1111-1111-111100000000",
        "55555555-5555-4444-0000-000000000000",
    ],
)
def test_a_canonical_uuid_string_is_kept_like_a_uuid_object(text: str) -> None:
    assert mask_text(text) == text
    assert mask_id(text) == text
    assert mask_field(text) == text
    assert mask_details({"ref": text}) == {"ref": text}


@pytest.mark.parametrize(
    "text",
    [
        f"ref{UUID_WITH_A_LUHN_RUN}",
        "41111111-1111-1111-abcd-ef0123456789-1",
        "41111111-11111111-abcd-ef0123456789",
    ],
)
def test_a_string_that_is_not_exactly_a_uuid_is_masked_like_any_text(text: str) -> None:
    assert mask_text(text) != text
    assert mask_id(text) == mask_text(text)
    assert mask_field(text) == mask_text(text)
    assert mask_details({"ref": text}) == {"ref": mask_text(text)}


@pytest.mark.parametrize(
    ("text", "expected"),
    [(f"card {VISA}", "card [CARD ****1111]"), ("ticket.claimed", "ticket.claimed"), ("", "")],
)
def test_mask_field(text: str, expected: str) -> None:
    assert mask_field(text) == expected


@pytest.mark.parametrize(("text", "message"), [("a\x00b", "NUL"), ("\udfff", "valid Unicode")])
def test_mask_field_refuses_what_postgres_cannot_store(text: str, message: str) -> None:
    with pytest.raises(AuditValueError, match=message):
        mask_field(text)


def _luhn_check_digit(body: str) -> str:
    return next(check for check in "0123456789" if luhn_valid(body + check))


@st.composite
def cards(draw: st.DrawFn) -> str:
    length = draw(st.integers(min_value=13, max_value=19))
    body = draw(st.text(alphabet="0123456789", min_size=length - 1, max_size=length - 1))
    return body + _luhn_check_digit(body)


# Letters and spaces around the number, so it stays one number (the masking tests cover the
# full range of surrounding text).
words = st.text(alphabet="abc XYZ:", max_size=20)
keys = st.text(alphabet="abcdef_", min_size=1, max_size=8)


@settings(max_examples=300)
@given(
    card=cards(),
    before=words,
    after=words,
    path=st.lists(keys, min_size=1, max_size=4),
    as_key=st.booleans(),
    as_number=st.sampled_from(["no", "int", "float"]),
)
def test_a_card_number_anywhere_in_details_never_reaches_the_hashed_row(
    card: str, before: str, after: str, path: list[str], as_key: bool, as_number: str
) -> None:
    text = f"{before}{card}{after}"
    leaf: object = text
    if as_number != "no" and card[0] != "0":
        leaf = int(card) if as_number == "int" else float(int(card))
    details: dict[str, Any] = {text: "x"} if as_key else {path[-1]: [leaf]}
    for key in reversed(path[:-1]):
        details = {key: details}

    stored = canonical_json(_row(details=mask_details(details)))

    assert card not in stored
    assert card not in json.dumps(_as_jsonb_prints(mask_details(details)))


def _as_jsonb_prints(value: object) -> object:
    """Floats as the decimal text jsonb stores for them, so a card inside one would show."""
    if isinstance(value, dict):
        return {key: _as_jsonb_prints(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_as_jsonb_prints(item) for item in value]
    if isinstance(value, float):
        return format(Decimal(repr(value)), "f")
    return value


def test_a_stored_row_that_cannot_be_hashed_counts_as_broken() -> None:
    """A row made by hand that does not fit the hashed form (here a naive timestamp, which
    timestamptz never returns) must be reported, not crash the check."""
    stored = SimpleNamespace(
        ts=datetime(2026, 11, 13, 10, 0),
        actor_type="system",
        actor_id=None,
        action="x",
        entity_type="x",
        entity_id="x",
        details={},
        prev_hash=GENESIS_HASH,
    )
    assert _stored_hash(cast("Row[Any]", stored)) is None
    assert _stored_hash(cast("Row[Any]", SimpleNamespace(**(vars(stored) | {"ts": TS})))) == (
        compute_hash(
            GENESIS_HASH,
            _row(
                actor_type=ActorType.SYSTEM,
                actor_id=None,
                action="x",
                entity_type="x",
                entity_id="x",
                details={},
            ),
        )
    )
