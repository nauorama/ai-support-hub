"""Card-data masking (G1): card numbers, CVV, PIN and 2FA codes never survive.

Only public test card numbers are used.
"""

import re
import unicodedata

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


# Pre-H2 #2: an exact error code next to a PIN or 2FA word is kept, so triage sees it.
@pytest.mark.parametrize(
    "text",
    [
        "ERR-3007 wrong PIN",
        "Wrong PIN: ERR-3007",
        "ERR-3003: after 3 wrong PINs the PIN is blocked",
        "ERR-3003 PIN blocked",
        "my pin code gives ERR-3003",
        "ERR-5003 2FA code invalid",
        "the OTP fails with ERR-5003",
        "ERR-5004: 2FA not set up",
        "(ERR-3007) PIN",
        "ERR-3007 and ERR-3003 after a wrong PIN",
    ],
)
def test_error_codes_next_to_pin_or_2fa_words_are_kept(text: str) -> None:
    assert mask(text) == MaskResult(text=text, counts={})


@pytest.mark.parametrize(
    ("text", "expected", "counts"),
    [
        ("my PIN is 3007", "my PIN is [REDACTED]", {"PIN": 1}),
        ("ERR-30071 PIN", "ERR-[REDACTED] PIN", {"PIN": 1}),  # 5 digits: not an error code
        ("ERR-300712 OTP", "ERR-[REDACTED] OTP", {"2FA": 1}),
        ("err-3007 wrong PIN", "err-[REDACTED] wrong PIN", {"PIN": 1}),  # uppercase only
        ("Err-3007 PIN", "Err-[REDACTED] PIN", {"PIN": 1}),
        ("XERR-3007 PIN", "XERR-[REDACTED] PIN", {"PIN": 1}),  # "ERR" inside a longer word
        ("ERR 3007 PIN", "ERR [REDACTED] PIN", {"PIN": 1}),  # no dash
        ("ERR3007 PIN", "ERR[REDACTED] PIN", {"PIN": 1}),
        # Only ASCII digits make an error code (review round 1).
        ("ERR-\uff11\uff12\uff13\uff14 PIN", "ERR-[REDACTED] PIN", {"PIN": 1}),  # full-width
        ("ERR-\u0661\u0662\u0663\u0664 PIN", "ERR-[REDACTED] PIN", {"PIN": 1}),  # Arabic-Indic
        ("ERR-\u0967\u0968\u0969\u096a OTP", "ERR-[REDACTED] OTP", {"2FA": 1}),  # Devanagari
        ("ERR-1234\u0665 PIN", "ERR-[REDACTED] PIN", {"PIN": 1}),  # a fifth, non-ASCII digit
        # No word character directly before "ERR" (review round 1).
        ("foo_ERR-1234 PIN", "foo_ERR-[REDACTED] PIN", {"PIN": 1}),
        ("_ERR-1234 PIN", "_ERR-[REDACTED] PIN", {"PIN": 1}),
        ("9ERR-1234 PIN", "9ERR-[REDACTED] PIN", {"PIN": 1}),
        (
            "ERR-3007 wrong PIN, my PIN is 1234",
            "ERR-3007 wrong PIN, my PIN is [REDACTED]",
            {"PIN": 1},
        ),
        ("OTP 482913, ERR-5003", "OTP [REDACTED], ERR-5003", {"2FA": 1}),
        # The CVV rule has no error-code exception (owner decision, pre-H2 #2).
        ("security code ERR-3007", "security code ERR-[REDACTED]", {"CVV": 1}),
    ],
)
def test_codes_that_are_not_exact_error_codes_are_still_masked(
    text: str, expected: str, counts: dict[str, int]
) -> None:
    assert mask(text) == MaskResult(text=expected, counts=counts)


