"""The workflow state (docs/ai-pipeline.md §2), P6-01.

One run handles one customer message; the checkpoint thread is the ticket (`thread_id` = ticket
id). Every run starts from `run_input`, which resets every field a node writes, so nothing of an
earlier run of the same ticket leaks into the next one.

What the state holds and why:
- identity (`ticket_id`, `run_id`, `message_id`, `customer_ref`) is set by the API code that
  starts the run, never by a model (AGENTS.md never-do 3);
- texts are G1-masked: no card data can enter the state, because the API masks before it saves
  the message the run reads;
- `placeholders` maps the run's G3 placeholders to the masked originals (e-mail, phone, partly
  masked IBAN), so model output can be restored for the operator. It is personal data, like the
  messages it comes from: never logged, and deleted with the ticket's checkpoints (security §6);
- chunk texts are stored once, in `evidence`.

Not here yet: `customer_context` (the customer profile and memory items, P6-05 and P7),
`features` and `confidence` (P6-02), account facts in the evidence (P7).
"""

from typing import Any, Literal, TypedDict

from pydantic import BaseModel, ConfigDict

from hub.domain.draft import Citation, DraftAnswer, ProposedAction
from hub.domain.triage import TriageResult
from hub.domain.verification import ClaimCheck, Verification
from hub.guardrails.injection import InjectionResult
from hub.kb.retriever import KbChunk

type RouteName = Literal["auto_reply", "approval", "handoff"]


class InputFlags(BaseModel):
    """What intake found in the message (ai-pipeline §2). Counts and scores only."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    injection: InjectionResult
    pii_placeholders: dict[str, int]


class Evidence(BaseModel):
    """What research found (ai-pipeline §4). Step 0 only until P6-04 and P7: one search with
    the masked ticket text, no tool calls, no account facts."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    chunks: list[KbChunk]
    top_score: float
    score_margin: float
    tool_calls: int = 0
    degraded: bool = False


class TicketState(TypedDict, total=False):
    # identity (set by the code that starts the run, never by a model)
    ticket_id: str
    run_id: str
    message_id: str
    customer_ref: str
    # input (G1-masked; `message_for_llm` and `history_for_llm` also hold G3 placeholders)
    message_masked: str
    message_for_llm: str
    history_for_llm: str
    placeholders: dict[str, str]
    input_flags: InputFlags | None
    # node outputs
    triage: TriageResult | None
    evidence: Evidence | None
    draft: DraftAnswer | None
    verification: Verification | None
    # The error class when the LLM claim check failed (the draft then counts as unsupported).
    verify_error: str | None
    retry_count: int
    route: RouteName | None
    route_reasons: list[str]
    review: dict[str, Any] | None  # ReviewDecision, set on resume (P6-03)
    final_message_id: str | None
    # `<node>:<error class>` for every node that failed; any entry hands the ticket off.
    errors: list[str]
    # role -> model that answered, for `runs.models`
    models: dict[str, str]
    # The ticket's status after the node that last changed it, for the stream's `ticket.status`.
    ticket_status: str | None


# Every class the checkpoint serializer may rebuild (`hub.graph.checkpoint`). Nested models are
# listed too: the serializer checks each object it rebuilds.
STATE_TYPES: tuple[type[Any], ...] = (
    InputFlags,
    InjectionResult,
    Evidence,
    KbChunk,
    TriageResult,
    DraftAnswer,
    Citation,
    ProposedAction,
    Verification,
    ClaimCheck,
)


def run_input(*, ticket_id: str, run_id: str, message_id: str, customer_ref: str) -> TicketState:
    """The input of a new run: the identity, and every node output reset."""
    return TicketState(
        ticket_id=ticket_id,
        run_id=run_id,
        message_id=message_id,
        customer_ref=customer_ref,
        message_masked="",
        message_for_llm="",
        history_for_llm="",
        placeholders={},
        input_flags=None,
        triage=None,
        evidence=None,
        draft=None,
        verification=None,
        verify_error=None,
        retry_count=0,
        route=None,
        route_reasons=[],
        review=None,
        final_message_id=None,
        errors=[],
        models={},
        ticket_status=None,
    )
