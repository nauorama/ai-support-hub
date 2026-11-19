"""research node (docs/ai-pipeline.md §3, §4).

Step 0: one hybrid (or dense) search with the masked ticket text, `visibility = external`,
top 6. The query is code-built: the model never chooses it.

The tool loop (P7-04, ai-pipeline §4), when the pipeline has a tool client and a research
prompt (`Pipeline.researches`): the `research` model (`prompts/research/v1.md`) reads the ticket
and the step-0 hits and may call `search_kb` and the four core read tools. Every call goes
through `hub.tools` (permissions G6, a `tool_calls` row and an audit record, the scope built from
the tickets row). Results go back to the model wrapped as data (`<tool_result name="...">`), with
data tags in them neutralised. A core result becomes account facts (`hub.tools.facts`: listed
fields only, G1-masked, ids `A1`, `A2`, ...); the model reads the same fact texts. A
`search_kb` hit joins the evidence after the step-0 hits (de-duplicated, at most 8 chunks).
The loop stops when the model answers without a tool call, when the run's research calls reach
5 (counted from `tool_calls`, `search_kb` and denied calls included, so a second session of the
run continues the count), or after 20 s. A model or tool failure keeps what was found and marks
the evidence `degraded`; a budget stop (G8) fails the node like any budget stop.

The retry (P6-04, ai-pipeline §5): when `verify` found unsupported claims and no retry was used,
the graph comes back here. The node then searches once more with the unsupported claims as the
query (G1-masked first: they are model-written), keeps the hits of the first search and adds
the new ones after them, at most `SEARCH_TOP_K` more, and counts the retry (`retry_count`, at
most 1, `hub.graph.build.MAX_RETRIES`). `top_score` and `score_margin` stay those of the ticket
search, so confidence reads the same thing on both passes. `research.retry` is audited with
counts only (a claim can quote the customer). A failed retry search keeps the first evidence.

A failed search does not stop the run (ai-pipeline §3: "continues with what it has"): the
evidence is empty and `degraded`, and the writer, with nothing to cite, answers
`can_answer = false`. B0 runs no search: its writer reads the whole KB.
"""

import asyncio
import json
import logging
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from hub.audit import record
from hub.domain.account import AccountFact
from hub.domain.enums import ActorType, LlmNode, ToolCallStatus
from hub.domain.verification import Verification
from hub.graph.context import GraphContext, ids
from hub.graph.draft import search
from hub.graph.pipeline import SEARCH_TOP_K, Pipeline
from hub.graph.state import Evidence, TicketState
from hub.graph.tools import tool_scope
from hub.guardrails.injection import neutralise_data_tags
from hub.guardrails.masking import mask
from hub.kb.chunk_text import render_chunks
from hub.kb.retriever import KbChunk
from hub.llm.prompts import Prompt, PromptError, cache_system
from hub.llm.schemas import BudgetExceededError, CallContext, Message
from hub.tools.facts import add_facts, fact_texts
from hub.tools.mcp_client import LocalResult, ToolOutcome, ToolSession
from hub.tools.permissions import (
    ALLOWLIST,
    MAX_CALLS_PER_RUN,
    UNKNOWN_TOOL_NAME,
    Caller,
    ToolNode,
)
from hub.tools.registry import MODEL_TOOLS, SEARCH_KB, TOOLS, model_tool_schemas

logger = logging.getLogger(__name__)

NODE = LlmNode.RESEARCH
MAX_CALLS = MAX_CALLS_PER_RUN[ToolNode.RESEARCH]
LOOP_TIMEOUT_S = 20.0
MAX_CHUNKS = 8  # ai-pipeline §4: merged and de-duplicated, max 8
# Arguments that are not a JSON object reach the permissions as this one field, which the
# argument models forbid: the call is denied `invalid_arguments` and recorded.
INVALID_JSON = "invalid_json"


def evidence(chunks: Sequence[KbChunk], *, degraded: bool = False) -> Evidence:
    """`top_score` and `score_margin` (top minus second) for the confidence features."""
    scores = [chunk.score for chunk in chunks]
    top = scores[0] if scores else 0.0
    margin = top - scores[1] if len(scores) > 1 else top
    return Evidence(
        chunks=list(chunks), top_score=top, score_margin=round(margin, 6), degraded=degraded
    )


def hints(verification: Verification) -> list[str]:
    """The unsupported claims of a check, G1-masked (they are model-written)."""
    return [mask(claim.claim).text for claim in verification.unsupported]


def hint_query(found: Sequence[str]) -> str:
    """The retry search's query. `make llm-requests` builds the recorded retry the same way."""
    return "\n".join(found)


