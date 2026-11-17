"""The evaluation runner against Postgres (P4-03).

- `make migrate` also upgrades the evaluation database (`-x db=eval`), after which the app role
  can write there (default privileges are per database).
- The runner sends evaluation tickets through the production `run_ticket`, resumes an
  interrupted run without repeating finished tickets, serves a re-run from the disk cache with no
  provider call, and stops before the first call when the estimate is above the limit.

Model answers come from the recordings in `tests/fixtures/llm` (the run tickets of
`tests/fixtures/run/tickets.jsonl`), served through a counting provider so the test sees every
call that would have reached a model.
"""

import json
import os
import subprocess
import sys
from collections.abc import Iterator
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

import psycopg
import pytest
from psycopg import sql

from hub.config import Settings
from hub.llm.gateway import FIXTURES_DIR, FixtureProvider
from hub.llm.schemas import ProviderError, ProviderRequest, ProviderResponse
from hub.observability.readiness import libpq_url
from hub_evals import results as files
from hub_evals.runner import TICKETS_FILE, EvalSetupError, Options, load_tickets, run

from ._api import Urls, own_database, run_sql
from ._compose import ROOT
from ._db import ALEMBIC_INI, app_url, as_user

DATABASE = "hub_eval_runner_test"
MIGRATE_DATABASE = "hub_eval_migrate_test"
RUN_TICKETS = {
    json.loads(line)["id"]: json.loads(line)["ticket"]
    for line in (ROOT / "apps/api/tests/fixtures/run/tickets.jsonl").read_text().splitlines()
    if line.strip()
}
NOT_RECORDED = "This exact text has no recorded model answer, so the run must fail."


# --- migrate step -------------------------------------------------------------------------------


def test_make_migrate_upgrades_the_eval_database_for_the_app_role(
    postgres_url: str, tmp_path: Path
) -> None:
    with psycopg.connect(libpq_url(postgres_url), autocommit=True) as connection:
        connection.execute(
            sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(MIGRATE_DATABASE))
        )
        connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(MIGRATE_DATABASE)))
    try:
        # The command of `make migrate`, with the migrator URL derived from DATABASE_URL_DIRECT
        # (EVAL_DATABASE_URL_DIRECT empty) and no .env (cwd is an empty folder).
        env = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith(("DATABASE_URL", "EVAL_DATABASE_URL"))
        }
        env["DATABASE_URL_DIRECT"] = as_user(postgres_url, "hub", "hub")
        env["EVAL_DATABASE_URL"] = app_url(postgres_url, database=MIGRATE_DATABASE)
        subprocess.run(
            [
                sys.executable,
                "-m",
                "alembic",
                "-c",
                str(ALEMBIC_INI),
                "-x",
                "db=eval",
                "upgrade",
                "head",
            ],
            cwd=tmp_path,
            env=env,
            check=True,
            capture_output=True,
        )
        urls = (
            as_user(postgres_url, "hub", "hub", database=MIGRATE_DATABASE),
            app_url(postgres_url, database=MIGRATE_DATABASE),
        )
        # The app role writes there now.
        with psycopg.connect(libpq_url(urls[1]), autocommit=True) as connection:
            connection.execute(
                "INSERT INTO customers (id, external_ref, display_name, email, tier, country)"
                " VALUES (gen_random_uuid(), 'CUS-0001', 'A', 'a@example.com', 'verified', 'SK')"
            )
        assert run_sql(urls, "SELECT count(*) FROM customers") == [(1,)]
        wrong = _alembic(tmp_path, env, "db=other")
        assert wrong.returncode != 0
        assert "unknown -x db=other" in wrong.stderr
        # An older .env without an evaluation database: skipped, `make migrate` still passes.
        skipped = _alembic(tmp_path, env | {"EVAL_DATABASE_URL": ""}, "db=eval")
        assert skipped.returncode == 0
        assert "evaluation database skipped" in skipped.stdout
        # The evaluation database must never be the app database.
        same = _alembic(
            tmp_path,
            env
            | {
                "EVAL_DATABASE_URL": app_url(postgres_url),
                "DATABASE_URL_DIRECT": env["DATABASE_URL_DIRECT"],
            },
            "db=eval",
        )
        assert same.returncode != 0
        assert "is the app database" in same.stderr
    finally:
        with psycopg.connect(libpq_url(postgres_url), autocommit=True) as connection:
            connection.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                    sql.Identifier(MIGRATE_DATABASE)
                )
            )


