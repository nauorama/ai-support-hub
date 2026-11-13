"""Card-data masking (G1, docs/security.md §3).

Card numbers (PAN) become ``[CARD ****1111]``; CVV, PIN and 2FA codes next to their trigger
words become ``[REDACTED]``. The same function masks customer text, operator text, log records
and traces (G9), so it must never raise: on an internal error it masks every digit instead.

Pure functions; no imports from the rest of the project.
"""

import re
from collections import Counter
from typing import Literal

from pydantic import BaseModel, ConfigDict

EntityType = Literal["PAN", "CVV", "PIN", "2FA"]

REDACTED = "[REDACTED]"

_MIN_PAN = 13
_MAX_PAN = 19
# A run this long is not a card someone typed; checking every window would cost too much.
_MAX_RUN_TO_CHECK = 1000
# Window lengths for runs over 19 digits, most common card lengths first, so the last 4 digits
# shown are most likely the card's own (a card typed with its expiry right after it).
_WINDOW_LENGTHS = (16, 15, 19, 13, 14, 17, 18)

# Between two digits: up to three separator characters (any whitespace including NBSP, dashes,
# + . / _) or one URL-encoded space, plus or dash. Wider than "spaces or dashes" on purpose: the
# same mask cleans logs, where URLs and form-encoded bodies appear.
_SEPARATOR = r"(?:[\s\-\u2010-\u2015+./_]{1,3}|%20|%2[bBdD])"
_DIGIT_RUN = re.compile(rf"\d(?:{_SEPARATOR}?\d){{{_MIN_PAN - 1},}}")
# Splits a run into its digit groups; the encoded separators come first so their digits
# ("%20") are not read as card digits.
_RUN_TOKEN = re.compile(r"%20|%2[bBdD]|(\d+)|[^\d%]+")

# Gap between a trigger word and its code: a few characters, no digits, no line break, and no
# brackets or asterisks, so the last 4 digits inside ``[CARD ****1111]`` are never taken for a
# code.
_GAP_BEFORE = r"[^\d\n\[\]*]{0,25}?"
_GAP_AFTER = r"[^\d\n\[\]*]{0,15}?"


def _code_patterns(triggers: str, digits: str) -> tuple[re.Pattern[str], re.Pattern[str]]:
    code = rf"(?<![\d*])\d{{{digits}}}(?!\d)"
    trigger = rf"\b(?:{triggers})\b"
    before = re.compile(rf"(?P<lead>{trigger}{_GAP_BEFORE})(?P<code>{code})", re.IGNORECASE)
    after = re.compile(rf"(?P<code>{code})(?P<trail>{_GAP_AFTER}{trigger})", re.IGNORECASE)
    return before, after


# Order matters: "security code" is a CVV word and is matched before the 2FA words.
_CODE_RULES: tuple[tuple[EntityType, tuple[re.Pattern[str], re.Pattern[str]]], ...] = (
    ("CVV", _code_patterns(r"cvv2?|cvc2?|security\s+code|card\s+verification\s+code", "3,4")),
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


class MaskResult(BaseModel):
    """Masked text and how many entities of each type were replaced."""

    model_config = ConfigDict(frozen=True)

    text: str
    counts: dict[EntityType, int]


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
    """Last 4 digits of the first Luhn-valid 13-19 digit window, if any."""
    for start in range(len(digits) - _MIN_PAN + 1):
        for length in _WINDOW_LENGTHS:
            window = digits[start : start + length]
            if len(window) == length and luhn_valid(window):
                return window[-4:]
    return None


def _mask_group_spans(run: str, groups: list[tuple[int, int, str]]) -> tuple[str, int]:
    """Mask every Luhn-valid span of whole digit groups with 13-19 digits.

    Catches a card next to other numbers, e.g. "option 2 - 4111 1111 1111 1111" or a card with
    the expiry typed after it. Leftmost start first, then the longest valid span from there.
    """
    pieces: list[str] = []
    cursor = 0
    found = 0
    index = 0
    while index < len(groups):
        best: int | None = None
        digits = ""
        for end in range(index, len(groups)):
            digits += groups[end][2]
            if len(digits) > _MAX_PAN:
                break
            if len(digits) >= _MIN_PAN and luhn_valid(digits):
                best = end
        if best is None:
            index += 1
            continue
        span_digits = "".join(group[2] for group in groups[index : best + 1])
        pieces.append(run[cursor : groups[index][0]])
        pieces.append(_card(span_digits[-4:]))
        cursor = groups[best][1]
        found += 1
        index = best + 1
    pieces.append(run[cursor:])
    return "".join(pieces), found


def _mask_run(run: str) -> tuple[str, int]:
    """Mask one run of 13+ digits with separators; returns the new text and the PAN count."""
    groups = [
        (token.start(), token.end(), token.group(1))
        for token in _RUN_TOKEN.finditer(run)
        if token.group(1) is not None
    ]
    digits = "".join(group[2] for group in groups)
    if len(digits) > _MAX_RUN_TO_CHECK:
        return _card(digits[-4:]), 1
    if len(digits) <= _MAX_PAN and luhn_valid(digits):
        return _card(digits[-4:]), 1
    masked, found = _mask_group_spans(run, groups)
    if found:
        return masked, found
    if len(digits) > _MAX_PAN:
        last4 = _valid_window(digits)
        if last4 is not None:
            return _card(last4), 1
    return run, 0


def _mask_cards(text: str, counts: Counter[EntityType]) -> str:
    def replace(match: re.Match[str]) -> str:
        masked, found = _mask_run(match.group(0))
        if found:
            counts["PAN"] += found
        return masked

    return _DIGIT_RUN.sub(replace, text)


def _mask_codes(text: str, counts: Counter[EntityType]) -> str:
    for entity, (before, after) in _CODE_RULES:

        def replace_before(match: re.Match[str], entity: EntityType = entity) -> str:
            counts[entity] += 1
            return match.group("lead") + REDACTED

        def replace_after(match: re.Match[str], entity: EntityType = entity) -> str:
            counts[entity] += 1
            return REDACTED + match.group("trail")

        text = before.sub(replace_before, text)
        text = after.sub(replace_after, text)
    return text


def _mask_all_digits(text: str) -> str:
    return re.sub(r"\d", "#", text)


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
        return MaskResult(text=fallback, counts={})
    return MaskResult(text=masked, counts=dict(counts))


def mask_text(text: str) -> str:
    """Only the masked text, for logs and traces."""
    return mask(text).text
