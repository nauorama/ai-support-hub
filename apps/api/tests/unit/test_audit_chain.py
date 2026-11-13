"""Audit hash chain and details masking (hub.audit.chain): pure parts, no database."""

import hashlib
import json
import re
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from hub.audit.chain import (
    GENESIS_HASH,
    AuditDetailsError,
    AuditRow,
    canonical_json,
    compute_hash,
    mask_details,
)
from hub.domain.enums import ActorType
from hub.guardrails.masking import luhn_valid

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


def test_a_card_number_given_as_a_number_is_masked() -> None:
    assert mask_details({"n": int(VISA)}) == {"n": "[CARD ****1111]"}


def test_a_long_number_without_a_card_in_it_stays_a_number() -> None:
    # 13+ digits go through masking like text; this one has no Luhn-valid 13-19 digit part.
    assert mask_details({"n": 1_000_000_000_000}) == {"n": 1_000_000_000_000}


@pytest.mark.parametrize(
    "value",
    [0, -5, 250, 999_999_999_999, True, False, None, 0.83, "ticket-42", "TB-ERR-401"],
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
        ({1: "x"}, "keys must be strings"),
        ({"s": {1, 2}}, "JSON types"),
        ({"d": TS}, "JSON types"),
    ],
)
def test_values_jsonb_cannot_store_are_refused(details: dict[Any, Any], message: str) -> None:
    with pytest.raises(AuditDetailsError, match=message):
        mask_details(details)


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
    as_number=st.booleans(),
)
def test_a_card_number_anywhere_in_details_never_reaches_the_hashed_row(
    card: str, before: str, after: str, path: list[str], as_key: bool, as_number: bool
) -> None:
    text = f"{before}{card}{after}"
    leaf: object = int(card) if as_number and card[0] != "0" else text
    details: dict[str, Any] = {text: "x"} if as_key else {path[-1]: [leaf]}
    for key in reversed(path[:-1]):
        details = {key: details}

    stored = canonical_json(_row(details=mask_details(details)))

    assert card not in stored
    assert card not in json.dumps(mask_details(details))
