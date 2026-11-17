"""The app container: the only shared state of the process (AGENTS.md, code style).

Built once in `create_app` and kept on `app.state.container`. Tests build their own.
"""

import dataclasses
from dataclasses import dataclass, field
from decimal import Decimal

from hub.api.limits import RateLimits
from hub.auth.customer_session import CustomerSessionSigner
from hub.auth.operator_session import OperatorSessionSigner
from hub.config import Settings
from hub.db.session import Database, connect
from hub.graph.linear import VARIANT, KbSearch, Pipeline, RunTasks, write_prompt_for
from hub.kb.service import EMBEDDING_CALL, KbServices, build_kb_services, index_counts
from hub.llm.budget import BudgetGuard
from hub.llm.gateway import build_gateway, embedder
from hub.llm.prompts import active_prompt
from hub.llm.recorder import DbCallRecorder
from hub.observability.readiness import (
    ReadinessCheck,
    kb_index_check,
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
    # The phase-3 workflow (intake → triage → search → write). None in tests that start no
    # run: then a new ticket gets the phase-2 placeholder answer and waits in the queue.
    pipeline: Pipeline | None = None
    runs: RunTasks = field(default_factory=RunTasks)
    # Knowledge-base index and search (P5-01, P5-02). None in tests that need no search: the
    # KB endpoints then answer 503.
    kb: KbServices | None = None

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


def build_pipeline(
    settings: Settings, database: Database, retriever: KbSearch | None = None
) -> Pipeline:
    """Gateway, budget guard and active prompts, loaded once: a broken config or prompt file
    stops the start. One guard serves the gateway (every online call) and the run (its start).
    `retriever`: the knowledge-base search; without it every run is handed to a human."""
    budget = BudgetGuard(database, default_limit=Decimal(str(settings.daily_budget_usd)))
    gateway = build_gateway(settings, DbCallRecorder(database), budget)
    return Pipeline(
        database=database,
        gateway=gateway,
        budget=budget,
        triage_prompt=active_prompt(settings.prompts_dir, gateway.models, "triage"),
        write_prompt=write_prompt_for(settings.prompts_dir, gateway.models, VARIANT),
        verify_prompt=active_prompt(settings.prompts_dir, gateway.models, "verify"),
        retriever=retriever,
    )


def build_container(settings: Settings) -> Container:
    database = connect(settings.database_url.get_secret_value())
    pipeline = build_pipeline(settings, database)
    kb = build_kb_services(settings, embedder(pipeline.gateway, EMBEDDING_CALL))
    # The search embeds its query through the same gateway the pipeline uses.
    pipeline = dataclasses.replace(pipeline, retriever=kb.retriever)

    async def counts() -> tuple[int, int]:
        return await index_counts(database, kb.index)

    return Container(
        settings=settings,
        database=database,
        pipeline=pipeline,
        kb=kb,
        readiness_checks={
            "postgres": postgres_check(settings.database_url.get_secret_value()),
            "qdrant": qdrant_check(settings.qdrant_url),
            "kb_index": kb_index_check(counts),
            "mcp": mcp_check(settings.mcp_server_url, settings.mcp_server_token.get_secret_value()),
        },
    )
