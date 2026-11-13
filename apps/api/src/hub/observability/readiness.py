"""Checks behind `/ready`: does each dependency answer? A check raises when it does not.

Each check opens and closes its own connection: readiness is probed rarely, and this keeps the
checks free of app lifecycle. The caller applies the timeout.
"""

from collections.abc import Awaitable, Callable
from typing import Any

import httpx2
import psycopg

ReadinessCheck = Callable[[], Awaitable[None]]
Connect = Callable[..., Awaitable[Any]]


def libpq_url(sqlalchemy_url: str) -> str:
    """`postgresql+psycopg://…` (SQLAlchemy) → `postgresql://…` (psycopg)."""
    scheme, sep, rest = sqlalchemy_url.partition("://")
    return f"{scheme.split('+', 1)[0]}{sep}{rest}"


def postgres_check(
    database_url: str, connect: Connect = psycopg.AsyncConnection.connect
) -> ReadinessCheck:
    async def check() -> None:
        connection = await connect(libpq_url(database_url), connect_timeout=2)
        async with connection:
            await connection.execute("SELECT 1")

    return check


def http_check(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    transport: httpx2.AsyncBaseTransport | None = None,
    accept_client_errors: bool = False,
) -> ReadinessCheck:
    """GET `url`. A 5xx always fails; a 4xx fails unless `accept_client_errors`."""

    async def check() -> None:
        async with httpx2.AsyncClient(transport=transport, headers=headers) as client:
            response = await client.get(url)
        if response.status_code >= 500 or (
            response.status_code >= 400 and not accept_client_errors
        ):
            response.raise_for_status()

    return check


def qdrant_check(
    qdrant_url: str, transport: httpx2.AsyncBaseTransport | None = None
) -> ReadinessCheck:
    # Qdrant's /readyz needs no API key (Qdrant docs, "Monitoring").
    return http_check(f"{qdrant_url.rstrip('/')}/readyz", transport=transport)


def mcp_check(
    mcp_server_url: str, token: str, transport: httpx2.AsyncBaseTransport | None = None
) -> ReadinessCheck:
    # A plain GET is not a valid MCP exchange, so the server may answer 4xx; any answer below
    # 500 shows it is up. The real MCP handshake is tested with the client in phase 7.
    headers = {"Authorization": f"Bearer {token}"} if token else None
    return http_check(
        mcp_server_url, headers=headers, transport=transport, accept_client_errors=True
    )
