"""Card-data masking (G1, docs/security.md §3).

Card numbers (PAN) become ``[CARD ****1111]``. CVV, PIN and 2FA codes next to their trigger
words, and CVV-like numbers directly next to a masked card or an expiry date, become
``[REDACTED]``. The same function masks customer text, operator text, log records and traces
(G9), so it must never raise: on an internal error it masks every digit instead.

Pure functions; no imports from the rest of the project.
"""

import re
import unicodedata
from collections import Counter
from typing import Literal

from pydantic import BaseModel, ConfigDict

EntityType = Literal["PAN", "CVV", "PIN", "2FA"]

REDACTED = "[REDACTED]"

_MIN_PAN = 13
_MAX_PAN = 19
# A run this long is not a card someone typed; checking every window would cost too much.
# The total input size is limited by the API (P2-06), not here.
_MAX_RUN_TO_CHECK = 1000
# Window lengths, most common card lengths first, so the last 4 digits shown are most likely
# the card's own (e.g. a card typed with its CVV or expiry right after it).
_WINDOW_LENGTHS = (16, 15, 19, 13, 14, 17, 18)

# Characters that may stand between the digits of one card number, up to five in a row.
# Wider than "spaces or dashes" on purpose: text is pasted from apps, PDFs and web pages, and
# the same mask cleans logs, where URLs and form-encoded bodies appear. Square brackets and
# asterisks are not separators, so ``[CARD ****1111]`` never joins a following number.
_SEPARATOR_CHARS = (
    r"\s"  # any whitespace, including NBSP and the ideographic space
    r"\-+./_()"
    r"\u00ad\u00b7"  # soft hyphen, middle dot
    r"\u200b-\u200d\u2060\ufeff"  # zero-width space, joiners, word joiner, BOM
    r"\u2010-\u2015\u2212\ufe63\uff0d"  # Unicode dashes, minus sign, small and full-width hyphen
)
_ENCODED_SEPARATOR = r"%20|%2[bBdD]"  # URL-encoded space, plus, dash
# A comma counts only when no space follows it: "4111,1111,1111,1111" is one number, but
# "1200, 3400, 5600" and "12.50, 8.00" are lists of amounts (owner decision, P2-03).
_SEPARATOR = rf"(?:(?:[{_SEPARATOR_CHARS}]|,(?!\s)){{1,5}}|{_ENCODED_SEPARATOR})"
_DIGIT_RUN = re.compile(rf"\d(?:{_SEPARATOR}?\d){{{_MIN_PAN - 1},}}")
_ENCODED = re.compile(_ENCODED_SEPARATOR)
_DIGIT = re.compile(r"\d")

# IBAN: country code, 2 check digits, then letters and digits, in groups or written together.
_IBAN_MIN = 15
_IBAN_MAX = 34
_IBAN_CANDIDATE = re.compile(r"(?<![A-Za-z0-9])[A-Z]{2}\d{2}(?:[ ]?[A-Z0-9]){11,40}")
_IBAN_GROUP = re.compile(r"[ ]?[A-Z0-9]+")

# Gap between a trigger word and its code: a few characters, no digits, no line break.
_GAP_BEFORE = r"[^\d\n]{0,25}?"
_GAP_AFTER = r"[^\d\n]{0,15}?"
# A code is not part of a longer number and not the last 4 digits after "****".
_CODE_START = r"(?<!\d)(?<!\*\*\*\*)"
# ... and not followed by more digits or a decimal part ("250.00" is an amount).
_CODE_END = r"(?!\d)(?![.,]\d)"
# "card ending 1111", "last 4 digits: 1111": these digits are not a code.
_LAST_DIGITS_BEFORE = re.compile(
    r"(?:ending(?:\s+(?:in|with))?|ends\s+(?:in|with)|last\s+(?:4|four)(?:\s+digits)?)\W{0,3}\Z",
    re.IGNORECASE,
)


def _code_patterns(triggers: str, digits: str) -> tuple[re.Pattern[str], re.Pattern[str]]:
    code = rf"{_CODE_START}\d{{{digits}}}{_CODE_END}"
    # Not inside a longer word, but "cvv737" and "OTP482913" still count.
    trigger = rf"(?<![^\W\d_])(?:{triggers})(?![^\W\d_])"
    before = re.compile(rf"(?P<lead>{trigger}{_GAP_BEFORE})(?P<code>{code})", re.IGNORECASE)
    after = re.compile(rf"(?P<code>{code})(?P<trail>{_GAP_AFTER}{trigger})", re.IGNORECASE)
    return before, after


