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
    match message.role:
        case MessageRole.CUSTOMER:
            return CUSTOMER_LABEL
        case MessageRole.ASSISTANT:
            return AI_LABEL
        case MessageRole.SYSTEM:
            return SYSTEM_LABEL
        case MessageRole.OPERATOR:
            # Security §7: the operator's first name and "Support team".
            operator = (
                await OperatorRepository(session).get(message.author_operator_id)
                if message.author_operator_id
                else None
            )
            if operator is None or not operator.name.strip():
                return OPERATOR_LABEL
            return f"{operator.name.split()[0]} · {OPERATOR_LABEL}"


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
