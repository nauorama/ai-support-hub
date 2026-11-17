"""Pure parts of the evaluation runner (P4-03): ticket loading, resume bookkeeping, the cost
estimate and limit, the summary files. The whole run against Postgres is tested in
`apps/api/tests/integration/test_eval_runner.py`."""

import json
from decimal import Decimal
from pathlib import Path

import pytest

from hub.config import Settings
from hub.db.session import connect
from hub.llm.gateway import CachingProvider
from hub.llm.schemas import BudgetExceededError, ProviderResponse
from hub_evals import results as files
from hub_evals.runner import (
    TICKETS_FILE,
    CostLimit,
    EvalSetupError,
    Report,
    build_pipeline,
    describe,
    estimate_cost,
    estimate_tokens,
    eval_database_url,
    files_sha256,
    git_state,
    history_turns,
    load_tickets,
    main,
    masked_counts,
    parse_args,
    pending,
    run_name,
    same_database,
)
from hub_evals.schemas import HistoryMessage, Ticket

ROOT = Path(__file__).resolve().parents[2]
FIXTURE_CONFIG = {
    "models_config": ROOT / "config" / "models.yaml",
    "prompts_dir": ROOT / "prompts",
}
# A public test card number (data_check list).
TEST_CARD = "4242 4242 4242 4242"


def _tickets() -> list[Ticket]:
    return load_tickets(TICKETS_FILE)


def _result(ticket_id: str, *, paid: str = "0", route: str = "approval") -> files.TicketResult:
    return files.TicketResult(
        ticket_id=ticket_id,
        run_id=None,
        ticket_status="pending_approval",
        run_status="completed",
        run_failed=False,
        route=route,
        route_reasons=["auto_reply_off"],
        error=None,
        triage=files.Triage(intent="limits", secondary_intent=None, urgency="low", summary="s"),
        draft=None,
        models={"triage": "m"},
        prompt_versions={"triage": "v1"},
        cost_usd=Decimal("0.004"),
        paid_cost_usd=Decimal(paid),
        input_tokens=10,
        output_tokens=5,
        llm_calls=2,
        latency_ms=12,
    )


def _config(**changes: object) -> files.RunConfig:
    values: dict[str, object] = {
        "variant": "b0_full_kb",
        "llm_mode": "fake",
        "graph_version": "linear-v1",
        "models": {"triage": ["a", "b"]},
        "prompt_versions": {"triage": "v1"},
        "git_sha": "abc1234",
        "git_dirty": False,
        "core_anchor_date": None,
        "tickets_file": "data/eval/tickets.dev.jsonl",
        "tickets_sha256": "0" * 64,
    }
    return files.RunConfig.model_validate(values | changes)


# --- tickets ------------------------------------------------------------------------------------


def test_load_tickets_reads_the_dev_set_and_applies_the_limit() -> None:
    assert len(_tickets()) == 20
    assert [ticket.id for ticket in load_tickets(TICKETS_FILE, 3)] == [
        ticket.id for ticket in _tickets()[:3]
    ]


def test_the_test_set_is_never_read(tmp_path: Path) -> None:
    forbidden = tmp_path / "tickets.test.jsonl"
    forbidden.write_text("", encoding="utf-8")
    with pytest.raises(EvalSetupError, match="rule 7"):
        load_tickets(forbidden)


def test_a_missing_or_invalid_ticket_file_is_a_setup_error(tmp_path: Path) -> None:
    with pytest.raises(EvalSetupError, match="not found"):
        load_tickets(tmp_path / "none.jsonl")
    broken = tmp_path / "broken.jsonl"
    broken.write_text('\n{"id": "T-1"}\n', encoding="utf-8")
    with pytest.raises(EvalSetupError, match="line 2 is invalid"):
        load_tickets(broken)


def test_pending_skips_finished_tickets() -> None:
    tickets = _tickets()[:3]
    assert [t.id for t in pending(tickets, [_result(tickets[1].id)])] == [
        tickets[0].id,
        tickets[2].id,
    ]


def test_history_and_message_are_masked_like_the_api_masks_them() -> None:
    ticket = _tickets()[0].model_copy(
        update={"history": [HistoryMessage(role="customer", text=f"my card {TEST_CARD}")]}
    )
    (turn,) = history_turns(ticket)
    assert TEST_CARD not in turn.text
    assert turn.role == "customer"


def test_masked_counts() -> None:
    from hub.guardrails.masking import MaskResult, mask

    assert masked_counts(mask(f"card {TEST_CARD}")) == {"PAN": 1}
    assert masked_counts(MaskResult(text="x", counts={}, failed=True)) == {"failed": 1}


