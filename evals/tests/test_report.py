"""`metrics.json` and `report.md` (P4-04): the metrics joined with the labels, on hand-made
tickets and results with known answers."""

from decimal import Decimal
from typing import Any

from hub_evals import report as r
from hub_evals.judges import (
    CitationResult,
    ClaimResult,
    FactResult,
    FaithfulnessResult,
    KeyFactsResult,
)
from hub_evals.results import DraftOut, Judgements, RunConfig, TicketResult, Triage
from hub_evals.schemas import Ticket

NO_FLAGS = {
    "wants_human": False,
    "reports_unauthorised_activity": False,
    "is_complaint_or_legal": False,
}


def ticket(ticket_id: str, **changes: Any) -> Ticket:  # noqa: ANN401
    values: dict[str, Any] = {
        "id": ticket_id,
        "customer_ref": "CUS-0001",
        "message": "where is my money",
        "history": [],
        "intent": "withdrawal_issue",
        "secondary_intent": None,
        "flags": NO_FLAGS,
        "gold_doc_ids": ["withdrawal-times"],
        "needs_account_data": False,
        "key_facts": ["fact one", "fact two"],
        "should_escalate": False,
        "must_handoff": False,
        "expected_reason": None,
        "difficulty": "normal",
        "tags": [],
    }
    return Ticket.model_validate(values | changes)


def draft(body: str = "Fact one. [1] Fact two. [1]", **changes: Any) -> DraftOut:  # noqa: ANN401
    values: dict[str, Any] = {
        "can_answer": True,
        "body": body,
        "missing_information": None,
        "citations": [{"marker": 1, "source_id": "withdrawal-times", "doc_id": "withdrawal-times"}],
        "evidence": {"variant": "b0_full_kb", "documents": [], "dropped_markers": []},
        "proposed_actions": [],
    }
    return DraftOut.model_validate(values | changes)


def judged(
    present: tuple[bool, ...] = (True, True),
    supported: tuple[bool, ...] = (True,),
    supports: tuple[bool, ...] = (True,),
    errors: dict[str, str] | None = None,
) -> Judgements:
    return Judgements(
        key_facts=KeyFactsResult(
            facts=[
                FactResult(fact=n, present=p, judge_present=p, quote_found=p)
                for n, p in enumerate(present, start=1)
            ]
        )
        if present
        else None,
        faithfulness=FaithfulnessResult(
            claims=[
                ClaimResult(claim="c", supported=s, judge_supported=s, quote_found=s)
                for s in supported
            ]
        ),
        citations=[
            CitationResult(marker=1, source_id="d", supports=s, judge_supports=s, quote_found=s)
            for s in supports
        ],
        errors=errors or {},
        llm_calls=3,
        cost_usd=Decimal("0.001"),
        paid_cost_usd=Decimal("0.001"),
    )


def result(ticket_id: str, **changes: Any) -> TicketResult:  # noqa: ANN401
    values: dict[str, Any] = {
        "ticket_id": ticket_id,
        "run_id": "r",
        "ticket_status": "pending_approval",
        "run_status": "completed",
        "run_failed": False,
        "route": "approval",
        "route_reasons": ["auto_reply_off"],
        "error": None,
        "triage": Triage(
            intent="withdrawal_issue",
            secondary_intent=None,
            urgency="normal",
            summary="s",
            **NO_FLAGS,
        ),
        "draft": draft(),
        "models": {},
        "prompt_versions": {},
        "cost_usd": Decimal("0.004"),
        "paid_cost_usd": Decimal("0.004"),
        "input_tokens": 10,
        "output_tokens": 5,
        "llm_calls": 2,
        "latency_ms": 4000,
        "judges": judged(),
    }
    return TicketResult.model_validate(values | changes)


def config(**changes: Any) -> RunConfig:  # noqa: ANN401
    values: dict[str, Any] = {
        "variant": "b0_full_kb",
        "llm_mode": "real",
        "graph_version": "linear-v1",
        "models": {"triage": ["a", "b"]},
        "prompt_versions": {"triage": "v1"},
        "judge_prompt_versions": {"judge_key_facts": "v1"},
        "git_sha": "abc",
        "git_dirty": False,
        "core_anchor_date": None,
        "tickets_file": "data/eval/tickets.dev.jsonl",
        "tickets_sha256": "x",
    }
    return RunConfig.model_validate(values | changes)


def check(data: dict[str, Any], name: str) -> dict[str, Any]:
    return next(row for row in data["checks"] if row["name"] == name)


# --- per ticket ---------------------------------------------------------------------------------


def test_wrong_answer() -> None:
    assert r.wrong_answer(result("T-0001")) is False
    assert r.wrong_answer(result("T-0001", judges=judged(present=(True, False)))) is True
    assert r.wrong_answer(result("T-0001", judges=judged(supported=(True, False)))) is True
    assert r.wrong_answer(result("T-0001", judges=None)) is None
    # A failed judge without a finding leaves the answer unknown, not right.
    assert r.wrong_answer(result("T-0001", judges=judged(errors={"citation": "E"}))) is None
    assert r.wrong_answer(result("T-0001", judges=Judgements())) is None


