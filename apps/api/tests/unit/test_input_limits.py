"""Text length limits of every request body that takes free text (api.md §1, §3; security T9;
P8-04): the limit passes, one character more is refused, and NUL is refused everywhere.

The routes answer such a body with `422 validation_error` before the handler runs (FastAPI
validates the body model first); the route-level checks are in test_tickets_api.py and the
integration tests. This table covers the models, so a limit changed in one place is caught.
"""

import uuid
from collections.abc import Callable
from typing import Any

import pytest
from pydantic import BaseModel, ValidationError

from hub.api.console_schemas import ActionIn, EditIn, LoginIn, RejectIn, ReplyIn
from hub.api.schemas import DemoSessionIn, FeedbackIn, MessageIn

MESSAGE = 4_000


def _issue(summary: str = "s", description: str = "d") -> dict[str, Any]:
    return {
        "type": "jira_create_issue",
        "params": {"issue_type": "bug", "summary": summary, "description": description},
    }


# (model, body builder taking the text, limit in characters)
Body = Callable[[str], dict[str, Any]]
CASES: list[tuple[type[BaseModel], Body, int]] = [
    (MessageIn, lambda text: {"message": text}, MESSAGE),
    (ReplyIn, lambda text: {"ticket_version": 1, "body": text}, MESSAGE),
    (EditIn, lambda text: {"ticket_version": 1, "body": text}, MESSAGE),
    (
        RejectIn,
        lambda text: {"ticket_version": 1, "reason": "other", "reply_body": text},
        MESSAGE,
    ),
    (RejectIn, lambda text: {"ticket_version": 1, "reason": "other", "note": text}, 1_000),
    (
        FeedbackIn,
        lambda text: {"message_id": str(uuid.uuid4()), "rating": "down", "comment": text},
        1_000,
    ),
    (ActionIn, lambda text: _issue(summary=text), 255),
    (ActionIn, lambda text: _issue(description=text), 5_000),
    (DemoSessionIn, lambda text: {"customer_ref": "CUS-0001", "access_code": text}, 200),
    (LoginIn, lambda text: {"email": "a@b.example", "password": text}, 1_024),
]
IDS = [
    "message",
    "reply",
    "edit",
    "reject-reply",
    "reject-note",
    "feedback-comment",
    "issue-summary",
    "issue-description",
    "access-code",
    "password",
]


@pytest.mark.parametrize(("model", "body", "limit"), CASES, ids=IDS)
def test_the_limit_passes_and_one_more_character_is_refused(
    model: type[BaseModel], body: Body, limit: int
) -> None:
    # A character above U+FFFF counts once, as the chat counter does (ui.md §2).
    model.model_validate(body("€" * (limit - 1) + "😀"))
    with pytest.raises(ValidationError):
        model.model_validate(body("a" * (limit + 1)))


@pytest.mark.parametrize(("model", "body", "limit"), CASES, ids=IDS)
def test_nul_is_refused(model: type[BaseModel], body: Body, limit: int) -> None:
    if model is LoginIn:
        pytest.skip("a password is hashed, never stored as text")
    if model is DemoSessionIn:
        pytest.skip("an access code is compared, never stored")
    with pytest.raises(ValidationError):
        model.model_validate(body("a\x00b"))
