"""Card-data masking (G1): card numbers, CVV, PIN and 2FA codes never survive.

Only public test card numbers are used.
"""

import re

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from hub.guardrails import masking
from hub.guardrails.masking import MaskResult, luhn_valid, mask, mask_text

VISA = "4111111111111111"
# The same number in Arabic-Indic digits.
ARABIC_INDIC_VISA = "\u0664" + "\u0661" * 15


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("4111111111111111", "[CARD ****1111]"),  # no separators
        ("4111 1111 1111 1111", "[CARD ****1111]"),
        ("4111-1111-1111-1111", "[CARD ****1111]"),
        ("4111  1111  1111  1111", "[CARD ****1111]"),
        ("4111 - 1111 - 1111 - 1111", "[CARD ****1111]"),
        ("4111\u00a01111\u00a01111\u00a01111", "[CARD ****1111]"),  # no-break spaces
        ("4111+1111+1111+1111", "[CARD ****1111]"),  # form-encoded
        ("4111%201111%201111%201111", "[CARD ****1111]"),  # URL-encoded
        ("4111%2D1111%2d1111%2B1111", "[CARD ****1111]"),
        ("4111.1111.1111.1111", "[CARD ****1111]"),
        ("card4111111111111111end", "card[CARD ****1111]end"),  # in the middle of a word
        ("4222222222222", "[CARD ****2222]"),  # 13 digits
        ("30569309025904", "[CARD ****5904]"),  # 14 digits
        ("3782 822463 10005", "[CARD ****0005]"),  # 15 digits, Amex grouping
        ("5555555555554444", "[CARD ****4444]"),  # 16 digits
        ("6205 5000 0000 0000 004", "[CARD ****0004]"),  # 19 digits
    ],
)
def test_card_numbers_are_masked(text: str, expected: str) -> None:
    result = mask(f"paid with {text} today")
    assert result.text == f"paid with {expected} today"
    assert result.counts == {"PAN": 1}


def test_several_numbers_in_one_text() -> None:
    result = mask("first 4111 1111 1111 1111, then 5555-5555-5555-4444 and 378282246310005.")
    assert result.text == "first [CARD ****1111], then [CARD ****4444] and [CARD ****0005]."
    assert result.counts == {"PAN": 3}


@pytest.mark.parametrize(
    "text",
    [
        "4111111111111112",  # 16 digits, fails Luhn
        "4111 1111 1111 1112",
        "4222222222223",  # 13 digits, fails Luhn
        "123456789012",  # 12 digits: too short
        "11111111111111111111",  # 20 digits, no Luhn-valid 13-19 digit part
        "2026-11-13T10:00:00.123456Z",
        "+49 30 1234 5678",
        "order 12345, ticket 678",
        "",
    ],
)
def test_other_numbers_are_kept(text: str) -> None:
    assert mask(text) == MaskResult(text=text, counts={})


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # Card and expiry typed together: the whole run goes, last 4 of the card are shown.
        ("41111111111111111228", "[CARD ****1111]"),
        # A separate expiry or a number in front stays; only the card goes.
        ("4111 1111 1111 1111 12/28", "[CARD ****1111] 12/28"),
        ("option 2 - 4111 1111 1111 1111", "option 2 - [CARD ****1111]"),
    ],
)
def test_card_next_to_other_digits(text: str, expected: str) -> None:
    result = mask(text)
    assert result.text == expected
    assert result.counts == {"PAN": 1}


@pytest.mark.parametrize(
    ("text", "expected", "entity"),
    [
        ("my cvv is 123", "my cvv is [REDACTED]", "CVV"),
        ("CVC2: 4567", "CVC2: [REDACTED]", "CVV"),
        ("security code on the back: 737", "security code on the back: [REDACTED]", "CVV"),
        ("123 is the CVV", "[REDACTED] is the CVV", "CVV"),
        ("PIN: 4821", "PIN: [REDACTED]", "PIN"),
        ("my pin code is 482193", "my pin code is [REDACTED]", "PIN"),
        ("4821 is my PIN", "[REDACTED] is my PIN", "PIN"),
        ("2FA 482913", "2FA [REDACTED]", "2FA"),
        ("the OTP was 48291374", "the OTP was [REDACTED]", "2FA"),
        ("your one-time code: 482913", "your one-time code: [REDACTED]", "2FA"),
        ("verification code 4829", "verification code [REDACTED]", "2FA"),
        ("SMS code - 482913", "SMS code - [REDACTED]", "2FA"),
        ("authenticator code 482913", "authenticator code [REDACTED]", "2FA"),
    ],
)
def test_codes_next_to_trigger_words(text: str, expected: str, entity: str) -> None:
    result = mask(text)
    assert result.text == expected
    assert result.counts == {entity: 1}


