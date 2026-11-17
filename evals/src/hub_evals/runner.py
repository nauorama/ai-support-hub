"""The evaluation runner (eval plan §4, P4-03): `make eval` → `python -m hub_evals.runner`.

Each evaluation ticket becomes a ticket of a seed customer in the evaluation database
(`EVAL_DATABASE_URL`, never the demo database) and goes through **the same `run_ticket`** as a
customer message to `POST /tickets` (`hub.graph.linear`; the LangGraph workflow in phase 6). The
pipeline differs only in what an evaluation needs: `llm_calls.context = eval`, the variant, the
run's own cost limit in place of the daily budget, and the disk cache.

- **Concurrency** 5 tickets at a time. Retries on provider errors are the gateway's (2 retries,
  then the fallback model).
- **Resume.** Results are written per ticket (`hub_evals/results.py`); a second start with the
  same run directory runs only the tickets not in `results.jsonl`. The run directory is
  `evals/runs/<date>-<variant>-<git sha>/`, so a re-run on the same day and commit continues the
  same run. `--restart` starts it again from the first ticket.
- **Disk cache** of model answers, on by default (`--no-cache` turns it off): `CachingProvider`
  in the gateway, under `evals/.cache/llm/<LLM_MODE>/`. A cache hit makes no provider call and
  is recorded with mode `fake` (nothing paid).
- **Cost.** Before the first call the runner prints an estimate (input tokens ≈ characters / 4,
  output tokens = the role's `max_tokens`, cached calls free) and stops when it is above
  `EVAL_MAX_COST_USD` in real mode. While the run goes, each ticket checks the money already paid
  in this run before its first model call, and every judge call checks it again; once the limit
  is reached the remaining tickets are not run or judged (and not written, so a resume with a
  higher limit runs them). The limit is soft by up to the answer calls of the tickets already in
  flight and one judge call each. Money paid for a ticket that is not written counts in this
  run only: a resume starts from the written results, and the disk cache serves the paid
  answers again for free (with `--no-cache` they are paid again). In fake mode nothing is paid
  and the limit does not apply.
- **Judges** (P4-05, on by default; `--no-judges` turns them off): after a ticket's run, its
  draft goes to the LLM judges (`hub_evals.judges`): key facts, faithfulness against the cited
  documents, and one citation call per marked sentence. Judge calls use the same gateway, cache
  and cost limit; they are recorded with node `judge` and no `run_id`. A judge that fails for
  good is noted in the ticket's result; an outage is not written, so a resume judges again.
- **Metrics and report** (P4-04): `metrics.json` and `report.md` from `hub_evals.report`.

The output names counts only, never ticket text. The test set is never read (AGENTS.md rule 7).
"""

import argparse
import asyncio
import dataclasses
import functools
import hashlib
import logging
import subprocess
import sys
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ValidationError
from sqlalchemy import select
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError

from hub.audit import record
from hub.config import Settings, SettingsError, load_settings
from hub.db.models import AuditLog, Draft, LlmCall, Run
from hub.db.models import Ticket as TicketRow
from hub.db.repositories import CustomerRepository, MessageRepository, TicketRepository
from hub.db.session import Database, connect
from hub.domain.draft import DraftAnswer
from hub.domain.enums import (
    ActorType,
    LlmCallContext,
    LlmCallMode,
    MessageRole,
    Route,
    TicketStatus,
)
from hub.domain.triage import TriageResult
from hub.graph.linear import GRAPH_VERSION, VARIANT, VARIANTS, Pipeline, run_ticket
from hub.graph.nodes.intake import ChatTurn, intake
from hub.graph.nodes.triage import triage_messages
from hub.graph.nodes.write import write_messages
from hub.guardrails.masking import MaskResult, mask
from hub.kb.full_text import KbSource, load_external_sources, render_kb
from hub.kb.loader import load_kb, read_kb
from hub.llm.gateway import CachingProvider, Gateway, Provider, build_gateway
from hub.llm.prompts import active_prompt
from hub.llm.recorder import CallRecorder, DbCallRecorder
from hub.llm.schemas import (
    BudgetExceededError,
    LlmCallRecord,
    LlmError,
    LlmUnavailableError,
    Message,
    ProviderRequest,
    Usage,
)
from hub.seed import load_customers, read_seed
from hub_evals import judges as judging
from hub_evals import report as reporting
from hub_evals import results as files
from hub_evals.data_check import TEST_SET_FILE
from hub_evals.schemas import Ticket

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[3]
TICKETS_FILE = ROOT / "data" / "eval" / "tickets.dev.jsonl"
SEED_DIR = ROOT / "data" / "seed"
KB_DIR = ROOT / "data" / "kb"
RUNS_DIR = ROOT / "evals" / "runs"
CACHE_DIR = ROOT / "evals" / ".cache" / "llm"
CONCURRENCY = 5
# A ticket's first message in the evaluation database says where it came from (audit only).
SOURCE = "eval"
BUDGET_REASON = "budget_exceeded"
# Run errors that are not written as results, so a resume runs the ticket again: the model and
# its fallback failed (outage, rate limit). A missing recording in fake mode is deterministic and
# stays a result.
RETRY_ERRORS = frozenset({"LlmUnavailableError"})
TRIAGE_FLAGS = ("wants_human", "reports_unauthorised_activity", "is_complaint_or_legal")
# Characters per token for the estimate: a rough rule for English text.
CHARS_PER_TOKEN = 4
# Output tokens of a role without `max_tokens` in the estimate.
DEFAULT_OUTPUT_TOKENS = 1000
# Citation judge calls per ticket in the estimate: the number of markers is known only after
# the answer. A run with more is still held by the running cost limit.
ESTIMATED_CITATIONS = 3

