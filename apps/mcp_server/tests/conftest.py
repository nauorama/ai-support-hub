"""Fixtures for the support-tools tests: the real seed files, a fixed anchor, a temp mock store."""

from collections.abc import Iterator
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from mcp.server.mcpserver import MCPServer

from support_tools.adapters.mock_store import MockStore
from support_tools.server import DEFAULT_SEED_DIR, ServerSettings, build_server

# A Thursday; the operations default for a fixed CORE_ANCHOR_DATE.
ANCHOR = date(2026, 11, 12)
NOW = datetime(2026, 11, 12, 9, 30, tzinfo=UTC)


@pytest.fixture
def settings(tmp_path: Path) -> ServerSettings:
    return ServerSettings(
        seed_dir=DEFAULT_SEED_DIR,
        mock_db=tmp_path / "mock.sqlite",
        anchor_date=ANCHOR,
        token="t" * 32,
    )


@pytest.fixture
def store(settings: ServerSettings) -> Iterator[MockStore]:
    opened = MockStore(settings.mock_db)
    yield opened
    opened.close()


@pytest.fixture
def server(settings: ServerSettings, store: MockStore) -> MCPServer:
    return build_server(settings, now=lambda: NOW, store=store)
