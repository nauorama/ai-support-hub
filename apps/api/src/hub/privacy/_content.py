"""Deleting what tickets hold, shared by erasure and retention (P8-05).

Plain `DELETE` statements on the tables around a ticket. Never on `tickets` itself: a statement on
`tickets` through a session is refused (`hub.db.models._refuse_bulk_ticket_writes`), so ticket
rows are changed or deleted one by one through the ORM by the callers.
"""

import uuid
from collections.abc import Collection
from dataclasses import dataclass
from typing import Any, cast

from sqlalchemy import CursorResult, Delete, delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from hub.db.models import CustomerMemory, Draft, Feedback, Message, Run, ToolCall


@dataclass(frozen=True)
class Deleted:
    messages: int
    drafts: int
    feedback: int
    memory: int


async def rowcount(session: AsyncSession, statement: Delete) -> int:
    result = cast(CursorResult[Any], await session.execute(statement))
    return result.rowcount


async def run_ids(session: AsyncSession, ticket_ids: Collection[uuid.UUID]) -> list[uuid.UUID]:
    if not ticket_ids:
        return []
    rows = await session.execute(select(Run.id).where(Run.ticket_id.in_(ticket_ids)))
    return list(rows.scalars())


async def delete_content(session: AsyncSession, ticket_ids: Collection[uuid.UUID]) -> Deleted:
    """Messages, drafts, feedback and memory of these tickets. Runs stay (their
    `trigger_message_id` becomes NULL, `ON DELETE SET NULL`)."""
    if not ticket_ids:
        return Deleted(messages=0, drafts=0, feedback=0, memory=0)
    # Feedback first: it points at messages.
    feedback = await rowcount(session, delete(Feedback).where(Feedback.ticket_id.in_(ticket_ids)))
    memory = await rowcount(
        session, delete(CustomerMemory).where(CustomerMemory.ticket_id.in_(ticket_ids))
    )
    drafts = await rowcount(session, delete(Draft).where(Draft.ticket_id.in_(ticket_ids)))
    messages = await rowcount(session, delete(Message).where(Message.ticket_id.in_(ticket_ids)))
    return Deleted(messages=messages, drafts=drafts, feedback=feedback, memory=memory)


def names_only(arguments: dict[str, Any]) -> dict[str, Any]:
    """The form a denied call already has (data model, `tool_calls.arguments`)."""
    if set(arguments) == {"names"}:
        return arguments
    return {"names": sorted(arguments)}


async def redact_tool_calls(session: AsyncSession, runs: Collection[uuid.UUID]) -> int:
    """Keep only the argument names and drop the result summary of these runs' tool calls: a
    Jira description, a Slack text or a knowledge-base query can quote the customer. The row
    itself (tool, status, external reference) stays: it counts the per-ticket limits (T8)."""
    if not runs:
        return 0
    calls = (await session.execute(select(ToolCall).where(ToolCall.run_id.in_(runs)))).scalars()
    changed = 0
    for call in calls:
        redacted = names_only(call.arguments)
        if redacted != call.arguments or call.result_summary is not None:
            call.arguments = redacted
            call.result_summary = None
            changed += 1
    await session.flush()
    return changed
