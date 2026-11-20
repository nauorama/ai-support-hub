"""`metrics.json` and `report.md` of an evaluation run (eval plan §2, §4; P4-04).

`evaluate` joins the labels of the ticket file with `results.jsonl` by ticket id and computes
the metrics of eval plan §2 with the pure functions of `hub_evals.metrics`; `render_report` turns
them into the report: the table of metrics against targets and red lines with pass/fail marks,
the confusion matrix, cost and latency, and the tickets with problems (worst first).

Every report says: synthetic data, a demo check and not a proof, and judge metrics are
unvalidated and from the same model family as the writer (eval plan §1, §5). The report names
ticket ids and labels only, never ticket or draft text: it is committed.
"""

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from hub_evals import metrics as m
from hub_evals.results import RunConfig, TicketResult
from hub_evals.schemas import Ticket

JUDGE_MARK = "unvalidated, same model family"
NOT_MEASURED_SAFETY = "phase 8 (`make eval-attacks`, `make leak-scan`)"


# --- run summary --------------------------------------------------------------------------------


def summary(results: Sequence[TicketResult], *, planned: int) -> dict[str, Any]:
    """Counts and sums of the finished tickets, as JSON-ready values."""
    cost = sum((result.cost_usd for result in results), Decimal(0))
    paid = sum((result.paid_cost_usd for result in results), Decimal(0))
    judged = [result.judges for result in results if result.judges is not None]
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
        "judge_llm_calls": sum(j.llm_calls for j in judged),
        "judge_cost_usd": str(sum((j.cost_usd for j in judged), Decimal(0))),
        "judge_paid_cost_usd": str(sum((j.paid_cost_usd for j in judged), Decimal(0))),
        "judge_errors": dict(
            sorted(Counter(f"{n}: {e}" for j in judged for n, e in j.errors.items()).items())
        ),
    }


# --- per ticket ---------------------------------------------------------------------------------


def read_kb(result: TicketResult) -> bool:
    """Whether the run read the knowledge base. Baseline B0 has no search step: the writer
    reads the whole knowledge base, so a run with a draft read it. A run without a draft (a
    failed run; from phase 6 a row-1 handoff, which skips research) did not."""
    return result.draft is not None


def answered(result: TicketResult) -> bool:
    """A draft with an answer to judge: `can_answer` and a non-empty body."""
    return result.draft is not None and result.draft.can_answer and bool(result.draft.body.strip())


def citation_code_problems(result: TicketResult) -> int:
    if result.draft is None:
        return 0
    dropped = result.draft.evidence.get("dropped_markers") or []
    markers = [int(c["marker"]) for c in result.draft.citations if "marker" in c]
    return m.citation_code_problems(result.draft.body, markers, [int(d) for d in dropped])


def wrong_answer(result: TicketResult) -> bool | None:
    """True when a judge found the answer wrong (key-fact coverage below 0.8, or an unsupported
    claim); None when no judge could say (no judges, or a judge failed without finding it
    wrong)."""
    judges = result.judges
    if judges is None:
        return None
    wrong = (judges.key_facts is not None and judges.key_facts.coverage < m.WRONG_COVERAGE) or (
        judges.faithfulness is not None and not judges.faithfulness.faithful
    )
    if wrong:
        return True
    if judges.errors or (judges.key_facts is None and judges.faithfulness is None):
        return None
    return False


def citations_invalid(result: TicketResult) -> bool:
    """A marker that does not point to supporting evidence: found by code, or a cited source
    the judge found not to support its sentence."""
    judged_bad = result.judges is not None and any(
        not citation.supports for citation in result.judges.citations
    )
    return citation_code_problems(result) > 0 or judged_bad


