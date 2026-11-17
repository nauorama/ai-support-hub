"""The workflow graph (docs/ai-pipeline.md §1, ADR-0002), P6-01.

```
intake → triage → policy_gate ─┬─ handoff → handoff_prep ─────────────┐
                               └─ continue → research → write → verify │
              verify ─┬─ retry (P6-04) → research                      │
                      └─ decide ─┬─ auto_reply → respond               │
                                 ├─ approval → human_review ◄──────────┘
                                 └─ handoff → handoff_prep
human_review → act → respond → end
```

`python -m hub.graph.build` prints the graph as Mermaid; a test compares its edges with the
diagram in ai-pipeline §1.

**Errors.** No node has an edge of its own for failures, so the drawn graph is the designed one.
A node that fails into a handoff (ai-pipeline §3) records `<node>:<error class>` in `errors`
instead of raising; the nodes up to `verify` are skipped once `errors` is set, and `policy_gate`
(after intake or triage) or `decide` (after research, write or verify) route the ticket to
`handoff_prep` (decision table row 2). The nodes from `handoff_prep` on do not catch: when one
of them fails, the graph stops and the run's last resort in `hub.graph.run` records the failure
and hands the ticket off.

**Timeouts.** Every node has one (ai-pipeline §3). A model node's limit is longer than one call
(`timeout_s` in config/models.yaml) so the gateway's retries and fallback can still run, and
shorter than all of them in a row: a stuck provider ends in a handoff, not a hanging stream. A
timeout is a failure like any other (`<node>:TimeoutError`), except where ai-pipeline §3 says the
node continues: a research timeout gives degraded empty evidence, a verify timeout an unsupported
draft (`verify_error = TimeoutError`).
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.errors import GraphBubbleUp
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.runtime import Runtime

from hub.graph.context import GraphContext
from hub.graph.nodes import (
    act,
    decide,
    handoff_prep,
    human_review,
    intake,
    policy_gate,
    research,
    respond,
    triage,
    verify,
    write,
)
from hub.graph.pipeline import STEP_CHECKING, STEP_SEARCHING, STEP_UNDERSTANDING, STEP_WRITING
from hub.graph.state import TicketState

logger = logging.getLogger(__name__)

type NodeFn = Callable[[TicketState, GraphContext], Awaitable[TicketState]]


class GraphNode(Protocol):
    """A node as LangGraph calls it: the state, and the runtime as a keyword."""

    __name__: str

    def __call__(
        self, state: TicketState, *, runtime: Runtime[GraphContext]
    ) -> Awaitable[TicketState]: ...


type Graph = CompiledStateGraph[TicketState, GraphContext, TicketState, TicketState]


@dataclass(frozen=True)
class NodeSpec:
    name: str
    run: NodeFn
    timeout_s: float
    step: str | None = None  # the `run.status` step shown when the node starts
    # Fails into a handoff: the error goes into `errors` and the run goes on to a routing node.
    catches: bool = True
    # Skipped once a node failed (the work would be wasted; the run is going to a human).
    skipped_after_error: bool = True
    # What a timeout turns into when the node does not fail into a handoff (ai-pipeline §3:
    # research continues degraded, verify treats the draft as unsupported). None: a failure.
    on_timeout: Callable[[TicketState], TicketState] | None = None


def research_timed_out(state: TicketState) -> TicketState:
    return TicketState(evidence=research.evidence((), degraded=True))


def verify_timed_out(state: TicketState) -> TicketState:
    return TicketState(verification=verify.unavailable(), verify_error="TimeoutError")


NODES = (
    NodeSpec("intake", intake.run, 15, STEP_UNDERSTANDING),
    NodeSpec("triage", triage.run, 75, STEP_UNDERSTANDING),
    NodeSpec("policy_gate", policy_gate.run, 10, skipped_after_error=False),
    NodeSpec("research", research.run, 30, STEP_SEARCHING, on_timeout=research_timed_out),
    NodeSpec("write", write.run, 150, STEP_WRITING),
    NodeSpec("verify", verify.run, 110, STEP_CHECKING, on_timeout=verify_timed_out),
    NodeSpec("decide", decide.run, 10, skipped_after_error=False),
    NodeSpec("handoff_prep", handoff_prep.run, 30, catches=False, skipped_after_error=False),
    NodeSpec("human_review", human_review.run, 30, catches=False, skipped_after_error=False),
    NodeSpec("act", act.run, 30, catches=False, skipped_after_error=False),
    NodeSpec("respond", respond.run, 30, catches=False, skipped_after_error=False),
)
NODE_TIMEOUTS_S = {spec.name: spec.timeout_s for spec in NODES}


def guarded(spec: NodeSpec) -> "GraphNode":
    """The node as the graph runs it: `run.status`, the timeout, and the error rule above."""

    async def node(state: TicketState, *, runtime: Runtime[GraphContext]) -> TicketState:
        errors = state.get("errors", [])
        if spec.skipped_after_error and errors:
            return TicketState()
        if spec.step is not None:
            await runtime.context.status(spec.step)
        try:
            async with asyncio.timeout(spec.timeout_s):
                return await spec.run(state, runtime.context)
        except GraphBubbleUp:
            raise  # LangGraph's own control flow (an interrupt, P6-03), never a failure
        except Exception as exc:
            if isinstance(exc, TimeoutError) and spec.on_timeout is not None:
                logger.warning("node timed out", extra={"node": spec.name})
                return spec.on_timeout(state)
            if not spec.catches:
                raise
            error = type(exc).__name__
            logger.warning(
                "node failed",
                extra={"node": spec.name, "run_id": state.get("run_id"), "error": error},
            )
            return TicketState(errors=[*errors, f"{spec.name}:{error}"])

    node.__name__ = spec.name
    return node


def after_policy_gate(state: TicketState) -> Literal["handoff_prep", "research"]:
    return "handoff_prep" if state.get("route") == "handoff" else "research"


def after_verify(state: TicketState) -> Literal["research", "decide"]:
    """The retry loop (P6-04) goes back to research; until then every draft goes on."""
    return "decide"


def after_decide(state: TicketState) -> Literal["respond", "human_review", "handoff_prep"]:
    route = state.get("route")
    if route == "auto_reply":
        return "respond"
    if route == "approval":
        return "human_review"
    return "handoff_prep"


def build_graph() -> StateGraph[TicketState, GraphContext, TicketState, TicketState]:
    graph = StateGraph(TicketState, context_schema=GraphContext)
    for spec in NODES:
        graph.add_node(spec.name, guarded(spec))
    graph.add_edge(START, "intake")
    graph.add_edge("intake", "triage")
    graph.add_edge("triage", "policy_gate")
    graph.add_conditional_edges("policy_gate", after_policy_gate, ["handoff_prep", "research"])
    graph.add_edge("research", "write")
    graph.add_edge("write", "verify")
    graph.add_conditional_edges("verify", after_verify, ["research", "decide"])
    graph.add_conditional_edges("decide", after_decide, ["respond", "human_review", "handoff_prep"])
    graph.add_edge("handoff_prep", "human_review")
    graph.add_edge("human_review", "act")
    graph.add_edge("act", "respond")
    graph.add_edge("respond", END)
    return graph


def compile_graph(checkpointer: BaseCheckpointSaver[Any] | None) -> Graph:
    return build_graph().compile(checkpointer=checkpointer, name="ticket")


def mermaid() -> str:
    """The graph image (Mermaid), generated from the code."""
    return compile_graph(None).get_graph().draw_mermaid()


if __name__ == "__main__":  # pragma: no cover - prints the diagram
    print(mermaid())
