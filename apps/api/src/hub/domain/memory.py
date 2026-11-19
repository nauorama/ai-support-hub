"""Memory between tickets (docs/ai-pipeline.md §9, ADR-0012, P6-05).

- `MemorySummary`: what the `memory_summary` model returns when a ticket is closed.
- `CustomerContext`: what `intake` loads for a run: up to 5 memory items that have not expired
  and the customer's other open tickets (id, intent, status). Context only: facts in an answer
  still need a citation (ai-pipeline §9).
- `shows_open_case`: the `memory_shows_open_case` condition of `approval_only`
  (ai-pipeline §6): another open ticket with the same intent, or a memory item of the last 14
  days on the same intent. A repeated contact means the first answer did not help.

- `profile`: tier, country and the restricted flag, loaded by intake with `core_get_customer`
  (P7-04); None when the tool failed or the run has no tool client.
"""

from datetime import datetime, timedelta

from pydantic import BaseModel, ConfigDict, Field

from hub.domain.account import CustomerProfile
from hub.domain.enums import Intent, TicketStatus

MEMORY_ITEMS = 5  # ai-pipeline §9: intake loads the 5 newest
OPEN_TICKETS = 10  # the newest open tickets; more add nothing to the repeat check
MEMORY_MAX_CHARS = 400  # data model `customer_memory.content`
REPEAT_WINDOW = timedelta(days=14)


class MemorySummary(BaseModel):
    """What the memory model returns. The field description goes to the model with the strict
    schema; the length limit is enforced here (a longer answer gets the repair call)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    summary: str = Field(
        description="One or two sentences, at most 300 characters: the topic of the ticket and"
        " its outcome.",
        min_length=1,
        max_length=300,
    )


class MemoryItem(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    ticket_id: str
    intent: Intent | None  # the closed ticket's intent
    summary: str  # masked (G1), no placeholders
    created_at: datetime


class OpenTicket(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    ticket_id: str
    intent: Intent | None
    status: TicketStatus


class CustomerContext(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    memory: list[MemoryItem] = Field(default_factory=list)
    open_tickets: list[OpenTicket] = Field(default_factory=list)
    profile: CustomerProfile | None = None


def shows_open_case(context: CustomerContext | None, intent: Intent | str, now: datetime) -> bool:
    """`memory_shows_open_case`: the same intent as another open ticket, or as a memory item of
    the last 14 days."""
    if context is None:
        return False
    if any(ticket.intent == intent for ticket in context.open_tickets):
        return True
    return any(
        item.intent == intent and now - item.created_at <= REPEAT_WINDOW for item in context.memory
    )