EXIT_OK = 0
EXIT_SETUP = 1
EXIT_ESTIMATE = 2
EXIT_COST_LIMIT = 3


class EvalSetupError(Exception):
    """The run cannot start (settings, database, files). The message names no ticket text."""


# --- cost limit ---------------------------------------------------------------------------------


@dataclass
class CostLimit:
    """The budget check of an evaluation pipeline (`Pipeline.budget`): refuses a ticket's first
    model call once the money paid in this run (resumed tickets included) reaches the limit.
    `enforced` is false in fake mode, where nothing is paid."""

    limit: Decimal
    enforced: bool
    spent: Decimal = Decimal(0)

    async def check(self) -> None:
        if self.enforced and self.spent >= self.limit:
            raise BudgetExceededError(self.spent, self.limit)

    def add(self, amount: Decimal) -> None:
        self.spent += amount

    @property
    def reached(self) -> bool:
        return self.enforced and self.spent >= self.limit


# --- pure helpers -------------------------------------------------------------------------------


def run_name(day: str, variant: str, sha: str, *, dirty: bool, llm_mode: str = "real") -> str:
    """`<date>-<variant>-<git sha>` (eval plan §4); `-dirty` with uncommitted changes, `-fake` for
    a fake-mode check, so a real run never meets the results of a fake one."""
    return (
        f"{day}-{variant}-{sha}{'-dirty' if dirty else ''}{'-fake' if llm_mode == 'fake' else ''}"
    )


def load_tickets(path: Path, limit: int | None = None) -> list[Ticket]:
    """The first `limit` tickets of the file, validated. Never the test set."""
    if path.name == TEST_SET_FILE:
        raise EvalSetupError(f"{TEST_SET_FILE} is not read by the runner (AGENTS.md rule 7)")
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        raise EvalSetupError(f"ticket file {path.name} not found") from None
    tickets: list[Ticket] = []
    for number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            tickets.append(Ticket.model_validate_json(line))
        except ValidationError:
            raise EvalSetupError(
                f"{path.name} line {number} is invalid; run `make data-check`"
            ) from None
    return tickets[:limit] if limit is not None else tickets


def pending(tickets: Sequence[Ticket], done: Sequence[files.TicketResult]) -> list[Ticket]:
    finished = {result.ticket_id for result in done}
    return [ticket for ticket in tickets if ticket.id not in finished]


def masked_counts(masked: MaskResult) -> dict[str, int]:
    """`messages.masked_entities`: counts by type, `{"failed": 1}` after a failed mask."""
    if masked.failed:
        return {"failed": 1}
    return {str(kind): count for kind, count in masked.counts.items()}


def history_turns(ticket: Ticket) -> list[ChatTurn]:
    """The earlier messages as the workflow will read them back from the database (masked)."""
    return [ChatTurn(role=item.role, text=mask(item.text).text) for item in ticket.history]


def estimate_tokens(request: ProviderRequest) -> int:
    seen = request.model_dump_json(include={"messages", "json_schema", "tools"})
    return len(seen) // CHARS_PER_TOKEN + 1


@dataclass(frozen=True)
class Estimate:
    calls: int
    cached_calls: int
    cost_usd: Decimal


