"""Every `/console` and `/admin` route declares a role (security §4; P2-07 "Done when").

The walk looks for a `RequireRole` dependency anywhere in a route's dependency tree. It is
checked against a small app with a route that has no role, so the test cannot pass vacuously.
FastAPI 0.162 keeps included routers as nested entries of `app.routes`; `iter_route_contexts`
(the function its OpenAPI generator uses) yields every route with its effective path and
dependency tree, router-level dependencies included.
"""

from collections.abc import Iterator

from fastapi import APIRouter, Depends, FastAPI
from fastapi.dependencies.models import Dependant
from fastapi.routing import RouteContext, iter_route_contexts

from hub.api.admin_jobs import RequireJobToken
from hub.api.dependencies import RequireRole
from hub.auth.roles import OPERATOR, RoleRequirement
from hub.config import Settings
from hub.deps import Container
from hub.main import create_app

PROTECTED_PREFIXES = ("/api/v1/console", "/api/v1/admin")


def _requirements(dependant: Dependant) -> Iterator[RoleRequirement | RequireJobToken]:
    """A role, or the job token (`/admin/jobs/*` only: GitHub Actions has no session, P8-05)."""
    if isinstance(dependant.call, RequireRole):
        yield dependant.call.requirement
    if isinstance(dependant.call, RequireJobToken):
        yield dependant.call
    for child in dependant.dependencies:
        yield from _requirements(child)


def _protected_routes(app: FastAPI) -> list[RouteContext]:
    return [
        route
        for route in iter_route_contexts(app.routes)
        if route.path is not None and route.path.startswith(PROTECTED_PREFIXES)
    ]


def routes_without_role(app: FastAPI) -> list[str]:
    return [
        f"{sorted(route.methods or ())} {route.path}"
        for route in _protected_routes(app)
        if not list(_requirements(route.dependant))
    ]


def _app() -> FastAPI:
    return create_app(Container(settings=Settings(_env_file=None, app_env="test")))


def test_every_console_and_admin_route_has_a_role() -> None:
    app = _app()
    assert _protected_routes(app), "no console routes found: the walk would pass vacuously"
    assert routes_without_role(app) == []


def test_only_the_job_routes_use_the_job_token() -> None:
    """security §4: the token gives no access to any other endpoint."""
    with_token = {
        route.path
        for route in _protected_routes(_app())
        if any(isinstance(found, RequireJobToken) for found in _requirements(route.dependant))
    }
    assert with_token == {"/api/v1/admin/jobs/{name}"}
    for route in _protected_routes(_app()):
        found = list(_requirements(route.dependant))
        assert not (
            any(isinstance(item, RequireJobToken) for item in found)
            and any(isinstance(item, RoleRequirement) for item in found)
        ), route.path


def test_the_walk_finds_a_route_without_a_role() -> None:
    app = FastAPI()
    router = APIRouter(prefix="/api/v1/admin")

    @router.get("/open")
    async def open_route() -> None: ...

    @router.get("/closed")
    async def closed_route(_operator: object = Depends(RequireRole(OPERATOR))) -> None: ...

    app.include_router(router)
    assert routes_without_role(app) == ["['GET'] /api/v1/admin/open"]


def test_phase_2_console_routes_are_present() -> None:
    paths = {
        (method, route.path)
        for route in _protected_routes(_app())
        for method in route.methods or ()
    }
    assert {
        ("GET", "/api/v1/console/tickets"),
        ("GET", "/api/v1/console/tickets/{ticket_id}"),
        ("POST", "/api/v1/console/tickets/{ticket_id}/claim"),
        ("POST", "/api/v1/console/tickets/{ticket_id}/release"),
        ("POST", "/api/v1/console/tickets/{ticket_id}/reply"),
        ("POST", "/api/v1/console/tickets/{ticket_id}/close"),
    } <= paths