def problems(ticket: Ticket, result: TicketResult) -> list[str]:
    """What went wrong with one ticket, for the list of worst tickets."""
    found: list[str] = []
    if result.run_failed:
        found.append(f"run failed ({result.error or 'no run'})")
    if result.triage.intent != ticket.intent:
        found.append(f"intent {result.triage.intent or 'none'} (label {ticket.intent})")
    flags = result.triage.flags()
    for flag in m.FLAGS:
        if getattr(ticket.flags, flag) and not (flags and flags.get(flag)):
            found.append(f"missed flag {flag}")
    if ticket.must_handoff and result.route != m.HANDOFF:
        found.append(f"must hand off, route {result.route or 'none'}")
    elif ticket.should_escalate and result.route not in m.ESCALATED_ROUTES:
        found.append(f"should escalate, route {result.route or 'none'}")
    if result.route == m.AUTO_REPLY and wrong_answer(result) is not False:
        found.append("wrong auto-reply")
    judges = result.judges
    if judges is not None:
        if judges.key_facts is not None and judges.key_facts.coverage < m.WRONG_COVERAGE:
            found.append(f"key-fact coverage {judges.key_facts.coverage:.2f}")
        if judges.faithfulness is not None and judges.faithfulness.unchecked:
            found.append("no claim listed by the faithfulness judge")
        elif judges.faithfulness is not None and not judges.faithfulness.faithful:
            found.append(f"{judges.faithfulness.unsupported} unsupported claims")
        for name, error in sorted(judges.errors.items()):
            found.append(f"judge {name} failed ({error})")
    if answered(result) and citations_invalid(result):
        found.append("invalid citation")
    return found


# --- the metrics --------------------------------------------------------------------------------


@dataclass(frozen=True)
class Row:
    """A row of the report table: the check, how its value is shown, the target and red line
    as eval plan §2 writes them."""

    check: m.Check
    shown: str
    target: str
    red_line: str


def evaluate(
    tickets: Sequence[Ticket], results: Sequence[TicketResult], *, planned: int
) -> dict[str, Any]:
    """`metrics.json`: the run summary and the metrics of eval plan §2. Results of tickets that
    are not in `tickets` are ignored."""
    labels = {ticket.id: ticket for ticket in tickets}
    pairs = [(labels[r.ticket_id], r) for r in results if r.ticket_id in labels]
    finished = [result for _, result in pairs]
    rows = _rows(pairs)
    data = summary(finished, planned=planned)
    data["metrics"] = _details(pairs)
    data["checks"] = [
        {
            **row.check.to_json(),
            "shown": row.shown,
            "target_text": row.target,
            "red_line_text": row.red_line,
        }
        for row in rows
    ]
    data["tickets_with_problems"] = [
        {"ticket_id": ticket.id, "problems": found}
        for ticket, found in sorted(
            ((t, problems(t, r)) for t, r in pairs), key=lambda item: -len(item[1])
        )
        if found
    ]
    return data


def _answers(pairs: Sequence[tuple[Ticket, TicketResult]]) -> dict[str, Any]:
    with_facts = [(t, r) for t, r in pairs if t.key_facts]
    kb = [(t, r) for t, r in with_facts if read_kb(r)]
    coverage_judged = [
        r.judges.key_facts.coverage
        for _, r in kb
        if r.judges is not None and r.judges.key_facts is not None
    ]
    answered_results = [r for _, r in pairs if answered(r)]
    faithful_judged = [
        r.judges.faithfulness
        for r in answered_results
        if r.judges is not None and r.judges.faithfulness is not None
    ]
    return {
        "key_fact_coverage": {
            "value": m.mean(coverage_judged),
            "covered": len(coverage_judged),
            "excluded_no_kb_search": len(with_facts) - len(kb),
            "excluded_not_judged": len(kb) - len(coverage_judged),
            "tickets_without_key_facts": len(pairs) - len(with_facts),
        },
        "faithfulness": {
            "unfaithful": m.rate(not f.faithful for f in faithful_judged),
            "answered": len(answered_results),
            "not_judged": len(answered_results) - len(faithful_judged),
            "quotes_not_found": sum(
                not c.quote_found and c.judge_supported for f in faithful_judged for c in f.claims
            ),
            "no_claims_listed": sum(f.unchecked for f in faithful_judged),
        },
        "citation_validity": {
            "invalid": m.rate(citations_invalid(r) for r in answered_results),
            "code_problems": sum(citation_code_problems(r) for r in answered_results),
            "judged_citations": sum(len(r.judges.citations) for r in answered_results if r.judges),
            "judged_unsupported": sum(
                not c.supports for r in answered_results if r.judges for c in r.judges.citations
            ),
        },
    }


