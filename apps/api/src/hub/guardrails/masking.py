"""Card-data masking (G1, docs/security.md §3).

Card numbers (PAN) become ``[CARD ****1111]``. CVV, PIN and 2FA codes next to their trigger
words, and CVV-like numbers directly next to a masked card or an expiry date, become
``[REDACTED]``; an exact error code such as ``ERR-3007`` is not a PIN or 2FA code. A valid
IBAN keeps only its country code, check digits and last 4 characters, ``[IBAN DE89 …3000]``
(ADR-0017), so no card digits hidden in it are stored. The same function
masks customer text, operator text, log records and traces (G9), so it must never raise: on an
internal error it masks every digit instead.

Pure functions; no imports from the rest of the project.
"""

import re
import unicodedata
from collections import Counter
from typing import Literal

from pydantic import BaseModel, ConfigDict

EntityType = Literal["PAN", "CVV", "PIN", "2FA", "IBAN"]
# Card data in the sense of the customer's "masked" note (docs/api.md); an IBAN is not.
CARD_ENTITIES: frozenset[str] = frozenset({"PAN", "CVV", "PIN", "2FA"})

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
_IBAN_CANDIDATE = re.compile(r"(?<![A-Za-z0-9])[A-Z]{2}\d{2}(?:[ ]?[A-Z0-9]){11,40}")
_IBAN_GROUP = re.compile(r"[ ]?[A-Z0-9]+")
_IBAN_START = re.compile(r"(?<![A-Za-z0-9])[A-Z]{2}\d{2}")
# Digits joined to the end of an IBAN, as in a card number: after a separator, or directly. An
# IBAN group holds only ASCII characters, so "4111" + Arabic-Indic or full-width digits ends
# the IBAN right before them; they still belong to the same number.
_AFTER_IBAN = re.compile(rf"{_SEPARATOR}?(?P<run>\d(?:{_SEPARATOR}?\d)*)")
# A card that crosses the IBAN's end is checked only when this many digits follow it: with
# fewer, no more than 4 card digits can be visible, as in "[CARD ****1111]" (ADR-0017).
_MIN_DIGITS_AFTER_IBAN = 5
# IBAN length by country (ISO 13616). A run is an IBAN only with a listed country code and
# exactly that length; "YZ" + a card number passing mod 97 is not one (ADR-0017).
# Source: SWIFT IBAN Registry release 101, via python-stdnum 2.2 ``stdnum/iban.dat``
# (tag 2.2, commit 6070c60, 2026-01-04). The registry itself refuses automated downloads.
# fmt: off
_IBAN_LENGTHS: dict[str, int] = {
    "AD": 24, "AE": 23, "AL": 28, "AT": 20, "AZ": 28, "BA": 20, "BE": 16, "BG": 22, "BH": 22,
    "BI": 27, "BR": 29, "BY": 28, "CH": 21, "CR": 22, "CY": 28, "CZ": 24, "DE": 22, "DJ": 27,
    "DK": 18, "DO": 28, "EE": 20, "EG": 29, "ES": 24, "FI": 18, "FK": 18, "FO": 18, "FR": 27,
    "GB": 22, "GE": 22, "GI": 23, "GL": 18, "GR": 27, "GT": 28, "HN": 28, "HR": 21, "HU": 28,
    "IE": 22, "IL": 23, "IQ": 23, "IS": 26, "IT": 27, "JO": 30, "KW": 30, "KZ": 20, "LB": 28,
    "LC": 32, "LI": 21, "LT": 20, "LU": 20, "LV": 21, "LY": 25, "MC": 27, "MD": 24, "ME": 22,
    "MK": 19, "MN": 20, "MR": 27, "MT": 31, "MU": 30, "NI": 28, "NL": 18, "NO": 15, "OM": 23,
    "PK": 24, "PL": 28, "PS": 29, "PT": 25, "QA": 29, "RO": 24, "RS": 22, "RU": 33, "SA": 24,
    "SC": 31, "SD": 18, "SE": 24, "SI": 19, "SK": 24, "SM": 27, "SO": 23, "ST": 25, "SV": 28,
    "TL": 23, "TN": 24, "TR": 26, "UA": 29, "VA": 22, "VG": 24, "XK": 20, "YE": 30,
}
# fmt: on

