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
        ("4111\u200b1111\u200b1111\u200b1111", "[CARD ****1111]"),  # zero-width spaces
        ("4111\u00ad1111\u00ad1111\u00ad1111", "[CARD ****1111]"),  # soft hyphens
        ("4111\u22121111\u22121111\u22121111", "[CARD ****1111]"),  # minus signs
        ("4111\uff0d1111\uff0d1111\uff0d1111", "[CARD ****1111]"),  # full-width hyphens
        ("4111\u20131111\u20131111\u20131111", "[CARD ****1111]"),  # en dashes
        ("4111,1111,1111,1111", "[CARD ****1111]"),
        ("4111\u00b71111\u00b71111\u00b71111", "[CARD ****1111]"),  # middle dots
        ("4111 -- 1111 -- 1111 -- 1111", "[CARD ****1111]"),  # gaps of 4 characters
        ("4111     1111     1111     1111", "[CARD ****1111]"),  # gaps of 5 characters
        ("(4111) 1111 1111 1111", "([CARD ****1111]"),
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
        # Fail Luhn, and no 13-19 digit part of them passes either.
        "6011000990139425",  # 16 digits
        "6011 0009 9013 9425",
        "4222222222223",  # 13 digits
        "123456789012",  # 12 digits: too short
        "11111111111111111111",  # 20 digits, no Luhn-valid 13-19 digit part
        "2026-11-13T10:00:00.123456Z",
        "+49 30 1234 5678",
        "order 12345, ticket 678",
        "4111 1111 1111      1111",  # gap of 6 characters: two numbers, not one
        "",
    ],
)
def test_other_numbers_are_kept(text: str) -> None:
    assert mask(text) == MaskResult(text=text, counts={})


@pytest.mark.parametrize(
    ("text", "words_before"),
    [
        ("41111111111111111228", ""),  # card and expiry typed together, 20 digits
        ("4111 1111 1111 1111 12/28", ""),  # "/" is a separator: the expiry joins the run
        ("4111 1111 1111 1111 12/28 737", ""),
        ("3782822463100051228", ""),  # Amex + MMYY, 19 digits, fails Luhn as a whole
        ("4111111111111111737", ""),  # card + CVV
        ("4111111111111112", ""),  # fails Luhn, but its first 15 digits pass
        ("6 4111 1111 1111 1111", ""),
        ("ticket 6 - 4111 1111 1111 1111", "ticket "),
        ("option 2 - 4111 1111 1111 1111", "option "),
        ("112 3782 822463 10005", ""),
    ],
)
def test_run_with_a_card_inside_is_masked_whole(text: str, words_before: str) -> None:
    # A run of 14+ digits that holds a valid card is masked whole (owner decision, P2-03), so
    # never more than 4 of the card's digits stay visible.
    result = mask(text)
    assert re.fullmatch(rf"{words_before}\[CARD \*{{4}}\d{{4}}\]", result.text), result.text
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
        ("and the code on the back is 737", "and the code on the back is [REDACTED]", "CVV"),
        ("cvv737", "cvv[REDACTED]", "CVV"),
        ("cvv **737**", "cvv **[REDACTED]**", "CVV"),
        ("PIN: [4821]", "PIN: [[REDACTED]]", "PIN"),
        ("OTP482913", "OTP[REDACTED]", "2FA"),
        ("Card ending 1111, cvv 737", "Card ending 1111, cvv [REDACTED]", "CVV"),
        # "ending" exempts only card digits from the CVV rules; PIN and 2FA are always masked.
        ("PIN ending 4821", "PIN ending [REDACTED]", "PIN"),
        ("my otp, ending in 482913", "my otp, ending in [REDACTED]", "2FA"),
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


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("card 4111 1111 1111 1111 exp 12/28 737", "card [CARD ****1111] exp 12/28 [REDACTED]"),
        ("[CARD ****1111], 737", "[CARD ****1111], [REDACTED]"),
        ("737 [CARD ****1111]", "[REDACTED] [CARD ****1111]"),
        ("valid until 12/2028 - 7373", "valid until 12/2028 - [REDACTED]"),
        ("12 / 28 737", "12 / 28 [REDACTED]"),
        ("4111 1111 1111 1111 exp 1228 737", "[CARD ****1111] exp 1228 [REDACTED]"),
        ("4111 1111 1111 1111 exp 12-28 737", "[CARD ****1111] exp 12-28 [REDACTED]"),
        ("valid until 1228, 737", "valid until 1228, [REDACTED]"),
        ("expiry: 12-28 737", "expiry: 12-28 [REDACTED]"),
        ("4111 1111 1111 1111 code 737", "[CARD ****1111] code [REDACTED]"),
        ("4111 1111 1111 1111, code: 737", "[CARD ****1111], code: [REDACTED]"),
    ],
)
def test_cvv_directly_next_to_a_card_or_expiry(text: str, expected: str) -> None:
    result = mask(text)
    assert result.text == expected
    assert result.counts.get("CVV") == 1