def _routing(pairs: Sequence[tuple[Ticket, TicketResult]]) -> dict[str, m.Rate]:
    return m.routing(
        [
            m.RoutedTicket(
                must_handoff=t.must_handoff,
                should_escalate=t.should_escalate,
                route=r.route,
                wrong_answer=wrong_answer(r),
            )
            for t, r in pairs
        ]
    )


def account_facts_used(result: TicketResult) -> bool:
    """The writer read account facts from the research loop (P7-06, `drafts.evidence`)."""
    return result.draft is not None and bool(result.draft.evidence.get("account_facts"))


def _account_data(pairs: Sequence[tuple[Ticket, TicketResult]]) -> dict[str, Any]:
    """Tickets labelled `needs_account_data` whose writer read account facts. Only tickets with
    a draft count: a row-1 handoff or a failed run never reaches research. Reported, no target
    (eval plan §2 has none)."""
    needing = [r for t, r in pairs if t.needs_account_data]
    drafted = [r for r in needing if r.draft is not None]
    return {
        "used": m.rate(account_facts_used(r) for r in drafted),
        "needs_account_data": len(needing),
        "excluded_no_draft": len(needing) - len(drafted),
        "facts_without_label": sum(
            account_facts_used(r) for t, r in pairs if not t.needs_account_data
        ),
    }


def ranked_ids(result: TicketResult) -> list[str] | None:
    """The ranked chunk ids of the run's first search (P5-05), from the draft's evidence; None
    when no search ran (B0) or there is no draft (a failed run, a row-1 handoff)."""
    return result.draft.ranked_chunk_ids if result.draft is not None else None


def _retrieval(pairs: Sequence[tuple[Ticket, TicketResult]]) -> m.RetrievalScores:
    # Tickets that ran no search, or have no gold documents, are excluded (P4-04).
    return m.retrieval([(ranked_ids(r), t.gold_doc_ids) for t, r in pairs])


def _details(pairs: Sequence[tuple[Ticket, TicketResult]]) -> dict[str, Any]:
    intents = [(t.intent.value, r.triage.intent) for t, r in pairs]
    flags = m.flag_recall([(t.flags.model_dump(), r.triage.flags()) for t, r in pairs])
    retrieval = _retrieval(pairs)
    answers = _answers(pairs)
    auto = [r.latency_ms for _, r in pairs if r.route == m.AUTO_REPLY]
    cost = m.cost_per_ticket([r.cost_usd for _, r in pairs])
    account = _account_data(pairs)
    return {
        "triage": {
            "accuracy": m.triage_accuracy(intents).to_json(),
            "confusion_matrix": m.confusion_matrix(intents),
            "flag_recall": {name: value.to_json() for name, value in flags.items()},
        },
        "routing": {name: value.to_json() for name, value in _routing(pairs).items()},
        "retrieval": {
            f"hit_at_{retrieval.k}": retrieval.hit_at_k.to_json(),
            "mrr": retrieval.mrr,
            "covered": retrieval.covered,
            "excluded_no_kb_search": retrieval.excluded,
        },
        "answers": {
            "judge": JUDGE_MARK,
            "key_fact_coverage": answers["key_fact_coverage"],
            "faithfulness": {
                **answers["faithfulness"],
                "unfaithful": answers["faithfulness"]["unfaithful"].to_json(),
            },
            "citation_validity": {
                **answers["citation_validity"],
                "invalid": answers["citation_validity"]["invalid"].to_json(),
            },
        },
        "account_data": {**account, "used": account["used"].to_json()},
        "cost": {"per_ticket_usd": str(cost) if cost is not None else None},
        "latency": {
            "auto_reply": m.latency(auto).to_json(),
            "all": m.latency([r.latency_ms for _, r in pairs]).to_json(),
        },
    }


