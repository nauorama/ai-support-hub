"""Operator login, logout, `me`, CSRF and roles against Postgres as the app role (P2-07)."""

import asyncio
import re
import uuid
from collections.abc import Iterator

import pytest
from argon2 import PasswordHasher
from fastapi import FastAPI
from fastapi.dependencies.models import Dependant
from fastapi.routing import iter_route_contexts
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from hub.api.dependencies import RequireRole
from hub.api.limits import RateLimiter
from hub.audit import ChainCheck, verify_chain
from hub.auth.passwords import hash_password, needs_rehash
from hub.db.models import Operator
from hub.db.repositories import OperatorRepository
from hub.deps import Container
from hub.main import create_app

from . import _api
from ._api import OPERATOR_PASSWORD, Urls, add_operator, operator_client, run_sql

DATABASE = "hub_auth_api_test"
EMAIL = "marta@tallybridge.test"


@pytest.fixture(scope="module")
def urls(postgres_url: str) -> Iterator[Urls]:
    with _api.own_database(postgres_url, DATABASE) as found:
        yield found


@pytest.fixture(autouse=True)
def _empty(urls: Urls) -> None:
    _api.empty_tables(urls)


@pytest.fixture
def container(urls: Urls) -> Container:
    return _api.container(urls)


@pytest.fixture
def app(container: Container) -> Iterator[FastAPI]:
    app = create_app(container)
    with TestClient(app):  # runs the lifespan, so the engine is disposed at the end
        yield app


@pytest.fixture
def operator_id(urls: Urls) -> uuid.UUID:
    return add_operator(urls, EMAIL, "operator")


def _login(app: FastAPI, password: str = OPERATOR_PASSWORD, email: str = EMAIL) -> TestClient:
    client = _api.client(app)
    client.post("/api/v1/auth/login", json={"email": email, "password": password})
    return client


def _actions(urls: Urls) -> list[tuple[str, str, dict[str, object]]]:
    rows = run_sql(urls, "SELECT action, entity_id, details FROM audit_log ORDER BY id")
    return [(action, entity_id, details) for action, entity_id, details in rows]


def _chain(urls: Urls) -> ChainCheck:
    async def check() -> ChainCheck:
        engine = create_async_engine(urls[1])
        try:
            async with AsyncSession(engine) as session:
                return await verify_chain(session)
        finally:
            await engine.dispose()

    return asyncio.run(check())


