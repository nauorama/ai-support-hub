"""PII placeholders (G3, docs/security.md §3).

Email addresses, phone numbers and IBANs in model input become ``<EMAIL_1>``, ``<PHONE_1>``,
``<IBAN_1>``. The text is already masked by G1, so an IBAN only appears in G1's partly masked
form (``[IBAN DE89 …3000]``, or ``[IBAN AT77 …]`` when digits follow it); restoring gives
that masked form back, never a full IBAN (ADR-0017). The mapping is kept in memory for the
run and is never logged (``repr`` hides it). One mapping serves the whole run: pass it back in
for the next text (the history, a follow-up message), so the same placeholder never stands for
two values.

Phone numbers are matched conservatively, so that amounts, dates, times, error codes and
masked card numbers stay as they are: an international number (``+`` or ``00``), a national
number that starts with ``0`` and has at least 9 digits, or the North American form
``(555) 123-4567``. A number never starts right after a digit and a space, so ``1 000 000 000``
stays an amount. A reference or account number that starts with ``0`` and is long enough is
taken for a phone number; that hides it from the model, but leaks nothing (owner decision,
P3-04). A number that is missed stays in the masked text, where it was already
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
# masked IBAN (``\u20263000``), nor right after a digit and a space or dash (a group of an
# amount such as ``1 000 000``), and may not run on into more digits, a time (``10:00``) or a
# decimal part. Digit groups are possessive (``\d++``), so a failed end check drops a whole
# group instead of splitting one.
_START = r"(?<![\w*\u2026+:.,/-])(?<!\d[ \-])"
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


def _escape_typed_placeholders(text: str) -> str:
    """A placeholder the customer typed (``<EMAIL_1>``) is written as ``&lt;EMAIL_1&gt;``, so
    the model never takes it for one of ours and restoring never gives it a value."""
    return _PLACEHOLDER.sub(lambda match: f"&lt;{match.group(0)[1:-1]}&gt;", text)


def replace_pii(text: str, mapping: Mapping[str, str] | None = None) -> PiiReplacement:
    """Replace every email, phone number and masked IBAN with a numbered placeholder.

    ``mapping`` is the run's mapping so far (from an earlier call); the result's mapping holds
    it plus the new placeholders. The same value always gets the same placeholder, and a new
    value gets the next free number of its type.

    ``text`` must be original text masked by G1, never text that already holds our
    placeholders (e.g. history stored after an earlier replacement): those would be escaped
    like placeholders the customer typed and never restored. Keep the original masked text and
    replace it again with the run's mapping.
    """
    text = _escape_typed_placeholders(text)
    known = dict(mapping or {})
    by_value: dict[tuple[str, str], str] = {}
    counters: dict[str, int] = {"EMAIL": 0, "PHONE": 0, "IBAN": 0}
    for placeholder, value in known.items():
        match = _PLACEHOLDER.fullmatch(placeholder)
        if match is None:
            raise ValueError("mapping holds a key that is not a placeholder")
        kind, number = match.group(1), int(match.group(2))
        by_value[(kind, value)] = placeholder
        counters[kind] = max(counters[kind], number)
    parts: list[str] = []
    position = 0
    for start, end, kind in _spans(text):
        value = text[start:end]
        found = by_value.get((kind, value))
        if found is None:
            counters[kind] += 1
            found = f"<{kind}_{counters[kind]}>"
            by_value[(kind, value)] = found
            known[found] = value
        parts.append(text[position:start])
        parts.append(found)
        position = end
    parts.append(text[position:])
    return PiiReplacement(text="".join(parts), mapping=known)


def restore_pii(text: str, mapping: Mapping[str, str]) -> str:
    """Put the original values back for the placeholders in ``mapping``; anything else that
    looks like a placeholder stays as it is. One pass, so a restored value is never expanded."""
    return _PLACEHOLDER.sub(lambda match: mapping.get(match.group(0), match.group(0)), text)


def redact_placeholders(text: str, replacement: str = "[REDACTED]") -> str:
    """Every placeholder of ours replaced by `replacement`: for model output that is kept
    beyond the run (memory, P6-05), where no mapping may restore contact data."""
    return _PLACEHOLDER.sub(replacement, text)
