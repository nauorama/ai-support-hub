"""The phase-3 workflow: a plain linear function `intake → triage → write` (P3-05).

Called by `POST /tickets` for a new ticket. There is no policy gate, no verification and no
auto-reply yet, so **every** run ends with the ticket in `pending_approval` (route `approval`,
reason `auto_reply_off`), the draft waiting for an operator. That includes a draft with
`can_answer = false`: the operator sees `missing_information` and answers. The LangGraph workflow
replaces this function in phase 6 and keeps its audit records (ai-pipeline §3: every node writes
one).

Order and transactions:
1. the run row is created and committed before the first model call: the gateway writes
   `llm_calls` rows in transactions of their own, and `llm_calls.run_id` refers to `runs.id`;
2. intake (code): `intake.done`;
3. triage (model): the ticket gets intent, urgency and summary; `triage.done`;
4. write (model): the draft is saved, the ticket moves to `pending_approval`; `draft.created`,
   `route.decided`.

On **any** error (a gateway error, an invalid answer after the repair call, a missing recording
in fake mode, a database error) the run is `failed` and the ticket goes to a human: `handed_off`
with reason `system_error` (AGENTS.md: on any error or doubt, hand off). Logs and the run row
name only the error class: an error message can quote customer text.

An operator may claim the ticket while the run is going (`processing → handed_off`, reason
`operator_takeover`). Then the run still finishes, but its draft is stored as `superseded` and
the ticket is left as the operator has it.
"""

import asyncio
import logging
import uuid
from collections.abc import Awaitable, Callable, Coroutine, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from hub.audit import record
from hub.db.models import Draft, LlmCall, Message, Run, Ticket
from hub.db.repositories import MessageRepository, TicketRepository
from hub.db.session import Database
from hub.domain.draft import DraftAnswer
from hub.domain.enums import (
    ActorType,
    DraftStatus,
    Intent,
    MessageRole,
    Route,
    RunStatus,
    TicketStatus,
    Urgency,
)
from hub.domain.triage import TriageResult
from hub.graph.nodes import triage as triage_node
from hub.graph.nodes import write as write_node
from hub.graph.nodes.intake import ChatTurn, IntakeResult, intake
from hub.kb.full_text import KbSource, load_external_sources, render_kb
from hub.llm.gateway import Gateway
from hub.llm.prompts import Prompt

logger = logging.getLogger(__name__)

GRAPH_VERSION = "linear-v1"
VARIANT = "b0_full_kb"
APPROVAL_REASONS = ["auto_reply_off"]
ERROR_REASONS = ["system_error"]

# Friendly step names of the `run.status` event (api.md §4): no internals.
STEP_UNDERSTANDING = "understanding"
STEP_WRITING = "writing"

RUN_FAILED_DETAIL = "The assistant could not prepare an answer. A support agent will reply here."

type Emit = Callable[[str, dict[str, Any]], Awaitable[None]]


class KbUnavailableError(Exception):
    """No external KB document in Postgres (`make kb-load` not run): no draft can be written."""


@dataclass(frozen=True)
class Pipeline:
    """What a run needs. Built once in the app container."""

    database: Database
    gateway: Gateway
    triage_prompt: Prompt
    write_prompt: Prompt

    @property
    def prompt_versions(self) -> dict[str, str]:
        """node -> version without the node prefix (data model §1, `runs.prompt_versions`)."""
        return {
            prompt.node: prompt.front.version for prompt in (self.triage_prompt, self.write_prompt)
        }


@dataclass(frozen=True)
class RunOutcome:
    run_id: uuid.UUID | None
    ticket_status: TicketStatus | None
    failed: bool


@dataclass
class RunTasks:
    """The runs in flight. A run is a task of its own, not part of the request: a browser that
    closes the stream does not cancel it. Held by the app container; the app waits for them at
    shutdown."""

    _tasks: set[asyncio.Task[None]] = field(default_factory=set)

    def start(self, job: Coroutine[Any, Any, None]) -> asyncio.Task[None]:
        task = asyncio.create_task(job)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def wait(self, timeout_s: float) -> None:
        if self._tasks:
            await asyncio.wait(set(self._tasks), timeout=timeout_s)


