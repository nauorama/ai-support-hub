"""`POST /admin/runs/recover` [admin] (api.md §3, P6-03): recover the runs stuck in `running`.

Runs inside the request (Cloud Run gives CPU only during a request, ADR-0007), one stuck run
after the other; what counts as stuck is in `hub.graph.recover`. Needs the CSRF token like every
state-changing console route (`RequireRole`).
"""

from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from hub.api.dependencies import ContainerDep, RequireRole
from hub.api.errors import ProblemError
from hub.auth.roles import ADMIN
from hub.db.models import Operator
from hub.domain.enums import ActorType
from hub.graph.recover import Recovery, recover_runs

router = APIRouter(prefix="/api/v1/admin/runs", tags=["admin"])


class RecoverOut(BaseModel):
    """Stuck runs found, recovered to their end, and failed (handed to a human)."""

    found: int
    resumed: int
    failed: int


@router.post("/recover")
async def recover(
    operator: Annotated[Operator, Depends(RequireRole(ADMIN))], container: ContainerDep
) -> RecoverOut:
    if container.pipeline is None:
        raise ProblemError(503, "workflow_unavailable", "The workflow is not configured.")
    report = await recover_runs(
        container.pipeline, Recovery(actor_type=ActorType.OPERATOR, actor_id=operator.id)
    )
    return RecoverOut(found=report.found, resumed=report.resumed, failed=report.failed)