# --- run directory and resume -------------------------------------------------------------------


def test_run_name_marks_uncommitted_changes() -> None:
    assert run_name("2026-11-17", "b0_full_kb", "abc1234", dirty=False) == (
        "2026-11-17-b0_full_kb-abc1234"
    )
    assert run_name("2026-11-17", "b0_full_kb", "abc1234", dirty=True).endswith("-dirty")
    assert run_name("2026-11-17", "b0_full_kb", "abc1234", dirty=False, llm_mode="fake").endswith(
        "abc1234-fake"
    )


def test_git_state_without_git(tmp_path: Path) -> None:
    assert git_state(tmp_path) == ("nogit", None)


def test_git_state_hashes_the_uncommitted_changes() -> None:
    sha, diff = git_state()
    assert sha != "nogit"
    assert diff is None or len(diff) == 64


def test_files_sha256_sees_content_and_names(tmp_path: Path) -> None:
    (tmp_path / "a.md").write_text("one", encoding="utf-8")
    first = files_sha256([tmp_path / "a.md"], tmp_path)
    (tmp_path / "a.md").write_text("two", encoding="utf-8")
    assert files_sha256([tmp_path / "a.md"], tmp_path) != first
    (tmp_path / "a.md").rename(tmp_path / "b.md")
    assert files_sha256([tmp_path / "b.md"], tmp_path) != first


def test_same_database_ignores_login_and_query() -> None:
    assert same_database(
        "postgresql+psycopg://hub_api:x@db:5432/hub", "postgresql+psycopg://other@db/hub?sslmode=x"
    )
    assert not same_database("postgresql+psycopg://a@db/hub", "postgresql+psycopg://a@db/hub_eval")
    assert not same_database("postgresql+psycopg://a@db/hub", "postgresql+psycopg://a@other/hub")


def test_a_cut_off_line_is_dropped_and_the_first_result_wins(tmp_path: Path) -> None:
    path = tmp_path / files.RESULTS_FILE
    files.append_result(path, _result("T-0001"))
    files.append_result(path, _result("T-0002"))
    files.append_result(path, _result("T-0001", route="handoff"))
    with path.open("a", encoding="utf-8") as handle:
        handle.write(_result("T-0003").model_dump_json()[:40])  # an interrupted write
    done = files.read_results(path)
    assert [(r.ticket_id, r.route) for r in done] == [
        ("T-0001", "approval"),
        ("T-0002", "approval"),
    ]
    files.rewrite_results(path, done)
    assert list(tmp_path.glob("*.tmp")) == []
    files.append_result(path, _result("T-0003"))
    assert [r.ticket_id for r in files.read_results(path)] == ["T-0001", "T-0002", "T-0003"]


def test_a_missing_results_file_is_empty(tmp_path: Path) -> None:
    assert files.read_results(tmp_path / "none.jsonl") == []
    assert files.read_config(tmp_path / "none.json") is None


def test_config_round_trip_and_differences(tmp_path: Path) -> None:
    path = tmp_path / files.CONFIG_FILE
    files.write_config(path, _config())
    assert files.read_config(path) == _config()
    # How the run is done may change on resume; what it answers may not.
    assert files.config_differences(_config(), _config(limit=3, cache=False, concurrency=1)) == []
    assert files.config_differences(
        _config(), _config(prompt_versions={"triage": "v2"}, llm_mode="real")
    ) == ["llm_mode", "prompt_versions"]


# --- cost ---------------------------------------------------------------------------------------


async def test_the_cost_limit_refuses_only_when_enforced_and_reached() -> None:
    limit = CostLimit(limit=Decimal("0.10"), enforced=True)
    await limit.check()
    limit.add(Decimal("0.10"))
    assert limit.reached
    with pytest.raises(BudgetExceededError):
        await limit.check()
    fake = CostLimit(limit=Decimal(0), enforced=False)
    await fake.check()
    assert not fake.reached


def _pipeline(tmp_path: Path, *, cache: bool) -> tuple[object, CachingProvider | None]:
    settings = Settings(_env_file=None, llm_mode="real", **FIXTURE_CONFIG)  # type: ignore[arg-type]
    database = connect("postgresql+psycopg://nobody@localhost:1/none")  # never connected
    pipeline, _, provider = build_pipeline(
        settings, database, variant="b0_full_kb", cache_dir=tmp_path if cache else None
    )
    return pipeline, provider


def test_build_pipeline_uses_eval_context_and_the_cache(tmp_path: Path) -> None:
    from hub.domain.enums import LlmCallContext

    pipeline, cache = _pipeline(tmp_path, cache=True)
    assert pipeline.context == LlmCallContext.EVAL  # type: ignore[attr-defined]
    assert isinstance(cache, CachingProvider)
    assert _pipeline(tmp_path, cache=False)[1] is None


