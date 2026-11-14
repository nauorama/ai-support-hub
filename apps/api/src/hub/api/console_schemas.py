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
    Intent,
    MessageRole,
    OperatorRole,
    Route,
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


class ConsoleTicketOut(QueueTicketOut):
    """`GET /console/tickets/{id}`. Fields of later phases are present and empty until then:
    runs and drafts (phase 6), evidence (phase 5), memory (phase 6), tool calls (phase 7),
    trace URL (phase 9)."""

    language: str | None
    confidence: Decimal | None
    ai_disclosed_at: datetime
    first_response_at: datetime | None
    resolved_at: datetime | None
    resolution: str | None
    messages: list[ConsoleMessageOut]
    latest_run: None = None
    draft: None = None
    evidence: list[Any] = Field(default_factory=list)
    memory: list[Any] = Field(default_factory=list)
    tool_calls: list[Any] = Field(default_factory=list)
    trace_url: None = None
