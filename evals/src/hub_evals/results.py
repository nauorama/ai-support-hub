"""The files of one evaluation run (eval plan §4): `config.json`, `results.jsonl`, `metrics.json`
and `report.md` in `evals/runs/<date>-<variant>-<git sha>/`.

`results.jsonl` gets one line per finished ticket, written as soon as the ticket is done, so an
interrupted run continues where it stopped: the tickets already in the file are not run again.
A line cut off by the interruption is dropped on resume and that ticket runs again.

Only `report.md` and `metrics.json` are committed; `results.jsonl` holds the drafts and stays
local (git-ignored). The metrics of eval plan §2 (P4-04) and the judges (P4-05) add to
`metrics.json` and `report.md`; until then they hold the run summary only.
"""

import json
from collections import Counter
from collections.abc import Iterable, Sequence
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

CONFIG_FILE = "config.json"
RESULTS_FILE = "results.jsonl"
METRICS_FILE = "metrics.json"
REPORT_FILE = "report.md"


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class RunConfig(_Frozen):
    """What decides the outcome of a run. A resumed run must have the same `fingerprint`: results
    of another prompt, model, variant or ticket file would mix in one report."""

    variant: str
    llm_mode: str
    graph_version: str
    models: dict[str, list[str]]  # role -> [model, fallback]
    prompt_versions: dict[str, str]
    git_sha: str
    git_dirty: bool
    git_diff_sha256: str | None = None
    prompts_sha256: str = ""
    kb_sha256: str = ""
    core_anchor_date: date | None
    tickets_file: str
    tickets_sha256: str
    # Not part of the fingerprint: they change how the run is done, not what it answers.
    limit: int | None = None
    cache: bool = True
    concurrency: int = 5
    max_cost_usd: Decimal = Decimal("1.00")

    def fingerprint(self) -> dict[str, Any]:
        return self.model_dump(
            mode="json", exclude={"limit", "cache", "concurrency", "max_cost_usd"}
        )


class Triage(_Frozen):
    intent: str | None
    secondary_intent: str | None
    urgency: str | None
    summary: str | None


class DraftOut(_Frozen):
    can_answer: bool
    body: str
    missing_information: str | None
    citations: list[dict[str, Any]]
    evidence: dict[str, Any]
    proposed_actions: list[dict[str, Any]]


class TicketResult(_Frozen):
    """One line of `results.jsonl`: what the workflow did with one evaluation ticket. Labels stay
    in the ticket file; metrics join the two by `ticket_id`."""

    ticket_id: str
    run_id: str | None
    ticket_status: str | None
    run_status: str | None
    run_failed: bool
    route: str | None
    route_reasons: list[str]
    error: str | None
    triage: Triage
    draft: DraftOut | None
    models: dict[str, str]
    prompt_versions: dict[str, str]
    # All calls of the run, replays of recordings and of the cache included.
    cost_usd: Decimal
    # Only calls that reached a real provider (`llm_calls.mode = 'real'`): what was paid.
    paid_cost_usd: Decimal
    input_tokens: int
    output_tokens: int
    llm_calls: int
    latency_ms: int
    events: list[str] = Field(default_factory=list)


