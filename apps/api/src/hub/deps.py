"""The app container: the only shared state of the process (AGENTS.md, code style).

Built once in `create_app` and kept on `app.state.container`. Tests build their own.
"""

from dataclasses import dataclass, field

from hub.api.limits import RateLimits
from hub.auth.customer_session import CustomerSessionSigner
from hub.auth.operator_session import OperatorSessionSigner
from hub.config import Settings
from hub.db.session import Database, connect
from hub.graph.linear import Pipeline, RunTasks
from hub.llm.gateway import build_gateway
from hub.llm.prompts import active_prompt
from hub.llm.recorder import DbCallRecorder
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
    # None in tests that need no database.
    database: Database | None = None
    rate_limits: RateLimits = field(default_factory=RateLimits)
    # The phase-3 workflow (intake → triage → write). None in tests that start no run: then a
    # new ticket gets the phase-2 placeholder answer and waits in the queue.
    pipeline: Pipeline | None = None
    runs: RunTasks = field(default_factory=RunTasks)

    @property
    def session_signer(self) -> CustomerSessionSigner | None:
        """`None` while SESSION_SECRET is empty: then no customer session can be made or read."""
        secret = self.settings.session_secret.get_secret_value()
        return CustomerSessionSigner(secret) if secret else None

    @property
    def operator_session_signer(self) -> OperatorSessionSigner | None:
        """`None` while SESSION_SECRET is empty: then no operator can log in."""
        secret = self.settings.session_secret.get_secret_value()
        return OperatorSessionSigner(secret) if secret else None


def build_pipeline(settings: Settings, database: Database) -> Pipeline:
    """Gateway and active prompts, loaded once: a broken config or prompt file stops the start."""
    gateway = build_gateway(settings, DbCallRecorder(database))
    return Pipeline(
        database=database,
        gateway=gateway,
        triage_prompt=active_prompt(settings.prompts_dir, gateway.models, "triage"),
        write_prompt=active_prompt(settings.prompts_dir, gateway.models, "write"),
    )


def build_container(settings: Settings) -> Container:
    database = connect(settings.database_url.get_secret_value())
    return Container(
        settings=settings,
        database=database,
        pipeline=build_pipeline(settings, database),
        readiness_checks={
            "postgres": postgres_check(settings.database_url.get_secret_value()),
            "qdrant": qdrant_check(settings.qdrant_url),
            "mcp": mcp_check(settings.mcp_server_url, settings.mcp_server_token.get_secret_value()),
        },
    )