@pytest.mark.parametrize(
    "text",
    [
        "cvv 12",  # too short for a CVV
        "cvv 12345",  # too long for a CVV
        "I paid 123 euros",  # no trigger word
        "pinned 4821",  # "pin" only as a whole word
        "my cvv is\n123",  # not across a line break
        "cvv for my card, which I opened in the branch near the station, 123",  # too far
        "error ERR-5003",  # an error code is not a 2FA code
    ],
)
def test_codes_without_a_trigger_are_kept(text: str) -> None:
    assert mask(text) == MaskResult(text=text, counts={})


def test_card_and_cvv_together() -> None:
    result = mask("card 4111 1111 1111 1111 exp 12/28 cvv 737")
    assert result.text == "card [CARD ****1111] exp 12/28 cvv [REDACTED]"
    assert result.counts == {"PAN": 1, "CVV": 1}


def test_last_four_of_a_masked_card_are_not_taken_for_a_code() -> None:
    for text in [f"cvv {VISA}", f"{VISA} cvv", f"PIN for {VISA}"]:
        assert "[CARD ****1111]" in mask(text).text


def test_mask_text_returns_only_the_text() -> None:
    assert mask_text(f"card {VISA}") == "card [CARD ****1111]"


@pytest.mark.parametrize(
    "text",
    [
        "😀 4111 1111 1111 1111 😀",
        "\x00\x1f\u202e4111111111111111",
        ARABIC_INDIC_VISA,  # Arabic-Indic digits are digits too
        "\ud800 lone surrogate",
        "1" * 5000,  # very long run: masked without checking every window
        "4111 " * 20_000,
    ],
)
def test_odd_input_never_raises(text: str) -> None:
    result = mask(text)
    assert VISA not in result.text
    assert "1" * 20 not in result.text


def test_arabic_indic_card_is_masked() -> None:
    assert mask(ARABIC_INDIC_VISA).text == "[CARD ****" + "\u0661" * 4 + "]"


def test_non_string_input_is_converted() -> None:
    assert mask(4111111111111111).text == "[CARD ****1111]"  # type: ignore[arg-type]


def test_internal_error_masks_every_digit(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(_text: str, _counts: object) -> str:
        raise RuntimeError("bug")

    monkeypatch.setattr(masking, "_mask_cards", broken)
    assert mask(f"card {VISA} ok") == MaskResult(text="card " + "#" * 16 + " ok", counts={})


def test_internal_error_on_non_string_input() -> None:
    class Unprintable:
        def __str__(self) -> str:
            raise ValueError("no")

    assert mask(Unprintable()) == MaskResult(text="[REDACTED]", counts={})  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("digits", "valid"),
    [(VISA, True), ("4111111111111112", False), ("0", True), ("", False), ("79927398713", True)],
)
def test_luhn(digits: str, valid: bool) -> None:
    assert luhn_valid(digits) is valid


# --- property test ---------------------------------------------------------------------------


def _luhn_check_digit(body: str) -> str:
    for check in "0123456789":
        if luhn_valid(body + check):
            return check
    raise AssertionError("unreachable")  # pragma: no cover


@st.composite
def formatted_cards(draw: st.DrawFn) -> tuple[str, str]:
    """A Luhn-valid 13-19 digit number in a common format: (digits, formatted)."""
    length = draw(st.integers(min_value=13, max_value=19))
    body = draw(st.text(alphabet="0123456789", min_size=length - 1, max_size=length - 1))
    digits = body + _luhn_check_digit(body)
    separator = draw(st.sampled_from(["", " ", "-", "  ", " - ", "\u00a0", "."]))
    group = draw(st.sampled_from([4, 3, 5, 6]))
    formatted = separator.join(digits[i : i + group] for i in range(0, len(digits), group))
    return digits, formatted


# Random text around the number. The character right before and after it is not a digit, so the
# number is still one number. "%" is excluded before it: "%20" is read as a URL-encoded space.
prefixes = st.text(max_size=40).filter(lambda s: not re.search(r"[\d%]\Z", s))
suffixes = st.text(max_size=40).filter(lambda s: not re.match(r"\d", s))


@settings(max_examples=500)
@given(card=formatted_cards(), prefix=prefixes, suffix=suffixes)
def test_any_luhn_valid_number_in_random_text_is_masked(
    card: tuple[str, str], prefix: str, suffix: str
) -> None:
    digits, formatted = card
    result = mask(prefix + formatted + suffix)
    assert digits not in result.text
    assert formatted not in result.text
    assert result.counts.get("PAN", 0) >= 1