def judge_requests(
    gateway: Gateway,
    judge_set: judging.Judges,
    ticket: Ticket,
    sources: Sequence[KbSource],
    draft_chars: int,
) -> list[ProviderRequest]:
    """The judge requests of one ticket at their largest, for the estimate: a draft of the
    write role's full `max_tokens`, all external documents as evidence, the longest document
    as the cited source, and `ESTIMATED_CITATIONS` citations."""
    draft = "x" * draft_chars
    longest = max((render_kb([source]) for source in sources), key=len, default="")
    calls: list[tuple[list[Message], type[BaseModel]]] = [
        (
            judging.faithfulness_messages(
                judge_set.faithfulness, draft=draft, evidence=render_kb(sources)
            ),
            judging.FaithfulnessVerdict,
        )
    ]
    calls += [
        (
            judging.citation_messages(judge_set.citation, sentence=draft[:400], source=longest),
            judging.CitationVerdict,
        )
    ] * ESTIMATED_CITATIONS
    if ticket.key_facts:
        calls.append(
            (
                judging.key_facts_messages(
                    judge_set.key_facts, draft=draft, key_facts=ticket.key_facts
                ),
                judging.KeyFactsVerdict,
            )
        )
    role = judge_set.key_facts.front.role
    return [gateway.chat_requests(role, messages, schema)[0] for messages, schema in calls]


def estimate_cost(
    pipeline: Pipeline,
    tickets: Sequence[Ticket],
    kb: str,
    cache: CachingProvider | None,
    judge_set: judging.Judges | None = None,
    sources: Sequence[KbSource] = (),
) -> Estimate:
    """An upper bound of what the pending tickets cost with real models: the triage and the write
    request of each ticket (the requests `run_ticket` sends, built by the same functions), each
    priced with its role's model at the estimated input tokens and the full `max_tokens`. A call
    whose answer is in the cache costs nothing. Repair calls and the fallback are not counted.
    With judges, each ticket adds its judge requests at their largest (`judge_requests`); they
    depend on the answer, so they are never counted as cached."""
    gateway: Gateway = pipeline.gateway
    calls = cached = 0
    total = Decimal(0)
    draft_chars = (
        gateway.models.role(pipeline.write_prompt.front.role).max_tokens or DEFAULT_OUTPUT_TOKENS
    ) * CHARS_PER_TOKEN
    for ticket in tickets:
        result = intake(mask(ticket.message).text, history_turns(ticket))
        requests = [
            gateway.chat_requests(
                pipeline.triage_prompt.front.role,
                triage_messages(pipeline.triage_prompt, result.message_for_llm),
                TriageResult,
            )[0],
            gateway.chat_requests(
                pipeline.write_prompt.front.role,
                write_messages(
                    pipeline.write_prompt,
                    kb=kb,
                    ticket=result.message_for_llm,
                    history=result.history_for_llm,
                ),
                DraftAnswer,
            )[0],
        ]
        judged = (
            judge_requests(gateway, judge_set, ticket, sources, draft_chars)
            if judge_set is not None
            else []
        )
        for request in requests:
            calls += 1
            if cache is not None and cache.contains(request):
                cached += 1
                continue
            total += _price(gateway, request)
        for request in judged:
            calls += 1
            total += _price(gateway, request)
    return Estimate(calls=calls, cached_calls=cached, cost_usd=total)


def _price(gateway: Gateway, request: ProviderRequest) -> Decimal:
    usage = Usage(
        input_tokens=estimate_tokens(request),
        output_tokens=request.max_tokens or DEFAULT_OUTPUT_TOKENS,
    )
    return gateway.pricing.cost(request.model, usage)


def git_state(root: Path = ROOT) -> tuple[str, str | None]:
    """Short commit id and the SHA-256 of the uncommitted changes of tracked files (`git diff
    HEAD`), None for a clean tree: two dirty states never pass for one. `nogit` without git."""
    try:
        sha = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],  # noqa: S607 - git from PATH
            cwd=root,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        diff = subprocess.run(
            ["git", "diff", "HEAD", "--binary"],  # noqa: S607
            cwd=root,
            capture_output=True,
            check=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError):
        return "nogit", None
    return sha, hashlib.sha256(diff).hexdigest() if diff.strip() else None


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def files_sha256(paths: Sequence[Path], root: Path) -> str:
    """One digest of several files, names included (a renamed file is a change)."""
    digest = hashlib.sha256()
    for path in sorted(paths):
        digest.update(str(path.relative_to(root)).encode("utf-8") + b"\0")
        digest.update(path.read_bytes() + b"\0")
    return digest.hexdigest()


def same_database(first: str, second: str) -> bool:
    """Same server and database name, whatever the login or the query string."""
    a, b = make_url(first), make_url(second)
    return (a.host, a.port or 5432, a.database) == (b.host, b.port or 5432, b.database)


# --- database -----------------------------------------------------------------------------------


def eval_database_url(settings: Settings) -> str:
    """EVAL_DATABASE_URL; refused when empty or the same as the demo database (eval plan §1)."""
    url = settings.eval_database_url.get_secret_value()
    if not url:
        raise EvalSetupError("EVAL_DATABASE_URL is empty")
    if same_database(url, settings.database_url.get_secret_value()):
        raise EvalSetupError("EVAL_DATABASE_URL is the demo database; use its own database")
    return url


