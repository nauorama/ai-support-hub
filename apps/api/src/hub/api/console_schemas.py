"""Request and response models of the auth and console endpoints (docs/api.md §3).

The operator view holds internal fields (route reasons, masking counts, citations with chunk
ids). It is never returned by a customer endpoint.
"""

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from hub.api.schemas import MessageText
from hub.domain.enums import (
    CustomerTier,
    DraftStatus,
    Intent,
    MessageRole,
    OperatorRole,
    RejectReason,
    Route,
    RunStatus,
    TicketStatus,
    Urgency,
)


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --- auth -----------------------------------------------------------------------------------


class LoginIn(_Strict):
    # 254: the longest valid email address. No format check: an unknown address is just a
    # failed login, and a format error would tell an attacker nothing useful either.
    email: Annotated[str, StringConstraints(min_length=1, max_length=254)]
    # Argon2 hashes any length; the cap keeps a huge password from costing hashing time.
    password: Annotated[str, StringConstraints(min_length=1, max_length=1024)]


class OperatorOut(BaseModel):
    id: uuid.UUID
    email: str
    name: str
    role: OperatorRole


class OperatorSessionOut(BaseModel):
    """Answer of `POST /auth/login` and `GET /auth/me`. The SPA sends `csrf_token` in the
    `X-CSRF-Token` header of every state-changing console request."""

    operator: OperatorOut
    csrf_token: str


# --- console --------------------------------------------------------------------------------


class TicketVersionIn(_Strict):
    """Body of claim, release and close: the `version` the console last saw (api.md §3,
    conflict rule). A changed ticket answers `409 stale_draft`."""

    ticket_version: Annotated[int, Field(ge=1)]


class ReplyIn(_Strict):
    ticket_version: Annotated[int, Field(ge=1)]
    # The same limit as a customer message; masked (G1) before it is stored.
    body: MessageText


# Ids of the draft's proposed actions the operator ticked ("a1", ...). Phase 7 runs them; until
# then they are checked against the draft and recorded in the audit log.
ActionIds = Annotated[
    list[Annotated[str, StringConstraints(pattern=r"^a[0-9]{1,3}$")]], Field(max_length=20)
]
# Same limit as a customer comment on feedback; masked (G1) before it is stored.
RejectNote = Annotated[str, StringConstraints(max_length=1000, pattern=r"^[^\x00]*$")]


class ApproveIn(_Strict):
    ticket_version: Annotated[int, Field(ge=1)]
    approved_actions: ActionIds = Field(default_factory=list)


class EditIn(_Strict):
    ticket_version: Annotated[int, Field(ge=1)]
    # The same limit as a customer message; masked (G1) before it is stored.
    body: MessageText
    approved_actions: ActionIds = Field(default_factory=list)


class RejectIn(_Strict):
    ticket_version: Annotated[int, Field(ge=1)]
    reason: RejectReason
    note: RejectNote | None = None
    # With a reply the operator's own answer is sent; without it the ticket stays handed off.
    reply_body: MessageText | None = None


class ConsoleCustomerOut(BaseModel):
    id: uuid.UUID
    customer_ref: str
    display_name: str
    tier: CustomerTier
    country: str


class AssigneeOut(BaseModel):
    id: uuid.UUID
    name: str


class QueueTicketOut(BaseModel):
    """One row of the operator queue; also the answer of claim, release, reply and close."""

    id: uuid.UUID
    status: TicketStatus
    intent: Intent | None
    urgency: Urgency | None
    summary: str | None
    last_route: Route | None
    route_reasons: list[str]
    customer: ConsoleCustomerOut
    assigned_operator: AssigneeOut | None
    # The last customer message (the ticket's creation when there is none).
    waiting_since: datetime
    created_at: datetime
    version: int


class ConsoleMessageOut(BaseModel):
    id: uuid.UUID
    role: MessageRole
    author_label: str
    is_ai_generated: bool
    author_operator_id: uuid.UUID | None
    # As stored: card data is already masked.
    body: str
    citations: list[dict[str, Any]]
    masked_entities: dict[str, int]
    created_at: datetime


class RunOut(BaseModel):
    """The latest run of a ticket: status, route and what it cost (data model, `runs`)."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    status: RunStatus
    route: Route | None
    route_reasons: list[str]
    confidence: Decimal | None
    prompt_versions: dict[str, str]
    models: dict[str, str]
    total_cost_usd: Decimal
    input_tokens: int
    output_tokens: int
    latency_ms: int | None
    # An error class only, never a message text.
    error: str | None
    started_at: datetime
    finished_at: datetime | None


class DraftOut(BaseModel):
    """The draft of the latest run (data model, `drafts`). `body` and `final_body` are stored
    card-masked; `evidence` holds what the operator checks the draft against."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    run_id: uuid.UUID
    status: DraftStatus
    can_answer: bool
    missing_information: str | None
    body: str
    citations: list[dict[str, Any]]
    evidence: dict[str, Any]
    proposed_actions: list[dict[str, Any]]
    groundedness: Decimal | None
    unsupported_claims: list[Any]
    reviewed_by: uuid.UUID | None
    reviewed_at: datetime | None
    final_body: str | None
    edit_ratio: Decimal | None
    reject_reason: RejectReason | None
    reject_note: str | None
    created_at: datetime


class DraftReviewOut(BaseModel):
    """Answer of approve, edit and reject: the reviewed draft, the message that was sent (none
    for a reject without a reply) and the ticket's queue row with its new `version`."""

    draft: DraftOut
    message: ConsoleMessageOut | None
    ticket: QueueTicketOut


class ConsoleTicketOut(QueueTicketOut):
    """`GET /console/tickets/{id}`. Fields of later phases are present and empty until then:
    evidence chunks (phase 5; the B0 cited documents are in `draft.evidence`), memory (phase 6),
    tool calls (phase 7), trace URL (phase 9)."""

    language: str | None
    confidence: Decimal | None
    ai_disclosed_at: datetime
    first_response_at: datetime | None
    resolved_at: datetime | None
    resolution: str | None
    messages: list[ConsoleMessageOut]
    latest_run: RunOut | None
    # The draft of the latest run, whatever its status.
    draft: DraftOut | None
    evidence: list[Any] = Field(default_factory=list)
    memory: list[Any] = Field(default_factory=list)
    tool_calls: list[Any] = Field(default_factory=list)
    trace_url: None = None
