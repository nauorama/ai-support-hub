"""`POST /api/v1/demo/session`: the demo customer login (docs/api.md §2). DEMO ONLY.

Stands in for the login of the Tallybridge app; see `hub/auth/customer_session.py`.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response

from hub.api.dependencies import ContainerDep, SessionDep, client_ip
from hub.api.errors import ProblemError
from hub.api.schemas import CustomerProfileOut, DemoSessionIn
from hub.audit import record
from hub.auth.customer_session import (
    COOKIE_NAME,
    MAX_AGE_SECONDS,
    CustomerSessionSigner,
    access_code_matches,
)
from hub.db.repositories import CustomerRepository
from hub.domain.enums import ActorType

router = APIRouter(prefix="/api/v1/demo", tags=["demo"])


def demo_signer(container: ContainerDep) -> CustomerSessionSigner:
    """503 while SESSION_SECRET or DEMO_ACCESS_CODE is empty: the demo chat is switched off.
    Resolved before the database session, so a switched-off demo opens no connection."""
    signer = container.session_signer
    if signer is None or not container.settings.demo_access_code.get_secret_value():
        raise ProblemError(503, "demo_unavailable", "The demo chat is not configured.")
    return signer


@router.post("/session")
async def create_demo_session(
    body: DemoSessionIn,
    request: Request,
    response: Response,
    container: ContainerDep,
    signer: Annotated[CustomerSessionSigner, Depends(demo_signer)],
    session: SessionDep,
) -> CustomerProfileOut:
    expected_code = container.settings.demo_access_code.get_secret_value()

    limiter = container.rate_limits.access_code_failures_per_ip
    ip = client_ip(request)
    retry_after = limiter.retry_after(ip)
    if retry_after is not None:
        raise ProblemError(
            429,
            "too_many_requests",
            "Too many wrong access codes. Try again later.",
            headers={"Retry-After": str(retry_after)},
        )
    if not access_code_matches(body.access_code, expected_code):
        limiter.hit(ip)
        raise ProblemError(401, "invalid_access_code", "The access code is not valid.")

    customer = await CustomerRepository(session).get_by_external_ref(body.customer_ref)
    if customer is None or customer.anonymised_at is not None:
        raise ProblemError(404, "not_found", "No demo customer with this reference.")

    await record(
        session,
        actor_type=ActorType.CUSTOMER,
        actor_id=customer.id,
        action="auth.demo_session",
        entity_type="customer",
        entity_id=customer.id,
    )
    await session.commit()

    response.set_cookie(
        COOKIE_NAME,
        signer.sign(customer.id),
        max_age=MAX_AGE_SECONDS,
        httponly=True,
        secure=True,
        samesite="lax",
        path="/",
    )
    return CustomerProfileOut(
        customer_ref=customer.external_ref,
        display_name=customer.display_name,
        tier=customer.tier,
    )