async def prepare_database(database: Database, seed_dir: Path, kb_dir: Path) -> None:
    """Seed customers and the knowledge base, as the app role. Idempotent: unchanged rows are
    not written again. Fails with a hint when the database is not migrated."""
    seed = read_seed(seed_dir)
    kb_files = read_kb(kb_dir)
    try:
        async with database.sessions.begin() as session:
            await load_customers(session, seed)
            await load_kb(session, kb_files)
    except DBAPIError as exc:
        raise EvalSetupError(
            f"the evaluation database is not ready ({type(exc.orig).__name__}); "
            "run `make migrate` (it also migrates hub_eval)"
        ) from None


async def create_ticket(database: Database, ticket: Ticket) -> tuple[Any, Any, bool]:
    """The ticket, its history and the new customer message in the evaluation database, masked
    as the API masks them (G1). Returns the ticket id, the message id and whether masking of the
    new message failed (then the API hands the ticket off without a run, and so does this)."""
    async with database.sessions.begin() as session:
        customer = await CustomerRepository(session).get_by_external_ref(ticket.customer_ref)
        if customer is None:
            raise EvalSetupError(f"seed customer {ticket.customer_ref} is missing")
        row = await TicketRepository(session).create(
            customer_id=customer.id, ai_disclosed_at=datetime.now(UTC)
        )
        messages = MessageRepository(session)
        for item in ticket.history:
            masked_item = mask(item.text)
            await messages.add(
                ticket_id=row.id,
                role=MessageRole(item.role),
                body=masked_item.text,
                is_ai_generated=item.role == MessageRole.ASSISTANT.value,
                masked_entities=masked_counts(masked_item),
            )
        masked = mask(ticket.message)
        message = await messages.add(
            ticket_id=row.id,
            role=MessageRole.CUSTOMER,
            body=masked.text,
            masked_entities=masked_counts(masked),
        )
        await record(
            session,
            actor_type=ActorType.CUSTOMER,
            actor_id=customer.id,
            action="ticket.created",
            entity_type="ticket",
            entity_id=row.id,
            details={"message_id": message.id, "source": SOURCE, "eval_ticket_id": ticket.id},
        )
        if masked.failed:
            row.last_route = Route.HANDOFF
            row.route_reasons = ["system_error"]
            await TicketRepository(session).change_status(row, TicketStatus.HANDED_OFF)
        return row.id, message.id, masked.failed


async def triage_flags(
    session: Any,  # noqa: ANN401 - AsyncSession
    ticket_id: Any,  # noqa: ANN401 - uuid.UUID
    run_id: Any,  # noqa: ANN401
) -> dict[str, bool]:
    """The flags of the run's `triage.done` audit record (the ticket row has no column for
    them); empty when the triage did not finish."""
    if run_id is None:
        return {}
    rows = await session.scalars(
        select(AuditLog)
        .where(
            AuditLog.action == "triage.done",
            AuditLog.entity_type == "ticket",
            AuditLog.entity_id == str(ticket_id),
        )
        .order_by(AuditLog.id)
    )
    for row in rows:
        if row.details.get("run_id") == str(run_id):
            return {name: bool(row.details.get(name)) for name in TRIAGE_FLAGS}
    return {}


async def collect_result(
    database: Database,
    ticket: Ticket,
    ticket_id: Any,  # noqa: ANN401 - uuid.UUID from the database
    run_id: Any,  # noqa: ANN401
    *,
    failed: bool,
    latency_ms: int,
    events: list[str],
) -> files.TicketResult:
    async with database.sessions() as session:
        row = await session.get(TicketRow, ticket_id)
        if row is None:  # pragma: no cover - created above, never deleted
            raise LookupError("ticket")
        run = await session.get(Run, run_id) if run_id is not None else None
        draft = (
            await session.scalar(select(Draft).where(Draft.run_id == run_id))
            if run_id is not None
            else None
        )
        calls = (
            (await session.scalars(select(LlmCall).where(LlmCall.run_id == run_id))).all()
            if run_id is not None
            else []
        )
        flags = await triage_flags(session, ticket_id, run_id)
    return files.TicketResult(
        ticket_id=ticket.id,
        run_id=str(run_id) if run_id is not None else None,
        ticket_status=row.status.value,
        run_status=run.status.value if run is not None else None,
        run_failed=failed,
        route=(run.route.value if run is not None and run.route is not None else None)
        or (row.last_route.value if row.last_route is not None else None),
        route_reasons=[str(reason) for reason in (run.route_reasons if run else row.route_reasons)],
        error=run.error if run is not None else None,
        triage=files.Triage(
            intent=row.intent.value if row.intent is not None else None,
            secondary_intent=row.secondary_intent.value if row.secondary_intent else None,
            urgency=row.urgency.value if row.urgency is not None else None,
            summary=row.summary,
            **flags,
        ),
        draft=files.DraftOut(
            can_answer=draft.can_answer,
            body=draft.body,
            missing_information=draft.missing_information,
            citations=list(draft.citations),
            evidence=dict(draft.evidence),
            proposed_actions=list(draft.proposed_actions),
        )
        if draft is not None
        else None,
        models={key: str(value) for key, value in (run.models if run else {}).items()},
        prompt_versions={
            key: str(value) for key, value in (run.prompt_versions if run else {}).items()
        },
        cost_usd=sum((call.cost_usd for call in calls), Decimal(0)),
        paid_cost_usd=sum(
            (call.cost_usd for call in calls if call.mode == LlmCallMode.REAL), Decimal(0)
        ),
        input_tokens=sum(call.input_tokens for call in calls),
        output_tokens=sum(call.output_tokens for call in calls),
        llm_calls=len(calls),
        latency_ms=latency_ms,
        events=events,
    )