# Order matters: "security code" is a CVV word and is matched before the 2FA words.
_CODE_RULES: tuple[tuple[EntityType, tuple[re.Pattern[str], re.Pattern[str]]], ...] = (
    (
        "CVV",
        _code_patterns(
            r"cvv2?|cvc2?|security\s+code|card\s+verification\s+code"
            r"|code\s+on\s+the\s+back(?:\s+of\s+(?:the|my)\s+card)?",
            "3,4",
        ),
    ),
    ("PIN", _code_patterns(r"pin(?:\s+code)?", "4,6")),
    (
        "2FA",
        _code_patterns(
            r"2fa|otp|one[\s-]time\s+(?:code|password|passcode)|verification\s+code"
            r"|sms\s+code|auth(?:entication|enticator)?\s+code",
            "4,8",
        ),
    ),
)

# A CVV without a trigger word: 3-4 digits directly next to a masked card or an expiry date,
# separated by at most three spaces or punctuation characters (owner decision, P2-03).
# Expiry: MM/YY or MM/YYYY anywhere, but not as part of a full date ("03/14/2026"); MM-YY and
# MMYY only right after "exp", "expiry" or "valid until". "code" right after a masked card is
# a CVV word.
_MASKED_CARD = r"\[CARD \*{4}\d{4}\](?:[^\w\n]{0,3}code(?![^\W\d_]))?"
_SLASH_EXPIRY = r"(?<![\d/])(?<!/ )(?:0[1-9]|1[0-2])\s?/\s?(?:\d{4}|\d{2})(?!\d)(?!\s?/\s?\d)"
_WORD_EXPIRY = (
    r"(?<![^\W\d_])(?:exp(?:iry)?\.?|valid\s+until)\s*:?\s*"
    r"(?:0[1-9]|1[0-2])-?\d{2}(?!\d)"
)
_CARD_OR_EXPIRY = rf"(?:{_MASKED_CARD}|{_SLASH_EXPIRY}|{_WORD_EXPIRY})"
_NEXT_TO = r"[^\w\n]{0,3}"
# Before a card the gap has no colon: "Order 1234: [CARD ****1111]" is a label, not a CVV.
_NEXT_TO_BEFORE = r"[^\w\n:]{0,3}"
_CVV_AFTER_CARD = re.compile(
    rf"(?P<lead>{_CARD_OR_EXPIRY}{_NEXT_TO})(?P<code>{_CODE_START}\d{{3,4}}{_CODE_END})",
    re.IGNORECASE,
)
_CVV_BEFORE_CARD = re.compile(
    rf"(?P<code>{_CODE_START}\d{{3,4}}{_CODE_END})(?P<trail>{_NEXT_TO_BEFORE}{_CARD_OR_EXPIRY})",
    re.IGNORECASE,
)
# Amounts stay visible: no CVV when a currency sign or code is next to the number.
_CURRENCY_WORD = (
    r"eur|euros?|usd|dollars?|gbp|pounds?|chf|pln|sek|nok|dkk|czk|huf|ron|bgn|jpy|cny|cad|aud"
)
_CURRENCY_AFTER = re.compile(rf"\s?(?:{_CURRENCY_WORD})(?![^\W\d_])", re.IGNORECASE)
_CURRENCY_BEFORE = re.compile(rf"(?<![^\W\d_])(?:{_CURRENCY_WORD})\s?\Z", re.IGNORECASE)


class MaskResult(BaseModel):
    """Masked text, how many entities of each type were replaced, and whether masking failed.

    ``failed`` means an internal error: every digit was replaced and the counts are unknown.
    """

    model_config = ConfigDict(frozen=True)

    text: str
    counts: dict[EntityType, int]
    failed: bool = False


def luhn_valid(digits: str) -> bool:
    """Luhn checksum of a string of digits (any Unicode decimal digits)."""
    if not digits:
        return False
    total = 0
    for position, char in enumerate(reversed(digits)):
        value = int(char)
        if position % 2 == 1:
            value *= 2
            if value > 9:
                value -= 9
        total += value
    return total % 10 == 0


def _card(last4: str) -> str:
    return f"[CARD ****{last4}]"


def _valid_window(digits: str) -> str | None:
    """Last 4 digits of the first Luhn-valid 13-19 digit part, if any."""
    for start in range(len(digits) - _MIN_PAN + 1):
        for length in _WINDOW_LENGTHS:
            window = digits[start : start + length]
            if len(window) == length and luhn_valid(window):
                return window[-4:]
    return None


def _mask_run(run: str) -> tuple[str, int]:
    """Mask one run of 13+ digits with separators; returns the new text and the PAN count.

    The whole run is masked when it is a valid card, or when it has 14 or more digits and any
    13-19 digit part of it is a valid card (a card with a number, CVV or expiry next to it).
    """
    digits = "".join(_DIGIT.findall(_ENCODED.sub("", run)))
    if len(digits) > _MAX_RUN_TO_CHECK:
        return _card(digits[-4:]), 1
    if len(digits) <= _MAX_PAN and luhn_valid(digits):
        return _card(digits[-4:]), 1
    if len(digits) > _MIN_PAN:
        last4 = _valid_window(digits)
        if last4 is not None:
            return _card(last4), 1
    return run, 0


