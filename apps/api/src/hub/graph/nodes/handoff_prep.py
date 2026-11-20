"""handoff_prep node (docs/ai-pipeline.md §3): the ticket goes to a human (P6-01, P6-02).

1. The operator summary (`handoff_summary` prompt, role `triage`): three lines, placeholders
   restored, then G1-masked, stored in `tickets.summary` in place of the triage summary. It is
   for operators only: never in the notice, never in the stream. Skipped when a node failed (the
   model or the budget is the likely cause) or no prompt is configured; any failure, a timeout
   included, gives the notice without a summary.
2. In one transaction: the ticket moves to `handed_off` with the route's reasons and
   `route.decided`, and the customer notice is saved as a `system` message (fixed texts from
   `config/policy.yaml`, `hub.guardrails.policy.handoff_notice`: the opening of the first reason,
   the safe first steps of every matching hard rule, the expected response time from the support
   hours), with `message.sent`. When an operator already has the ticket (claimed while the run
   worked), nothing changes and no notice is sent.
3. After the commit, when the notice is urgent (`is_urgent`: triage urgency `high`, an urgent
   reason or intent; it promises a reply within 30 or 60 minutes), one Slack alert to
   `#support-escalations` through `hub.tools` (P7-05): called by code, before the pause, with the
   approval basis `urgent_handoff` and the scope's urgency `high` from the policy (never from
   arguments), so every urgent notice is backed by an alert. Its text holds the ticket id, the
   intent and the reasons, never customer text. A failed alert is recorded in `tool_calls` (shown
   in the console) and never fails the handoff.
4. The notice goes to the stream (`message` event with its id), before the run waits for the
   operator (ai-pipeline §1).

A failure in steps 2 and 4 is not caught: the run's last resort (`hub.graph.run`) records it and
hands the ticket off.
"""

import asyncio
import logging
import uuid
from datetime import UTC, datetime

from hub.audit import record
from hub.db.repositories import MessageRepository, TicketRepository
from hub.domain.enums import ActorType, LlmNode, MessageRole, TicketStatus, ToolCallStatus
from hub.domain.handoff import HandoffSummary
from hub.graph.context import GraphContext, failure_reasons, ids
from hub.graph.draft import OutputMask
from hub.graph.nodes.intake import intake_result
from hub.graph.nodes.policy_gate import restricted
from hub.graph.pipeline import Pipeline
from hub.graph.routing import gate_matches
from hub.graph.state import TicketState
from hub.graph.store import hand_off, ticket
from hub.graph.tools import tool_scope
from hub.guardrails.policy import handoff_notice, is_urgent, safe_first_steps
from hub.llm.schemas import CallContext
from hub.tools.permissions import Approval, ApprovalBasis, ToolNode

logger = logging.getLogger(__name__)

# Shorter than the node's own timeout, so a slow summary never fails the handoff.
SUMMARY_TIMEOUT_S = 20.0
NO_RESEARCH = "(the assistant did not search)"
SLACK_TOOL = "slack_post_escalation"
# The alert's own limit: the summary (`SUMMARY_TIMEOUT_S`) and the alert together stay well
# inside the node's timeout (graph/build.py, a test checks it), so a hung Slack call is an
# `error` row, never a failed handoff (review finding 5, P7-05).
ALERT_TIMEOUT_S = 8.0


def reasons(state: TicketState) -> list[str]:
    """The route's reasons; when the node that should have set them failed (e.g. `decide`
    itself), the failure's reason, so a handoff never goes out without one (row 2)."""
    return list(state.get("route_reasons") or failure_reasons(state.get("errors", [])))


def findings(state: TicketState) -> str:
    """What the assistant found, for the `<kb>` part of the summary prompt: section titles and
    what the writer said was missing. No chunk text."""
    evidence = state.get("evidence")
    if evidence is None:
        return NO_RESEARCH
    lines = [f"- {chunk.title}: {chunk.section}" for chunk in evidence.chunks] or ["(no hits)"]
    draft = state.get("draft")
    if draft is not None and draft.missing_information:
        lines.append(f"Missing according to the assistant: {draft.missing_information}")
    return "\n".join(lines)