def test_citation_problems_by_code_and_by_judge() -> None:
    assert not r.citations_invalid(result("T-0001"))
    dropped = draft(evidence={"variant": "b0_full_kb", "documents": [], "dropped_markers": [2]})
    assert r.citation_code_problems(result("T-0001", draft=dropped)) == 1
    assert r.citations_invalid(result("T-0001", draft=dropped))
    no_entry = draft(body="Fact. [1] Other. [3]")
    assert r.citation_code_problems(result("T-0001", draft=no_entry)) == 1
    assert r.citations_invalid(result("T-0001", judges=judged(supports=(True, False))))
    assert r.citation_code_problems(result("T-0001", draft=None)) == 0


def test_answered_and_read_kb() -> None:
    assert r.answered(result("T-0001"))
    assert not r.answered(result("T-0001", draft=draft(body="", can_answer=False)))
    assert not r.answered(result("T-0001", draft=None))
    assert r.read_kb(result("T-0001", draft=draft(body="", can_answer=False)))
    assert not r.read_kb(result("T-0001", draft=None))


def test_problems_of_one_ticket() -> None:
    labelled = ticket(
        "T-0001",
        intent="fees_fx",
        flags=NO_FLAGS | {"wants_human": True},
        must_handoff=True,
        should_escalate=True,
        expected_reason="customer_request",
    )
    found = r.problems(
        labelled,
        result(
            "T-0001",
            route="auto_reply",
            judges=judged(present=(True, False), supported=(False,), errors={"citation": "E"}),
        ),
    )
    assert found == [
        "intent withdrawal_issue (label fees_fx)",
        "missed flag wants_human",
        "must hand off, route auto_reply",
        "wrong auto-reply",
        "key-fact coverage 0.50",
        "1 unsupported claims",
        "judge citation failed (E)",
    ]
    assert r.problems(ticket("T-0002"), result("T-0002")) == []
    unchecked = judged(supported=()).model_copy(
        update={"faithfulness": FaithfulnessResult(claims=[], cites=True)}
    )
    assert r.problems(ticket("T-0002"), result("T-0002", judges=unchecked)) == [
        "no claim listed by the faithfulness judge"
    ]
    assert r.wrong_answer(result("T-0002", judges=unchecked)) is True
    failed = r.problems(
        ticket("T-0003", should_escalate=True, expected_reason="no_kb_answer"),
        result(
            "T-0003",
            run_failed=True,
            error="X",
            route=None,
            draft=None,
            judges=None,
            triage=Triage(intent=None, secondary_intent=None, urgency=None, summary=None),
        ),
    )
    assert failed == [
        "run failed (X)",
        "intent none (label withdrawal_issue)",
        "should escalate, route none",
    ]


# --- metrics ------------------------------------------------------------------------------------


def test_evaluate_counts_with_known_answers() -> None:
    tickets = [
        ticket("T-0001"),
        ticket("T-0002", intent="fees_fx"),
        ticket(
            "T-0003",
            must_handoff=True,
            should_escalate=True,
            expected_reason="fraud_report",
            flags=NO_FLAGS | {"reports_unauthorised_activity": True},
        ),
        ticket("T-0004", key_facts=[]),
    ]
    results = [
        result("T-0001"),
        result("T-0002", judges=judged(present=(True, False), supported=(True, False))),
        result(
            "T-0003",
            run_failed=True,
            route="handoff",
            draft=None,
            judges=None,
            triage=Triage(intent=None, secondary_intent=None, urgency=None, summary=None),
        ),
        result("T-0004", judges=judged(present=()), draft=draft(body="", can_answer=False)),
        result("T-9999"),  # not in the ticket file: ignored
    ]
    data = r.evaluate(tickets, results, planned=4)
    metrics = data["metrics"]
    assert data["tickets_finished"] == 4
    assert metrics["triage"]["accuracy"]["count"] == 2  # T-0002 wrong, T-0003 none
    assert metrics["triage"]["flag_recall"]["reports_unauthorised_activity"]["count"] == 0
    assert metrics["routing"]["hard_handoff_recall"]["count"] == 1
    assert metrics["routing"]["escalation_precision"]["total"] == 4
    coverage = metrics["answers"]["key_fact_coverage"]
    # T-0001: 1.0, T-0002: 0.5; T-0003 has key facts but no KB search; T-0004 has none.
    assert coverage["value"] == 0.75
    assert coverage["covered"] == 2
    assert coverage["excluded_no_kb_search"] == 1
    assert coverage["tickets_without_key_facts"] == 1
    faithfulness = metrics["answers"]["faithfulness"]
    assert (faithfulness["answered"], faithfulness["unfaithful"]["count"]) == (2, 1)
    assert faithfulness["no_claims_listed"] == 0
    assert metrics["retrieval"]["covered"] == 0
    assert metrics["retrieval"]["excluded_no_kb_search"] == 4
    assert metrics["cost"]["per_ticket_usd"] == "0.004"
    assert metrics["latency"]["all"]["p50_ms"] == 4000.0
    assert data["judge_llm_calls"] == 9
    assert data["judge_paid_cost_usd"] == "0.003"

    assert check(data, "triage accuracy")["status"] == "red line missed"  # 2 of 4
    assert check(data, "hard-handoff recall")["status"] == "pass"
    assert check(data, "hit@5")["status"] == "not measured"
    assert check(data, "key-fact coverage")["status"] == "below target"  # 0.75 = red line
    assert check(data, "key-fact coverage")["judged"]
    assert check(data, "faithfulness: answered tickets with an unsupported claim")["status"] == (
        "pass"
    )
    assert check(data, "wrong auto-reply")["status"] == "pass"
    assert check(data, "latency p95, auto-replied (ms)")["status"] == "not measured"
    assert check(data, "PAN leakage")["status"] == "not measured"
    # Three problems each; a tie keeps the order of the ticket file.
    assert [item["ticket_id"] for item in data["tickets_with_problems"]] == ["T-0002", "T-0003"]