def retry_hints(state: TicketState) -> list[str]:
    """The hints of the last check; empty on a first pass (a new run has no verification)."""
    verification = state.get("verification")
    return hints(verification) if verification is not None else []


def merged(first: Evidence, more: Sequence[KbChunk]) -> Evidence:
    """The first search's hits, then up to `SEARCH_TOP_K` new ones in their rank order; `rank`
    is the position in the merged list, so the stored evidence reads 1 to n."""
    seen = {chunk.chunk_id for chunk in first.chunks}
    added = [chunk for chunk in more if chunk.chunk_id not in seen][:SEARCH_TOP_K]
    chunks = [
        chunk.model_copy(update={"rank": rank})
        for rank, chunk in enumerate([*first.chunks, *added], start=1)
    ]
    return first.model_copy(update={"chunks": chunks})


async def retry(state: TicketState, ctx: GraphContext, queries: list[str]) -> TicketState:
    ticket_id, run_id = ids(state)
    first = state.get("evidence") or evidence((), degraded=True)
    try:
        more = await search(ctx.pipeline, hint_query(queries))
    except Exception as exc:
        logger.warning(
            "research retry search failed",
            extra={"run_id": state["run_id"], "error": type(exc).__name__},
        )
        more = []
    found = merged(first, more)
    async with ctx.pipeline.database.sessions.begin() as session:
        await record(
            session,
            actor_type=ActorType.AI,
            actor_id=None,
            action="research.retry",
            entity_type="run",
            entity_id=run_id,
            details={
                "ticket_id": ticket_id,
                "unsupported_claims": len(queries),
                "chunks_added": len(found.chunks) - len(first.chunks),
            },
        )
    return TicketState(evidence=found, retry_count=state.get("retry_count", 0) + 1)


async def run(state: TicketState, ctx: GraphContext) -> TicketState:
    pipeline = ctx.pipeline
    if not pipeline.verifies:
        return TicketState(evidence=evidence(()))
    queries = retry_hints(state)
    if queries:
        return await retry(state, ctx, queries)
    try:
        chunks = await search(pipeline, state["message_for_llm"])
    except Exception as exc:
        logger.warning(
            "research degraded", extra={"run_id": state["run_id"], "error": type(exc).__name__}
        )
        return TicketState(evidence=evidence((), degraded=True))
    if not pipeline.researches:
        return TicketState(evidence=evidence(chunks))
    return await with_tools(state, ctx, evidence(chunks))


# --- the tool loop (P7-04) ----------------------------------------------------------------------


def research_tools() -> list[dict[str, Any]]:
    """The function schemas the model sees: the research allow-list under the model's names,
    built from the argument models (no identity argument)."""
    names = {TOOLS[name].model_name for name in ALLOWLIST[ToolNode.RESEARCH]}
    return model_tool_schemas(frozenset(name for name in names if name is not None))


def research_messages(prompt: Prompt, *, kb: str, ticket: str, history: str) -> list[Message]:
    """The first messages of the loop. `make llm-requests` uses the same function, so the
    recorded requests match what `research` sends."""
    if prompt.node != NODE.value:
        raise PromptError(f"{prompt.version} is not a research prompt")
    return cache_system(prompt.render({"kb": kb, "ticket": ticket, "history": history}))


def tool_result(name: str, body: str) -> str:
    """A tool result as data: the text inside cannot open or close a data tag."""
    return f'<tool_result name="{name}">\n{neutralise_data_tags(body)}\n</tool_result>'


def shown_tool(name: str) -> str:
    """The name in a result's tag: a model chooses it, so only a known tool name is shown."""
    return name if name in MODEL_TOOLS else UNKNOWN_TOOL_NAME


def arguments_of(text: str) -> dict[str, Any]:
    try:
        parsed = json.loads(text) if text.strip() else {}
    except ValueError:
        return {INVALID_JSON: True}
    return parsed if isinstance(parsed, dict) else {INVALID_JSON: True}


@dataclass
class Found:
    """What the loop collected so far; kept when the loop stops early."""

    chunks: list[KbChunk]
    facts: list[AccountFact] = field(default_factory=list)
    model: str | None = None
    degraded: bool = False

    def add_chunks(self, hits: Sequence[KbChunk]) -> list[KbChunk]:
        """Add new hits up to `MAX_CHUNKS`; returns the hits the evidence holds."""
        known = {chunk.chunk_id for chunk in self.chunks}
        kept: list[KbChunk] = []
        for hit in hits:
            if hit.chunk_id not in known:
                if len(self.chunks) >= MAX_CHUNKS:
                    continue
                self.chunks.append(hit)
                known.add(hit.chunk_id)
            kept.append(hit)
        return kept


