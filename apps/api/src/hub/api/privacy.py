"""Privacy endpoints (api.md §2, §3; security §6; P8-05).

- `GET /privacy` (public): the short privacy text the chat links to.
- `DELETE /admin/customers/{id}/data` [admin]: erasure (`hub.privacy.erasure`) with a report of
  what was removed. The database part is one transaction; the checkpoints of the customer's
  tickets are deleted after the commit through the checkpoint store. A failure there answers
  500 and the erasure can simply be repeated (it is safe to run again).
"""

import uuid
from collections.abc import Collection
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from hub.api.dependencies import ContainerDep, RequireRole, SessionDep
from hub.api.errors import ProblemError
from hub.auth.roles import ADMIN
from hub.db.models import Operator
from hub.deps import Container
from hub.graph.checkpoint import CheckpointStore
from hub.privacy import erase_customer
from hub.privacy.notice import SECTIONS, TITLE, UPDATED_AT

public_router = APIRouter(prefix="/api/v1", tags=["privacy"])
admin_router = APIRouter(prefix="/api/v1/admin/customers", tags=["admin"])


class PrivacySectionOut(BaseModel):
    heading: str
    text: str


class PrivacyOut(BaseModel):
    title: str
    updated_at: str
    sections: list[PrivacySectionOut]


@public_router.get("/privacy")
async def privacy() -> PrivacyOut:
    return PrivacyOut(
        title=TITLE,
        updated_at=UPDATED_AT,
        sections=[PrivacySectionOut(heading=s.heading, text=s.text) for s in SECTIONS],
    )


class ErasureOut(BaseModel):
    """What the erasure removed. Counts only; the audit record `customer.erased` has the same."""

    customer_id: uuid.UUID
    anonymised_at: datetime
    tickets_anonymised: int
    messages_deleted: int
    drafts_deleted: int
    memory_deleted: int
    feedback_deleted: int
    tool_calls_redacted: int
    runs_ended: int
    checkpoint_threads_deleted: int


@admin_router.delete("/{customer_id}/data")
async def erase(
    customer_id: uuid.UUID,
    operator: Annotated[Operator, Depends(RequireRole(ADMIN))],
    session: SessionDep,
    container: ContainerDep,
) -> ErasureOut:
    report = await erase_customer(session, customer_id, operator_id=operator.id)
    if report is None:
        raise ProblemError(404, "not_found", "Customer not found.")
    await session.commit()
    checkpoints = await delete_checkpoints(container, report.ticket_ids)
    return ErasureOut(
        customer_id=report.customer_id,
        anonymised_at=report.anonymised_at,
        tickets_anonymised=report.tickets_anonymised,
        messages_deleted=report.messages_deleted,
        drafts_deleted=report.drafts_deleted,
        memory_deleted=report.memory_deleted,
        feedback_deleted=report.feedback_deleted,
        tool_calls_redacted=report.tool_calls_redacted,
        runs_ended=report.runs_ended,
        checkpoint_threads_deleted=checkpoints,
    )


async def delete_checkpoints(container: Container, ticket_ids: Collection[uuid.UUID]) -> int:
    """Delete the checkpoints of these tickets (thread id = ticket id) through the store of the
    workflow, or a store of its own when no workflow is configured (the tables exist anyway)."""
    if not ticket_ids:
        return 0
    threads = [str(ticket_id) for ticket_id in ticket_ids]
    if container.pipeline is not None:
        return await container.pipeline.checkpoints.delete_threads(threads)
    store = CheckpointStore.postgres(container.settings.database_url.get_secret_value())
    try:
        return await store.delete_threads(threads)
    finally:
        await store.close()