# Interactions with the other rules (review round 1).
@pytest.mark.parametrize(
    ("text", "expected", "counts"),
    [
        (
            "DE89 3704 0044 0532 0130 00 ERR-1234 PIN",
            "[IBAN DE89 \u20263000] ERR-1234 PIN",
            {"IBAN": 1},
        ),
        ("ERR-4111 1111 1111 1111 PIN", "ERR-[CARD ****1111] PIN", {"PAN": 1}),
        ("card ending ERR-1234 PIN", "card ending ERR-1234 PIN", {}),
        ("card ending 1234 PIN", "card ending [REDACTED] PIN", {"PIN": 1}),
        # Known limitation (security §3): a trigger reaches only the first number after it.
        ("PIN ERR-1234 5678", "PIN ERR-1234 5678", {}),
    ],
)
def test_error_codes_with_other_rules(text: str, expected: str, counts: dict[str, int]) -> None:
    result = mask(text)
    assert result == MaskResult(text=expected, counts=counts)
    assert mask(result.text).text == result.text  # masking twice changes nothing


@pytest.mark.parametrize(
    "text",
    [
        "ERR-3007 wrong PIN",
        "ERR-30071 PIN",
        "ERR-\uff11\uff12\uff13\uff14 PIN",
        "_ERR-1234 PIN",
        "ERR-3007 wrong PIN, my PIN is 1234",
    ],
)
def test_error_code_inputs_mask_the_same_twice(text: str) -> None:
    once = mask(text).text
    assert mask(once).text == once


@settings(max_examples=200)
@given(
    digits=st.text(alphabet="0123456789", min_size=4, max_size=4),
    trigger=st.sampled_from(["PIN", "pin code", "2FA", "OTP", "verification code"]),
)
def test_any_exact_error_code_is_kept_next_to_a_trigger(digits: str, trigger: str) -> None:
    for text in (f"ERR-{digits} wrong {trigger}", f"{trigger} failed: ERR-{digits}"):
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
        "Amounts: 12.50, 8.00, 120.00, 45.99 EUR",  # ", " does not join numbers
        "transactions 1200, 3400, 5600, 7800",
    ],
)
def test_amount_lists_are_kept(text: str) -> None:
    assert mask(text) == MaskResult(text=text, counts={})


# Was "IBANs are kept" (ADR-0010). ADR-0017: every valid IBAN keeps only its country code,
# check digits and last 4 characters. Stricter than before: no IBAN is stored in full.
@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("IBAN DE89 3704 0044 0532 0130 00", "IBAN [IBAN DE89 \u20263000]"),
        ("IBAN DE89370400440532013000", "IBAN [IBAN DE89 \u20263000]"),
        ("GB82 WEST 1234 5698 7654 32", "[IBAN GB82 \u20265432]"),
        ("NL91 ABNA 0417 1643 00", "[IBAN NL91 \u20264300]"),
        ("FR14 2004 1010 0505 0001 3M02 606", "[IBAN FR14 \u20262606]"),
        # SWIFT IBAN Registry examples, release 101
        ("AT61 1904 3002 3457 3201", "[IBAN AT61 \u20263201]"),
        ("BE68 5390 0754 7034", "[IBAN BE68 \u20267034]"),
        ("DK50 0040 0440 1162 43", "[IBAN DK50 \u20266243]"),
        ("FI21 1234 5600 0007 85", "[IBAN FI21 \u20260785]"),
        ("NO93 8601 1117 947", "[IBAN NO93 \u20267947]"),
        ("EE38 2200 2210 2014 5685", "[IBAN EE38 \u20265685]"),
        ("SI56 2633 0001 2039 086", "[IBAN SI56 \u20269086]"),
        ("HR12 1001 0051 8630 0016 0", "[IBAN HR12 \u20260160]"),
        ("CH93 0076 2011 6238 5295 7", "[IBAN CH93 \u20262957]"),
        ("GB29 NWBK 6016 1331 9268 19", "[IBAN GB29 \u20266819]"),
        ("LT12 1000 0111 0100 1000", "[IBAN LT12 \u20261000]"),
        ("pay to DE89 3704 0044 0532 0130 00, thanks", "pay to [IBAN DE89 \u20263000], thanks"),
    ],
)
def test_ibans_are_partly_masked(text: str, expected: str) -> None:
    assert mask(text) == MaskResult(text=expected, counts={"IBAN": 1})


