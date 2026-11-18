"""Intake node (docs/ai-pipeline.md §3), the code part of phase 3: G4 and G3.

The ticket text is already masked by G1 (the API masks before anything else). Intake scores it
for injection patterns (G4) and replaces email addresses, phone numbers and masked IBANs with
placeholders (G3), with one mapping for the whole run: the new message first, then the earlier
messages of the ticket. Every text given to `replace_pii` is original G1-masked text from the
`messages` table, never text that already holds placeholders (P3-04 review): text with
placeholders lives only in the run's memory and is never stored.

The graph node (`run`, P6-01) reads the trigger message and the ticket's earlier messages from
`messages` and writes `intake.done` (entity the run: injection score, pattern group names and
placeholder counts, never text).

Language (P6-02): the English heuristic of the output checks (`looks_english`, no dependency),
stricter for short messages (`language`), gives `en` or None; None counts as not English and
hands the ticket off (reason `language`, owner decision). `tickets.language` gets the same
value. `pan_found`: G1 masked a card number in the message (row 1,
`pan_in_message_with_fraud_words`).

Customer context (P6-05, ai-pipeline §9): up to 5 memory items that have not expired and the
customer's other open tickets (id, intent, status), for `memory_shows_open_case` in `decide`.
The customer is the ticket's own, never one a model named. `intake.done` gets their counts.

Not built yet: the customer profile (`core_get_customer`, P7-04).
"""

import re
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from sqlalchemy.ext.asyncio import AsyncSession

from hub.audit import record
from hub.db.models import Message
from hub.db.repositories import MemoryRepository, MessageRepository, TicketRepository
from hub.domain.enums import ActorType, TicketStatus
from hub.domain.memory import (
    MEMORY_ITEMS,
    OPEN_TICKETS,
    CustomerContext,
    MemoryItem,
    OpenTicket,
)
from hub.graph.context import GraphContext, ids
from hub.graph.state import InputFlags, TicketState
from hub.graph.store import ticket
from hub.guardrails.injection import InjectionResult, score_injection
from hub.guardrails.output_checks import (
    MIN_WORDS_FOR_LANGUAGE,
    has_other_language_words,
    looks_english,
    words,
)
from hub.guardrails.placeholders import replace_pii, restore_pii

HISTORY_TURNS = 10  # ai-pipeline §2: the last 10 turns of the ticket
NO_HISTORY = "(no earlier messages)"
ENGLISH = "en"
_MASKED_CARD = re.compile(r"\[CARD \*{4}")


def language(message_masked: str) -> str | None:
    """`en` when the message looks English, None when it does not (unknown). Stricter than the
    verify step's `looks_english` for short messages (under `MIN_WORDS_FOR_LANGUAGE` words, which
    `looks_english` passes): one function word of another language makes them unknown, so
    "Hola, mi tarjeta no funciona" is handed off (owner decision, PR #36 review)."""
    if not looks_english(message_masked):
        return None
    short = len(words(message_masked)) < MIN_WORDS_FOR_LANGUAGE
    if short and has_other_language_words(message_masked):
        return None
    return ENGLISH


def pan_found(message: Message) -> bool:
    """G1 masked a card number in the message: its counts, or the mask in the text (a failed
    mask has no counts)."""
    counts = message.masked_entities or {}
    return counts.get("PAN", 0) > 0 or _MASKED_CARD.search(message.body) is not None


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


def history_turns(messages: Sequence[Message], trigger_id: uuid.UUID) -> list[ChatTurn]:
    """The messages before the trigger message, oldest first, as stored (masked)."""
    turns: list[ChatTurn] = []
    for message in messages:
        if message.id == trigger_id:
            break
        turns.append(ChatTurn(role=message.role.value, text=message.body))
    return turns


def intake_result(state: TicketState) -> IntakeResult:
    """The intake result the later nodes need, rebuilt from the state."""
    flags = state.get("input_flags")
    return IntakeResult(
        message_for_llm=state.get("message_for_llm", ""),
        history_for_llm=state.get("history_for_llm", ""),
        injection=flags.injection if flags else InjectionResult(score=0.0, patterns=[]),
        pii_counts=dict(flags.pii_placeholders) if flags else {},
        mapping=dict(state.get("placeholders", {})),
    )


async def customer_context(
    session: AsyncSession, customer_id: uuid.UUID, ticket_id: uuid.UUID
) -> CustomerContext:
    memory = MemoryRepository(session)
    items = await memory.active_for_customer(customer_id, limit=MEMORY_ITEMS)
    others = await memory.open_tickets(customer_id, exclude=ticket_id, limit=OPEN_TICKETS)
    return CustomerContext(
        memory=[
            MemoryItem(
                ticket_id=str(item.ticket_id),
                intent=intent,
                summary=item.content,
                created_at=item.created_at,
            )
            for item, intent in items
        ],
        open_tickets=[
            OpenTicket(ticket_id=str(other.id), intent=other.intent, status=other.status)
            for other in others
        ],
    )


async def run(state: TicketState, ctx: GraphContext) -> TicketState:
    ticket_id, run_id = ids(state)
    message_id = uuid.UUID(state["message_id"])
    async with ctx.pipeline.database.sessions.begin() as session:
        messages = await MessageRepository(session).list_for_ticket(ticket_id)
        trigger = next(message for message in messages if message.id == message_id)
        result = intake(trigger.body, history_turns(messages, message_id))
        found_language = language(trigger.body)
        repository = TicketRepository(session)
        found = await repository.lock(await ticket(session, ticket_id))
        if found.status == TicketStatus.PROCESSING:
            found.language = found_language
            await repository.save(found)
        context = await customer_context(session, found.customer_id, ticket_id)
        await record(
            session,
            actor_type=ActorType.AI,
            actor_id=None,
            action="intake.done",
            entity_type="run",
            entity_id=run_id,
            details={
                "ticket_id": ticket_id,
                "injection_score": result.injection.score,
                "injection_patterns": result.injection.patterns,
                "pii_placeholders": result.pii_counts,
                "language": found_language,
                "pan_found": pan_found(trigger),
                "memory_items": len(context.memory),
                "open_tickets": len(context.open_tickets),
            },
        )
    return TicketState(
        message_masked=trigger.body,
        message_for_llm=result.message_for_llm,
        history_for_llm=result.history_for_llm,
        placeholders=dict(result.mapping),
        input_flags=InputFlags(
            injection=result.injection,
            pii_placeholders=result.pii_counts,
            language=found_language,
            pan_found=pan_found(trigger),
        ),
        customer_context=context,
    )