async def run_eval_ticket(pipeline: Pipeline, ticket: Ticket) -> files.TicketResult:
    """One evaluation ticket through the production `run_ticket`."""
    ticket_id, message_id, mask_failed = await create_ticket(pipeline.database, ticket)
    events: list[str] = []

    async def emit(name: str, data: dict[str, Any]) -> None:
        events.append(name)

    start = time.perf_counter()
    if mask_failed:
        return await collect_result(
            pipeline.database, ticket, ticket_id, None, failed=True, latency_ms=0, events=events
        )
    outcome = await run_ticket(pipeline, ticket_id=ticket_id, message_id=message_id, emit=emit)
    latency_ms = round((time.perf_counter() - start) * 1000)
    return await collect_result(
        pipeline.database,
        ticket,
        ticket_id,
        outcome.run_id,
        failed=outcome.failed,
        latency_ms=latency_ms,
        events=events,
    )


# --- judges -------------------------------------------------------------------------------------


@dataclass
class CallTally:
    """Wraps the recorder of one ticket's judge gateway: counts its `llm_calls` rows, their
    cost and what was paid (rows with mode `real`; a cache hit is `fake`). Each paid call goes
    into the run's cost limit as it is recorded, so a judge that fails later (an outage, the
    limit) loses none of the money already paid."""

    inner: CallRecorder
    limit: CostLimit | None = None
    calls: int = 0
    cost_usd: Decimal = Decimal(0)
    paid_cost_usd: Decimal = Decimal(0)

    def _add(self, call: LlmCallRecord) -> None:
        self.calls += 1
        self.cost_usd += call.cost_usd
        if call.mode == LlmCallMode.REAL:
            self.paid_cost_usd += call.cost_usd
            if self.limit is not None:
                self.limit.add(call.cost_usd)

    async def record(self, call: LlmCallRecord) -> None:
        await self.inner.record(call)
        self._add(call)

    async def record_blocked(self, call: LlmCallRecord, entities: dict[str, int]) -> None:
        await self.inner.record_blocked(call, entities)
        self._add(call)


def cited_sources(draft: files.DraftOut, sources: Mapping[str, KbSource]) -> dict[int, KbSource]:
    """marker -> the source its citation names (unknown ids were dropped by the run)."""
    cited: dict[int, KbSource] = {}
    for citation in draft.citations:
        source = sources.get(str(citation.get("source_id")))
        if source is not None and isinstance(citation.get("marker"), int):
            cited.setdefault(citation["marker"], source)
    return cited


