"""The memory summary of a closed ticket (docs/ai-pipeline.md §9, ADR-0012, P6-05).

`write_summary` runs after the close is committed (the close endpoint; the `close-stale` job of
P8-05 will call it too). It never raises: memory is a convenience, a failure must not undo or
fail a close.

1. Skip when the ticket already has a summary (`closed` is final; a replayed close).
2. The ticket's last 10 customer-visible messages (customer, assistant and operator replies;
   never `system` messages or internal notes, enforced here, not only in the prompt), masked by
   G1, with email, phone and IBAN replaced by placeholders (G3), as `role: text` lines.
3. The budget guard (G8) before the call: the gateway checks it only for `online` calls, and a
   close must not spend money once the day's budget is gone.
4. One structured call (`prompts/memory_summary`, role `triage`, `llm_calls.context = memory`,
   no run), at most `SUMMARY_TIMEOUT_S`; a call cut off by it gets an `error` row
   (`Gateway.record_abandoned`), so the failure is in `llm_calls` too.
5. The answer is cleaned: placeholders become `[REDACTED]` (no contact data in memory), then the
   card mask (G1); a failed mask stores nothing. Stored in `customer_memory` with
   `memory.written` (counts only), or `memory.failed` with the error class.
"""

import asyncio
import logging
import time
import uuid
from collections import Counter
from collections.abc import Sequence

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from hub.audit import record
from hub.db.models import Message
from hub.db.repositories import MemoryRepository, MessageRepository, TicketRepository
from hub.db.session import Database
from hub.domain.enums import ActorType, LlmCallContext, LlmNode, MessageRole
from hub.domain.memory import MEMORY_MAX_CHARS, MemorySummary
from hub.guardrails.masking import mask
from hub.guardrails.placeholders import redact_placeholders, replace_pii
from hub.llm.budget import BudgetCheck
from hub.llm.gateway import Gateway
from hub.llm.prompts import Prompt
from hub.llm.schemas import CallContext

logger = logging.getLogger(__name__)

TURNS = 10  # as the run's history (ai-pipeline §9)
# Shorter than one call of the `triage` role (30 s): the close request waits for it.
SUMMARY_TIMEOUT_S = 20.0
# Only what the customer saw: their own messages and the replies sent to them. `system` messages
# (handoff notices, placeholders) and anything internal never reach the memory model (owner
# decision, PR #38 review); the Hub never stores a restriction reason at all.
CUSTOMER_VISIBLE = frozenset({MessageRole.CUSTOMER, MessageRole.ASSISTANT, MessageRole.OPERATOR})
REDACTED = "[REDACTED]"


class MaskFailedError(Exception):
    """G1 could not mask the summary: nothing is stored."""


def transcript(messages: Sequence[Message]) -> str:
    """The last `TURNS` customer-visible messages as `role: text`, with one placeholder mapping
    for all."""
    visible = [message for message in messages if message.role in CUSTOMER_VISIBLE]
    mapping: dict[str, str] = {}
    lines = []
    for message in visible[-TURNS:]:
        replaced = replace_pii(message.body, mapping)
        mapping = dict(replaced.mapping)
        lines.append(f"{message.role.value}: {replaced.text}")
    return "\n\n".join(lines)


def clean(summary: str) -> tuple[str, Counter[str]]:
    """Placeholders out, card data masked; counts by entity type, for the audit record."""
    result = mask(redact_placeholders(summary, REDACTED))
    if result.failed:
        raise MaskFailedError
    counts = Counter({str(kind): count for kind, count in result.counts.items()})
    return result.text[:MEMORY_MAX_CHARS], counts


async def write_summary(
    database: Database,
    gateway: Gateway,
    prompt: Prompt,
    ticket_id: uuid.UUID,
    *,
    budget: BudgetCheck | None = None,
) -> None:
    try:
        await _write(database, gateway, prompt, ticket_id, budget)
    except Exception as exc:
        error = type(exc).__name__
        logger.warning("memory summary failed", extra={"ticket_id": str(ticket_id), "error": error})
        try:
            async with database.sessions.begin() as session:
                await _audit(session, "memory.failed", ticket_id, {"error": error})
        except Exception:
            logger.exception("memory failure not recorded", extra={"ticket_id": str(ticket_id)})


async def _write(
    database: Database,
    gateway: Gateway,
    prompt: Prompt,
    ticket_id: uuid.UUID,
    budget: BudgetCheck | None,
) -> None:
    async with database.sessions() as session:
        ticket = await TicketRepository(session).get(ticket_id)
        if ticket is None or await MemoryRepository(session).for_ticket(ticket_id) is not None:
            return
        customer_id = ticket.customer_id
        messages = await MessageRepository(session).list_for_ticket(ticket_id)
    text = transcript(messages)
    if not text:
        return
    if budget is not None:
        await budget.check()
    call = CallContext(
        context=LlmCallContext.MEMORY,
        node=LlmNode.MEMORY_SUMMARY,
        run_id=None,
        prompt_version=prompt.version,
    )
    start = time.perf_counter()
    try:
        async with asyncio.timeout(SUMMARY_TIMEOUT_S):
            result = await gateway.complete_structured(
                prompt.front.role, prompt.render({"ticket": text}), MemorySummary, call=call
            )
    except TimeoutError:
        latency_ms = round((time.perf_counter() - start) * 1000)
        await gateway.record_abandoned(prompt.front.role, call, latency_ms=latency_ms)
        raise
    content, masked = clean(result.value.summary)
    try:
        async with database.sessions.begin() as session:
            memory = await MemoryRepository(session).add(
                customer_id=customer_id, ticket_id=ticket_id, content=content
            )
            await _audit(
                session,
                "memory.written",
                ticket_id,
                {
                    "memory_id": memory.id,
                    "chars": len(content),
                    "masked": dict(masked),
                    "prompt_version": prompt.version,
                    "model": result.model,
                },
            )
    except IntegrityError:
        # A concurrent close of the same ticket stored its summary first.
        logger.info("memory summary already stored", extra={"ticket_id": str(ticket_id)})


async def _audit(
    session: AsyncSession, action: str, ticket_id: uuid.UUID, details: dict[str, object]
) -> None:
    await record(
        session,
        actor_type=ActorType.AI,
        actor_id=None,
        action=action,
        entity_type="ticket",
        entity_id=ticket_id,
        details=details,
    )
