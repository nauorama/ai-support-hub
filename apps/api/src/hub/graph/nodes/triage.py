"""Triage node (docs/ai-pipeline.md §3): one structured call on the `triage` role.

The ticket text is already masked (G1 runs in the API before anything else). Any gateway error
(`StructuredOutputError`, `LlmUnavailableError`, a G2 block) is raised to the caller, which hands
the ticket to a human with reason `system_error`; this function never guesses a result.
"""

import uuid

from hub.domain.enums import LlmCallContext, LlmNode
from hub.domain.triage import TriageResult
from hub.llm.gateway import Gateway
from hub.llm.prompts import Prompt, PromptError
from hub.llm.schemas import CallContext, Message

NODE = LlmNode.TRIAGE


def triage_messages(prompt: Prompt, ticket: str) -> list[Message]:
    """The messages of a triage call. `make llm-requests` uses the same function, so the
    recorded requests match what `triage` sends."""
    if prompt.node != NODE.value:
        raise PromptError(f"{prompt.version} is not a triage prompt")
    return prompt.render({"ticket": ticket})


async def triage(
    gateway: Gateway,
    prompt: Prompt,
    ticket: str,
    *,
    run_id: uuid.UUID | None = None,
    context: LlmCallContext = LlmCallContext.ONLINE,
) -> TriageResult:
    call = CallContext(context=context, node=NODE, run_id=run_id, prompt_version=prompt.version)
    result = await gateway.complete_structured(
        prompt.front.role, triage_messages(prompt, ticket), TriageResult, call=call
    )
    return result.value