async def judge_ticket(
    gateway: Gateway,
    judge_set: judging.Judges,
    ticket: Ticket,
    draft: files.DraftOut,
    sources: Mapping[str, KbSource],
    limit: CostLimit | None = None,
) -> files.Judgements:
    """The judges of one draft (eval plan §5). Key facts are judged on every draft (an empty
    one states none, without a call); faithfulness and citations only on an answer. The
    evidence of faithfulness is the cited documents, as the writer cited them. An outage
    (`LlmUnavailableError`) is raised: the result is not written and a resume judges again.
    Any other judge error is noted and leaves the ticket out of that metric.

    `limit`: the run's cost limit. It is checked before every judge call (`BudgetExceededError`
    is raised, like an outage) and gets each paid call as it is recorded."""
    tally = CallTally(gateway.recorder, limit)
    judge_gateway = dataclasses.replace(gateway, recorder=tally)
    errors: dict[str, str] = {}
    body = draft.body if draft.can_answer else ""
    cited = cited_sources(draft, sources)

    async def attempt[T](name: str, job: Callable[[], Awaitable[T]]) -> T | None:
        if limit is not None:
            await limit.check()
        try:
            return await job()
        except LlmUnavailableError:
            raise
        except LlmError as exc:
            errors.setdefault(name, type(exc).__name__)
            return None

    key_facts = (
        await attempt(
            "key_facts",
            lambda: judging.judge_key_facts(
                judge_gateway, judge_set.key_facts, draft=body, key_facts=ticket.key_facts
            ),
        )
        if ticket.key_facts
        else None
    )
    faithfulness = None
    citations: list[judging.CitationResult] = []
    if body.strip():
        evidence = render_kb(
            sorted({source.id: source for source in cited.values()}.values(), key=lambda s: s.id)
        )
        faithfulness = await attempt(
            "faithfulness",
            lambda: judging.judge_faithfulness(
                judge_gateway, judge_set.faithfulness, draft=body, evidence=evidence
            ),
        )
        for marker, sentence in judging.marked_sentences(body):
            source = cited.get(marker)
            if source is None:
                continue  # a marker without a citation: found by the code check
            verdict = await attempt(
                "citation",
                functools.partial(
                    judging.judge_citation,
                    judge_gateway,
                    judge_set.citation,
                    marker=marker,
                    source_id=source.id,
                    sentence=sentence,
                    source=render_kb([source]),
                ),
            )
            if verdict is not None:
                citations.append(verdict)
    return files.Judgements(
        key_facts=key_facts,
        faithfulness=faithfulness,
        citations=citations,
        errors=errors,
        llm_calls=tally.calls,
        cost_usd=tally.cost_usd,
        paid_cost_usd=tally.paid_cost_usd,
    )


# --- the run ------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Options:
    tickets: Path = TICKETS_FILE
    variant: str = VARIANT
    limit: int | None = None
    cache: bool = True
    restart: bool = False
    judges: bool = True
    run_dir: Path | None = None
    concurrency: int = CONCURRENCY
    runs_dir: Path = RUNS_DIR
    cache_dir: Path = CACHE_DIR
    seed_dir: Path = SEED_DIR
    kb_dir: Path = KB_DIR


@dataclass
class Report:
    """What `run` did, for the command line and the tests."""

    run_dir: Path
    planned: int
    skipped: int
    ran: int
    stopped_at_cost_limit: bool = False
    stopped_by_estimate: bool = False
    unavailable: int = 0
    estimate: Estimate | None = None
    cache_hits: int = 0
    cache_misses: int = 0
    # Paid in this run directory: written results (resumed ones included) and the calls of
    # this run, also those of tickets that were not written.
    spent_usd: Decimal = Decimal(0)
    lines: list[str] = field(default_factory=list)


def build_pipeline(
    settings: Settings,
    database: Database,
    *,
    variant: str,
    cache_dir: Path | None,
    provider: Provider | None = None,
) -> tuple[Pipeline, CostLimit, CachingProvider | None]:
    """The evaluation pipeline, its cost limit and its cache (None with `--no-cache`).
    `provider`: tests pass a scripted provider in place of the one of `LLM_MODE`."""
    gateway = build_gateway(settings, DbCallRecorder(database), None, cache_dir=cache_dir)
    if provider is not None:
        if cache_dir is not None:
            provider = CachingProvider(provider, cache_dir / settings.llm_mode)
        gateway = dataclasses.replace(gateway, provider=provider)
    cache = gateway.provider if isinstance(gateway.provider, CachingProvider) else None
    limit = CostLimit(
        limit=Decimal(str(settings.eval_max_cost_usd)), enforced=settings.llm_mode == "real"
    )
    pipeline = Pipeline(
        database=database,
        gateway=gateway,
        budget=limit,
        triage_prompt=active_prompt(settings.prompts_dir, gateway.models, "triage"),
        write_prompt=active_prompt(settings.prompts_dir, gateway.models, "write"),
        context=LlmCallContext.EVAL,
        variant=variant,
    )
    return pipeline, limit, cache


def run_config(
    settings: Settings,
    pipeline: Pipeline,
    options: Options,
    judge_set: judging.Judges | None = None,
) -> files.RunConfig:
    sha, diff = git_state()
    models = pipeline.gateway.models
    prompts = [pipeline.triage_prompt, pipeline.write_prompt]
    if judge_set is not None:
        prompts += judge_set.prompts
    prompt_files = [
        settings.prompts_dir / prompt.node / f"{prompt.front.version}.md" for prompt in prompts
    ]
    nodes = [("triage", pipeline.triage_prompt), ("write", pipeline.write_prompt)]
    if judge_set is not None:
        nodes.append(("judge", judge_set.key_facts))
    return files.RunConfig(
        variant=pipeline.variant,
        llm_mode=settings.llm_mode,
        graph_version=GRAPH_VERSION,
        models={
            node: [choice.model for choice in models.role(prompt.front.role).choices()]
            for node, prompt in nodes
        },
        prompt_versions=pipeline.prompt_versions,
        judge_prompt_versions=judge_set.prompt_versions if judge_set is not None else {},
        git_sha=sha,
        git_dirty=diff is not None,
        git_diff_sha256=diff,
        prompts_sha256=files_sha256(prompt_files, settings.prompts_dir),
        kb_sha256=files_sha256(
            [p for p in options.kb_dir.rglob("*") if p.is_file()], options.kb_dir
        ),
        core_anchor_date=settings.core_anchor_date,
        tickets_file=_relative(options.tickets),
        tickets_sha256=file_sha256(options.tickets),
        limit=options.limit,
        cache=options.cache,
        concurrency=options.concurrency,
        max_cost_usd=Decimal(str(settings.eval_max_cost_usd)),
    )


