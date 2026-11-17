"""The deterministic metrics of eval plan §2 (P4-04): pure functions, no I/O.

- **Counts with intervals.** Every rate is a `Rate`: count, total and the 95% Wilson interval
  (eval plan §1.7), because one ticket of 20 is 5 points.
- **Triage:** intent accuracy, confusion matrix, recall of the three flags.
- **Routing:** hard-handoff recall, escalation recall and precision, automation share, wrong
  auto-replies.
- **Retrieval** (from phase 5): hit@k and MRR of the first search.
- **Judge metrics** (key-fact coverage, faithfulness, citation validity) are computed here from
  the judge results, but they are only as good as the judge: reports mark them "unvalidated, same
  model family" (eval plan §5).
- **Cost and latency:** mean cost per ticket, percentiles of latency.

**Coverage.** A ticket that ran no knowledge-base search (row-1 handoffs skip research) is left
out of the retrieval scores and of key-fact coverage, even when it carries `gold_doc_ids` or
`key_facts` (P4-02 review); every score says how many tickets it covers. In baseline B0 there is
no search step: the writer reads the whole knowledge base, so a ticket "read the KB" when it has a
draft, and retrieval is not measured.
"""

import math
import re
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Any

# 95% two-sided normal quantile.
Z_95 = 1.959964
HIT_K = 5
# A key-fact coverage below this makes an auto-reply wrong (eval plan §2).
WRONG_COVERAGE = 0.8
FLAGS = ("wants_human", "reports_unauthorised_activity", "is_complaint_or_legal")
ESCALATED_ROUTES = frozenset({"approval", "handoff"})
AUTO_REPLY = "auto_reply"
HANDOFF = "handoff"
NO_PREDICTION = "none"


# --- counts and intervals -----------------------------------------------------------------------


def wilson(count: int, total: int, z: float = Z_95) -> tuple[float, float]:
    """The Wilson score interval of `count` successes in `total` trials; (0, 1) when total is 0.
    0 of 20 gives (0, 0.161): the "up to 16%" of eval plan §2."""
    if total < 0 or not 0 <= count <= total:
        raise ValueError("need 0 <= count <= total")
    if total == 0:
        return 0.0, 1.0
    p = count / total
    z2 = z * z
    centre = (p + z2 / (2 * total)) / (1 + z2 / total)
    half = z * math.sqrt(p * (1 - p) / total + z2 / (4 * total * total)) / (1 + z2 / total)
    return max(0.0, centre - half), min(1.0, centre + half)


@dataclass(frozen=True)
class Rate:
    """`count` of `total`, with its 95% Wilson interval. `value` is None for an empty total."""

    count: int
    total: int

    @property
    def value(self) -> float | None:
        return self.count / self.total if self.total else None

    @property
    def interval(self) -> tuple[float, float]:
        return wilson(self.count, self.total)

    def to_json(self) -> dict[str, Any]:
        low, high = self.interval
        return {
            "count": self.count,
            "total": self.total,
            "value": _round(self.value),
            "ci95": [_round(low), _round(high)] if self.total else None,
        }

    def __str__(self) -> str:
        if not self.total:
            return "0 of 0"
        low, high = self.interval
        return f"{self.count} of {self.total} ({self.value:.0%}; 95% CI {low:.0%}-{high:.0%})"


def rate(flags: Iterable[bool]) -> Rate:
    values = list(flags)
    return Rate(count=sum(values), total=len(values))


def percentile(values: Sequence[float], q: float) -> float | None:
    """The q-th percentile (0-100) by linear interpolation between the closest ranks (the
    default of numpy); None for no values."""
    if not 0 <= q <= 100:
        raise ValueError("q must be in [0, 100]")
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * q / 100
    lower = math.floor(position)
    upper = min(lower + 1, len(ordered) - 1)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def _round(value: float | None) -> float | None:
    return round(value, 4) if value is not None else None


# --- triage -------------------------------------------------------------------------------------


def triage_accuracy(pairs: Sequence[tuple[str, str | None]]) -> Rate:
    """(label, predicted intent) pairs; a missing prediction (failed run) counts as wrong."""
    return rate(label == predicted for label, predicted in pairs)


def confusion_matrix(pairs: Sequence[tuple[str, str | None]]) -> dict[str, dict[str, int]]:
    """label -> predicted -> count; a missing prediction is `none`."""
    matrix: dict[str, Counter[str]] = {}
    for label, predicted in pairs:
        matrix.setdefault(label, Counter())[predicted or NO_PREDICTION] += 1
    return {label: dict(sorted(row.items())) for label, row in sorted(matrix.items())}


def flag_recall(
    pairs: Sequence[tuple[Mapping[str, bool], Mapping[str, bool | None] | None]],
) -> dict[str, Rate]:
    """(labelled flags, flags the triage found) pairs: per flag, the labelled tickets whose flag
    was found. A missing triage (failed run) found nothing."""
    return {
        flag: rate(bool(found and found.get(flag)) for labels, found in pairs if labels[flag])
        for flag in FLAGS
    }


# --- routing ------------------------------------------------------------------------------------


@dataclass(frozen=True)
class RoutedTicket:
    """What routing metrics need of one ticket. `wrong_answer`: the judges found the answer
    wrong (coverage below 0.8 or unfaithful), None when not judged."""

    must_handoff: bool
    should_escalate: bool
    route: str | None
    wrong_answer: bool | None = None


def _escalated(item: RoutedTicket) -> bool:
    # A failed run ends with a handoff (`system_error`), so it has a route too.
    return item.route in ESCALATED_ROUTES