def outcome_text(found: Found, outcome: ToolOutcome, hits: dict[str, KbChunk]) -> str:
    """What the model reads for one call. Core results are the fact texts, never the raw
    result; a search result is the kept hits."""
    if outcome.status is ToolCallStatus.DENIED:
        return f"The call was refused ({outcome.error})."
    if outcome.status is ToolCallStatus.ERROR or outcome.result is None:
        return f"The call failed ({outcome.error})."
    if outcome.tool == SEARCH_KB:
        kept = [hits[chunk_id] for chunk_id in outcome.result.get("chunk_ids", [])]
        return render_chunks(kept) or "No sections found."
    texts = fact_texts(outcome.tool, outcome.result)
    if texts is None:
        return "The result could not be read."
    found.facts, these = add_facts(found.facts, outcome.tool, texts)
    if not these:
        return "No room for more account facts."
    return "\n".join(f"Source id: {fact.fact_id}\n{fact.text}" for fact in these)


async def tool_loop(
    state: TicketState, pipeline: Pipeline, prompt: Prompt, session: ToolSession, found: Found
) -> None:
    hits: dict[str, KbChunk] = {}

    async def search_kb(arguments: dict[str, Any]) -> LocalResult:
        # The query is model-written: masked (G1) before it is searched; stored masked too.
        kept = found.add_chunks(await search(pipeline, mask(arguments["query"]).text))
        hits.update({hit.chunk_id: hit for hit in kept})
        ids_ = [hit.chunk_id for hit in kept]
        return LocalResult(result={"chunk_ids": ids_}, summary=" ".join(ids_) or "no hits")

    session.local(SEARCH_KB, search_kb)
    messages = research_messages(
        prompt,
        kb=render_chunks(found.chunks),
        ticket=state["message_for_llm"],
        history=state["history_for_llm"],
    )
    tools = research_tools()
    call = CallContext(
        context=pipeline.context,
        node=NODE,
        run_id=uuid.UUID(state["run_id"]),
        prompt_version=prompt.version,
    )
    # Each turn with a tool call adds at least one recorded call, so the limit ends the loop;
    # the range is only a backstop.
    for _ in range(MAX_CALLS + 1):
        if await session.calls_used() >= MAX_CALLS:
            return
        completion = await pipeline.gateway.complete(
            prompt.front.role, messages, tools=tools, call=call
        )
        found.model = completion.model
        if not completion.tool_calls:
            return
        # The model's turn goes back as it wrote it, but G1-masked: a card number in its text or
        # arguments would make the gateway refuse every later request (G2).
        messages.append(
            {
                "role": "assistant",
                "content": mask(completion.content).text if completion.content else None,
                "tool_calls": [
                    {
                        "id": tool_call.id,
                        "type": "function",
                        "function": {
                            "name": mask(tool_call.name).text,
                            "arguments": mask(tool_call.arguments).text,
                        },
                    }
                    for tool_call in completion.tool_calls
                ],
            }
        )
        for tool_call in completion.tool_calls:
            outcome = await session.call(
                tool_call.name, arguments_of(tool_call.arguments), caller=Caller.MODEL
            )
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "content": tool_result(
                        shown_tool(tool_call.name), outcome_text(found, outcome, hits)
                    ),
                }
            )


async def with_tools(state: TicketState, ctx: GraphContext, first: Evidence) -> TicketState:
    pipeline = ctx.pipeline
    tools, prompt = pipeline.tools, pipeline.research_prompt
    if tools is None or prompt is None:  # pragma: no cover - `Pipeline.researches` checks both
        return TicketState(evidence=first)
    found = Found(chunks=list(first.chunks))
    session = tools.session(await tool_scope(pipeline, state, ToolNode.RESEARCH))
    try:
        async with asyncio.timeout(LOOP_TIMEOUT_S):
            await tool_loop(state, pipeline, prompt, session, found)
    except BudgetExceededError:
        raise
    except Exception as exc:  # the loop continues with what it has (ai-pipeline §3)
        found.degraded = True
        logger.warning(
            "research loop degraded",
            extra={"run_id": state["run_id"], "error": type(exc).__name__},
        )
    chunks = [
        chunk.model_copy(update={"rank": rank}) for rank, chunk in enumerate(found.chunks, start=1)
    ]
    found_evidence = first.model_copy(
        update={
            "chunks": chunks,
            "account_facts": found.facts,
            "tool_calls": await session.calls_used(),
            "degraded": found.degraded,
        }
    )
    models = state.get("models", {})
    if found.model is not None:
        models = {**models, NODE.value: found.model}
    return TicketState(evidence=found_evidence, models=models)