def _relative(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(ROOT))
    except ValueError:
        return path.name


async def run(settings: Settings, options: Options, *, provider: Provider | None = None) -> Report:
    """The whole run. `provider`: tests pass a scripted provider (the cache still wraps it unless
    `options.cache` is false)."""
    if options.variant not in VARIANTS:
        raise EvalSetupError(f"unknown variant {options.variant!r}; known: {', '.join(VARIANTS)}")
    if options.concurrency < 1:
        raise EvalSetupError("concurrency must be at least 1")
    tickets = load_tickets(options.tickets, options.limit)
    database = connect(eval_database_url(settings))
    try:
        return await _run(settings, options, tickets, database, provider)
    finally:
        await database.dispose()


async def _run(
    settings: Settings,
    options: Options,
    tickets: list[Ticket],
    database: Database,
    provider: Provider | None,
) -> Report:
    pipeline, limit, cache = build_pipeline(
        settings,
        database,
        variant=options.variant,
        cache_dir=options.cache_dir if options.cache else None,
        provider=provider,
    )
    judge_set = (
        judging.load_judges(settings.prompts_dir, pipeline.gateway.models)
        if options.judges
        else None
    )
    config = run_config(settings, pipeline, options, judge_set)
    day = datetime.now(UTC).date().isoformat()
    run_dir = options.run_dir or options.runs_dir / run_name(
        day, config.variant, config.git_sha, dirty=config.git_dirty, llm_mode=config.llm_mode
    )
    run_dir.mkdir(parents=True, exist_ok=True)
    results_path = run_dir / files.RESULTS_FILE
    config_path = run_dir / files.CONFIG_FILE

    stored = files.read_config(config_path)
    if options.restart and results_path.exists():
        # Kept, not deleted: a restart by mistake must not lose paid results.
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
        results_path.replace(run_dir / f"results.before-{stamp}.jsonl")
    elif stored is not None:
        differences = files.config_differences(stored, config)
        if differences:
            raise EvalSetupError(
                f"{run_dir.name} was run with other settings ({', '.join(differences)}); "
                "use --restart or another --run-dir"
            )
    done = files.read_results(results_path)
    files.rewrite_results(results_path, done)
    files.write_config(config_path, config)
    for result in done:
        limit.add(result.paid_total_usd)

    todo = pending(tickets, done)
    report = Report(run_dir=run_dir, planned=len(tickets), skipped=len(tickets) - len(todo), ran=0)

    await prepare_database(database, options.seed_dir, options.kb_dir)
    async with database.sessions() as session:
        sources = await load_external_sources(session)
    if not sources:
        raise EvalSetupError("no external KB document in the evaluation database")
    report.estimate = estimate_cost(pipeline, todo, render_kb(sources), cache, judge_set, sources)
    by_id = {source.id: source for source in sources}
    if limit.enforced and limit.spent + report.estimate.cost_usd > limit.limit:
        report.stopped_by_estimate = True
        report.spent_usd = limit.spent
        _finish(run_dir, config, tickets, results_path, report, cache)
        return report

    semaphore = asyncio.Semaphore(options.concurrency)

    async def one(ticket: Ticket) -> None:
        async with semaphore:
            if limit.reached:
                report.stopped_at_cost_limit = True
                return
            result = await run_eval_ticket(pipeline, ticket)
            limit.add(result.paid_cost_usd)
            if BUDGET_REASON in result.route_reasons:
                # Not a result: the ticket never ran. A resume with a higher limit runs it.
                report.stopped_at_cost_limit = True
                return
            if result.error in RETRY_ERRORS:
                # The provider was down: says nothing about the workflow. A resume runs it again
                # (cheap: answers that came are in the cache).
                report.unavailable += 1
                return
            if judge_set is not None and result.draft is not None:
                if limit.reached:
                    # Not judged, so not written: a resume with a higher limit judges it (its
                    # answer comes from the cache).
                    report.stopped_at_cost_limit = True
                    return
                # The judges' paid calls go into the limit as they are recorded, also when a
                # later judge call fails (`CallTally`).
                try:
                    judgements = await judge_ticket(
                        pipeline.gateway, judge_set, ticket, result.draft, by_id, limit
                    )
                except LlmUnavailableError:
                    report.unavailable += 1
                    return
                except BudgetExceededError:
                    report.stopped_at_cost_limit = True
                    return
                result = result.model_copy(update={"judges": judgements})
            files.append_result(results_path, result)
            report.ran += 1

    await asyncio.gather(*(one(ticket) for ticket in todo))
    report.spent_usd = limit.spent
    _finish(run_dir, config, tickets, results_path, report, cache)
    return report