# --- pure helpers ---------------------------------------------------------------------------


def history_turns(messages: Sequence[Message], trigger_id: uuid.UUID) -> list[ChatTurn]:
    """The messages before the trigger message, oldest first, as stored (masked)."""
    turns: list[ChatTurn] = []
    for message in messages:
        if message.id == trigger_id:
            break
        turns.append(ChatTurn(role=message.role.value, text=message.body))
    return turns


def draft_from_answer(
    answer: DraftAnswer, sources: Sequence[KbSource], intake_result: IntakeResult
) -> tuple[dict[str, Any], int]:
    """The column values of the draft, and the number of citations dropped because their id is
    not an external document. Placeholders are restored with the run's mapping (G3): the
    operator never sees a dangling `<EMAIL_1>`."""
    by_id = {source.id: source for source in sources}
    citations: list[dict[str, Any]] = []
    unknown = 0
    for citation in answer.citations:
        source = by_id.get(citation.source_id)
        if source is None:
            unknown += 1
            continue
        citations.append(
            {
                "marker": citation.marker,
                "source_id": source.id,
                "doc_id": source.id,
                "title": source.title,
                "url": source.url,
            }
        )
    cited = {citation["doc_id"] for citation in citations}
    documents = [
        {"doc_id": source.id, "title": source.title, "url": source.url}
        for source in sources
        if source.id in cited
    ]
    actions = [
        {
            "id": f"a{index}",
            "type": action.type,
            "params": {
                "issue_type": action.issue_type,
                "summary": intake_result.restore(action.summary),
                "description": intake_result.restore(action.description),
            },
        }
        for index, action in enumerate(answer.proposed_actions, start=1)
    ]
    missing = answer.missing_information
    return (
        {
            "can_answer": answer.can_answer,
            "missing_information": intake_result.restore(missing) if missing else None,
            "body": intake_result.restore(answer.answer) if answer.can_answer else "",
            "citations": citations,
            "evidence": {"variant": VARIANT, "documents": documents},
            "proposed_actions": actions,
        },
        unknown,
    )


def run_failed_problem() -> dict[str, Any]:
    """Data of the `error` event (api.md §4): problem details, no internals."""
    return {
        "type": "about:blank",
        "title": "Internal Server Error",
        "status": 500,
        "detail": RUN_FAILED_DETAIL,
        "code": "internal_error",
    }


# --- steps ----------------------------------------------------------------------------------


async def _start(
    pipeline: Pipeline, ticket_id: uuid.UUID, message_id: uuid.UUID
) -> tuple[uuid.UUID, IntakeResult] | None:
    async with pipeline.database.sessions.begin() as session:
        ticket = await session.get(Ticket, ticket_id)
        if ticket is None or ticket.status != TicketStatus.PROCESSING:
            return None
        messages = await MessageRepository(session).list_for_ticket(ticket_id)
        trigger = next(
            (m for m in messages if m.id == message_id and m.role == MessageRole.CUSTOMER), None
        )
        if trigger is None:
            return None
        run = Run(
            ticket_id=ticket_id,
            trigger_message_id=message_id,
            status=RunStatus.RUNNING,
            graph_version=GRAPH_VERSION,
            prompt_versions=pipeline.prompt_versions,
        )
        session.add(run)
        await session.flush()
        result = intake(trigger.body, history_turns(messages, message_id))
        await record(
            session,
            actor_type=ActorType.AI,
            actor_id=None,
            action="intake.done",
            entity_type="run",
            entity_id=run.id,
            details={
                "ticket_id": ticket_id,
                "injection_score": result.injection.score,
                "injection_patterns": result.injection.patterns,
                "pii_placeholders": result.pii_counts,
            },
        )
        return run.id, result