def test_evaluate_of_nothing() -> None:
    data = r.evaluate([ticket("T-0001")], [], planned=1)
    assert data["tickets_finished"] == 0
    assert check(data, "triage accuracy")["status"] == "not measured"
    assert check(data, "cost per ticket (USD)")["status"] == "not measured"
    assert data["tickets_with_problems"] == []


# --- report.md ----------------------------------------------------------------------------------


def test_the_report_marks_demo_check_and_unvalidated_judges() -> None:
    tickets = [ticket("T-0001"), ticket("T-0002", intent="fees_fx")]
    data = r.evaluate(tickets, [result("T-0001"), result("T-0002")], planned=2)
    text = r.render_report(config(), data, run_name="run-1")
    assert text.startswith("# Evaluation run run-1")
    assert "**Demo check, not a proof**" in text
    assert "unvalidated, same model family" in text
    assert "| key-fact coverage *(judged)* | 1.00 (2 tickets) | ≥ 0.85 | 0.75 | ✅ pass |" in text
    assert "| triage accuracy | 1 of 2 (50%; 95% CI " in text
    assert "| hit@5 | no ticket ran a KB search (0 of 2 tickets) |" in text
    assert "| escalation recall | 0 of 0; 0 missed | 1.00 | 1 miss | — not measured |" in text
    assert "| fees_fx | withdrawal_issue 1 |" in text
    assert "| withdrawal_issue | **withdrawal_issue** 1 |" in text
    assert "| T-0002 | intent withdrawal_issue (label fees_fx) |" in text
    assert "Judge prompts | judge_key_facts `v1`" in text
    assert "Fake models" not in text
    # No ticket or draft text in a committed report.
    assert "where is my money" not in text
    assert "Fact one" not in text


def test_the_report_of_a_fake_run_without_judges_or_problems() -> None:
    data = r.evaluate([ticket("T-0001")], [result("T-0001", judges=None)], planned=1)
    text = r.render_report(
        config(llm_mode="fake", judge_prompt_versions={}), data, run_name="run-2"
    )
    assert "**Fake models:**" in text
    assert "| Judge prompts | off |" in text
    assert "## Tickets with problems\n\nNone." in text


# --- retrieval (P5-05) --------------------------------------------------------------------------


def _rag_draft(*chunk_ids: str) -> DraftOut:
    chunks = [{"chunk_id": chunk_id, "text": "t"} for chunk_id in chunk_ids]
    return draft(evidence={"variant": "rag_hybrid", "chunks": chunks, "dropped_markers": []})


def test_hit_at_5_and_mrr_come_from_the_ranked_chunks_of_the_first_search() -> None:
    tickets = [
        ticket("T-0001"),  # gold withdrawal-times: rank 2
        ticket("T-0002", gold_doc_ids=["error-codes#ERR-2003"]),  # rank 6: not in the top 5
        ticket("T-0003"),  # no draft: no ranked list, left out
        ticket("T-0004", gold_doc_ids=[]),  # no gold document: left out
    ]
    results = [
        result("T-0001", draft=_rag_draft("fees#0", "withdrawal-times#3", "x#1")),
        result(
            "T-0002", draft=_rag_draft("a#0", "b#0", "c#0", "d#0", "e#0", "error-codes#ERR-2003")
        ),
        result("T-0003", draft=None, judges=None),
        result("T-0004", draft=_rag_draft("withdrawal-times#0")),
    ]
    assert r.ranked_ids(results[0]) == ["fees#0", "withdrawal-times#3", "x#1"]
    assert r.ranked_ids(results[2]) is None
    data = r.evaluate(tickets, results, planned=4)
    retrieval = data["metrics"]["retrieval"]
    assert retrieval["hit_at_5"]["count"] == 1
    assert retrieval["hit_at_5"]["total"] == 2
    assert retrieval["mrr"] == (1 / 2 + 1 / 6) / 2
    assert (retrieval["covered"], retrieval["excluded_no_kb_search"]) == (2, 2)
    text = r.render_report(config(variant="rag_hybrid"), data, run_name="rag")
    assert "| hit@5 | 1 of 2 (50%" in text


def test_older_results_without_verification_still_read() -> None:
    old = draft()
    assert old.groundedness is None
    assert old.unsupported_claims == []
