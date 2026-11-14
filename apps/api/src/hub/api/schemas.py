"""Request and response models of the customer endpoints (docs/api.md §2).

Customer responses carry only what the chat shows. Internal fields (chunk ids, scores,
confidence, route reasons, intent) never appear here.
"""

import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, StringConstraints

from hub.api.limits import MAX_MESSAGE_CHARS
from hub.domain.enums import CustomerTier, FeedbackRating, MessageRole, TicketStatus

# Security §7 (EU AI Act Article 50): shown at the start of every conversation.
AI_DISCLOSURE = (
    "You are chatting with Tallybridge's AI assistant. It can make mistakes. "
    "You can ask for a human at any time."
)

MAX_FEEDBACK_COMMENT_CHARS = 1_000

# Length is counted in characters (code points), as the chat counter does (docs/ui.md §2).
# `strip_whitespace` is not used: the stored text stays what the customer wrote (masked).
MessageText = Annotated[
    str, StringConstraints(min_length=1, max_length=MAX_MESSAGE_CHARS, pattern=r"\S")
]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DemoSessionIn(_Strict):
    customer_ref: Annotated[str, StringConstraints(pattern=r"^CUS-\d{4}$")]
    access_code: Annotated[str, StringConstraints(min_length=1, max_length=200)]


class CustomerProfileOut(BaseModel):
    customer_ref: str
    display_name: str
    tier: CustomerTier


class MessageIn(_Strict):
    message: MessageText


class FeedbackIn(_Strict):
    message_id: uuid.UUID
    rating: FeedbackRating
    comment: Annotated[str, StringConstraints(max_length=MAX_FEEDBACK_COMMENT_CHARS)] | None = None


class CitationOut(BaseModel):
    marker: int
    title: str
    url: str


class MessageOut(BaseModel):
    id: uuid.UUID
    role: MessageRole
    author_label: str
    is_ai_generated: bool
    body: str
    citations: list[CitationOut]
    # True when the server hid card data in this message; the chat shows a note (docs/ui.md §2).
    masked: bool
    created_at: datetime


class TicketOut(BaseModel):
    id: uuid.UUID
    status: TicketStatus
    ai_disclosure: str = AI_DISCLOSURE
    ai_disclosed_at: datetime
    created_at: datetime


class Page[T](BaseModel):
    items: list[T]
    next_cursor: str | None = None


class FeedbackOut(BaseModel):
    id: uuid.UUID


class TicketCreatedEvent(BaseModel):
    ticket_id: uuid.UUID
    message: MessageOut


def citations_for_customer(stored: list[Any]) -> list[CitationOut]:
    """Only marker, title and url: chunk and document ids are internal."""
    return [
        CitationOut(marker=item["marker"], title=item["title"], url=item["url"])
        for item in stored
        if isinstance(item, dict)
    ]


SseEventName = Literal["ticket.created", "message", "done"]