async def _save_triage(
    pipeline: Pipeline,
    ticket_id: uuid.UUID,
    run_id: uuid.UUID,
    result: TriageResult,
    model: str,
    intake_result: IntakeResult,
) -> None:
    async with pipeline.database.sessions.begin() as session:
        repository = TicketRepository(session)
        ticket = await repository.lock(await _ticket(session, ticket_id))
        if ticket.status == TicketStatus.PROCESSING:
            ticket.intent = Intent(result.intent)
            ticket.secondary_intent = (
                Intent(result.secondary_intent) if result.secondary_intent else None
            )
            ticket.urgency = Urgency(result.urgency)
            ticket.summary = intake_result.restore(result.summary)
            await repository.save(ticket)
        await record(
            session,
            actor_type=ActorType.AI,
            actor_id=None,
            action="triage.done",
            entity_type="ticket",
            entity_id=ticket_id,
            details={
                "run_id": run_id,
                **triage_node.audit_details(result, pipeline.triage_prompt.version, model),
            },
        )


async def _load_kb(pipeline: Pipeline) -> list[KbSource]:
    async with pipeline.database.sessions() as session:
        sources = await load_external_sources(session)
    if not sources:
        raise KbUnavailableError
    return sources


async def _totals(session: AsyncSession, run_id: uuid.UUID) -> tuple[Decimal, int, int]:
    row = (
        await session.execute(
            select(
                func.coalesce(func.sum(LlmCall.cost_usd), 0),
                func.coalesce(func.sum(LlmCall.input_tokens), 0),
                func.coalesce(func.sum(LlmCall.output_tokens), 0),
            ).where(LlmCall.run_id == run_id)
        )
    ).one()
    return Decimal(row[0]), int(row[1]), int(row[2])


async def _finish_run(
    session: AsyncSession,
    run_id: uuid.UUID,
    *,
    status: RunStatus,
    route: Route,
    reasons: list[str],
    models: dict[str, str],
    error: str | None = None,
) -> None:
    run = await session.get(Run, run_id)
    if run is None:  # pragma: no cover - the row was committed in `_start`
        return
    run.total_cost_usd, run.input_tokens, run.output_tokens = await _totals(session, run_id)
    run.status = status
    run.route = route
    run.route_reasons = reasons
    run.models = models
    run.error = error
    run.finished_at = datetime.now(UTC)


async def _ticket(session: AsyncSession, ticket_id: uuid.UUID) -> Ticket:
    ticket = await session.get(Ticket, ticket_id)
    if ticket is None:  # pragma: no cover - tickets are never deleted while a run goes on
        raise LookupError("ticket")
    return ticket


async def _save_draft(
    pipeline: Pipeline,
    ticket_id: uuid.UUID,
    run_id: uuid.UUID,
    answer: DraftAnswer,
    sources: Sequence[KbSource],
    intake_result: IntakeResult,
    models: dict[str, str],
) -> TicketStatus:
    values, unknown = draft_from_answer(answer, sources, intake_result)
    async with pipeline.database.sessions.begin() as session:
        repository = TicketRepository(session)
        ticket = await repository.lock(await _ticket(session, ticket_id))
        waits = ticket.status == TicketStatus.PROCESSING
        draft = Draft(
            run_id=run_id,
            ticket_id=ticket_id,
            status=DraftStatus.PENDING if waits else DraftStatus.SUPERSEDED,
            **values,
        )
        session.add(draft)
        await _finish_run(
            session,
            run_id,
            status=RunStatus.COMPLETED,
            route=Route.APPROVAL,
            reasons=APPROVAL_REASONS,
            models=models,
        )
        await session.flush()
        await record(
            session,
            actor_type=ActorType.AI,
            actor_id=None,
            action="draft.created",
            entity_type="draft",
            entity_id=draft.id,
            details={
                "run_id": run_id,
                "ticket_id": ticket_id,
                "status": draft.status.value,
                "can_answer": answer.can_answer,
                "citations": len(values["citations"]),
                "unknown_citations": unknown,
                "proposed_actions": len(values["proposed_actions"]),
                "prompt_version": pipeline.write_prompt.version,
                "model": models["write"],
            },
        )
        if waits:
            ticket.last_route = Route.APPROVAL
            ticket.route_reasons = APPROVAL_REASONS
            await repository.change_status(ticket, TicketStatus.PENDING_APPROVAL)
            await _audit_route(session, ticket_id, run_id, Route.APPROVAL, APPROVAL_REASONS)
        return ticket.status