def test_the_estimate_prices_two_calls_per_ticket_and_cached_calls_are_free(
    tmp_path: Path,
) -> None:
    from hub.graph.linear import Pipeline

    pipeline, cache = _pipeline(tmp_path, cache=True)
    assert isinstance(pipeline, Pipeline)
    assert cache is not None
    tickets = _tickets()[:2]
    first = estimate_cost(pipeline, tickets, "kb text", cache)
    assert (first.calls, first.cached_calls) == (4, 0)
    assert first.cost_usd > 0
    # Store an answer for the first ticket's triage request: that call is free now.
    from hub.domain.triage import TriageResult
    from hub.graph.nodes.intake import intake
    from hub.graph.nodes.triage import triage_messages
    from hub.guardrails.masking import mask
    from hub.llm.gateway import write_fixture

    request = pipeline.gateway.chat_requests(
        "triage",
        triage_messages(
            pipeline.triage_prompt,
            intake(mask(tickets[0].message).text, history_turns(tickets[0])).message_for_llm,
        ),
        TriageResult,
    )[0]
    (tmp_path / "real").mkdir()
    write_fixture(tmp_path / "real", request, ProviderResponse(content="{}"))
    second = estimate_cost(pipeline, tickets, "kb text", cache)
    assert (second.calls, second.cached_calls) == (4, 1)
    assert second.cost_usd < first.cost_usd
    assert estimate_tokens(request) > 0


# --- settings and command line ------------------------------------------------------------------


def test_the_demo_database_is_refused() -> None:
    with pytest.raises(EvalSetupError, match="empty"):
        eval_database_url(Settings(_env_file=None))
    same = "postgresql+psycopg://a@h/hub"
    with pytest.raises(EvalSetupError, match="demo database"):
        eval_database_url(Settings(_env_file=None, database_url=same, eval_database_url=same))
    with pytest.raises(EvalSetupError, match="demo database"):
        eval_database_url(
            Settings(
                _env_file=None,
                database_url=same,
                eval_database_url="postgresql+psycopg://b:pw@h:5432/hub?application_name=x",
            )
        )


def test_parse_args() -> None:
    options = parse_args(["--limit", "10", "--no-cache", "--restart", "--concurrency", "2"])
    assert (options.limit, options.cache, options.restart, options.concurrency) == (
        10,
        False,
        True,
        2,
    )
    assert parse_args([]).cache
    with pytest.raises(SystemExit):
        parse_args(["--limit", "0"])
    with pytest.raises(SystemExit):
        parse_args(["--variant", "rag_hybrid"])  # not built yet (phase 5)


def test_main_reports_a_setup_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("EVAL_DATABASE_URL", "")
    monkeypatch.chdir(Path(__file__).parent)  # no .env here
    assert main(["--limit", "1"]) == 1
    assert "EVAL_DATABASE_URL is empty" in capsys.readouterr().err


def test_describe_names_counts_only(tmp_path: Path) -> None:
    report = Report(
        run_dir=tmp_path / "r",
        planned=10,
        skipped=4,
        ran=6,
        stopped_at_cost_limit=True,
        stopped_by_estimate=True,
        unavailable=2,
    )
    lines = describe(report, Settings(_env_file=None))
    assert any("2 tickets failed because the model was unavailable" in line for line in lines)
    assert any("estimate is above" in line for line in lines)
    assert any("10 tickets, 4 already finished, 6 run now" in line for line in lines)
    assert any("EVAL_MAX_COST_USD" in line for line in lines)


# --- summary ------------------------------------------------------------------------------------


def test_summary_and_report(tmp_path: Path) -> None:
    done = [_result("T-0001", paid="0.003"), _result("T-0002", route="handoff")]
    data = files.summary(done, planned=3)
    assert data["tickets_finished"] == 2
    assert data["routes"] == {"approval": 1, "handoff": 1}
    assert data["paid_cost_usd"] == "0.003"
    assert data["cost_per_ticket_usd"] == "0.004"
    files.write_metrics(tmp_path / files.METRICS_FILE, data)
    assert json.loads((tmp_path / files.METRICS_FILE).read_text())["tickets_planned"] == 3
    report = files.render_report(_config(), data, run_name="r1")
    assert "2 of 3" in report
    assert "Fake models" in report
    assert "demo check" in report.lower()
    assert files.summary([], planned=0)["cost_per_ticket_usd"] is None
    assert "Fake models" not in files.render_report(_config(llm_mode="real"), data, run_name="r")
