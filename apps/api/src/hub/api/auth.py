"""Operator login, logout and `me` (docs/api.md §3, security §4).

Login answers every failure the same way (`401 invalid_credentials`): unknown email, inactive
operator, wrong password and a locked account cannot be told apart, so the endpoint does not
reveal which addresses exist. An unknown email is checked against a dummy hash, so it costs the
same Argon2 time as a real one. Failures count twice: per IP in memory (5 per 15 minutes, a
brake on guessing across accounts) and per account in the database (lockout after 10).

The audit log gets ids only: never the email that was tried (security §5).
"""

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response

from hub.api.console_schemas import LoginIn, OperatorOut, OperatorSessionOut
from hub.api.dependencies import (
    ContainerDep,
    OperatorCookie,
    OperatorDep,
    SessionDep,
    check_csrf,
    client_ip,
)
from hub.api.errors import ProblemError
from hub.audit import record
from hub.auth.lockout import LoginState, after_failure, after_success, is_locked
from hub.auth.operator_session import COOKIE_NAME, MAX_AGE_SECONDS, OperatorSessionSigner
from hub.auth.passwords import hash_password, needs_rehash, verify_password
from hub.db.models import Operator
from hub.db.repositories import OperatorRepository
from hub.domain.enums import ActorType

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])

# Verified when the email is unknown, so that case takes as long as a wrong password.
_DUMMY_HASH = hash_password("not-a-real-password")  # timing dummy, never stored


def _signer(container: ContainerDep) -> OperatorSessionSigner:
    signer = container.operator_session_signer
    if signer is None:
        raise ProblemError(503, "login_unavailable", "Operator login is not configured.")
    return signer


SignerDep = Annotated[OperatorSessionSigner, Depends(_signer)]


def _invalid_credentials() -> ProblemError:
    return ProblemError(401, "invalid_credentials", "The email or password is not valid.")


def _session_out(
    operator: Operator, signer: OperatorSessionSigner, cookie: str
) -> OperatorSessionOut:
    return OperatorSessionOut(
        operator=OperatorOut(
            id=operator.id, email=operator.email, name=operator.name, role=operator.role
        ),
        csrf_token=signer.csrf_token(cookie),
    )


@router.post("/login")
async def login(
    body: LoginIn,
    request: Request,
    response: Response,
    container: ContainerDep,
    signer: SignerDep,
    session: SessionDep,
) -> OperatorSessionOut:
    limiter = container.rate_limits.login_failures_per_ip
    ip = client_ip(request)
    retry_after = limiter.retry_after(ip)
    if retry_after is not None:
        raise ProblemError(
            429,
            "too_many_requests",
            "Too many failed logins. Try again later.",
            headers={"Retry-After": str(retry_after)},
        )

    now = datetime.now(UTC)
    operators = OperatorRepository(session)
    # Locked: two failures at the same time must both count.
    operator = await operators.get_by_email(body.email, for_update=True)
    password_ok = verify_password(
        operator.password_hash if operator is not None else _DUMMY_HASH, body.password
    )

    if operator is None or not operator.is_active:
        await _audit_failure(
            session, operator, reason="unknown" if operator is None else "inactive"
        )
        await session.commit()
        limiter.hit(ip)
        raise _invalid_credentials()

    state = LoginState(operator.failed_logins, operator.locked_until)
    if is_locked(state, now) or not password_ok:
        # A correct password does not count as a failure while the account is locked, and it
        # does not unlock it either.
        reason = "locked" if is_locked(state, now) else "wrong_password"
        if reason == "wrong_password":
            state = after_failure(state, now)
            operator.failed_logins = state.failed_logins
            operator.locked_until = state.locked_until
        await _audit_failure(session, operator, reason=reason, locked=is_locked(state, now))
        await session.commit()
        limiter.hit(ip)
        raise _invalid_credentials()

    state = after_success()
    operator.failed_logins = state.failed_logins
    operator.locked_until = state.locked_until
    operator.last_login_at = now
    if needs_rehash(operator.password_hash):
        # Same password, new parameters: not a password change, so sessions stay.
        operator.password_hash = hash_password(body.password)
    await record(
        session,
        actor_type=ActorType.OPERATOR,
        actor_id=operator.id,
        action="auth.login",
        entity_type="operator",
        entity_id=operator.id,
    )
    await session.commit()

    cookie = signer.sign(operator.id, operator.session_version)
    response.set_cookie(
        COOKIE_NAME,
        cookie,
        max_age=MAX_AGE_SECONDS,
        httponly=True,
        secure=True,
        samesite="lax",
        path="/",
    )
    return _session_out(operator, signer, cookie)


async def _audit_failure(
    session: SessionDep, operator: Operator | None, *, reason: str, locked: bool = False
) -> None:
    # An unknown email has no id; the email itself never goes into the log.
    await record(
        session,
        actor_type=ActorType.OPERATOR,
        actor_id=None if operator is None else operator.id,
        action="auth.login_failed",
        entity_type="operator",
        entity_id="unknown" if operator is None else operator.id,
        details={"reason": reason, "locked": locked},
    )


@router.post("/logout", status_code=204)
async def logout(
    response: Response,
    operator: OperatorDep,
    _csrf: Annotated[None, Depends(check_csrf)],
    session: SessionDep,
) -> None:
    """Ends every session of this operator: the stored `session_version` moves on, so all
    cookies issued so far (a copied one too) stop working (security §4)."""
    await OperatorRepository(session).end_sessions(operator)
    await record(
        session,
        actor_type=ActorType.OPERATOR,
        actor_id=operator.id,
        action="auth.logout",
        entity_type="operator",
        entity_id=operator.id,
    )
    await session.commit()
    response.delete_cookie(COOKIE_NAME, path="/", secure=True, httponly=True, samesite="lax")


@router.get("/me")
async def me(
    operator: OperatorDep, signer: SignerDep, hub_operator: OperatorCookie = None
) -> OperatorSessionOut:
    # `operator` exists only for a valid cookie, so the cookie is present here.
    if hub_operator is None:  # pragma: no cover - guarded by the dependency
        raise ProblemError(401, "unauthorized", "An operator session is required.")
    return _session_out(operator, signer, hub_operator)
