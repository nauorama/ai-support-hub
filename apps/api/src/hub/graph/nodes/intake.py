"""Intake node (docs/ai-pipeline.md §3), the code part of phase 3: G4 and G3.

The ticket text is already masked by G1 (the API masks before anything else). Intake scores it
for injection patterns (G4) and replaces email addresses, phone numbers and masked IBANs with
placeholders (G3), with one mapping for the whole run: the new message first, then the earlier
messages of the ticket. Every text given to `replace_pii` is original G1-masked text from the
`messages` table, never text that already holds placeholders (P3-04 review): text with
placeholders lives only in the run's memory and is never stored.

Not built yet: language detection, the customer profile (`core_get_customer`, phase 7), memory
items and other open tickets (phase 6).
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from hub.guardrails.injection import InjectionResult, score_injection
from hub.guardrails.placeholders import replace_pii, restore_pii

HISTORY_TURNS = 10  # ai-pipeline §2: the last 10 turns of the ticket
NO_HISTORY = "(no earlier messages)"


@dataclass(frozen=True, slots=True)
class ChatTurn:
    role: str  # customer, assistant, operator or system (`messages.role`)
    text: str  # masked by G1


@dataclass(frozen=True, slots=True)
class IntakeResult:
    message_for_llm: str
    history_for_llm: str
    injection: InjectionResult
    pii_counts: dict[str, int]
    # placeholder -> masked original; personal data, so never logged or shown in a repr
    mapping: dict[str, str] = field(repr=False)

    def restore(self, text: str) -> str:
        """Put the masked originals back for the run's placeholders."""
        return restore_pii(text, self.mapping)


def _counts(mapping: Mapping[str, str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for placeholder in mapping:
        kind = placeholder[1:].split("_", 1)[0]
        counts[kind] = counts.get(kind, 0) + 1
    return counts


def intake(message_masked: str, history: Sequence[ChatTurn] = ()) -> IntakeResult:
    """`history`: the earlier messages of the ticket, oldest first, without the new message."""
    ticket = replace_pii(message_masked)
    mapping = ticket.mapping
    lines: list[str] = []
    for turn in list(history)[-HISTORY_TURNS:]:
        replaced = replace_pii(turn.text, mapping)
        mapping = replaced.mapping
        lines.append(f"{turn.role}: {replaced.text}")
    return IntakeResult(
        message_for_llm=ticket.text,
        history_for_llm="\n\n".join(lines) if lines else NO_HISTORY,
        # Scored on the masked text, as the customer wrote it: a placeholder cannot hide a phrase.
        injection=score_injection(message_masked),
        pii_counts=_counts(mapping),
        mapping=dict(mapping),
    )
