"""human_review node (docs/ai-pipeline.md §3, ADR-0008).

P6-01, approval route: the draft is stored for the operator and the ticket waits in
`pending_approval`, in one transaction with `draft.created`, `verify.done` (RAG variants) and
`route.decided`. When an operator claimed the ticket while the run worked, the draft is stored
as `superseded` and the ticket is left as the operator has it. On the handoff route
`handoff_prep` already moved the ticket; a draft the run wrote (a handoff from `decide`) is stored
as `superseded` for the operator (P6-02).

The run does not pause yet: the operator's decision goes through the console endpoints as in
phase 3. P6-03 adds `interrupt()` here and resumes the run with `Command(resume=…)`. A failure
here is not caught: the run's last resort (`hub.graph.run`) records it and hands the ticket off.
"""

from sqlalchemy.ext.asyncio import AsyncSession

from hub.audit import record
from hub.db.models import Draft
from hub.db.repositories import TicketRepository
from hub.domain.enums import ActorType, DraftStatus, Route, TicketStatus
from hub.graph.context import GraphContext, ids
from hub.graph.draft import (
    OutputMask,
    draft_from_answer,
    evidence_for_writer,
    verify_audit_details,
)
from hub.graph.nodes.intake import intake_result
from hub.graph.pipeline import Pipeline
from hub.graph.state import TicketState
from hub.graph.store import audit_route, ticket, to_decimal


async def store_draft(
    session: AsyncSession, pipeline: Pipeline, state: TicketState, *, status: DraftStatus
) -> tuple[Draft, OutputMask]:
    """The run's draft with `draft.created` and `verify.done` (RAG variants). Also used by
    `respond` for an auto-reply (status `approved`)."""
    answer = state["draft"]
    if answer is None:  # pragma: no cover - callers check the draft first
        raise ValueError("no draft")
    ticket_id, run_id = ids(state)
    research = state.get("evidence")
    evidence = await evidence_for_writer(pipeline, research.chunks if research else ())
    verification = state.get("verification")
    models = state.get("models", {})
    clean = OutputMask(intake_result(state))
    values, unknown = draft_from_answer(answer, evidence, clean, verification)
    draft = Draft(run_id=run_id, ticket_id=ticket_id, status=status, **values)
    session.add(draft)
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
            "variant": evidence.variant,
            "research_degraded": bool(research and research.degraded),
            # Ids and scores only: the chunk texts are in the draft.
            "chunks": [
                {"chunk_id": chunk.chunk_id, "score": round(chunk.score, 4)}
                for chunk in evidence.chunks or ()
            ],
            "citations": len(values["citations"]),
            "unknown_citations": unknown,
            "dropped_markers": len(values["evidence"]["dropped_markers"]),
            "masked": dict(clean.counts),
            "proposed_actions": len(values["proposed_actions"]),
            "prompt_version": pipeline.write_prompt.version,
            "model": models.get("write"),
        },
    )
    if verification is not None and pipeline.verify_prompt is not None:
        details = verify_audit_details(verification)
        if state.get("verify_error"):
            details["llm_check"] = False
            details["error"] = state.get("verify_error")
        await record(
            session,
            actor_type=ActorType.AI,
            actor_id=None,
            action="verify.done",
            entity_type="draft",
            entity_id=draft.id,
            details={
                "run_id": run_id,
                "ticket_id": ticket_id,
                **details,
                "prompt_version": pipeline.verify_prompt.version,
                "model": models.get("verify"),
            },
        )
    return draft, clean


async def run(state: TicketState, ctx: GraphContext) -> TicketState:
    if state.get("draft") is None:
        return TicketState()
    pipeline = ctx.pipeline
    if state.get("route") != "approval":
        # A handoff after `decide` (e.g. `no_kb_answer`, `masking_failed`): `handoff_prep` moved
        # the ticket; the draft is kept for the operator as `superseded` (its missing information
        # and the `verify.done` codes stay readable), never sent.
        async with pipeline.database.sessions.begin() as session:
            await store_draft(session, pipeline, state, status=DraftStatus.SUPERSEDED)
        return TicketState()
    ticket_id, run_id = ids(state)
    reasons = list(state.get("route_reasons", []))
    async with pipeline.database.sessions.begin() as session:
        repository = TicketRepository(session)
        found = await repository.lock(await ticket(session, ticket_id))
        waits = found.status == TicketStatus.PROCESSING
        await store_draft(
            session,
            pipeline,
            state,
            status=DraftStatus.PENDING if waits else DraftStatus.SUPERSEDED,
        )
        if waits:
            found.last_route = Route.APPROVAL
            found.route_reasons = reasons
            found.confidence = to_decimal(state.get("confidence"))
            await repository.change_status(found, TicketStatus.PENDING_APPROVAL)
            await audit_route(
                session,
                pipeline,
                ticket_id,
                run_id,
                Route.APPROVAL,
                reasons,
                state.get("confidence"),
                state.get("features"),
            )
        return TicketState(ticket_status=found.status.value)