def routing(items: Sequence[RoutedTicket]) -> dict[str, Rate]:
    """Hard-handoff recall, escalation recall and precision, automation share, and wrong
    auto-replies (auto-replied tickets that should have escalated or whose answer is wrong; an
    auto-reply without a judgement counts as wrong, because a red line must not pass on a
    missing check)."""
    auto = [item for item in items if item.route == AUTO_REPLY]
    escalated = [item for item in items if _escalated(item)]
    return {
        "hard_handoff_recall": rate(i.route == HANDOFF for i in items if i.must_handoff),
        "escalation_recall": rate(_escalated(i) for i in items if i.should_escalate),
        "escalation_precision": rate(i.should_escalate for i in escalated),
        "automation_share": rate(i.route == AUTO_REPLY for i in items),
        "wrong_auto_reply": rate(
            i.should_escalate or i.wrong_answer is None or i.wrong_answer for i in auto
        ),
    }


# --- retrieval (phase 5) ------------------------------------------------------------------------


def _matches(retrieved: str, gold: str) -> bool:
    """A retrieved id is a chunk id (`withdrawal-times#2`, `error-codes#ERR-2003`) or a document
    id. A gold article id matches any of its chunks; a gold chunk id matches only itself."""
    return retrieved == gold or ("#" not in gold and retrieved.split("#", 1)[0] == gold)


def first_gold_rank(ranked: Sequence[str], gold: Sequence[str]) -> int | None:
    """1-based rank of the first retrieved id that matches a gold id; None when none does."""
    for rank, retrieved in enumerate(ranked, start=1):
        if any(_matches(retrieved, item) for item in gold):
            return rank
    return None


def hit_at_k(ranked: Sequence[str], gold: Sequence[str], k: int = HIT_K) -> bool:
    rank = first_gold_rank(ranked[:k], gold)
    return rank is not None


def reciprocal_rank(ranked: Sequence[str], gold: Sequence[str]) -> float:
    rank = first_gold_rank(ranked, gold)
    return 1 / rank if rank is not None else 0.0


@dataclass(frozen=True)
class RetrievalScores:
    hit_at_k: Rate
    mrr: float | None
    covered: int
    excluded: int
    k: int = HIT_K


def retrieval(items: Sequence[tuple[Sequence[str] | None, Sequence[str]]]) -> RetrievalScores:
    """(ranked ids of the first search or None when no search ran, gold ids) pairs. A ticket
    without a search or without gold documents is excluded."""
    covered = [(ranked, gold) for ranked, gold in items if ranked is not None and gold]
    ranks = [reciprocal_rank(ranked, gold) for ranked, gold in covered]
    return RetrievalScores(
        hit_at_k=rate(hit_at_k(ranked, gold) for ranked, gold in covered),
        mrr=sum(ranks) / len(ranks) if ranks else None,
        covered=len(covered),
        excluded=len(items) - len(covered),
    )


# --- answers (judge metrics; unvalidated, same model family) -------------------------------------


def mean(values: Sequence[float]) -> float | None:
    return sum(values) / len(values) if values else None


def citation_code_problems(body: str, citations: Sequence[int], dropped: Sequence[int]) -> int:
    """Markers that do not point to a known source, found by code (ai-pipeline §5 check 2):
    markers the run dropped because their citation named an unknown document, and markers in the
    body without a citation entry."""
    in_body = {int(number) for number in re.findall(r"\[(\d+)\]", body)}
    return len(dropped) + len(in_body - set(citations))


# --- cost and latency ---------------------------------------------------------------------------


def cost_per_ticket(costs: Sequence[Decimal]) -> Decimal | None:
    return sum(costs, Decimal(0)) / len(costs) if costs else None


@dataclass(frozen=True)
class Latency:
    count: int
    p50_ms: float | None
    p95_ms: float | None

    def to_json(self) -> dict[str, Any]:
        return {"count": self.count, "p50_ms": self.p50_ms, "p95_ms": self.p95_ms}


def latency(values_ms: Sequence[int]) -> Latency:
    values = [float(value) for value in values_ms]
    return Latency(count=len(values), p50_ms=percentile(values, 50), p95_ms=percentile(values, 95))


# --- targets and red lines (eval plan §2) -------------------------------------------------------


class Status(StrEnum):
    PASS = "pass"  # noqa: S105 - meets the target, not a password
    BELOW_TARGET = "below target"  # misses the target, still inside the red line
    RED_LINE = "red line missed"
    NOT_MEASURED = "not measured"
    REPORTED = "reported"  # no target


@dataclass(frozen=True)
class Check:
    """One row of the report table. `higher_is_better`: the value must be at least the target
    and the red line (accuracy, recall); otherwise at most (errors, cost, latency). The red line
    is the last acceptable value (PRD §7, eval plan §2): reaching it is "below target", anything
    worse is a breach."""

    name: str
    value: float | None
    target: float | None
    red_line: float | None
    higher_is_better: bool
    shown: str = ""
    judged: bool = False

    @property
    def status(self) -> Status:
        if self.value is None:
            return Status.NOT_MEASURED
        if self.target is None:
            return Status.REPORTED
        if self._meets(self.target):
            return Status.PASS
        if self.red_line is None or self._meets(self.red_line):
            return Status.BELOW_TARGET
        return Status.RED_LINE

    def _meets(self, bound: float) -> bool:
        # A small tolerance: 0.85 computed as 17/20 must meet 0.85.
        if self.higher_is_better:
            return self.value is not None and self.value >= bound - 1e-9
        return self.value is not None and self.value <= bound + 1e-9

    def to_json(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "value": _round(self.value),
            "target": self.target,
            "red_line": self.red_line,
            "higher_is_better": self.higher_is_better,
            "status": self.status.value,
            "judged": self.judged,
        }
