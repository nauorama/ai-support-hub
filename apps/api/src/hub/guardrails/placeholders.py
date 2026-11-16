"""PII placeholders (G3, docs/security.md §3).

Email addresses, phone numbers and IBANs in model input become ``<EMAIL_1>``, ``<PHONE_1>``,
``<IBAN_1>``. The text is already masked by G1, so an IBAN only appears in G1's partly masked
form (``[IBAN DE89 …3000]``, or ``[IBAN AT77 …]`` when digits follow it); restoring gives
that masked form back, never a full IBAN (ADR-0017). The mapping is kept in memory for the run and
is never logged (``repr`` hides it).

Phone numbers are matched conservatively, so that amounts, dates, times, error codes and
masked card numbers stay as they are: an international number (``+`` or ``00``), a national
number that starts with ``0`` and has at least 9 digits, or the North American form
``(555) 123-4567``. A number that is missed stays in the masked text, where it was already
allowed (ADR-0010); G3 is minimisation, not a leak guard.

Pure functions; no imports from the rest of the project.
"""

import re
from collections.abc import Mapping
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

PiiType = Literal["EMAIL", "PHONE", "IBAN"]

_EMAIL = re.compile(r"(?<![\w.%+'-])[\w.%+'-]+@(?:[^\W_](?:[\w-]*[^\W_])?\.)+[^\W\d_]{2,}(?![\w-])")
# Exactly what guardrails/masking.py writes for an IBAN (ADR-0017).
_MASKED_IBAN = re.compile(r"\[IBAN [A-Z]{2}\d{2} \u2026(?:[A-Z0-9]{4})?\]")

# A phone number may not start inside a word, a number, a masked card (``****1111``) or a
# masked IBAN (``\u20263000``), and may not run on into more digits, a time (``10:00``) or a
# decimal part. Digit groups are possessive (``\d++``), so a failed end check drops a whole
# group instead of splitting one.
_START = r"(?<![\w*\u2026+:.,/-])"
_END = r"(?![\w:]|[.,/-]\d)"
_INTERNATIONAL = re.compile(rf"{_START}(?:\+|00)[1-9]\d{{0,2}}(?:[ .\-/]?\(?\d++\)?){{1,6}}{_END}")
# National: a leading 0 (or ``(0``). Dots and slashes are not separators here, so a date such
# as ``01.12.2026`` or ``01/12/2026 10`` is never one.
_NATIONAL = re.compile(rf"{_START}\(?0\d++\)?(?:[ \-]?\d++){{0,5}}{_END}")
_NORTH_AMERICAN = re.compile(rf"{_START}(?:\(\d{{3}}\) ?|\d{{3}}[ .\-])\d{{3}}[.\-]\d{{4}}{_END}")
_INTERNATIONAL_DIGITS = range(8, 16)  # E.164: at most 15 digits, country code included
_NATIONAL_DIGITS = range(9, 14)

_PLACEHOLDER = re.compile(r"<(EMAIL|PHONE|IBAN)_([1-9][0-9]*)>")


class PiiReplacement(BaseModel):
    """Model input with placeholders, and how to restore them."""

    model_config = ConfigDict(frozen=True)

    text: str
    # placeholder -> original (masked) text; personal data, so never shown in a repr or log
    mapping: dict[str, str] = Field(default_factory=dict, repr=False)


def _digit_count(text: str) -> int:
    return sum(char.isdigit() for char in text)


def _phone_spans(text: str) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    for pattern, lengths in (
        (_INTERNATIONAL, _INTERNATIONAL_DIGITS),
        (_NATIONAL, _NATIONAL_DIGITS),
        (_NORTH_AMERICAN, range(10, 11)),
    ):
        for match in pattern.finditer(text):
            if _digit_count(match.group(0)) in lengths:
                spans.append(match.span())
    return spans


def _spans(text: str) -> list[tuple[int, int, PiiType]]:
    """Non-overlapping spans, earliest first; on overlap the earlier, then the longer, wins."""
    found: list[tuple[int, int, PiiType]] = [
        *((m.start(), m.end(), "EMAIL") for m in _EMAIL.finditer(text)),
        *((m.start(), m.end(), "IBAN") for m in _MASKED_IBAN.finditer(text)),
        *((start, end, "PHONE") for start, end in _phone_spans(text)),
    ]
    found.sort(key=lambda span: (span[0], -span[1]))
    kept: list[tuple[int, int, PiiType]] = []
    for span in found:
        if not kept or span[0] >= kept[-1][1]:
            kept.append(span)
    return kept


def replace_pii(text: str) -> PiiReplacement:
    """Replace every email, phone number and masked IBAN with a numbered placeholder.

    The same value always gets the same placeholder. A placeholder the customer typed
    themselves (``<EMAIL_1>``) keeps its number taken, so restoring never turns the
    customer's own text into a value it did not stand for.
    """
    taken = {match.group(0) for match in _PLACEHOLDER.finditer(text)}
    by_value: dict[tuple[PiiType, str], str] = {}
    mapping: dict[str, str] = {}
    counters: dict[PiiType, int] = {"EMAIL": 0, "PHONE": 0, "IBAN": 0}
    parts: list[str] = []
    position = 0
    for start, end, kind in _spans(text):
        value = text[start:end]
        placeholder = by_value.get((kind, value))
        if placeholder is None:
            while True:
                counters[kind] += 1
                placeholder = f"<{kind}_{counters[kind]}>"
                if placeholder not in taken:
                    break
            by_value[(kind, value)] = placeholder
            mapping[placeholder] = value
        parts.append(text[position:start])
        parts.append(placeholder)
        position = end
    parts.append(text[position:])
    return PiiReplacement(text="".join(parts), mapping=mapping)


def restore_pii(text: str, mapping: Mapping[str, str]) -> str:
    """Put the original values back for the placeholders in ``mapping``; anything else that
    looks like a placeholder stays as it is. One pass, so a restored value is never expanded."""
    return _PLACEHOLDER.sub(lambda match: mapping.get(match.group(0), match.group(0)), text)