async def operator_summary(state: TicketState, pipeline: Pipeline, why: list[str]) -> str | None:
    prompt = pipeline.handoff_prompt
    if prompt is None or state.get("errors"):
        return None
    _, run_id = ids(state)
    call = CallContext(
        context=pipeline.context,
        node=LlmNode.HANDOFF_PREP,
        run_id=run_id,
        prompt_version=prompt.version,
    )
    messages = prompt.render(
        {
            "reasons": ", ".join(why),
            "ticket": state.get("message_for_llm", ""),
            "history": state.get("history_for_llm", ""),
            "kb": findings(state),
        }
    )
    try:
        async with asyncio.timeout(SUMMARY_TIMEOUT_S):
            result = await pipeline.gateway.complete_structured(
                prompt.front.role, messages, HandoffSummary, call=call
            )
    except Exception as exc:
        logger.warning(
            "handoff summary failed",
            extra={"run_id": str(run_id), "error": type(exc).__name__},
        )
        return None
    return OutputMask(intake_result(state))(result.value.lines())


async def run(state: TicketState, ctx: GraphContext) -> TicketState:
    pipeline = ctx.pipeline
    policy = pipeline.policy
    ticket_id, run_id = ids(state)
    why = reasons(state)
    triage = state.get("triage")
    matches = gate_matches(
        policy,
        triage,
        state.get("message_masked", ""),
        state.get("input_flags"),
        restricted(state),
    )
    urgent = is_urgent(
        policy, matches, triage.urgency if triage else None, triage.intent if triage else None
    )
    notice = handoff_notice(policy, why, matches, urgent=urgent, now=datetime.now(UTC))
    summary = await operator_summary(state, pipeline, why)
    message_id: uuid.UUID | None = None
    async with pipeline.database.sessions.begin() as session:
        found = await TicketRepository(session).lock(await ticket(session, ticket_id))
        ours = found.status == TicketStatus.PROCESSING
        if ours and summary is not None:
            found.summary = summary
        status = await hand_off(
            session,
            pipeline,
            ticket_id,
            run_id,
            why,
            state.get("confidence"),
            state.get("features"),
        )
        if ours:
            message = await MessageRepository(session).add(
                ticket_id=ticket_id, role=MessageRole.SYSTEM, body=notice, run_id=run_id
            )
            message_id = message.id
            await record(
                session,
                actor_type=ActorType.SYSTEM,
                actor_id=None,
                action="message.sent",
                entity_type="ticket",
                entity_id=ticket_id,
                details={
                    "message_id": message.id,
                    "run_id": run_id,
                    "kind": "handoff_notice",
                    "reasons": why,
                    "urgent": urgent,
                    "safe_first_steps": len(safe_first_steps(policy, matches)),
                    "operator_summary": summary is not None,
                },
            )
    if message_id is not None and urgent:
        await urgent_alert(state, ctx, why)
    if message_id is not None:
        await ctx.emit("message", {"message_id": str(message_id)})
    return TicketState(ticket_status=status.value)


def alert_text(ticket_id: uuid.UUID, intent: str | None, why: list[str]) -> str:
    """Fixed fields only: the alert goes to a Slack channel, never customer text."""
    return (
        f"Urgent handoff: ticket {ticket_id}, intent {intent or 'unknown'}, "
        f"reasons {', '.join(why) or 'none'}. Reply due within the urgent response time."
    )


async def urgent_alert(state: TicketState, ctx: GraphContext, why: list[str]) -> bool:
    """One Slack alert for an urgent handoff; True when it was posted. Never raises: the
    notice is already sent, and the tool client records a failed or denied call."""
    tools = ctx.pipeline.tools
    if tools is None:
        return False
    ticket_id, run_id = ids(state)
    triage = state.get("triage")
    text = alert_text(ticket_id, triage.intent if triage else None, why)
    try:
        scope = await tool_scope(ctx.pipeline, state, ToolNode.HANDOFF_PREP, urgent=True)
        outcome = await tools.session(scope).call(
            SLACK_TOOL,
            {"text": text},
            timeout_s=ALERT_TIMEOUT_S,
            approval=Approval(
                tool=SLACK_TOOL,
                ticket_id=ticket_id,
                basis=ApprovalBasis.URGENT_HANDOFF,
                arguments={"text": text},
            ),
        )
    except Exception as exc:
        logger.warning(
            "urgent alert failed", extra={"run_id": str(run_id), "error": type(exc).__name__}
        )
        return False
    if outcome.status is not ToolCallStatus.OK:
        logger.warning(
            "urgent alert not posted", extra={"run_id": str(run_id), "status": outcome.status}
        )
        return False
    return True
