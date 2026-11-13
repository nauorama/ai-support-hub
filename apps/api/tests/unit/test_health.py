"""`/health` needs nothing; `/ready` reports each dependency and is 503 when one fails."""

import asyncio
from collections.abc import Callable

import httpx2
import pytest
from fastapi.testclient import TestClient

from hub.api import health
from hub.config import Settings
from hub.deps import build_container
from hub.observability.readiness import libpq_url, mcp_check, postgres_check, qdrant_check

MakeClient = Callable[..., TestClient]


async def ok() -> None:
    return None


async def refused() -> None:
    raise ConnectionRefusedError("postgresql://hub:secret@db/hub refused")


async def hangs() -> None:
    await asyncio.sleep(10)


def test_health_needs_no_dependency(make_client: MakeClient) -> None:
    response = make_client({"postgres": refused}).get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_ready_when_every_check_passes(make_client: MakeClient) -> None:
    response = make_client({"postgres": ok, "qdrant": ok, "mcp": ok}).get("/ready")
    assert response.status_code == 200
    assert response.json() == {
        "status": "ready",
        "checks": {
            "postgres": {"status": "ok"},
            "qdrant": {"status": "ok"},
            "mcp": {"status": "ok"},
        },
    }


def test_not_ready_names_the_failing_check_without_its_message(make_client: MakeClient) -> None:
    response = make_client({"postgres": refused, "qdrant": ok}).get("/ready")
    assert response.status_code == 503
    assert response.json() == {
        "status": "not_ready",
        "checks": {
            "postgres": {"status": "error", "error": "ConnectionRefusedError"},
            "qdrant": {"status": "ok"},
        },
    }
    assert "secret" not in response.text


def test_hanging_check_times_out(make_client: MakeClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(health, "CHECK_TIMEOUT_SECONDS", 0.05)
    response = make_client({"qdrant": hangs}).get("/ready")
    assert response.status_code == 503
    assert response.json()["checks"]["qdrant"] == {"status": "error", "error": "timeout"}


def test_container_checks_postgres_qdrant_and_mcp() -> None:
    container = build_container(Settings(_env_file=None))
    assert set(container.readiness_checks) == {"postgres", "qdrant", "mcp"}


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (
            "postgresql+psycopg://hub:hub@localhost:5432/hub",
            "postgresql://hub:hub@localhost:5432/hub",
        ),
        ("postgresql://hub@db/hub", "postgresql://hub@db/hub"),
    ],
)
def test_libpq_url_drops_the_driver(url: str, expected: str) -> None:
    assert libpq_url(url) == expected


class FakeConnection:
    def __init__(self) -> None:
        self.queries: list[str] = []
        self.closed = False

    async def __aenter__(self) -> "FakeConnection":
        return self

    async def __aexit__(self, *_: object) -> None:
        self.closed = True

    async def execute(self, query: str) -> None:
        self.queries.append(query)


async def test_postgres_check_runs_select_1_and_closes() -> None:
    connection = FakeConnection()
    calls: list[tuple[str, dict[str, object]]] = []

    async def connect(url: str, **kwargs: object) -> FakeConnection:
        calls.append((url, kwargs))
        return connection

    await postgres_check("postgresql+psycopg://hub@db/hub", connect=connect)()
    assert calls == [("postgresql://hub@db/hub", {"connect_timeout": 2})]
    assert connection.queries == ["SELECT 1"]
    assert connection.closed


def _transport(status: int, seen: list[httpx2.Request]) -> httpx2.MockTransport:
    def handle(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(status)

    return httpx2.MockTransport(handle)


async def test_qdrant_check_calls_readyz() -> None:
    seen: list[httpx2.Request] = []
    await qdrant_check("http://qdrant:6333/", transport=_transport(200, seen))()
    assert str(seen[0].url) == "http://qdrant:6333/readyz"


@pytest.mark.parametrize("status", [404, 503])
async def test_qdrant_check_fails_on_error_status(status: int) -> None:
    with pytest.raises(httpx2.HTTPStatusError):
        await qdrant_check("http://qdrant:6333", transport=_transport(status, []))()


@pytest.mark.parametrize("status", [200, 400, 405, 406])
async def test_mcp_check_accepts_any_answer_below_500(status: int) -> None:
    seen: list[httpx2.Request] = []
    await mcp_check("http://mcp:8001/mcp", "tok", transport=_transport(status, seen))()
    assert seen[0].headers["authorization"] == "Bearer tok"


async def test_mcp_check_fails_on_5xx_and_sends_no_auth_without_token() -> None:
    seen: list[httpx2.Request] = []
    with pytest.raises(httpx2.HTTPStatusError):
        await mcp_check("http://mcp:8001/mcp", "", transport=_transport(502, seen))()
    assert "authorization" not in seen[0].headers