@pytest.mark.parametrize(
    "text",
    [
        "card ****1111 was charged 250 EUR",
        "[CARD ****1111] was charged 250 EUR",
        "[CARD ****1111] 250 EUR",
        "[CARD ****1111] 250EUR",
        "[CARD ****1111] 250 euros",
        "[CARD ****1111] \u20ac250",
        "[CARD ****1111] \u20ac 250",
        "exp 12/28, 250$",
        "12/28 \u00a3120",
        "USD 250 [CARD ****1111]",
        "[CARD ****1111] was charged 250",  # not directly next to the card
        "12/28 then 737",
        "13/28 737",  # not an expiry date
        "1228 737",  # MMYY counts only after "exp", "expiry" or "valid until"
        "12-28 737",
        "the code 737",  # "code" is a CVV word only right after a masked card
        "[CARD ****1111]: 250.00",  # a decimal part makes it an amount
        "[CARD ****1111] 250,00",
        "on 05/25 120.50",
        "03/14/2026",  # a full date is not an expiry date
        "12/28/2025 1500",
        "2025/12/28 1500",
        "Order 1234: [CARD ****1111]",  # a label before a colon is not a CVV
    ],
)
def test_amounts_and_far_numbers_are_kept(text: str) -> None:
    assert mask(text).counts.get("CVV") is None
    assert mask(text).text == text


@pytest.mark.parametrize(
    "text",
    [
        "IBAN DE89 3704 0044 0532 0130 00",  # public example IBANs (ADR-0010: IBANs are kept)
        "IBAN DE89370400440532013000",
        "GB82 WEST 1234 5698 7654 32",
        "NL91 ABNA 0417 1643 00",
        "FR14 2004 1010 0505 0001 3M02 606",
        "Amounts: 12.50, 8.00, 120.00, 45.99 EUR",  # ", " does not join numbers
        "transactions 1200, 3400, 5600, 7800",
    ],
)
def test_ibans_and_amount_lists_are_kept(text: str) -> None:
    assert mask(text) == MaskResult(text=text, counts={})


def test_card_after_an_iban_is_still_masked() -> None:
    result = mask("IBAN DE89 3704 0044 0532 0130 00 4111 1111 1111 1111")
    assert result.text == "IBAN DE89 3704 0044 0532 0130 00 [CARD ****1111]"
    assert result.counts == {"PAN": 1}


def test_invalid_iban_is_not_protected() -> None:
    # Check digits 00 never pass mod 97, so the card in it is masked as usual.
    assert mask("XX00 4111 1111 1111 1111").text == "XX[CARD ****1111]"


def test_card_and_cvv_together() -> None:
    result = mask("card 4111 1111 1111 1111 exp 12/28 cvv 737")
    assert result.text == "card [CARD ****1111] exp 12/28 cvv [REDACTED]"
    assert result.counts == {"PAN": 1, "CVV": 1}


def test_last_four_of_a_masked_card_are_not_taken_for_a_code() -> None:
    for text in [f"cvv {VISA}", f"{VISA} cvv", f"PIN for {VISA}"]:
        assert "[CARD ****1111]" in mask(text).text


def test_masking_twice_changes_nothing() -> None:
    once = mask(f"card {VISA} exp 12/28, cvv 737, pin 4821, [CARD ****1111] 1234").text
    assert mask(once) == MaskResult(text=once, counts={})


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
    assert mask(f"card {VISA} ok") == MaskResult(
        text="card " + "#" * 16 + " ok", counts={}, failed=True
    )


def test_internal_error_on_non_string_input() -> None:
    class Unprintable:
        def __str__(self) -> str:
            raise ValueError("no")

    result = mask(Unprintable())  # type: ignore[arg-type]
    assert result == MaskResult(text="[REDACTED]", counts={}, failed=True)


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


# Common formats, including invisible and look-alike characters from copy and paste.
SEPARATORS = [
    "",
    " ",
    "-",
    "  ",
    " - ",
    "\u00a0",
    ".",
    ",",
    "\u200b",
    "\u00ad",
    "\u2013",
    "\u2212",
    "\uff0d",
    "\u00b7",
    " -- ",
    "%20",
    "+",
]


@st.composite
def formatted_cards(draw: st.DrawFn) -> tuple[str, str]:
    """A Luhn-valid 13-19 digit number in a common format: (digits, formatted)."""
    length = draw(st.integers(min_value=13, max_value=19))
    body = draw(st.text(alphabet="0123456789", min_size=length - 1, max_size=length - 1))
    digits = body + _luhn_check_digit(body)
    separator = draw(st.sampled_from(SEPARATORS))
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


# Digits right next to the card, with separators between: the whole run must go, so at most
# the 4 digits inside the placeholder are visible (B1/B2 of the P2-03 review).
words = st.text(alphabet="abcdefghijklmnopqrstuvwxyz ", max_size=20)
digit_groups = st.text(alphabet="0123456789", min_size=1, max_size=4)
short_separators = st.sampled_from([" ", "-", " - ", ",", "/", "\u00a0", "  "])


@settings(max_examples=500)
@given(
    card=formatted_cards(),
    before=digit_groups,
    after=digit_groups,
    sep_before=short_separators,
    sep_after=short_separators,
    head=words,
    tail=words,
)
def test_card_with_digits_around_it_is_masked_whole(
    card: tuple[str, str],
    before: str,
    after: str,
    sep_before: str,
    sep_after: str,
    head: str,
    tail: str,
) -> None:
    _digits, formatted = card
    text = f"{head}x{before}{sep_before}{formatted}{sep_after}{after}x{tail}"
    result = mask(text)
    assert re.fullmatch(r"[a-z ]*x\[CARD \*{4}\d{4}\]x[a-z ]*", result.text), result.text
    assert result.counts == {"PAN": 1}
