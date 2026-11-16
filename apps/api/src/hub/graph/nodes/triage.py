"""Triage node (docs/ai-pipeline.md §3): one structured call on the `triage` role.

The ticket text is already masked (G1 runs in the API before anything else). Any gateway error
(`StructuredOutputError`, `LlmUnavailableError`, a G2 block) is raised to the caller, which hands
the ticket to a human with reason `system_error`; this function never guesses a result.

The caller writes the `triage.done` audit record with `audit_details` (security §5): the
decision and the prompt version, never the ticket text or the summary.
"""

import uuid

from hub.domain.enums import LlmCallContext, LlmNode
from hub.domain.triage import TriageResult
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
