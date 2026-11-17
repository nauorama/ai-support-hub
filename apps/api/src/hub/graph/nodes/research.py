"""research node (docs/ai-pipeline.md §3, §4).

P6-01 builds step 0 only: one hybrid (or dense) search with the masked ticket text,
`visibility = external`, top 6. The query is code-built: the model never chooses it. The tool
loop (a second search, account tools) comes with P7; the retry hints with P6-04.

A failed search does not stop the run (ai-pipeline §3: "continues with what it has"): the
evidence is empty and `degraded`, and the writer, with nothing to cite, answers
`can_answer = false`. B0 runs no search: its writer reads the whole KB.
"""

import logging
from collections.abc import Sequence

from hub.graph.context import GraphContext
from hub.graph.draft import search
from hub.graph.state import Evidence, TicketState
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


async def run(state: TicketState, ctx: GraphContext) -> TicketState:
    pipeline = ctx.pipeline
    if not pipeline.verifies:
        return TicketState(evidence=evidence(()))
    try:
        chunks = await search(pipeline, state["message_for_llm"])
    except Exception as exc:
        logger.warning(
            "research degraded", extra={"run_id": state["run_id"], "error": type(exc).__name__}
        )
        return TicketState(evidence=evidence((), degraded=True))
    return TicketState(evidence=evidence(chunks))