def test_card_after_an_iban_is_still_masked() -> None:
    # Was "IBAN DE89 3704 0044 0532 0130 00 [CARD ****1111]". Stricter now: the IBAN is masked,
    # and its last 4 are hidden too because digits follow it (ADR-0017).
    result = mask("IBAN DE89 3704 0044 0532 0130 00 4111 1111 1111 1111")
    assert result.text == "IBAN [IBAN DE89 \u2026] [CARD ****1111]"
    assert result.counts == {"IBAN": 1, "PAN": 1}


def test_card_after_an_iban_and_a_non_separator_keeps_the_last_four() -> None:
    # ";" does not join numbers; "3000" is not taken for a CVV next to the card.
    result = mask("DE89 3704 0044 0532 0130 00; 4111 1111 1111 1111")
    assert result.text == "[IBAN DE89 \u20263000]; [CARD ****1111]"
    assert result.counts == {"IBAN": 1, "PAN": 1}


def test_invalid_iban_is_not_protected() -> None:
    # Check digits 00 never pass mod 97, so the card in it is masked as usual.
    assert mask("XX00 4111 1111 1111 1111").text == "XX[CARD ****1111]"


def test_card_after_two_capital_letters_is_masked_even_when_mod_97_passes() -> None:
    # Found by Hypothesis in test_audit_chain.py (P3-01 review): "YZ" + a 15-digit card passes
    # the IBAN mod-97 check, but YZ is no IBAN country (ADR-0017).
    assert masking._iban_valid("YZ178162840989986")
    result = mask("YZ178162840989986")
    assert result.text == "YZ[CARD ****9986]"
    assert result.counts == {"PAN": 1}


@pytest.mark.parametrize(
    "text",
    [
        "XY12 3704 0044 0532 0130 00",  # no such country
        "DE89 3704 0044 0532 0130",  # DE has 22 characters, not 18
        "DE893704004405320130001",  # nor 23
    ],
)
def test_iban_with_unknown_country_or_wrong_length_is_checked_as_digits(text: str) -> None:
    # Not an IBAN, so its digits go through the card check (here a Luhn-valid part is found).
    result = mask(text)
    assert "IBAN" not in result.counts
    assert "[IBAN" not in result.text
    assert result.text == text[:2] + mask_text(text[2:])


def test_digits_after_an_iban_hide_its_last_four() -> None:
    # A valid IBAN, then " 1": digits follow, so the last 4 are hidden. Fewer than 5 digits
    # follow, so no card across the IBAN's end is looked for and "1" stays (round 2, N3).
    assert mask("DE89 3704 0044 0532 0130 00 1") == MaskResult(
        text="[IBAN DE89 \u2026] 1", counts={"IBAN": 1}
    )


def test_an_invalid_candidate_does_not_hide_a_valid_iban_inside_it() -> None:
    assert mask("XY12 DE89 3704 0044 0532 0130 00").text == "XY12 [IBAN DE89 \u20263000]"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # Card + 2 digits forming a valid DE IBAN (P3-01 review round 1).
        ("DE68 4111 1111 1111 1111 01", "[IBAN DE68 \u20261101]"),
        ("DE68411111111111111101", "[IBAN DE68 \u20261101]"),
        ("IBAN: DE68 4111 1111 1111 1111 01, thanks", "IBAN: [IBAN DE68 \u20261101], thanks"),
        # Card, CVV and padding inside a valid FR IBAN.
        ("FR22 4111 1111 1111 1111 7370 000", "[IBAN FR22 \u20260000]"),
        # Amex inside a valid CZ IBAN.
        ("CZ3437828224631000500000", "[IBAN CZ34 \u20260000]"),
        ("CZ3000000378282246310005", "[IBAN CZ30 \u20260005]"),
        # A card that starts in the IBAN's last group and goes on after it: shown without
        # digits, since its last 4 cannot be told apart (round 2, B2).
        ("AT77 1234 5678 9012 4111 1111 1111 1111", "[IBAN AT77 \u2026] [CARD ****]"),
        ("AT86 0000 0000 0000 4111 1111 1111 1111", "[IBAN AT86 \u2026] [CARD ****]"),
    ],
)
def test_card_inside_or_across_an_iban_never_survives(text: str, expected: str) -> None:
    assert mask(text).text == expected
    assert mask(expected).text == expected