# Gap between a trigger word and its code: a few characters, no digits, no line break.
_GAP_BEFORE = r"[^\d\n]{0,25}?"
_GAP_AFTER = r"[^\d\n]{0,15}?"
# A code is not part of a longer number and not the last 4 digits after "****".
# ... nor the last 4 characters of a masked IBAN: exactly "[IBAN DE89 \u20263000]".
_CODE_START = r"(?<!\d)(?<!\*\*\*\*)(?!(?<=\[IBAN [A-Z]{2}\d{2} \u2026)[A-Z0-9]{4}\])"
# ... and not followed by more digits or a decimal part ("250.00" is an amount).
_CODE_END = r"(?!\d)(?![.,]\d)"
# "card ending 1111", "last 4 digits: 1111": these digits are not a code.
_LAST_DIGITS_BEFORE = re.compile(
    r"(?:ending(?:\s+(?:in|with))?|ends\s+(?:in|with)|last\s+(?:4|four)(?:\s+digits)?)\W{0,3}\Z",
    re.IGNORECASE,
)


# An exact error code such as "ERR-3007" is not a PIN or 2FA code, even next to "PIN": triage
# needs it (pre-H2 #2). Uppercase only, and "ERR" not the end of a longer word; "ERR-30071"
# is not an error code, so its digits are still masked.
_NOT_AN_ERROR_CODE = r"(?!(?<=(?<![^\W\d_])(?-i:ERR)-)\d{4}(?!\d))"


def _code_patterns(
    triggers: str, digits: str, *, keep_error_codes: bool = False
) -> tuple[re.Pattern[str], re.Pattern[str]]:
    start = _CODE_START + (_NOT_AN_ERROR_CODE if keep_error_codes else "")
    code = rf"{start}\d{{{digits}}}{_CODE_END}"
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
    ("PIN", _code_patterns(r"pin(?:\s+code)?", "4,6", keep_error_codes=True)),
    (
        "2FA",
        _code_patterns(
            r"2fa|otp|one[\s-]time\s+(?:code|password|passcode)|verification\s+code"
            r"|sms\s+code|auth(?:entication|enticator)?\s+code",
            "4,8",
            keep_error_codes=True,
        ),
    ),
)

# A CVV without a trigger word: 3-4 digits directly next to a masked card or an expiry date,
# separated by at most three spaces or punctuation characters (owner decision, P2-03).
# Expiry: MM/YY or MM/YYYY anywhere, but not as part of a full date ("03/14/2026"); MM-YY and
# MMYY only right after "exp", "expiry" or "valid until". "code" right after a masked card is
# a CVV word.
_MASKED_CARD = r"\[CARD \*{4}(?:\d{4})?\](?:[^\w\n]{0,3}code(?![^\W\d_]))?"
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


def _iban_spans(text: str) -> list[tuple[int, int, str]]:
    """Start, end and compact form of each valid IBAN in the text.

    Valid means a listed country code, that country's exact length, and the mod-97 check.
    A candidate is tried at every possible start, so an invalid one ("XY12 DE89 ...") cannot
    hide a valid IBAN that starts inside it.
    """
    spans: list[tuple[int, int, str]] = []
    position = 0
    while (start := _IBAN_START.search(text, position)) is not None:
        found = _valid_iban_at(text, start.start())
        if found is None:
            position = start.start() + 1
            continue
        spans.append((start.start(), found[0], found[1]))
        position = found[0]
    return spans