def _alembic(cwd: Path, env: dict[str, str], target: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "alembic", "-c", str(ALEMBIC_INI), "-x", target, "upgrade", "head"],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


# --- the runner ---------------------------------------------------------------------------------


@dataclass
class CountingProvider:
    """The recordings, counting every call that would have reached a model."""

    inner: FixtureProvider = field(default_factory=lambda: FixtureProvider(FIXTURES_DIR))
    calls: int = 0

    async def complete(self, request: ProviderRequest) -> ProviderResponse:
        self.calls += 1
        return await self.inner.complete(request)

    async def embed(self, request: ProviderRequest) -> ProviderResponse:
        self.calls += 1
        return await self.inner.embed(request)


@pytest.fixture(scope="module")
def eval_db(postgres_url: str) -> Iterator[Urls]:
    with own_database(postgres_url, DATABASE) as urls:
        yield urls


def _tickets_file(path: Path, texts: list[str]) -> Path:
    base = load_tickets(TICKETS_FILE)[1]
    assert base.history == []
    lines = [
        base.model_copy(update={"id": f"T-90{number:02d}", "message": text}).model_dump_json()
        for number, text in enumerate(texts, start=1)
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _settings(urls: Urls, **changes: object) -> Settings:
    values: dict[str, object] = {
        "app_env": "test",
        # Any database but the evaluation one: the runner never touches it.
        "database_url": "postgresql+psycopg://hub_api:hub_api@localhost:1/hub",
        "eval_database_url": urls[1],
        "llm_mode": "real",
        "models_config": ROOT / "config" / "models.yaml",
        "prompts_dir": ROOT / "prompts",
    }
    return Settings(_env_file=None, **(values | changes))  # type: ignore[arg-type]


def _results(run_dir: Path) -> dict[str, files.TicketResult]:
    return {r.ticket_id: r for r in files.read_results(run_dir / files.RESULTS_FILE)}


async def test_resume_cache_and_results(eval_db: Urls, tmp_path: Path) -> None:
    tickets = _tickets_file(
        tmp_path / "tickets.jsonl", [RUN_TICKETS["run_01"], RUN_TICKETS["run_02"], NOT_RECORDED]
    )
    provider = CountingProvider()
    settings = _settings(eval_db)
    run_dir = tmp_path / "run"
    options = Options(
        tickets=tickets, limit=2, run_dir=run_dir, cache_dir=tmp_path / "cache", runs_dir=tmp_path
    )

    first = await run(settings, options, provider=provider)
    assert (first.planned, first.skipped, first.ran) == (2, 0, 2)
    assert provider.calls == 4  # triage and write of two tickets
    assert first.estimate is not None
    assert first.estimate.calls == 4
    assert first.estimate.cached_calls == 0
    done = _results(run_dir)
    sepa, no_answer = done["T-9001"], done["T-9002"]
    assert (sepa.route, sepa.route_reasons, sepa.ticket_status) == (
        "approval",
        ["auto_reply_off"],
        "pending_approval",
    )
    assert sepa.draft is not None
    assert sepa.draft.can_answer
    assert sepa.draft.evidence["variant"] == "b0_full_kb"
    assert sepa.triage.intent is not None
    assert sepa.paid_cost_usd == sepa.cost_usd > 0
    assert sepa.llm_calls == 2
    assert sepa.events == ["run.status", "run.status", "ticket.status"]
    assert no_answer.draft is not None
    assert not no_answer.draft.can_answer
    rows = run_sql(eval_db, "SELECT DISTINCT context, mode FROM llm_calls")
    assert rows == [("eval", "real")]

    # An interrupted write, then a resume with all three tickets: only the third runs.
    with (run_dir / files.RESULTS_FILE).open("a", encoding="utf-8") as handle:
        handle.write('{"ticket_id": "T-9003", "run_')
    second = await run(settings, dataclass_replace(options, limit=None), provider=provider)
    assert (second.planned, second.skipped, second.ran) == (3, 2, 1)
    assert provider.calls == 5  # the missing recording fails at once, without the fallback
    failed = _results(run_dir)["T-9003"]
    assert failed.run_failed
    assert (failed.route, failed.route_reasons, failed.ticket_status) == (
        "handoff",
        ["system_error"],
        "handed_off",
    )
    assert failed.error == "FixtureMissingError"
    assert failed.draft is None
    metrics = json.loads((run_dir / files.METRICS_FILE).read_text())
    assert metrics["tickets_finished"] == 3
    assert metrics["runs_failed"] == 1
    assert "3 of 3" in (run_dir / files.REPORT_FILE).read_text()

    # The same tickets again from the start: every answer comes from the cache.
    third = await run(settings, dataclass_replace(options, restart=True), provider=provider)
    assert third.ran == 2
    # The earlier results are kept beside the new ones, never deleted.
    (kept,) = run_dir.glob("results.before-*.jsonl")
    assert len(files.read_results(kept)) == 3
    assert provider.calls == 5
    assert (third.cache_hits, third.cache_misses) == (4, 0)
    assert third.estimate is not None
    assert third.estimate.cached_calls == 4
    assert third.estimate.cost_usd == 0
    replayed = _results(run_dir)["T-9001"]
    assert replayed.paid_cost_usd == 0
    assert replayed.cost_usd == sepa.cost_usd
    assert replayed.draft == sepa.draft
    modes = run_sql(
        eval_db, "SELECT DISTINCT mode FROM llm_calls WHERE run_id = %s", replayed.run_id
    )
    assert modes == [("fake",)]

    # Without the cache the provider is called again.
    fourth = await run(
        settings, dataclass_replace(options, restart=True, cache=False, limit=1), provider=provider
    )
    assert fourth.ran == 1
    assert provider.calls == 7

    # Another ticket file is another run: refused without --restart.
    _tickets_file(tickets, [RUN_TICKETS["run_02"]])
    with pytest.raises(EvalSetupError, match="tickets_sha256"):
        await run(settings, options, provider=provider)


@dataclass
class DownProvider:
    """A provider that is down: every call fails."""

    calls: int = 0

    async def complete(self, request: ProviderRequest) -> ProviderResponse:
        self.calls += 1
        raise ProviderError("ServiceUnavailable", transient=False)

    async def embed(self, request: ProviderRequest) -> ProviderResponse:
        return await self.complete(request)


async def test_a_ticket_failed_by_an_outage_runs_again_on_resume(
    eval_db: Urls, tmp_path: Path
) -> None:
    tickets = _tickets_file(tmp_path / "tickets.jsonl", [RUN_TICKETS["run_01"]])
    options = Options(tickets=tickets, run_dir=tmp_path / "run", cache_dir=tmp_path / "cache")
    down = DownProvider()
    report = await run(_settings(eval_db), options, provider=down)
    assert down.calls == 2  # the model and its fallback
    assert (report.ran, report.unavailable) == (0, 1)
    assert files.read_results(tmp_path / "run" / files.RESULTS_FILE) == []
    resumed = await run(_settings(eval_db), options, provider=CountingProvider())
    assert (resumed.skipped, resumed.ran) == (0, 1)


async def test_the_estimate_above_the_limit_stops_before_any_call(
    eval_db: Urls, tmp_path: Path
) -> None:
    tickets = _tickets_file(tmp_path / "tickets.jsonl", [RUN_TICKETS["run_01"]])
    provider = CountingProvider()
    report = await run(
        _settings(eval_db, eval_max_cost_usd=0.0001),
        Options(tickets=tickets, run_dir=tmp_path / "run", cache_dir=tmp_path / "cache"),
        provider=provider,
    )
    assert report.stopped_by_estimate
    assert report.ran == 0
    assert provider.calls == 0
    assert files.read_results(tmp_path / "run" / files.RESULTS_FILE) == []
    assert json.loads((tmp_path / "run" / files.METRICS_FILE).read_text())["stopped_by_estimate"]


async def test_fake_mode_pays_nothing_and_ignores_the_limit(eval_db: Urls, tmp_path: Path) -> None:
    tickets = _tickets_file(tmp_path / "tickets.jsonl", [RUN_TICKETS["run_01"]])
    report = await run(
        _settings(eval_db, llm_mode="fake", eval_max_cost_usd=0),
        Options(tickets=tickets, run_dir=tmp_path / "run", cache_dir=tmp_path / "cache"),
    )
    assert report.ran == 1
    assert not report.stopped_by_estimate
    (result,) = files.read_results(tmp_path / "run" / files.RESULTS_FILE)
    assert result.route == "approval"
    assert result.paid_cost_usd == Decimal(0)
    assert (tmp_path / "cache" / "fake").is_dir()


def dataclass_replace(options: Options, **changes: object) -> Options:
    import dataclasses

    return dataclasses.replace(options, **changes)  # type: ignore[arg-type]