async def _audit_route(
    session: AsyncSession,
    ticket_id: uuid.UUID,
    run_id: uuid.UUID | None,
    route: Route,
    reasons: list[str],
) -> None:
    await record(
        session,
        actor_type=ActorType.SYSTEM,
        actor_id=None,
        action="route.decided",
        entity_type="ticket",
        entity_id=ticket_id,
        details={
            "run_id": run_id,
            "route": route.value,
            "reasons": reasons,
            "graph_version": GRAPH_VERSION,
        },
    )


async def _fail(
    pipeline: Pipeline,
    ticket_id: uuid.UUID,
    run_id: uuid.UUID | None,
    error: str,
    models: dict[str, str],
) -> TicketStatus | None:
    """The run is `failed`, the ticket goes to a human (`system_error`) unless it already has
    one. Returns the ticket status."""
    async with pipeline.database.sessions.begin() as session:
        if run_id is not None:
            await _finish_run(
                session,
                run_id,
                status=RunStatus.FAILED,
                route=Route.HANDOFF,
                reasons=ERROR_REASONS,
                models=models,
                error=error,
            )
        repository = TicketRepository(session)
        ticket = await repository.lock(await _ticket(session, ticket_id))
        if ticket.status == TicketStatus.PROCESSING:
            ticket.last_route = Route.HANDOFF
            ticket.route_reasons = ERROR_REASONS
            await repository.change_status(ticket, TicketStatus.HANDED_OFF)
            await _audit_route(session, ticket_id, run_id, Route.HANDOFF, ERROR_REASONS)
        return ticket.status


# --- the run --------------------------------------------------------------------------------


async def run_ticket(
    pipeline: Pipeline, *, ticket_id: uuid.UUID, message_id: uuid.UUID, emit: Emit
) -> RunOutcome:
    """One run for the customer message `message_id` of a ticket in `processing`. Never raises
    for a failed step: the failure is recorded and the ticket handed off. Emits the stream
    events `run.status`, then `ticket.status` (and `error` before it when the run failed)."""
    run_id: uuid.UUID | None = None
    models: dict[str, str] = {}
    try:
        await emit("run.status", {"step": STEP_UNDERSTANDING})
        started = await _start(pipeline, ticket_id, message_id)
        if started is None:
            # Nothing to do: the ticket is no longer the AI's, or the message is not its own.
            return RunOutcome(run_id=None, ticket_status=None, failed=False)
        run_id, intake_result = started

        triaged = await triage_node.triage_structured(
            pipeline.gateway, pipeline.triage_prompt, intake_result.message_for_llm, run_id=run_id
        )
        models["triage"] = triaged.model
        await _save_triage(pipeline, ticket_id, run_id, triaged.value, triaged.model, intake_result)

        await emit("run.status", {"step": STEP_WRITING})
        sources = await _load_kb(pipeline)
        written = await write_node.write(
            pipeline.gateway,
            pipeline.write_prompt,
            kb=render_kb(sources),
            ticket=intake_result.message_for_llm,
            history=intake_result.history_for_llm,
            run_id=run_id,
        )
        models["write"] = written.model
        status = await _save_draft(
            pipeline, ticket_id, run_id, written.value, sources, intake_result, models
        )
    except Exception as exc:
        error = type(exc).__name__
        logger.warning("run failed", extra={"run_id": str(run_id), "error": error})
        try:
            failed_status = await _fail(pipeline, ticket_id, run_id, error, models)
        except Exception as second:
            logger.error(
                "run failure could not be recorded",
                extra={"run_id": str(run_id), "error": type(second).__name__},
            )
            failed_status = None
        await emit("error", run_failed_problem())
        if failed_status is not None:
            await emit("ticket.status", {"status": failed_status.value})
        return RunOutcome(run_id=run_id, ticket_status=failed_status, failed=True)
    await emit("ticket.status", {"status": status.value})
    return RunOutcome(run_id=run_id, ticket_status=status, failed=False)