def _rows(pairs: Sequence[tuple[Ticket, TicketResult]]) -> list[Row]:
    intents = [(t.intent.value, r.triage.intent) for t, r in pairs]
    accuracy = m.triage_accuracy(intents)
    flags = m.flag_recall([(t.flags.model_dump(), r.triage.flags()) for t, r in pairs])
    retrieval = _retrieval(pairs)
    answers = _answers(pairs)
    routes = _routing(pairs)
    coverage = answers["key_fact_coverage"]
    unfaithful: m.Rate = answers["faithfulness"]["unfaithful"]
    invalid: m.Rate = answers["citation_validity"]["invalid"]
    escalation = routes["escalation_recall"]
    auto_latency = m.latency([r.latency_ms for _, r in pairs if r.route == m.AUTO_REPLY])
    account = _account_data(pairs)
    cost = m.cost_per_ticket([r.cost_usd for _, r in pairs])
    rows = [
        Row(
            m.Check("triage accuracy", accuracy.value, 0.9, 0.8, True),
            str(accuracy),
            "≥ 18 of 20",
            "16 of 20",
        ),
    ]
    for flag in m.FLAGS:
        recall = flags[flag]
        rows.append(
            Row(
                m.Check(f"flag recall: {flag}", recall.value, 1.0, 1.0, True),
                str(recall),
                "all",
                "all",
            )
        )
    hit = retrieval.hit_at_k
    no_search = f"no ticket ran a KB search (0 of {len(pairs)} tickets)"
    rows += [
        Row(
            m.Check(f"hit@{retrieval.k}", hit.value, 0.9, 0.8, True),
            str(hit) if retrieval.covered else no_search,
            "≥ 0.90",
            "0.80",
        ),
        Row(
            m.Check("MRR", retrieval.mrr, None, None, True),
            f"{retrieval.mrr:.2f} ({retrieval.covered} tickets)"
            if retrieval.mrr is not None
            else no_search,
            "reported",
            "—",
        ),
        Row(
            m.Check("key-fact coverage", coverage["value"], 0.85, 0.75, True, judged=True),
            f"{coverage['value']:.2f} ({coverage['covered']} tickets)"
            if coverage["value"] is not None
            else "no judged ticket",
            "≥ 0.85",
            "0.75",
        ),
        Row(
            m.Check(
                "faithfulness: answered tickets with an unsupported claim",
                float(unfaithful.count) if unfaithful.total else None,
                1,
                2,
                False,
                judged=True,
            ),
            str(unfaithful),
            "≤ 1",
            "2",
        ),
        Row(
            m.Check(
                "citation validity: answered tickets with an invalid marker",
                float(invalid.count) if invalid.total else None,
                0,
                1,
                False,
                judged=True,
            ),
            str(invalid),
            "0",
            "1",
        ),
        Row(
            m.Check("hard-handoff recall", routes["hard_handoff_recall"].value, 1.0, 1.0, True),
            str(routes["hard_handoff_recall"]),
            "1.00",
            "1.00",
        ),
        Row(
            m.Check(
                # Scored by the misses: the red line is "1 miss".
                "escalation recall",
                float(escalation.total - escalation.count) if escalation.total else None,
                0,
                1,
                False,
            ),
            f"{escalation}; {escalation.total - escalation.count} missed",
            "1.00",
            "1 miss",
        ),
        Row(
            m.Check("escalation precision", routes["escalation_precision"].value, None, None, True),
            str(routes["escalation_precision"]),
            "reported",
            "—",
        ),
        Row(
            m.Check("wrong auto-reply", float(routes["wrong_auto_reply"].count), 0, 0, False),
            str(routes["wrong_auto_reply"]),
            "0",
            "any",
        ),
        Row(
            m.Check("automation share", routes["automation_share"].value, None, None, True),
            str(routes["automation_share"]),
            "reported",
            "—",
        ),
        Row(
            m.Check("account facts used", account["used"].value, None, None, True),
            f"{account['used']} of tickets needing account data with a draft"
            f" ({account['excluded_no_draft']} without a draft)",
            "reported",
            "—",
        ),
        Row(
            m.Check(
                "cost per ticket (USD)",
                float(cost) if cost is not None else None,
                0.05,
                0.10,
                False,
            ),
            f"${cost:.4f}" if cost is not None else "no ticket",
            "≤ €0.05",
            "€0.10",
        ),
        Row(
            m.Check("latency p50, auto-replied (ms)", auto_latency.p50_ms, 8000, None, False),
            _ms(auto_latency.p50_ms, auto_latency.count),
            "≤ 8 s",
            "—",
        ),
        Row(
            m.Check("latency p95, auto-replied (ms)", auto_latency.p95_ms, 20000, 30000, False),
            _ms(auto_latency.p95_ms, auto_latency.count),
            "≤ 20 s",
            "30 s",
        ),
    ]
    for name, target, red in (
        ("PAN leakage", "0", "0"),
        ("attack success", "0", "1 of 16"),
        ("internal leak", "0", "0"),
    ):
        rows.append(Row(m.Check(name, None, None, None, False), NOT_MEASURED_SAFETY, target, red))
    return rows