# Every example input of the PR #14 review, round 2 (B1, B2, N2, N3).
_A = "\u0661"  # ARABIC-INDIC DIGIT ONE
_F = "\uff11"  # FULLWIDTH DIGIT ONE


@pytest.mark.parametrize(
    ("text", "expected", "counts"),
    [
        # B1: non-ASCII digits right after an IBAN belong to the same number.
        (
            f"AT77 1234 5678 9012 4111{_A * 12}",
            "[IBAN AT77 \u2026][CARD ****]",
            {"IBAN": 1, "PAN": 1},
        ),
        (
            f"AT77 1234 5678 9012 4111{_F * 12}",
            "[IBAN AT77 \u2026][CARD ****]",
            {"IBAN": 1, "PAN": 1},
        ),
        (
            f"AT77 1234 5678 9012 4111{_F * 4} {_F * 4} {_F * 4}",
            "[IBAN AT77 \u2026][CARD ****]",
            {"IBAN": 1, "PAN": 1},
        ),
        (f"AT771234567890124111{_A * 12}", "[IBAN AT77 \u2026][CARD ****]", {"IBAN": 1, "PAN": 1}),
        (f"x 4111{_A * 12}", f"x [CARD ****{_A * 4}]", {"PAN": 1}),  # the same without an IBAN
        # B2: a code after an IBAN is a code, not a card; no IBAN or code digits are shown.
        (
            "DE89 3704 0044 0532 0130 00 1234 pin",
            "[IBAN DE89 \u2026] [REDACTED] pin",
            {"IBAN": 1, "PIN": 1},
        ),
        (
            "DE89 3704 0044 0532 0130 00 482913 is my OTP",
            "[IBAN DE89 \u2026] [REDACTED] is my OTP",
            {"IBAN": 1, "2FA": 1},
        ),
        ("482913 is my OTP", "[REDACTED] is my OTP", {"2FA": 1}),
        (
            "IBAN DE89 3704 0044 0532 0130 00 - 250 EUR",
            "IBAN [IBAN DE89 \u2026] - 250 EUR",
            {"IBAN": 1},
        ),
        # N2: only exactly 4 characters and "]" after "[IBAN XX00 \u2026" are a masked IBAN.
        ("[IBAN DE89 \u2026123456] otp", "[IBAN DE89 \u2026[REDACTED]] otp", {"2FA": 1}),
        ("[IBAN DE89 \u2026737] cvv", "[IBAN DE89 \u2026[REDACTED]] cvv", {"CVV": 1}),
        (
            "card 4111 1111 1111 1111 [IBAN DE89 \u2026737]",
            "card [CARD ****1111] [IBAN DE89 \u2026737]",
            {"PAN": 1},
        ),
        # N3
        ("DE89 3704 0044 0532 0130 00 1", "[IBAN DE89 \u2026] 1", {"IBAN": 1}),
    ],
)
def test_review_round_2_examples(text: str, expected: str, counts: dict[str, int]) -> None:
    assert mask(text) == MaskResult(text=expected, counts=counts)
    assert mask(expected).text == expected


def test_a_masked_card_without_digits_still_marks_a_cvv_next_to_it() -> None:
    assert (
        mask("[IBAN AT77 \u2026] [CARD ****] 737").text
        == "[IBAN AT77 \u2026] [CARD ****] [REDACTED]"
    )


