"""The files of one evaluation run (eval plan §4): `config.json`, `results.jsonl`, `metrics.json`
and `report.md` in `evals/runs/<date>-<variant>-<git sha>/`.

`results.jsonl` gets one line per finished ticket, written as soon as the ticket is done, so an
interrupted run continues where it stopped: the tickets already in the file are not run again.
A line cut off by the interruption is dropped on resume and that ticket runs again.

Only `report.md` and `metrics.json` are committed; `results.jsonl` holds the drafts and stays
local (git-ignored). The metrics of eval plan §2 and the report are built from these files in
`hub_evals.report`.
"""

import json
from collections.abc import Iterable
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from hub_evals.judges import CitationResult, FaithfulnessResult, KeyFactsResult

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
    # Judge prompt versions; empty when the run has no judges (`--no-judges`). Judge results of
    # another prompt or none at all must not mix with these in one report.
    judge_prompt_versions: dict[str, str] = Field(default_factory=dict)
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
    # The flags of the `triage.done` audit record (the ticket row has no column for them); None
    # when the triage did not finish.
    wants_human: bool | None = None
    reports_unauthorised_activity: bool | None = None
    is_complaint_or_legal: bool | None = None

    def flags(self) -> dict[str, bool | None] | None:
        if self.wants_human is None:
            return None
        return {
            "wants_human": self.wants_human,
            "reports_unauthorised_activity": self.reports_unauthorised_activity,
            "is_complaint_or_legal": self.is_complaint_or_legal,
        }


class DraftOut(_Frozen):
    can_answer: bool
    body: str
    missing_information: str | None
    citations: list[dict[str, Any]]
    evidence: dict[str, Any]
    proposed_actions: list[dict[str, Any]]
    # From the verify step (P5-04, RAG variants); B0 results and older files have none.
    groundedness: Decimal | None = None
    unsupported_claims: list[dict[str, Any]] = Field(default_factory=list)

    @property
    def ranked_chunk_ids(self) -> list[str] | None:
        """The chunk ids of the first search in rank order (`evidence.chunks`, P5-03); None
        when the run made no search (B0)."""
        chunks = self.evidence.get("chunks")
        if not isinstance(chunks, list):
            return None
        return [str(chunk["chunk_id"]) for chunk in chunks]


class Judgements(_Frozen):
    """What the LLM judges said about one draft (P4-05; unvalidated, same model family).
    `key_facts` is None when the ticket has no key facts; `faithfulness` is None when there is
    no answer to judge (`can_answer` false or an empty body). `errors`: judge name -> error
    class of a judge call that failed for good; its metric leaves the ticket out."""

    key_facts: KeyFactsResult | None = None
    faithfulness: FaithfulnessResult | None = None
    citations: list[CitationResult] = Field(default_factory=list)
    errors: dict[str, str] = Field(default_factory=dict)
    llm_calls: int = 0
    cost_usd: Decimal = Decimal(0)
    paid_cost_usd: Decimal = Decimal(0)


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
    # None: the run had no judges (`--no-judges`) or the ticket has no draft.
    judges: Judgements | None = None

    @property
    def paid_total_usd(self) -> Decimal:
        """Paid for the answer and its judges: what counts against the run's cost limit."""
        return self.paid_cost_usd + (self.judges.paid_cost_usd if self.judges else Decimal(0))


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


def write_metrics(path: Path, data: dict[str, Any]) -> None:
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
