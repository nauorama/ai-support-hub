"""What every node gets besides the state (LangGraph runtime context, P6-01).

The context is per run and is never checkpointed: it holds the pipeline (database, gateway,
prompts) and the stream of the request that started the run.
"""

import uuid
from dataclasses import dataclass

from hub.graph.pipeline import STEP_CHECKING, STEP_SEARCHING, Emit, Pipeline
from hub.graph.state import TicketState

ERROR_REASONS = ["system_error"]
BUDGET_REASONS = ["budget_exceeded"]


@dataclass
class GraphContext:
    pipeline: Pipeline
    emit: Emit
    # The step the stream showed last; the run sends `understanding` itself before the graph.
    last_step: str | None = None

    async def status(self, step: str) -> None:
        """`run.status` when a node starts (api.md §4). A step is sent once, even when two nodes
        share it (intake and triage are both `understanding`); B0 has no search and no check,
        so it never shows them."""
        if step == self.last_step:
            return
        if not self.pipeline.verifies and step in (STEP_SEARCHING, STEP_CHECKING):
            return
        self.last_step = step
        await self.emit("run.status", {"step": step})


def ids(state: TicketState) -> tuple[uuid.UUID, uuid.UUID]:
    """The ticket and run ids of the state."""
    return uuid.UUID(state["ticket_id"]), uuid.UUID(state["run_id"])


def failure_reasons(errors: list[str]) -> list[str]:
    """`budget_exceeded` when a budget stop (G8) ended the run, `system_error` otherwise
    (ai-pipeline §6, row 2)."""
    if any(error.endswith(":BudgetExceededError") for error in errors):
        return list(BUDGET_REASONS)
    return list(ERROR_REASONS)


def error_class(errors: list[str]) -> str | None:
    """The error class of the first failure, for `runs.error` (never an error message: it can
    quote customer text)."""
    return errors[0].split(":", 1)[1] if errors else None