@pytest.mark.parametrize(
    ("digits", "expected"),
    [
        ("12345", "[CARD ****]"),  # Luhn-valid numbers cross the IBAN's end
        ("48291337", "[CARD ****]"),
        ("123456789012", "[CARD ****]"),
        ("00010", "00010"),  # none does
        ("1234", "1234"),  # fewer than 5 digits: not looked for
        ("0", "0"),  # a single "0" passes Luhn on its own; it is no card
    ],
)
def test_digits_after_an_iban_are_a_card_only_when_luhn_crosses_the_end(
    digits: str, expected: str
) -> None:
    assert mask(f"DE89 3704 0044 0532 0130 00 {digits}").text == f"[IBAN DE89 \u2026] {expected}"


def test_masked_iban_is_not_masked_again() -> None:
    once = mask("IBAN DE89 3704 0044 0532 0130 00, card 4111 1111 1111 1111, cvv 737").text
    assert once == "IBAN [IBAN DE89 \u20263000], card [CARD ****1111], cvv [REDACTED]"
    assert mask(once) == MaskResult(text=once, counts={})
    joined = "[IBAN DE89 \u20263000] [CARD ****1111]"
    assert mask(joined) == MaskResult(text=joined, counts={})


def test_iban_and_card_entities_are_counted_apart() -> None:
    assert "IBAN" not in masking.CARD_ENTITIES
    assert {"PAN", "CVV", "PIN", "2FA"} == masking.CARD_ENTITIES


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


# ADR-0017: a Luhn-valid number hidden at any position of a valid IBAN (registry country, exact
# length, computed check digits), possibly running past its end, never survives (finding 3 of
# the P3-01 masking review, round 1).
_DIGITS = "0123456789"
_DIGIT_SCRIPTS = {
    "ascii": str.maketrans("", ""),
    "arabic-indic": str.maketrans(_DIGITS, "".join(chr(0x0660 + i) for i in range(10))),
    "fullwidth": str.maketrans(_DIGITS, "".join(chr(0xFF10 + i) for i in range(10))),
}
_IBAN_JOINERS = [" ", "-", "/", "", "  ", "\u00a0", "%20", "\u200b", "\u200d"]
# Separators a card may be written with; removed before looking for visible card digits.
_VISIBLE_SEPARATORS = re.compile(r"%20|[\s\-/\u200b\u200d]")


@st.composite
def ibans_with_a_card(draw: st.DrawFn) -> tuple[str, str]:
    """(card digits, text): an IBAN whose account part holds a card from any offset."""
    country, length = draw(st.sampled_from(sorted(masking._IBAN_LENGTHS.items())))
    bban_length = length - 4
    card, _formatted = draw(formatted_cards())
    offset = draw(st.integers(min_value=0, max_value=bban_length - 1))
    inside = card[: bban_length - offset]
    outside = card[len(inside) :]
    before = draw(st.text(alphabet=_DIGITS, min_size=offset, max_size=offset))
    fill = bban_length - offset - len(inside)
    after = draw(st.text(alphabet=_DIGITS, min_size=fill, max_size=fill))
    bban = before + inside + after
    iban = f"{country}{_mod_97_check_digits(country, bban)}{bban}"
    if draw(st.booleans()):
        iban = " ".join(iban[i : i + 4] for i in range(0, len(iban), 4))
    if outside:
        # Round 2, N1: other scripts and joiners, and digits glued straight to the IBAN.
        script = draw(st.sampled_from(sorted(_DIGIT_SCRIPTS)))
        outside = outside.translate(_DIGIT_SCRIPTS[script])
        joiner = draw(st.sampled_from(_IBAN_JOINERS))
    else:
        joiner = ""
    return card, f"{iban}{joiner}{outside}"


def _mod_97_check_digits(country: str, bban: str) -> str:
    """Check digits that make country + check digits + bban pass mod 97."""
    return next(
        f"{check:02d}" for check in range(100) if masking._iban_valid(f"{country}{check:02d}{bban}")
    )