def _iban_valid(compact: str) -> bool:
    """ISO 13616 check: move the first 4 characters to the end, letters to numbers, mod 97."""
    rearranged = compact[4:] + compact[:4]
    number = "".join(str(int(char, 36)) for char in rearranged)
    return int(number) % 97 == 1


def _iban_spans(text: str) -> list[tuple[int, int]]:
    """Start and end of each valid IBAN in the text.

    An IBAN is kept (ADR-0010: IBANs stay in the database; G3 replaces them in model input).
    The longest valid IBAN ending at a group boundary wins.
    """
    spans: list[tuple[int, int]] = []
    for candidate in _IBAN_CANDIDATE.finditer(text):
        compact = ""
        best_end: int | None = None
        for group in _IBAN_GROUP.finditer(candidate.group(0)):
            compact += group.group(0).strip()
            if _IBAN_MIN <= len(compact) <= _IBAN_MAX and _iban_valid(compact):
                best_end = candidate.start() + group.end()
        if best_end is not None:
            spans.append((candidate.start(), best_end))
    return spans


def _mask_cards(text: str, counts: Counter[EntityType]) -> str:
    iban_spans = _iban_spans(text)

    def replace(match: re.Match[str]) -> str:
        iban_end = next((end for start, end in iban_spans if start <= match.start() < end), None)
        if iban_end is not None:
            # Keep the IBAN; a card typed right after it is still masked.
            cut = iban_end - match.start()
            return match.group(0)[:cut] + _mask_cards(match.group(0)[cut:], counts)
        masked, found = _mask_run(match.group(0))
        if found:
            counts["PAN"] += found
        return masked

    return _DIGIT_RUN.sub(replace, text)


def _names_last_digits(match: re.Match[str]) -> bool:
    start = match.start("code")
    return _LAST_DIGITS_BEFORE.search(match.string[max(0, start - 30) : start]) is not None


def _is_currency_sign(char: str) -> bool:
    return unicodedata.category(char) == "Sc"  # the re module has no Unicode property classes


def _next_to_currency(match: re.Match[str]) -> bool:
    """A currency sign or code directly before or after the number, at most one space between."""
    start, end = match.start("code"), match.end("code")
    before = match.string[max(0, start - 10) : start]
    after = match.string[end : end + 10]
    sign_before = before.removesuffix(" ")[-1:]
    sign_after = after.removeprefix(" ")[:1]
    return (
        (bool(sign_before) and _is_currency_sign(sign_before))
        or (bool(sign_after) and _is_currency_sign(sign_after))
        or _CURRENCY_BEFORE.search(before) is not None
        or _CURRENCY_AFTER.match(after) is not None
    )


def _redact_code(
    pattern: re.Pattern[str],
    text: str,
    entity: EntityType,
    counts: Counter[EntityType],
    *,
    keep_amounts: bool = False,
) -> str:
    # "Card ending 1111" names card digits, not a CVV. PIN and 2FA codes are masked anyway: a
    # code written after "ending" is still a code (P2-03 review).
    skip_last_digits = entity == "CVV"

    def replace(match: re.Match[str]) -> str:
        if (skip_last_digits and _names_last_digits(match)) or (
            keep_amounts and _next_to_currency(match)
        ):
            return match.group(0)
        counts[entity] += 1
        code_start = match.start("code") - match.start()
        code_end = match.end("code") - match.start()
        whole = match.group(0)
        return whole[:code_start] + REDACTED + whole[code_end:]

    return pattern.sub(replace, text)


def _mask_codes(text: str, counts: Counter[EntityType]) -> str:
    for entity, (before, after) in _CODE_RULES:
        text = _redact_code(before, text, entity, counts)
        text = _redact_code(after, text, entity, counts)
    for pattern in (_CVV_AFTER_CARD, _CVV_BEFORE_CARD):
        text = _redact_code(pattern, text, "CVV", counts, keep_amounts=True)
    return text


def _mask_all_digits(text: str) -> str:
    return _DIGIT.sub("#", text)


def mask(text: str) -> MaskResult:
    """Mask card numbers, CVV, PIN and 2FA codes; never raises."""
    counts: Counter[EntityType] = Counter()
    try:
        # Callers are typed, but a log record can hold anything.
        source = text if isinstance(text, str) else str(text)
        masked = _mask_cards(source, counts)
        masked = _mask_codes(masked, counts)
    except Exception:
        # Should not happen; if it does, lose information rather than leak a card number.
        fallback = _mask_all_digits(text) if isinstance(text, str) else REDACTED
        return MaskResult(text=fallback, counts={}, failed=True)
    return MaskResult(text=masked, counts=dict(counts))


def mask_text(text: str) -> str:
    """Only the masked text, for logs and traces."""
    return mask(text).text