def _ms(value: float | None, count: int) -> str:
    return f"{value / 1000:.1f} s ({count} runs)" if value is not None else "no auto-reply"


# --- report.md ----------------------------------------------------------------------------------

_MARK = {
    m.Status.PASS.value: "✅ pass",
    m.Status.BELOW_TARGET.value: "⚠️ below target",
    m.Status.RED_LINE.value: "❌ red line missed",
    m.Status.NOT_MEASURED.value: "— not measured",
    m.Status.REPORTED.value: "reported",
}


def render_report(config: RunConfig, data: dict[str, Any], *, run_name: str) -> str:
    routes = ", ".join(f"{name} {count}" for name, count in data["routes"].items()) or "none"
    reasons = (
        ", ".join(f"{name} {count}" for name, count in data["route_reasons"].items()) or "none"
    )
    errors = ", ".join(f"{name} {count}" for name, count in data["errors"].items()) or "none"
    judge_errors = (
        ", ".join(f"{name} {count}" for name, count in data["judge_errors"].items()) or "none"
    )
    judges = _pairs(config.judge_prompt_versions) if config.judge_prompt_versions else "off"
    lines = [
        f"# Evaluation run {run_name}",
        "",
        "Synthetic data. **Demo check, not a proof** (eval plan §2): with 20 tickets one ticket "
        "is 5 points, and 0 errors in 20 still allows a true error rate of up to 16% (95% "
        "Wilson interval). The tickets are also the tuning set, so the numbers are optimistic.",
        "",
        f"Judge metrics (marked *judged*) are **{JUDGE_MARK}**: the judge is Claude Haiku 5.5 "
        "like the writer and is not checked against human labels (eval plan §5).",
        "",
        "| Setting | Value |",
        "|---|---|",
        f"| Variant | `{config.variant}` |",
        f"| LLM mode | `{config.llm_mode}` |",
        f"| Graph version | `{config.graph_version}` |",
        f"| Prompt versions | {_pairs(config.prompt_versions)} |",
        f"| Judge prompts | {judges} |",
        f"| Models | {_pairs({k: ' / '.join(v) for k, v in config.models.items()})} |",
        f"| Git | `{config.git_sha}`{' (uncommitted changes)' if config.git_dirty else ''} |",
        f"| Anchor date | {config.core_anchor_date or 'not set (real date)'} |",
        f"| Tickets | `{config.tickets_file}` |",
        "",
        "## Metrics against targets",
        "",
        "Rates show the count, the total and the 95% Wilson interval.",
        "",
        "| Metric | Value | Target | Red line | Result |",
        "|---|---|---|---|---|",
    ]
    for check in data["checks"]:
        name = check["name"] + (" *(judged)*" if check["judged"] else "")
        lines.append(
            f"| {name} | {check['shown']} | {check['target_text']} | {check['red_line_text']} "
            f"| {_MARK[check['status']]} |"
        )
    metrics = data["metrics"]
    coverage = metrics["answers"]["key_fact_coverage"]
    faithfulness = metrics["answers"]["faithfulness"]
    citations = metrics["answers"]["citation_validity"]
    lines += [
        "",
        "Cost is in USD, compared with the euro targets of eval plan §2 one to one. Latency "
        "targets apply to auto-replied runs; "
        f"all runs: p50 {_seconds(metrics['latency']['all']['p50_ms'])}, "
        f"p95 {_seconds(metrics['latency']['all']['p95_ms'])}.",
        "",
        "**Coverage of the scores.** "
        f"Key-fact coverage: {coverage['covered']} tickets judged, "
        f"{coverage['excluded_no_kb_search']} left out (no KB search), "
        f"{coverage['excluded_not_judged']} not judged, "
        f"{coverage['tickets_without_key_facts']} without key facts. "
        f"Faithfulness: {faithfulness['answered']} answered tickets, "
        f"{faithfulness['not_judged']} not judged, "
        f"{faithfulness['quotes_not_found']} claims counted unsupported because the judge's "
        "quote was not in the evidence, "
        f"{faithfulness['no_claims_listed']} cited answers counted unfaithful because the judge "
        "listed no claim. "
        f"Citations: {citations['code_problems']} marker problems found by code, "
        f"{citations['judged_unsupported']} of {citations['judged_citations']} judged citations "
        "not supported. "
        f"Retrieval: {metrics['retrieval']['covered']} tickets, "
        f"{metrics['retrieval']['excluded_no_kb_search']} left out "
        "(no KB search, or no gold documents).",
        "",
        "## Triage",
        "",
        f"Accuracy: {_rate(metrics['triage']['accuracy'])}.",
        "",
        "Flag recall: "
        + "; ".join(
            f"`{name}` {_rate(value)}" for name, value in metrics["triage"]["flag_recall"].items()
        )
        + ".",
        "",
        "Confusion matrix (label → predicted: count):",
        "",
        "| Label | Predicted |",
        "|---|---|",
    ]
    for label, row in metrics["triage"]["confusion_matrix"].items():
        predicted = ", ".join(
            f"**{name}** {count}" if name == label else f"{name} {count}"
            for name, count in row.items()
        )
        lines.append(f"| {label} | {predicted} |")
    lines += [
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
        f"| Model calls (answers) | {data['llm_calls']} |",
        f"| Cost of the answers (USD) | {data['cost_usd']} |",
        f"| Judge calls | {data['judge_llm_calls']} |",
        f"| Cost of the judges (USD) | {data['judge_cost_usd']} |",
        f"| Judge errors | {judge_errors} |",
        f"| Paid in this run (USD) | "
        f"{Decimal(data['paid_cost_usd']) + Decimal(data['judge_paid_cost_usd'])} |",
        "",
        "## Tickets with problems",
        "",
    ]
    worst = data["tickets_with_problems"][:20]
    if worst:
        lines += ["| Ticket | Problems |", "|---|---|"]
        lines += [f"| {item['ticket_id']} | {'; '.join(item['problems'])} |" for item in worst]
    else:
        lines.append("None.")
    lines.append("")
    if config.llm_mode == "fake":
        lines[2:2] = [
            "**Fake models:** answers are recorded responses; a ticket without a recording is "
            "handed off (`system_error`) and a judge without one fails. These numbers check the "
            "runner, not the answers.",
            "",
        ]
    return "\n".join(lines)


def _rate(value: dict[str, Any]) -> str:
    if not value["total"]:
        return "0 of 0"
    low, high = value["ci95"]
    return f"{value['count']} of {value['total']} (95% CI {low:.0%}-{high:.0%})"


def _seconds(value: float | None) -> str:
    return f"{value / 1000:.1f} s" if value is not None else "—"


def _pairs(values: dict[str, str]) -> str:
    return ", ".join(f"{key} `{value}`" for key, value in sorted(values.items())) or "none"
