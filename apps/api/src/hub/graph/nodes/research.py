"""research node (docs/ai-pipeline.md §3, §4).

Step 0: one hybrid (or dense) search with the masked ticket text, `visibility = external`,
top 6. The query is code-built: the model never chooses it. The tool loop (a second search,
account tools) comes with P7.

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

import logging
from collections.abc import Sequence

from hub.audit import record
from hub.domain.enums import ActorType
from hub.domain.verification import Verification
from hub.graph.context import GraphContext, ids
from hub.graph.draft import search
from hub.graph.pipeline import SEARCH_TOP_K
from hub.graph.state import Evidence, TicketState
from hub.guardrails.masking import mask
from hub.kb.retriever import KbChunk

logger = logging.getLogger(__name__)


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
    return TicketState(evidence=evidence(chunks))