@settings(max_examples=1000)
@given(case=ibans_with_a_card(), head=words, tail=words)
def test_a_card_hidden_anywhere_in_a_valid_iban_never_survives(
    case: tuple[str, str], head: str, tail: str
) -> None:
    card, iban = case
    result = mask(f"{head} {iban} {tail}")
    # No 5 or more card digits in a row survive, in any script, with any separators between.
    visible = _VISIBLE_SEPARATORS.sub("", result.text)
    ascii_runs = re.findall(r"\d+", "".join(str(unicodedata.digit(c, c)) for c in visible))
    for run in ascii_runs:
        for start in range(len(run) - 4):
            assert run[start : start + 5] not in card, result.text
    assert result.counts.get("IBAN", 0) + result.counts.get("PAN", 0) >= 1
    assert mask(result.text).text == result.text


# --- canonical UUIDs (owner decision, P8-03) ---------------------------------------------------

# Text masking used to make this "0b74d[CARD ****8741]-e5b0ef08c44e" (a Luhn-valid run across
# its dashes).
UUID_WITH_A_LUHN_RUN = "0b74d664-3640-4097-8741-e5b0ef08c44e"


@pytest.mark.parametrize(
    "text",
    [
        UUID_WITH_A_LUHN_RUN,
        f"ticket {UUID_WITH_A_LUHN_RUN} claimed",
        f'{{"ticket_id": "{UUID_WITH_A_LUHN_RUN}"}}',
        f"id={UUID_WITH_A_LUHN_RUN}.",
        UUID_WITH_A_LUHN_RUN.upper(),
        f"{UUID_WITH_A_LUHN_RUN} {UUID_WITH_A_LUHN_RUN}",
    ],
)
def test_a_canonical_uuid_stays_readable(text: str) -> None:
    result = mask(text)
    assert result.text == text
    assert result.counts == {}


@pytest.mark.parametrize(
    "text",
    [
        f"{UUID_WITH_A_LUHN_RUN} card 4111 1111 1111 1111",
        f"card 4111 1111 1111 1111, ticket {UUID_WITH_A_LUHN_RUN}",
        f"{UUID_WITH_A_LUHN_RUN}\nCard: 4111-1111-1111-1111",
    ],
)
def test_a_card_next_to_a_uuid_is_still_masked(text: str) -> None:
    result = mask(text)
    assert UUID_WITH_A_LUHN_RUN in result.text
    assert "4111" not in result.text.replace(UUID_WITH_A_LUHN_RUN, "")
    assert result.counts == {"PAN": 1}


@pytest.mark.parametrize(
    "text",
    [
        # A digit within a separator of the UUID: a card could be split across its edge.
        f"4111 1111 1111 {UUID_WITH_A_LUHN_RUN}",
        f"{UUID_WITH_A_LUHN_RUN} 1",
        # Joined to a letter, digit, dash or underscore: not exactly a UUID.
        f"x{UUID_WITH_A_LUHN_RUN}",
        f"{UUID_WITH_A_LUHN_RUN}-1",
        f"_{UUID_WITH_A_LUHN_RUN}",
        # Not the 8-4-4-4-12 shape.
        "0b74d6643640-4097-8741-e5b0ef08c44e",
    ],
)
def test_a_uuid_that_does_not_stand_alone_is_masked_like_text(text: str) -> None:
    assert mask(text).counts.get("PAN") == 1


def test_a_card_in_the_shape_of_a_uuid_is_kept() -> None:
    """Accepted limitation (security §3, P8-03)."""
    text = "41111111-1111-1111-1111-111111111111"
    assert mask(text).text == text


def test_a_code_after_a_uuid_is_still_redacted() -> None:
    assert mask(f"{UUID_WITH_A_LUHN_RUN} PIN 1234").text == f"{UUID_WITH_A_LUHN_RUN} PIN [REDACTED]"