def _valid_iban_at(text: str, start: int) -> tuple[int, str] | None:
    """End and compact form of a valid IBAN starting at ``start``, if there is one."""
    candidate = _IBAN_CANDIDATE.match(text, start)
    if candidate is None:
        return None
    length = _IBAN_LENGTHS.get(candidate.group(0)[:2])
    if length is None:
        return None
    compact = ""
    for group in _IBAN_GROUP.finditer(candidate.group(0)):
        compact += group.group(0).strip()
        if len(compact) >= length:
            if len(compact) == length and _iban_valid(compact):
                return start + group.end(), compact
            return None
    return None


def _card_across_iban_end(tail: str, run: str) -> bool:
    """Whether the IBAN's last digits and the first digits of the run right after it form a
    Luhn-valid 13-19 digit number."""
    for take in range(1, min(len(run), _MAX_PAN - 1) + 1):  # at least 1 digit of the IBAN
        for length in _WINDOW_LENGTHS:
            need = length - take
            if 1 <= need <= len(tail) and luhn_valid(tail[-need:] + run[:take]):
                return True
    return False


def _is_a_code(text: str, start: int, end: int) -> bool:
    """Whether a PIN, 2FA or CVV rule takes exactly ``text[start:end]`` as its code, with the
    trigger word after it ("482913 is my OTP"). A trigger before it cannot reach past the
    IBAN's digits. The code rules mask it later, so it is not taken for a card here."""
    for _entity, (_before, after) in _CODE_RULES:
        match = after.match(text, start)
        if match is not None and match.end("code") == end:
            return True
    return False


def _mask_iban(text: str, end: int, compact: str, counts: Counter[EntityType]) -> tuple[str, int]:
    """Masked IBAN plus the digit run joined to it; returns the text and where it stops.

    The IBAN keeps its country code, check digits and last 4 characters (ADR-0017). When more
    digits follow, the last 4 are hidden too: they could be the first digits of a card that
    goes on after the IBAN. The run is masked as a card when it holds one itself, or, when it
    has at least 5 digits and is not a PIN, 2FA or CVV code, when a Luhn-valid number crosses
    the IBAN's end; that card is shown as ``[CARD ****]``, without digits.
    """
    counts["IBAN"] += 1
    joined = _AFTER_IBAN.match(text, end)
    if joined is None:
        return f"[IBAN {compact[:4]} \u2026{compact[-4:]}]", end
    run = joined.group("run")
    digits = "".join(_DIGIT.findall(_ENCODED.sub("", run)))
    # _mask_run expects a run of at least 13 digits: a shorter one ("0") can pass Luhn alone.
    masked_run, found = _mask_run(run) if len(digits) >= _MIN_PAN else (run, 0)
    if not found:
        tail = re.sub(r"^.*\D", "", compact)  # the IBAN's last digits, after its last letter
        if (
            len(digits) >= _MIN_DIGITS_AFTER_IBAN
            and not _is_a_code(text, joined.start("run"), joined.end("run"))
            and _card_across_iban_end(tail, digits)
        ):
            masked_run, found = "[CARD ****]", 1
    if found:
        counts["PAN"] += found
    separator = text[end : joined.start("run")]
    return f"[IBAN {compact[:4]} \u2026]{separator}{masked_run}", joined.end()


def _mask_card_runs(text: str, counts: Counter[EntityType]) -> str:
    def replace(match: re.Match[str]) -> str:
        masked, found = _mask_run(match.group(0))
        if found:
            counts["PAN"] += found
        return masked

    return _DIGIT_RUN.sub(replace, text)


def _mask_cards(text: str, counts: Counter[EntityType]) -> str:
    """Mask every valid IBAN partly and every card number (ADR-0017).

    Text between IBANs is masked on its own: an IBAN starts with letters, so no card number
    reaches into one from the left. The digit run masked after an IBAN never holds the next
    one: an IBAN needs a letter right after a character that is not a letter or digit.
    """
    parts: list[str] = []
    position = 0
    for start, end, compact in _iban_spans(text):
        parts.append(_mask_card_runs(text[position:start], counts))
        masked, position = _mask_iban(text, end, compact, counts)
        parts.append(masked)
    parts.append(_mask_card_runs(text[position:], counts))
    return "".join(parts)


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
