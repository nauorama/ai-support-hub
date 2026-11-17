"""Triage node (docs/ai-pipeline.md §3): one structured call on the `triage` role.

The ticket text is already masked (G1 runs in the API before anything else). Any gateway error
(`StructuredOutputError`, `LlmUnavailableError`, a G2 block, a G8 budget stop) is raised to the
caller, which hands the ticket to a human with reason `system_error` (`budget_exceeded` for the
budget stop); this function never guesses a result.

The graph node (`run`, P6-01) checks the daily budget first (G8: once per run, before the first
model call), then saves intent, urgency and the card-masked summary on the ticket and writes
`triage.done` with `audit_details` (security §5): the decision and the prompt version, never the
ticket text or the summary.
"""

import uuid

from hub.audit import record
from hub.db.repositories import TicketRepository
from hub.domain.enums import ActorType, Intent, LlmCallContext, LlmNode, TicketStatus, Urgency
from hub.domain.triage import TriageResult
from hub.graph.context import GraphContext, ids
from hub.graph.draft import OutputMask
from hub.graph.nodes.intake import intake_result
from hub.graph.state import TicketState
from hub.graph.store import ticket
from hub.llm.gateway import Gateway
from hub.llm.prompts import Prompt, PromptError
from hub.llm.schemas import CallContext, Message, Structured

NODE = LlmNode.TRIAGE


def triage_messages(prompt: Prompt, ticket: str) -> list[Message]:
    """The messages of a triage call. `make llm-requests` uses the same function, so the
    recorded requests match what `triage` sends."""
    if prompt.node != NODE.value:
        raise PromptError(f"{prompt.version} is not a triage prompt")
    return prompt.render({"ticket": ticket})


async def triage_structured(
    gateway: Gateway,
    prompt: Prompt,
    ticket: str,
    *,
    run_id: uuid.UUID | None = None,
    context: LlmCallContext = LlmCallContext.ONLINE,
) -> Structured[TriageResult]:
    """The result with the model, tokens and cost of the call."""
    call = CallContext(context=context, node=NODE, run_id=run_id, prompt_version=prompt.version)
    return await gateway.complete_structured(
        prompt.front.role, triage_messages(prompt, ticket), TriageResult, call=call
    )


async def triage(
    gateway: Gateway,
    prompt: Prompt,
    ticket: str,
    *,
    run_id: uuid.UUID | None = None,
    context: LlmCallContext = LlmCallContext.ONLINE,
) -> TriageResult:
    result = await triage_structured(gateway, prompt, ticket, run_id=run_id, context=context)
    return result.value


def audit_details(result: TriageResult, prompt_version: str, model: str) -> dict[str, object]:
    """Details of `triage.done`: the decision, never the ticket text or the summary (the summary
    is model text about the ticket and can repeat it)."""
    return {
        "intent": result.intent,
        "secondary_intent": result.secondary_intent,
        "urgency": result.urgency,
        "wants_human": result.wants_human,
        "reports_unauthorised_activity": result.reports_unauthorised_activity,
        "is_complaint_or_legal": result.is_complaint_or_legal,
        "asks_for_action": result.asks_for_action,
        "injection_suspected": result.injection_suspected,
        "prompt_version": prompt_version,
        "model": model,
    }


async def run(state: TicketState, ctx: GraphContext) -> TicketState:
    pipeline = ctx.pipeline
    ticket_id, run_id = ids(state)
    await pipeline.budget.check()
    triaged = await triage_structured(
        pipeline.gateway,
        pipeline.triage_prompt,
        state["message_for_llm"],
        run_id=run_id,
        context=pipeline.context,
    )
    result = triaged.value
    clean = OutputMask(intake_result(state))
    async with pipeline.database.sessions.begin() as session:
        repository = TicketRepository(session)
        found = await repository.lock(await ticket(session, ticket_id))
        if found.status == TicketStatus.PROCESSING:
            found.intent = Intent(result.intent)
            found.secondary_intent = (
                Intent(result.secondary_intent) if result.secondary_intent else None
            )
            found.urgency = Urgency(result.urgency)
            found.summary = clean(result.summary)
            await repository.save(found)
        await record(
            session,
            actor_type=ActorType.AI,
            actor_id=None,
            action="triage.done",
            entity_type="ticket",
            entity_id=ticket_id,
            details={
                "run_id": run_id,
                **audit_details(result, pipeline.triage_prompt.version, triaged.model),
                "summary_masked": dict(clean.counts),
            },
        )
    return TicketState(triage=result, models={**state.get("models", {}), "triage": triaged.model})