def read_results(path: Path) -> list[TicketResult]:
    """The valid lines of `results.jsonl`, first one per ticket. A missing file is empty."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []
    results: dict[str, TicketResult] = {}
    for line in lines:
        if not line.strip():
            continue
        try:
            result = TicketResult.model_validate_json(line)
        except ValidationError:
            # An interrupted write: the ticket runs again.
            continue
        results.setdefault(result.ticket_id, result)
    return list(results.values())


def rewrite_results(path: Path, results: Iterable[TicketResult]) -> None:
    """Keep only the valid lines, so a resumed run appends after a complete line. Written to a
    temporary file and moved into place: an interruption here never loses finished tickets."""
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text("".join(_line(result) for result in results), encoding="utf-8")
    temporary.replace(path)


def append_result(path: Path, result: TicketResult) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write(_line(result))
        handle.flush()


def _line(result: TicketResult) -> str:
    return result.model_dump_json() + "\n"


def write_config(path: Path, config: RunConfig) -> None:
    path.write_text(config.model_dump_json(indent=2) + "\n", encoding="utf-8")


def read_config(path: Path) -> RunConfig | None:
    try:
        return RunConfig.model_validate_json(path.read_bytes())
    except FileNotFoundError:
        return None


def config_differences(stored: RunConfig, current: RunConfig) -> list[str]:
    """Names of the fingerprint fields that differ: a run with differences cannot be resumed."""
    old, new = stored.fingerprint(), current.fingerprint()
    return sorted(name for name in new if old.get(name) != new[name])


# --- summary (P4-04 adds the metrics of eval plan §2) -----------------------------------------


def summary(results: Sequence[TicketResult], *, planned: int) -> dict[str, Any]:
    """Counts and sums of the finished tickets, as JSON-ready values."""
    cost = sum((result.cost_usd for result in results), Decimal(0))
    paid = sum((result.paid_cost_usd for result in results), Decimal(0))
    return {
        "tickets_planned": planned,
        "tickets_finished": len(results),
        "runs_failed": sum(result.run_failed for result in results),
        "routes": dict(sorted(Counter(result.route or "none" for result in results).items())),
        "route_reasons": dict(
            sorted(Counter(reason for r in results for reason in r.route_reasons).items())
        ),
        "errors": dict(sorted(Counter(r.error for r in results if r.error).items())),
        "llm_calls": sum(result.llm_calls for result in results),
        "cost_usd": str(cost),
        "paid_cost_usd": str(paid),
        "cost_per_ticket_usd": str(cost / len(results)) if results else None,
    }


def write_metrics(path: Path, data: dict[str, Any]) -> None:
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def render_report(config: RunConfig, data: dict[str, Any], *, run_name: str) -> str:
    """`report.md` of the run. Only the run summary until P4-04 and P4-05."""
    routes = ", ".join(f"{name} {count}" for name, count in data["routes"].items()) or "none"
    reasons = (
        ", ".join(f"{name} {count}" for name, count in data["route_reasons"].items()) or "none"
    )
    errors = ", ".join(f"{name} {count}" for name, count in data["errors"].items()) or "none"
    lines = [
        f"# Evaluation run {run_name}",
        "",
        "Synthetic data. Demo check, not a proof (eval plan §2).",
        "",
        "| Setting | Value |",
        "|---|---|",
        f"| Variant | `{config.variant}` |",
        f"| LLM mode | `{config.llm_mode}` |",
        f"| Graph version | `{config.graph_version}` |",
        f"| Prompt versions | {_pairs(config.prompt_versions)} |",
        f"| Models | {_pairs({k: ' / '.join(v) for k, v in config.models.items()})} |",
        f"| Git | `{config.git_sha}`{' (uncommitted changes)' if config.git_dirty else ''} |",
        f"| Anchor date | {config.core_anchor_date or 'not set (real date)'} |",
        f"| Tickets | `{config.tickets_file}` |",
        "",
        "## Run summary",
        "",
        "| | |",
        "|---|---|",
        f"| Tickets finished | {data['tickets_finished']} of {data['tickets_planned']} |",
        f"| Failed runs | {data['runs_failed']} |",
        f"| Routes | {routes} |",
        f"| Route reasons | {reasons} |",
        f"| Run errors | {errors} |",
        f"| Model calls | {data['llm_calls']} |",
        f"| Cost of the answers (USD) | {data['cost_usd']} |",
        f"| Paid in this run (USD) | {data['paid_cost_usd']} |",
        "",
        "The metrics of eval plan §2 come with P4-04, the judge metrics with P4-05.",
        "",
    ]
    if config.llm_mode == "fake":
        lines[4:4] = [
            "**Fake models:** answers are recorded responses; a ticket without a recording is "
            "handed off (`system_error`). These numbers check the runner, not the answers.",
            "",
        ]
    return "\n".join(lines)


def _pairs(values: dict[str, str]) -> str:
    return ", ".join(f"{key} `{value}`" for key, value in sorted(values.items())) or "none"
