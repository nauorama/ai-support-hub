"""Console review of a draft (docs/api.md §3, P3-05b): approve, edit, reject.

Who wrote the text decides the label (security §7, owner decision):
- **approve** sends the AI text unchanged: an `assistant` message with the reviewer in
  `author_operator_id`, labelled "AI assistant · reviewed by <name>". An edit whose masked text
  equals the draft is the same thing and is stored as an approve.
- **edit** sends the operator's text: an `operator` message, labelled "<name> · Support team".
- **reject** sends nothing (the ticket goes to `handed_off` and waits in the queue) or the
  operator's own reply (an `operator` message; the ticket is `answered`).

Every request runs under the ticket row lock, checks `ticket_version`, the draft's status and
another operator's claim, and writes the draft, the message, the ticket and the audit records in
one transaction. `Idempotency-Key`: the draft stores the operator-scoped key of its review
(`drafts.review_key`, every kind of review, a reject without a reply included) and the approved
actions (P7-05). The same key with the same request (draft, outcome, final text, approved actions,
reject reason and note) returns the first result; with anything else, or a key a free reply
already used, `409 idempotency_key_reused`.

After the commit the draft's run, paused in `human_review` (P6-03), is resumed with a
`ReviewDecision` (`hub.api.resume`): the decision is already written, so the run records it, `act`
runs the approved Jira actions (P7-05) and the run ends. A replay does not resume again.
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from hub.api.console import (
    IdempotencyKey,
    Writer,
    _audit,
    _change_status,
    _check_not_claimed_by_other,
    _check_not_closed,
    _check_version,
    _message_out,
    _row,
    _ticket_or_404,
)
from hub.api.console_schemas import ApproveIn, DraftOut, DraftReviewOut, EditIn, RejectIn
from hub.api.dependencies import ContainerDep, SessionDep
from hub.api.errors import ProblemError
from hub.api.message_helpers import audit_masking, masked_entities
from hub.api.resume import paused_run, resume_after_commit
from hub.audit import record
from hub.db.models import Draft, Message, Operator, Ticket
from hub.db.repositories import DraftRepository, MessageRepository, TicketRepository
from hub.deps import Container
from hub.domain.enums import (
    ActorType,
    DraftStatus,
    MessageRole,
    RejectReason,
    Resolution,
    TicketStatus,
)
from hub.domain.review import (
    ReviewDecision,
    ReviewKind,
    citations_kept,
    edit_ratio,
    unknown_markers,
)
from hub.guardrails.masking import MaskResult, mask

router = APIRouter(prefix="/api/v1/console", tags=["console"])


@dataclass(frozen=True)
class _Outgoing:
    """The message a review sends, decided before anything is written."""

    role: MessageRole
    body: str
    citations: list[dict[str, Any]]
    masked: MaskResult | None


@dataclass(frozen=True)
class _Review:
    """What the operator asked for, after masking. `outgoing` is None for a reject without a
    reply."""

    status: DraftStatus
    outgoing: _Outgoing | None
    approved_actions: list[str]
    reject_reason: RejectReason | None = None
    reject_note: MaskResult | None = None


# --- endpoints ------------------------------------------------------------------------------


@router.post("/drafts/{draft_id}/approve")
async def approve_draft(
    draft_id: uuid.UUID,
    body: ApproveIn,
    idempotency_key: IdempotencyKey,
    operator: Writer,
    session: SessionDep,
    container: ContainerDep,
) -> DraftReviewOut:
    """Send the draft unchanged. A draft with `can_answer = false` cannot be approved
    unchanged: `409 draft_cannot_answer` (editing and sending it is allowed)."""
    draft = await _draft_or_404(session, draft_id)
    review = _approve(draft, body.approved_actions)
    return await _apply(
        session, container, operator, draft, body.ticket_version, idempotency_key, review
    )


@router.post("/drafts/{draft_id}/edit")
async def edit_draft(
    draft_id: uuid.UUID,
    body: EditIn,
    idempotency_key: IdempotencyKey,
    operator: Writer,
    session: SessionDep,
    container: ContainerDep,
) -> DraftReviewOut:
    """Send the operator's text. It is masked (G1); every `[n]` marker in it must have a
    citation in the draft (otherwise `422 validation_error`). Text equal to the draft, apart from
    leading and trailing whitespace (a text area often adds a newline), is an approve."""
    draft = await _draft_or_404(session, draft_id)
    masked = mask(body.body)
    if masked.text.strip() == draft.body.strip():
        # The draft text is sent; the mask result still counts what the operator typed (a card
        # number that masks to the draft's `[CARD ****1111]`) for the message and the audit log.
        review = _approve(draft, body.approved_actions, masked=masked)
    else:
        if unknown_markers(masked.text, draft.citations):
            raise ProblemError(
                422,
                "validation_error",
                "The request is not valid.",
                extensions={
                    "errors": [
                        {
                            "loc": ["body", "body"],
                            "type": "unknown_citation_marker",
                            "msg": "A citation marker has no source in the draft.",
                        }
                    ]
                },
            )
        review = _Review(
            status=DraftStatus.EDITED,
            outgoing=_Outgoing(
                role=MessageRole.OPERATOR,
                body=masked.text,
                citations=citations_kept(masked.text, draft.citations),
                masked=masked,
            ),
            approved_actions=body.approved_actions,
        )
    return await _apply(
        session, container, operator, draft, body.ticket_version, idempotency_key, review
    )


@router.post("/drafts/{draft_id}/reject")
async def reject_draft(
    draft_id: uuid.UUID,
    body: RejectIn,
    idempotency_key: IdempotencyKey,
    operator: Writer,
    session: SessionDep,
    container: ContainerDep,
) -> DraftReviewOut:
    """Reject the draft with a reason. With `reply_body` the operator's own answer is sent
    (masked, no citations); without it the ticket stays with a human (`handed_off`)."""
    draft = await _draft_or_404(session, draft_id)
    outgoing = None
    if body.reply_body is not None:
        reply = mask(body.reply_body)
        outgoing = _Outgoing(role=MessageRole.OPERATOR, body=reply.text, citations=[], masked=reply)
    review = _Review(
        status=DraftStatus.REJECTED,
        outgoing=outgoing,
        approved_actions=[],
        reject_reason=body.reason,
        reject_note=None if body.note is None else mask(body.note),
    )
    return await _apply(
        session, container, operator, draft, body.ticket_version, idempotency_key, review
    )


# --- steps ----------------------------------------------------------------------------------


async def _draft_or_404(session: AsyncSession, draft_id: uuid.UUID) -> Draft:
    draft = await DraftRepository(session).get(draft_id)
    if draft is None:
        raise ProblemError(404, "not_found", "Draft not found.")
    return draft


def _approve(
    draft: Draft, approved_actions: list[str], *, masked: MaskResult | None = None
) -> _Review:
    if not draft.can_answer:
        raise ProblemError(
            409,
            "draft_cannot_answer",
            "This draft says the sources do not answer the question. Edit it or reject it.",
        )
    return _Review(
        status=DraftStatus.APPROVED,
        # The draft body was masked when it was stored (OutputMask); it is not masked again.
        # `masked` is set only for an edit equal to the draft (`edit_draft`).
        outgoing=_Outgoing(
            role=MessageRole.ASSISTANT,
            body=draft.body,
            citations=[dict(citation) for citation in draft.citations],
            masked=masked,
        ),
        approved_actions=approved_actions,
    )


def _check_actions(draft: Draft, approved_actions: list[str]) -> None:
    known = {action.get("id") for action in draft.proposed_actions if isinstance(action, dict)}
    if any(action_id not in known for action_id in approved_actions):
        raise ProblemError(
            422,
            "validation_error",
            "The request is not valid.",
            extensions={
                "errors": [
                    {
                        "loc": ["body", "approved_actions"],
                        "type": "unknown_action",
                        "msg": "An approved action is not proposed in the draft.",
                    }
                ]
            },
        )


async def _apply(
    session: AsyncSession,
    container: Container,
    operator: Operator,
    draft: Draft,
    ticket_version: int,
    idempotency_key: uuid.UUID,
    review: _Review,
) -> DraftReviewOut:
    ticket = await _ticket_or_404(session, draft.ticket_id)
    scoped_key = f"operator:{operator.id}:{idempotency_key}"
    messages = MessageRepository(session)

    replay = await _replay(session, draft, review, scoped_key)
    if replay is not None:
        return replay

    # Checked under the ticket row lock, with the draft reloaded: a review, a close or a
    # customer message that committed while this request waited is seen here.
    await TicketRepository(session).lock(ticket)
    await DraftRepository(session).get(draft.id, for_update=True)
    replay = await _replay(session, draft, review, scoped_key)
    if replay is not None:
        return replay
    _check_not_closed(ticket)
    # Before the version: when two operators review one draft, the second learns that the
    # draft was already reviewed, not that "the customer wrote again" (review round 1).
    if draft.status != DraftStatus.PENDING:
        raise _no_longer_waits()
    _check_version(ticket, ticket_version)
    if ticket.status != TicketStatus.PENDING_APPROVAL:
        raise _no_longer_waits()
    _check_not_claimed_by_other(ticket, operator)
    _check_actions(draft, review.approved_actions)

    ticket_id, draft_id = ticket.id, draft.id
    try:
        run_id = await paused_run(session, draft.run_id)
        message = await _write(session, operator, ticket, draft, review, scoped_key)
        decision = _decision(operator, review, message)
        await session.commit()
    except IntegrityError:
        # The same key was used concurrently for a request on another ticket.
        await session.rollback()
        used = await DraftRepository(session).by_review_key(scoped_key) is not None
        if not used and await messages.get_by_idempotency_key(scoped_key) is None:
            raise
        raise _key_reused() from None
    await resume_after_commit(container, ticket_id, run_id, decision)
    return await _result(session, ticket_id, draft_id, message)


_KIND: dict[DraftStatus, ReviewKind] = {
    DraftStatus.APPROVED: "approve",
    DraftStatus.EDITED: "edit",
    DraftStatus.REJECTED: "reject",
}


def _decision(operator: Operator, review: _Review, message: Message | None) -> ReviewDecision:
    """What resumes the run: built from the request after masking, never by a model."""
    outgoing = review.outgoing
    return ReviewDecision(
        kind=_KIND[review.status],
        operator_id=str(operator.id),
        # The operator's own text; an approve sends the AI text, which the draft holds.
        body=None if outgoing is None or review.status == DraftStatus.APPROVED else outgoing.body,
        approved_actions=list(review.approved_actions),
        reject_reason=None if review.reject_reason is None else review.reject_reason.value,
        message_id=None if message is None else str(message.id),
    )


async def _write(
    session: AsyncSession,
    operator: Operator,
    ticket: Ticket,
    draft: Draft,
    review: _Review,
    scoped_key: str,
) -> Message | None:
    message = None
    outgoing = review.outgoing
    if outgoing is not None:
        message = await MessageRepository(session).add(
            ticket_id=ticket.id,
            role=outgoing.role,
            body=outgoing.body,
            is_ai_generated=outgoing.role == MessageRole.ASSISTANT,
            author_operator_id=operator.id,
            run_id=draft.run_id,
            citations=outgoing.citations,
            masked_entities=None if outgoing.masked is None else masked_entities(outgoing.masked),
            idempotency_key=scoped_key,
        )

    now = datetime.now(UTC)
    draft.status = review.status
    draft.reviewed_by = operator.id
    draft.reviewed_at = now
    draft.final_body = None if outgoing is None else outgoing.body
    draft.edit_ratio = (
        edit_ratio(draft.body, outgoing.body)
        if outgoing is not None and review.status != DraftStatus.REJECTED
        else None
    )
    draft.reject_reason = review.reject_reason
    draft.reject_note = None if review.reject_note is None else review.reject_note.text
    draft.approved_actions = list(review.approved_actions)
    draft.review_key = scoped_key

    if message is not None:
        if ticket.first_response_at is None:
            ticket.first_response_at = now
        ticket.resolution = _RESOLUTION[review.status]
        await _change_status(session, ticket, TicketStatus.ANSWERED)
    else:
        await _change_status(session, ticket, TicketStatus.HANDED_OFF)

    await record(
        session,
        actor_type=ActorType.OPERATOR,
        actor_id=operator.id,
        action=f"draft.{review.status.value}",
        entity_type="draft",
        entity_id=draft.id,
        details={
            "ticket_id": ticket.id,
            "run_id": draft.run_id,
            "message_id": None if message is None else message.id,
            "edit_ratio": None if draft.edit_ratio is None else str(draft.edit_ratio),
            "approved_actions": review.approved_actions,
            "reject_reason": None if review.reject_reason is None else review.reject_reason.value,
            "with_reply": review.status == DraftStatus.REJECTED and message is not None,
        },
    )
    if message is not None:
        await _audit(session, operator, "message.sent", ticket.id, {"message_id": message.id})
        if outgoing is not None and outgoing.masked is not None:
            await audit_masking(
                session, outgoing.masked, entity_type="message", entity_id=message.id
            )
    if review.reject_note is not None:
        await audit_masking(session, review.reject_note, entity_type="draft", entity_id=draft.id)
    return message


_RESOLUTION = {
    DraftStatus.APPROVED: Resolution.AI_APPROVED,
    DraftStatus.EDITED: Resolution.AI_EDITED,
    DraftStatus.REJECTED: Resolution.HUMAN,
}


async def _replay(
    session: AsyncSession,
    draft: Draft,
    review: _Review,
    scoped_key: str,
) -> DraftReviewOut | None:
    """The first answer for a repeated request, or None when this is the first one. The key is
    scoped to the operator, so `operator` is implied by a found draft."""
    reviewed = await DraftRepository(session).by_review_key(scoped_key)
    stored = await MessageRepository(session).get_by_idempotency_key(scoped_key)
    if reviewed is None:
        if stored is not None:
            # The key sent a free reply (or a review on another ticket before P7-05).
            raise _key_reused()
        return None
    if not _same_review(reviewed, draft, review):
        raise _key_reused()
    return await _result(session, reviewed.ticket_id, reviewed.id, stored)


def _same_review(reviewed: Draft, draft: Draft, review: _Review) -> bool:
    """Whether the stored review is this request: the same draft, outcome, final text, approved
    actions, reject reason and note (P3-05b review note 1, P7-05)."""
    outgoing = review.outgoing
    note = None if review.reject_note is None else review.reject_note.text
    return (
        reviewed.id == draft.id
        and reviewed.status == review.status
        and reviewed.final_body == (None if outgoing is None else outgoing.body)
        and list(reviewed.approved_actions) == list(review.approved_actions)
        and reviewed.reject_reason == review.reject_reason
        and reviewed.reject_note == note
    )


def _no_longer_waits() -> ProblemError:
    return ProblemError(409, "conflict", "This draft no longer waits for review.")


def _key_reused() -> ProblemError:
    return ProblemError(
        409,
        "idempotency_key_reused",
        "This Idempotency-Key was already used for a different request.",
    )


async def _result(
    session: AsyncSession, ticket_id: uuid.UUID, draft_id: uuid.UUID, message: Message | None
) -> DraftReviewOut:
    draft = await DraftRepository(session).get(draft_id)
    if draft is None:  # pragma: no cover - read in this request; drafts are deleted only by erasure
        raise ProblemError(404, "not_found", "Draft not found.")
    return DraftReviewOut(
        draft=DraftOut.model_validate(draft),
        message=None if message is None else await _message_out(session, message),
        ticket=await _row(session, ticket_id),
    )
