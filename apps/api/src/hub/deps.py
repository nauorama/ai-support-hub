"""The app container: the only shared state of the process (AGENTS.md, code style).

Built once in `create_app` and kept on `app.state.container`. Tests build their own.
"""

from dataclasses import dataclass, field

from hub.config import Settings
from hub.observability.readiness import (
    ReadinessCheck,
    mcp_check,
    postgres_check,
    qdrant_check,
)


@dataclass(frozen=True)
class Container:
    settings: Settings
    readiness_checks: dict[str, ReadinessCheck] = field(default_factory=dict)


def build_container(settings: Settings) -> Container:
    return Container(
        settings=settings,
        readiness_checks={
            "postgres": postgres_check(settings.database_url),
            "qdrant": qdrant_check(settings.qdrant_url),
            "mcp": mcp_check(settings.mcp_server_url, settings.mcp_server_token.get_secret_value()),
        },
    )
