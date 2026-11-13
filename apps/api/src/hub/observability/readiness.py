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


class UnexpectedStatusError(Exception):
    """The dependency answered, but not with a status that shows it is usable."""


def http_check(
    url: str,
    *,
    accept: Callable[[int], bool],
    headers: dict[str, str] | None = None,
    transport: httpx2.AsyncBaseTransport | None = None,
) -> ReadinessCheck:
    """GET `url`; fails unless `accept(status)` is true. Redirects are not followed."""

    async def check() -> None:
        async with httpx2.AsyncClient(transport=transport, headers=headers) as client:
            response = await client.get(url)
        if not accept(response.status_code):
            raise UnexpectedStatusError(f"HTTP {response.status_code}")

    return check


def _is_success(status: int) -> bool:
    return 200 <= status < 300


# What an MCP streamable-HTTP endpoint may answer to a plain GET that is not an MCP exchange.
# 401, 403 and 404 are left out: a wrong token, a refused caller or a wrong path means the Hub
# cannot use the server, so it is not ready.
_MCP_REACHABLE_STATUSES = frozenset({400, 405, 406})


def _mcp_answered(status: int) -> bool:
    return _is_success(status) or status in _MCP_REACHABLE_STATUSES


def qdrant_check(
    qdrant_url: str, transport: httpx2.AsyncBaseTransport | None = None
) -> ReadinessCheck:
    # Qdrant's /readyz needs no API key (Qdrant docs, "Monitoring").
    return http_check(f"{qdrant_url.rstrip('/')}/readyz", accept=_is_success, transport=transport)


def mcp_check(
    mcp_server_url: str, token: str, transport: httpx2.AsyncBaseTransport | None = None
) -> ReadinessCheck:
    # Reachability only; the real MCP handshake is tested with the client in phase 7.
    # Accept: application/json, so the server never opens an SSE stream for this GET.
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return http_check(mcp_server_url, accept=_mcp_answered, headers=headers, transport=transport)
