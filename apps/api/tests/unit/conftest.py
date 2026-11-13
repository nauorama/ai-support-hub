"""Test app: settings without `.env`, fake readiness checks, routes that fail on purpose."""

import json
from collections.abc import Callable
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel

from hub.api.errors import ProblemError
from hub.config import Settings
from hub.deps import Container
from hub.main import create_app
from hub.observability.readiness import ReadinessCheck

# Public test card number (Visa test range); never a real card.
TEST_CARD = "4111 1111 1111 1111"
# Placeholder password for the test settings; the logs must never contain it.
TEST_DB_PASSWORD = "placeholder-db-password"  # noqa: S105 - not a real password

MakeClient = Callable[..., TestClient]
LogLines = Callable[[], list[dict[str, Any]]]


class Payload(BaseModel):
    amount: int


def _build_app(checks: dict[str, ReadinessCheck]) -> FastAPI:
    # _env_file=None: unit tests must not read the developer's .env.
    settings = Settings(
        _env_file=None,
        app_env="test",
        database_url=f"postgresql+psycopg://hub:{TEST_DB_PASSWORD}@db/hub",
    )
    app = create_app(Container(settings=settings, readiness_checks=checks))

    @app.get("/boom")
    async def boom() -> None:
        raise RuntimeError(f"failed while handling card {TEST_CARD}")

    @app.post("/boom-with-body")
    async def boom_with_body(body: dict[str, str]) -> None:
        # Request content and a secret in the frame's locals; neither may reach the logs.
        message = body["message"]
        dsn = settings.database_url.get_secret_value()
        raise RuntimeError(f"failed after {len(message)} chars, {len(dsn)} chars of dsn")

    @app.get("/conflict")
    async def conflict() -> None:
        raise ProblemError(409, "stale_draft", "The draft is out of date.")

    @app.post("/payload")
    async def payload(body: Payload) -> dict[str, int]:
        return {"amount": body.amount}

    return app


@pytest.fixture
def test_db_password() -> str:
    return TEST_DB_PASSWORD


@pytest.fixture
def test_card() -> str:
    return TEST_CARD


@pytest.fixture
def make_client() -> MakeClient:
    def make(checks: dict[str, ReadinessCheck] | None = None) -> TestClient:
        # raise_server_exceptions=False: assert on what the caller gets, not a test-side raise.
        return TestClient(_build_app(checks or {}), raise_server_exceptions=False)

    return make


@pytest.fixture
def log_lines(capsys: pytest.CaptureFixture[str]) -> LogLines:
    """The JSON log lines written to stdout so far."""

    def read() -> list[dict[str, Any]]:
        out = capsys.readouterr().out
        return [json.loads(line) for line in out.splitlines() if line.startswith("{")]

    return read
