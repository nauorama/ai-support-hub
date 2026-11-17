"""Write node (docs/ai-pipeline.md §3): one structured call on the `write` role.

RAG (`write/v2`, P5-03): the chunks of the research step go into the user part with their
`Source id:` lines, and citations name chunk ids.

B0 (ai-pipeline §10) has no retrieval: every external KB document is in the system part of the
prompt (`hub.kb.full_text`), marked for prompt caching, and the ticket and history are in the
user part. The texts are already masked (G1) and hold placeholders (G3). Any gateway error is
raised to the caller, which hands the ticket to a human with reason `system_error`
(`budget_exceeded` for a G8 budget stop); in the graph (`run`, P6-01) the error is recorded
in the state and the ticket is handed off.
"""

import uuid

from hub.domain.draft import DraftAnswer
from hub.domain.enums import LlmCallContext, LlmNode
from hub.graph.context import GraphContext, ids
from hub.graph.draft import evidence_for_writer
from hub.graph.state import TicketState
from hub.llm.gateway import Gateway
from hub.llm.prompts import Prompt, PromptError, cache_system
from hub.llm.schemas import CallContext, Message, Structured

NODE = LlmNode.WRITE


def write_messages(prompt: Prompt, *, kb: str, ticket: str, history: str) -> list[Message]:
    """The messages of a write call. `make llm-requests` uses the same function, so the
    recorded requests match what `write` sends."""
    if prompt.node != NODE.value:
        raise PromptError(f"{prompt.version} is not a write prompt")
    return cache_system(prompt.render({"kb": kb, "ticket": ticket, "history": history}))


async def write(
    gateway: Gateway,
    prompt: Prompt,
    *,
    kb: str,
    ticket: str,
    history: str,
    run_id: uuid.UUID | None = None,
    context: LlmCallContext = LlmCallContext.ONLINE,
) -> Structured[DraftAnswer]:
    call = CallContext(context=context, node=NODE, run_id=run_id, prompt_version=prompt.version)
    messages = write_messages(prompt, kb=kb, ticket=ticket, history=history)
    return await gateway.complete_structured(prompt.front.role, messages, DraftAnswer, call=call)


async def run(state: TicketState, ctx: GraphContext) -> TicketState:
    pipeline = ctx.pipeline
    _, run_id = ids(state)
    research = state.get("evidence")
    evidence = await evidence_for_writer(pipeline, research.chunks if research else ())
    written = await write(
        pipeline.gateway,
        pipeline.write_prompt,
        kb=evidence.kb,
        ticket=state["message_for_llm"],
        history=state["history_for_llm"],
        run_id=run_id,
        context=pipeline.context,
    )
    return TicketState(
        draft=written.value, models={**state.get("models", {}), "write": written.model}
    )
