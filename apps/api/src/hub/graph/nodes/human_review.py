"""human_review node (docs/ai-pipeline.md §3, ADR-0008).

P6-01, approval route: the draft is stored for the operator and the ticket waits in
`pending_approval`, in one transaction with `draft.created`, `verify.done` (RAG variants) and
`route.decided`. When an operator claimed the ticket while the run worked, the draft is stored
as `superseded` and the ticket is left as the operator has it. On the handoff route
`handoff_prep` already moved the ticket; a draft the run wrote (a handoff from `decide`) is stored
as `superseded` for the operator (P6-02).

Then the run pauses with `interrupt()` (P6-03, ADR-0008) while a human has the ticket and has not
answered it: `runs.status = interrupted`, the state stays in the Postgres checkpoint under the
ticket. The console resumes it (`hub.graph.run.resume_run`) with a `ReviewDecision` once the
operator approved, edited or rejected the draft, sent the first reply of a handoff or closed the
ticket. A failure here is not caught: the run's last resort (`hub.graph.run`) records it and
hands the ticket off.
"""

from langgraph.types import interrupt
from sqlalchemy.ext.asyncio import AsyncSession

from hub.audit import record
from hub.db.models import Draft, Run
from hub.db.repositories import DraftRepository, TicketRepository
from hub.domain.enums import ActorType, DraftStatus, Route, RunStatus, TicketStatus
from hub.domain.review import ReviewDecision
from hub.graph.context import GraphContext, error_class, ids
from hub.graph.draft import (
    OutputMask,
    draft_from_answer,
    evidence_for_writer,
    verify_audit_details,
)
from hub.graph.nodes.handoff_prep import reasons
from hub.graph.nodes.intake import intake_result
from hub.graph.pipeline import Pipeline
from hub.graph.state import TicketState
from hub.graph.store import audit_route, finish_run, ticket, to_decimal

# A human has the ticket and has not answered yet: the run waits for the decision.
WAITING = frozenset({TicketStatus.PENDING_APPROVAL, TicketStatus.HANDED_OFF})


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


async def record_outcome(session: AsyncSession, state: TicketState, status: RunStatus) -> None:
    """The run row's outcome: route, reasons, confidence, features, models, totals, error. At the
    pause (`interrupted`, not finished yet) and again when the run ends (`respond`)."""
    _, run_id = ids(state)
    errors = state.get("errors", [])
    route = state.get("route") or "handoff"
    await finish_run(
        session,
        run_id,
        status=status,
        route=Route(route),
        reasons=reasons(state) if route == "handoff" else list(state.get("route_reasons", [])),
        models=dict(state.get("models", {})),
        error=error_class(errors),
        confidence=state.get("confidence"),
        features=state.get("features"),
    )


async def prepare(state: TicketState, ctx: GraphContext) -> tuple[TicketStatus, bool]:
    """The work before the pause: the draft for the operator and the ticket's move to
    `pending_approval` (approval route). Returns the ticket's status and whether the run waits.

    Safe to run again (ADR-0008): a recovered run may repeat it after a crash. A draft the run
    already stored is not stored again, and the ticket moves only from `processing`.

    The run waits when a human now has the ticket and has not answered it yet
    (`pending_approval` or `handed_off`); `runs.status` becomes `interrupted` in the same
    transaction. An operator who already answered or closed the ticket while the run worked
    (claimed it, `operator_takeover`) leaves nothing to wait for: the run goes on and ends.
    """
    pipeline = ctx.pipeline
    ticket_id, run_id = ids(state)
    approval = state.get("route") == "approval" and state.get("draft") is not None
    async with pipeline.database.sessions.begin() as session:
        repository = TicketRepository(session)
        found = await repository.lock(await ticket(session, ticket_id))
        stored = await DraftRepository(session).for_run(run_id) is not None
        if approval and found.status == TicketStatus.PROCESSING and not stored:
            await store_draft(session, pipeline, state, status=DraftStatus.PENDING)
            found.last_route = Route.APPROVAL
            found.route_reasons = list(state.get("route_reasons", []))
            found.confidence = to_decimal(state.get("confidence"))
            await repository.change_status(found, TicketStatus.PENDING_APPROVAL)
            await audit_route(
                session,
                pipeline,
                ticket_id,
                run_id,
                Route.APPROVAL,
                list(state.get("route_reasons", [])),
                state.get("confidence"),
                state.get("features"),
            )
        elif state.get("draft") is not None and not stored:
            # A handoff after `decide` (e.g. `no_kb_answer`, `masking_failed`): `handoff_prep`
            # moved the ticket; the draft is kept for the operator as `superseded` (its missing
            # information and the `verify.done` codes stay readable), never sent. Also the draft
            # of an approval when an operator claimed the ticket while the run worked.
            await store_draft(session, pipeline, state, status=DraftStatus.SUPERSEDED)
        # A failed run has nothing to resume for (no draft, no actions): it ends `failed` and
        # the operator answers with plain replies.
        waits = found.status in WAITING and not state.get("errors")
        if waits:
            await record_outcome(session, state, RunStatus.INTERRUPTED)
        return found.status, waits


async def run(state: TicketState, ctx: GraphContext) -> TicketState:
    """`prepare`, then the pause. LangGraph runs the node again from the start when the run is
    resumed, and `interrupt()` then returns the decision instead of pausing: a run that is
    already `interrupted` therefore skips `prepare` (its ticket has moved on since, so
    `prepare` would no longer say that the run waits)."""
    _, run_id = ids(state)
    async with ctx.pipeline.database.sessions.begin() as session:
        run_row = await session.get(Run, run_id)
        paused = run_row is not None and run_row.status == RunStatus.INTERRUPTED
    if not paused:
        status, waits = await prepare(state, ctx)
        if not waits:
            return TicketState(ticket_status=status.value)
    # Ids only: the console reads our own tables, never the checkpoint (ADR-0008).
    value = interrupt({"ticket_id": state["ticket_id"], "run_id": state["run_id"]})
    return TicketState(review=ReviewDecision.model_validate(value))