def _finish(
    run_dir: Path,
    config: files.RunConfig,
    tickets: Sequence[Ticket],
    results_path: Path,
    report: Report,
    cache: CachingProvider | None,
) -> None:
    order = {ticket.id: index for index, ticket in enumerate(tickets)}
    finished = sorted(
        (r for r in files.read_results(results_path) if r.ticket_id in order),
        key=lambda r: order[r.ticket_id],
    )
    data = reporting.evaluate(tickets, finished, planned=len(tickets))
    data["cache"] = {
        "enabled": cache is not None,
        "hits": cache.hits if cache is not None else 0,
        "misses": cache.misses if cache is not None else 0,
    }
    data["stopped_at_cost_limit"] = report.stopped_at_cost_limit
    data["stopped_by_estimate"] = report.stopped_by_estimate
    files.write_metrics(run_dir / files.METRICS_FILE, data)
    (run_dir / files.REPORT_FILE).write_text(
        reporting.render_report(config, data, run_name=run_dir.name), encoding="utf-8"
    )
    if cache is not None:
        report.cache_hits, report.cache_misses = cache.hits, cache.misses


# --- command line -------------------------------------------------------------------------------


def parse_args(argv: Sequence[str]) -> Options:
    parser = argparse.ArgumentParser(prog="hub_evals.runner", description=__doc__.splitlines()[0])
    parser.add_argument("--tickets", type=Path, default=TICKETS_FILE)
    parser.add_argument("--variant", default=VARIANT, choices=VARIANTS)
    parser.add_argument("--limit", type=int, default=None, help="only the first N tickets")
    parser.add_argument("--no-cache", action="store_true", help="turn the disk cache off")
    parser.add_argument("--restart", action="store_true", help="run every ticket again")
    parser.add_argument("--run-dir", type=Path, default=None, help="resume this run directory")
    parser.add_argument("--no-judges", action="store_true", help="skip the LLM judges")
    parser.add_argument("--concurrency", type=int, default=CONCURRENCY)
    args = parser.parse_args(list(argv))
    if args.limit is not None and args.limit < 1:
        parser.error("--limit must be at least 1")
    return Options(
        tickets=args.tickets,
        variant=args.variant,
        limit=args.limit,
        cache=not args.no_cache,
        restart=args.restart,
        judges=not args.no_judges,
        run_dir=args.run_dir,
        concurrency=args.concurrency,
    )


def describe(report: Report, settings: Settings) -> list[str]:
    lines = [f"eval: run directory {_relative(report.run_dir)}"]
    if report.estimate is not None:
        estimate = report.estimate
        paid = "fake mode, nothing is paid" if settings.llm_mode == "fake" else "real mode"
        lines.append(
            f"eval: estimate ${estimate.cost_usd} for {estimate.calls} calls "
            f"({estimate.cached_calls} cached; {paid}); limit ${settings.eval_max_cost_usd}"
        )
    lines.append(
        f"eval: {report.planned} tickets, {report.skipped} already finished, {report.ran} run now"
    )
    lines.append(f"eval: cache hits {report.cache_hits}, misses {report.cache_misses}")
    if report.unavailable:
        lines.append(
            f"eval: {report.unavailable} tickets failed because the model was unavailable; "
            "they are not written, run again to retry them"
        )
    if report.stopped_by_estimate:
        lines.append("eval: the estimate is above EVAL_MAX_COST_USD; nothing was run")
    if report.stopped_at_cost_limit:
        lines.append("eval: stopped at EVAL_MAX_COST_USD; raise it and run again to continue")
    return lines


def main(argv: Sequence[str]) -> int:
    logging.basicConfig(level=logging.WARNING)
    options = parse_args(argv)
    try:
        settings = load_settings()
        report = asyncio.run(run(settings, options))
    except (EvalSetupError, SettingsError) as error:
        print(f"eval: {error}", file=sys.stderr)
        return EXIT_SETUP
    for line in describe(report, settings):
        print(line)
    if report.stopped_by_estimate:
        return EXIT_ESTIMATE
    if report.stopped_at_cost_limit:
        return EXIT_COST_LIMIT
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