def test_login_sets_a_secure_http_only_cookie_and_returns_the_csrf_token(
    app: FastAPI, urls: Urls, operator_id: uuid.UUID
) -> None:
    client = _api.client(app)
    response = client.post(
        "/api/v1/auth/login", json={"email": EMAIL.upper(), "password": OPERATOR_PASSWORD}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["operator"] == {
        "id": str(operator_id),
        "email": EMAIL,
        "name": "Marta Kováčová",
        "role": "operator",
    }
    assert len(body["csrf_token"]) == 64
    cookie = response.headers["set-cookie"]
    assert cookie.startswith("hub_operator=")
    for flag in ("HttpOnly", "Secure", "SameSite=lax", "Max-Age=28800", "Path=/"):
        assert flag in cookie
    # The token is in the body only, never in a cookie a script could read.
    assert body["csrf_token"] not in cookie

    me = client.get("/api/v1/auth/me")
    assert me.status_code == 200
    assert me.json() == body
    assert run_sql(urls, "SELECT last_login_at IS NOT NULL FROM operators") == [(True,)]
    assert _actions(urls) == [("auth.login", str(operator_id), {})]


@pytest.mark.parametrize("case", ["wrong_password", "unknown", "inactive"])
def test_every_failed_login_looks_the_same_and_is_audited_without_the_email(
    app: FastAPI, urls: Urls, case: str
) -> None:
    add_operator(urls, EMAIL, active=case != "inactive")
    email = "nobody@tallybridge.test" if case == "unknown" else EMAIL
    password = "wrong-password" if case == "wrong_password" else OPERATOR_PASSWORD
    response = _api.client(app).post(
        "/api/v1/auth/login", json={"email": email, "password": password}
    )
    assert response.status_code == 401
    assert response.json()["code"] == "invalid_credentials"
    assert response.json()["detail"] == "The email or password is not valid."
    assert "set-cookie" not in response.headers
    [(action, _entity, details)] = _actions(urls)
    assert action == "auth.login_failed"
    assert details == {"reason": case, "locked": False}
    dump = str(run_sql(urls, "SELECT * FROM audit_log"))
    assert "@" not in dump
    assert _chain(urls).ok


def test_ten_wrong_passwords_lock_the_account_in_the_database(
    app: FastAPI, urls: Urls, operator_id: uuid.UUID, container: Container
) -> None:
    # Only the account lockout here: the per-IP limit (5) is tested separately.
    container.rate_limits.login_failures_per_ip = RateLimiter(100, 15 * 60)
    for attempt in range(10):
        response = _api.client(app).post(
            "/api/v1/auth/login", json={"email": EMAIL, "password": f"wrong-{attempt}"}
        )
        assert response.status_code == 401
    [(failed, locked)] = run_sql(urls, "SELECT failed_logins, locked_until > now() FROM operators")
    assert (failed, locked) == (0, True)

    # The right password does not open a locked account and is answered like any failure.
    response = _api.client(app).post(
        "/api/v1/auth/login", json={"email": EMAIL, "password": OPERATOR_PASSWORD}
    )
    assert response.status_code == 401
    assert response.json()["code"] == "invalid_credentials"
    assert _actions(urls)[-1][2] == {"reason": "locked", "locked": True}

    # The lock is in the database: a new app (a restart) still refuses, until it ends.
    restarted = create_app(_api.container(urls))
    response = _api.client(restarted).post(
        "/api/v1/auth/login", json={"email": EMAIL, "password": OPERATOR_PASSWORD}
    )
    assert response.status_code == 401
    run_sql(urls, "UPDATE operators SET locked_until = now() - interval '1 second'")
    assert operator_client(restarted, EMAIL).get("/api/v1/auth/me").status_code == 200
    assert run_sql(urls, "SELECT failed_logins, locked_until FROM operators") == [(0, None)]


def test_a_successful_login_resets_the_failure_count(
    app: FastAPI, urls: Urls, operator_id: uuid.UUID
) -> None:
    for _ in range(3):
        _login(app, "wrong")
    assert run_sql(urls, "SELECT failed_logins FROM operators") == [(3,)]
    operator_client(app, EMAIL)
    assert run_sql(urls, "SELECT failed_logins FROM operators") == [(0,)]


def test_five_failed_logins_from_one_ip_get_429(app: FastAPI, operator_id: uuid.UUID) -> None:
    for _ in range(5):
        assert _login(app, "wrong").cookies.get("hub_operator") is None
    response = _api.client(app).post(
        "/api/v1/auth/login", json={"email": EMAIL, "password": OPERATOR_PASSWORD}
    )
    assert response.status_code == 429
    assert int(response.headers["Retry-After"]) > 0


def test_logout_ends_every_session_of_the_operator(
    app: FastAPI, urls: Urls, operator_id: uuid.UUID
) -> None:
    first = operator_client(app, EMAIL)
    second = operator_client(app, EMAIL)
    copied = _api.client(app)
    copied.cookies.set("hub_operator", first.cookies["hub_operator"], domain="testserver")

    response = first.post("/api/v1/auth/logout")
    assert response.status_code == 204
    assert 'hub_operator=""' in response.headers["set-cookie"]
    assert "Max-Age=0" in response.headers["set-cookie"]
    # The copied cookie and the other browser are logged out too.
    assert copied.get("/api/v1/auth/me").status_code == 401
    assert second.get("/api/v1/auth/me").status_code == 401
    assert run_sql(urls, "SELECT session_version FROM operators") == [(2,)]
    assert [action for action, _e, _d in _actions(urls)][-1] == "auth.logout"
    # A new login works with the new version.
    assert operator_client(app, EMAIL).get("/api/v1/auth/me").status_code == 200


def test_logout_needs_the_csrf_token(app: FastAPI, operator_id: uuid.UUID) -> None:
    client = operator_client(app, EMAIL)
    del client.headers["X-CSRF-Token"]
    response = client.post("/api/v1/auth/logout")
    assert response.status_code == 403
    assert response.json()["code"] == "csrf_failed"
    assert client.get("/api/v1/auth/me").status_code == 200


def test_a_password_change_ends_the_sessions(
    app: FastAPI, urls: Urls, operator_id: uuid.UUID
) -> None:
    client = operator_client(app, EMAIL)

    async def change() -> None:
        engine = create_async_engine(urls[1])
        try:
            async with AsyncSession(engine) as session:
                operator = await session.get(Operator, operator_id)
                assert operator is not None
                await OperatorRepository(session).change_password(
                    operator, hash_password("a-new-password")
                )
                await session.commit()
        finally:
            await engine.dispose()

    asyncio.run(change())
    assert client.get("/api/v1/auth/me").status_code == 401
    assert operator_client(app, EMAIL, "a-new-password").get("/api/v1/auth/me").status_code == 200


def test_a_deactivated_operator_loses_the_session(
    app: FastAPI, urls: Urls, operator_id: uuid.UUID
) -> None:
    client = operator_client(app, EMAIL)
    run_sql(urls, "UPDATE operators SET is_active = false")
    assert client.get("/api/v1/auth/me").status_code == 401


def test_a_demoted_operator_loses_the_role_at_once(app: FastAPI, urls: Urls) -> None:
    add_operator(urls, EMAIL, "lead")
    client = operator_client(app, EMAIL)
    assert client.get("/api/v1/auth/me").json()["operator"]["role"] == "lead"
    run_sql(urls, "UPDATE operators SET role = 'compliance'")
    # The role is read from the database on every request, never from the cookie.
    assert client.get("/api/v1/auth/me").json()["operator"]["role"] == "compliance"


def test_without_a_session_me_and_console_answer_401(app: FastAPI) -> None:
    client = _api.client(app)
    assert client.get("/api/v1/auth/me").status_code == 401
    assert client.get("/api/v1/console/tickets").status_code == 401
    client.cookies.set("hub_operator", "forged", domain="testserver")
    response = client.get("/api/v1/console/tickets")
    assert response.status_code == 401
    assert response.json()["code"] == "unauthorized"


def test_a_customer_session_is_not_an_operator_session(app: FastAPI, urls: Urls) -> None:
    _api.add_customer(urls)
    customer = _api.customer_client(app)
    customer.cookies.set("hub_operator", customer.cookies["hub_customer"], domain="testserver")
    assert customer.get("/api/v1/console/tickets").status_code == 401


def test_login_is_unavailable_without_a_session_secret(urls: Urls) -> None:
    settings = _api.container(urls).settings.model_copy(update={"session_secret": SecretStr("")})
    app = create_app(Container(settings=settings, database=_api.container(urls).database))
    response = _api.client(app).post(
        "/api/v1/auth/login", json={"email": EMAIL, "password": OPERATOR_PASSWORD}
    )
    assert response.status_code == 503
    assert response.json()["code"] == "login_unavailable"


def test_login_rehashes_an_old_hash_without_ending_sessions(app: FastAPI, urls: Urls) -> None:
    operator_id = add_operator(urls, EMAIL)
    old = PasswordHasher(time_cost=1, memory_cost=8192, parallelism=1).hash(OPERATOR_PASSWORD)
    run_sql(urls, "UPDATE operators SET password_hash = %s", old)
    client = operator_client(app, EMAIL)
    [(stored, version)] = run_sql(urls, "SELECT password_hash, session_version FROM operators")
    assert stored != old
    assert not needs_rehash(stored)
    assert version == 1
    assert client.get("/api/v1/auth/me").json()["operator"]["id"] == str(operator_id)


# --- P8-04: CSRF on every state-changing operator route, cookie flags ------------------------

_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


def _has_role_dependency(dependant: Dependant) -> bool:
    return isinstance(dependant.call, RequireRole) or any(
        _has_role_dependency(child) for child in dependant.dependencies
    )


def _state_changing_operator_routes(app: FastAPI) -> list[tuple[str, str]]:
    """Every non-GET route behind `RequireRole`, with its path parameters filled in."""
    routes: list[tuple[str, str]] = []
    for route in iter_route_contexts(app.routes):
        # Starlette's own routes (`/openapi.json`, the docs pages) have no dependency tree.
        dependant = getattr(route, "dependant", None)
        if route.path is None or dependant is None or not _has_role_dependency(dependant):
            continue
        path = re.sub(r"\{[^}]+\}", str(uuid.uuid4()), route.path)
        routes.extend(
            (method, path) for method in sorted(route.methods or ()) if method not in _SAFE_METHODS
        )
    return routes


def test_every_state_changing_operator_route_needs_the_csrf_token(
    app: FastAPI, operator_id: uuid.UUID
) -> None:
    routes = _state_changing_operator_routes(app)
    # The walk must find the console's write routes, or it would pass vacuously.
    assert ("POST", "/api/v1/admin/kb/reindex") in routes
    assert len(routes) >= 10, routes
    client = operator_client(app, EMAIL)
    client.headers.pop("X-CSRF-Token")
    for method, path in routes:
        for headers in ({}, {"X-CSRF-Token": "0" * 64}, {"X-CSRF-Token": ""}):
            response = client.request(
                method, path, json={}, headers={"Idempotency-Key": str(uuid.uuid4()), **headers}
            )
            # Before the body is validated and before the role is checked (security §4).
            assert response.status_code == 403, (method, path, response.text)
            assert response.json()["code"] == "csrf_failed", (method, path)


def test_a_csrf_token_of_another_operator_is_refused(app: FastAPI, urls: Urls) -> None:
    add_operator(urls, EMAIL, "admin")
    add_operator(urls, "other@tallybridge.test", "admin")
    mine = operator_client(app, EMAIL)
    theirs = operator_client(app, "other@tallybridge.test")
    mine.headers["X-CSRF-Token"] = theirs.headers["X-CSRF-Token"]
    response = mine.post("/api/v1/admin/kb/reindex")
    assert response.status_code == 403
    assert response.json()["code"] == "csrf_failed"


def test_the_logout_cookie_deletion_keeps_the_cookie_flags(
    app: FastAPI, operator_id: uuid.UUID
) -> None:
    cookie = operator_client(app, EMAIL).post("/api/v1/auth/logout").headers["set-cookie"]
    assert cookie.startswith('hub_operator=""')
    for flag in ("HttpOnly", "Secure", "SameSite=lax", "Max-Age=0", "Path=/"):
        assert flag in cookie
