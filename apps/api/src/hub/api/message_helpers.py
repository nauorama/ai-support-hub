"""Message steps shared by the customer and the console endpoints: author labels, masking
counts and the `guardrail.masked` audit record."""

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from hub.audit import record
from hub.db.models import Message
from hub.db.repositories import OperatorRepository
from hub.domain.enums import ActorType, MessageRole
from hub.guardrails.masking import MaskResult

SYSTEM_LABEL = "Tallybridge"
CUSTOMER_LABEL = "You"
AI_LABEL = "AI assistant"
OPERATOR_LABEL = "Support team"


async def author_label(session: AsyncSession, message: Message) -> str:
    """Security §7: "AI assistant" for AI text, "<first name> · Support team" for an operator's
    text, and "AI assistant · reviewed by <first name>" for an AI draft an operator approved
    unchanged (an assistant message with `author_operator_id`, P3-05b)."""
    match message.role:
        case MessageRole.CUSTOMER:
            return CUSTOMER_LABEL
        case MessageRole.ASSISTANT:
            reviewer = await _first_name(session, message.author_operator_id)
            return AI_LABEL if reviewer is None else f"{AI_LABEL} · reviewed by {reviewer}"
        case MessageRole.SYSTEM:
            return SYSTEM_LABEL
        case MessageRole.OPERATOR:
            name = await _first_name(session, message.author_operator_id)
            return OPERATOR_LABEL if name is None else f"{name} · {OPERATOR_LABEL}"


async def _first_name(session: AsyncSession, operator_id: uuid.UUID | None) -> str | None:
    operator = await OperatorRepository(session).get(operator_id) if operator_id else None
    if operator is None or not operator.name.strip():
        return None
    return operator.name.split()[0]


def masked_entities(masked: MaskResult) -> dict[str, int]:
    """Counts by type only, e.g. `{"PAN": 1}` (data model, `messages.masked_entities`). After a
    failed mask the counts are unknown but every digit was hidden: `{"failed": 1}`, so the
    customer view still says that something was masked."""
    if masked.failed:
        return {"failed": 1}
    return {str(kind): count for kind, count in masked.counts.items()}


async def audit_masking(
    session: AsyncSession, masked: MaskResult, *, entity_type: str, entity_id: uuid.UUID
) -> None:
    """`guardrail.masked` when something was masked, and also when masking failed: then every
    digit was replaced and the counts are unknown, and the failure must leave a trace."""
    if masked.counts or masked.failed:
        await record(
            session,
            actor_type=ActorType.SYSTEM,
            actor_id=None,
            action="guardrail.masked",
            entity_type=entity_type,
            entity_id=entity_id,
            details={"counts": dict(masked.counts), "failed": masked.failed},
        )
