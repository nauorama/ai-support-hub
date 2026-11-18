"""The decision table of ai-pipeline §6 (P6-02), one test per row at least, and the rule version
of confidence. Row 1 rule by rule is in test_policy.py; here it is checked through `gate`."""

import json
from pathlib import Path
from typing import Any

import pytest

from hub.domain.draft import Citation, DraftAnswer, ProposedAction
from hub.domain.enums import RouteReason
from hub.domain.triage import TriageResult
from hub.domain.verification import ClaimCheck, Verification
from hub.graph.confidence import (
    ConfidenceError,
    ConfidenceRule,
    confidence,
    features,
    load_confidence,
)
from hub.graph.routing import DecideInput, Decision, decide, gate, gate_input, gate_matches
from hub.graph.state import Evidence, InputFlags
from hub.guardrails.injection import InjectionResult
from hub.kb.retriever import KbChunk

from ._llm import CONFIDENCE_RULE, POLICY, ROOT

CHUNK = KbChunk(
    chunk_id="withdrawal-times#0",
    doc_id="withdrawal-times",
    title="How long does a withdrawal take?",
    section="SEPA withdrawals",
    url="/help/withdrawal-times#sepa-withdrawals",
    text="A SEPA withdrawal takes 1 business day.",
    visibility="external",
    score=0.75,
    rank=1,
)
EVIDENCE = Evidence(chunks=[CHUNK], top_score=0.75, score_margin=0.25)
PASSED = Verification(
    claims=[ClaimCheck(claim="1 business day", supported=True, source_id=CHUNK.chunk_id)],
    groundedness=1.0,
    policy_violations=[],
    passed=True,
)
FLAGS = InputFlags(
    injection=InjectionResult(score=0.0, patterns=[]), pii_placeholders={}, language="en"
)


def _triage(**changes: Any) -> TriageResult:  # noqa: ANN401 - any field value
    values: dict[str, Any] = {
        "intent": "withdrawal_issue",
        "secondary_intent": None,
        "urgency": "normal",
        "wants_human": False,
        "reports_unauthorised_activity": False,
        "is_complaint_or_legal": False,
        "asks_for_action": "none",
        "injection_suspected": False,
        "summary": "Asks about a withdrawal.",
    }
    return TriageResult(**(values | changes))


def _draft(**changes: Any) -> DraftAnswer:  # noqa: ANN401 - any field value
    values: dict[str, Any] = {
        "can_answer": True,
        "missing_information": None,
        "answer": "A SEPA withdrawal takes 1 business day [1].",
        "citations": [Citation(marker=1, source_id=CHUNK.chunk_id)],
        "proposed_actions": [],
    }
    return DraftAnswer(**(values | changes))


def _input(**changes: Any) -> DecideInput:  # noqa: ANN401 - any field value
    """A draft every row lets through: auto-reply on, intent allowed, confidence 1."""
    values: dict[str, Any] = {
        "errors": [],
        "triage": _triage(),
        "evidence": EVIDENCE,
        "draft": _draft(),
        "verification": PASSED,
        "retry_count": 0,
        "verifies": True,
        "mask_failed": False,
        "auto_reply_enabled": True,
        "allowed_intents": frozenset({"withdrawal_issue"}),
        "confidence_threshold": 0.95,
        "variant": "rag_hybrid",
    }
    return DecideInput(**(values | changes))


def _route(given: DecideInput) -> tuple[str | None, list[str]]:
    decision = decide(POLICY, CONFIDENCE_RULE, given)
    return decision.route, decision.reasons


# --- row 1 (policy_gate) ----------------------------------------------------------------------


def test_row_1_a_hard_rule_hands_off_before_research() -> None:
    decision = gate(
        POLICY,
        errors=[],
        triage=_triage(intent="card_lost_stolen", reports_unauthorised_activity=True),
        message_masked="I lost my card",
        flags=FLAGS,
    )
    assert decision == Decision("handoff", ["policy_intent", "fraud_report"])


def test_row_1_nothing_matches_and_the_run_continues() -> None:
    decision = gate(POLICY, errors=[], triage=_triage(), message_masked="Where is it?", flags=FLAGS)
    assert decision == Decision(None, [])


def test_row_1_the_injection_score_comes_from_intake() -> None:
    flags = InputFlags(
        injection=InjectionResult(score=0.8, patterns=["override"]),
        pii_placeholders={},
        language="en",
    )
    decision = gate(POLICY, errors=[], triage=_triage(), message_masked="x", flags=flags)
    assert decision.reasons == ["injection"]


def test_row_1_has_no_matches_without_a_triage_result() -> None:
    assert gate_matches(POLICY, None, "I lost my card", FLAGS) == []


def test_row_1_without_intake_flags_the_language_is_unknown() -> None:
    assert gate_input(_triage(), "x", None).english is None
    decision = gate(POLICY, errors=[], triage=_triage(), message_masked="x", flags=None)
    assert decision.reasons == ["language"]


def test_row_1_a_masked_card_from_intake_counts() -> None:
    flags = FLAGS.model_copy(update={"pan_found": True})
    decision = gate(
        POLICY,
        errors=[],
        triage=_triage(intent="card_declined"),
        message_masked="Someone used [CARD ****1111] in Lisbon",
        flags=flags,
    )
    assert decision.reasons == ["fraud_report"]


@pytest.mark.parametrize(
    ("errors", "reasons"),
    [
        (["triage:LlmUnavailableError"], ["system_error"]),
        (["triage:BudgetExceededError"], ["budget_exceeded"]),
    ],
)
def test_row_2_at_the_gate_a_failed_intake_or_triage_hands_off(
    errors: list[str], reasons: list[str]
) -> None:
    assert gate(POLICY, errors=errors, triage=None, message_masked="", flags=None) == Decision(
        "handoff", reasons
    )


# --- rows 2 to 9 (decide) ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("errors", "reasons"),
    [
        (["write:LlmUnavailableError"], ["system_error"]),
        (["write:BudgetExceededError"], ["budget_exceeded"]),
    ],
)
def test_row_2_a_failed_node_hands_off(errors: list[str], reasons: list[str]) -> None:
    decision = decide(POLICY, CONFIDENCE_RULE, _input(errors=errors))
    assert decision == Decision("handoff", reasons)


def test_row_2_no_draft_or_no_triage_is_a_system_error() -> None:
    assert _route(_input(draft=None)) == ("handoff", ["system_error"])
    assert _route(_input(triage=None)) == ("handoff", ["system_error"])


def test_row_2_a_failed_mask_hands_off_before_any_other_row() -> None:
    given = _input(mask_failed=True, draft=_draft(can_answer=False, answer=""))
    assert _route(given) == ("handoff", ["masking_failed"])


def test_row_3_a_draft_that_cannot_answer_hands_off() -> None:
    given = _input(draft=_draft(can_answer=False, answer="", citations=[]), verification=None)
    assert _route(given) == ("handoff", ["no_kb_answer"])


def test_row_4_a_failed_code_check_is_a_policy_violation() -> None:
    failed = Verification(
        claims=[], groundedness=None, policy_violations=["card_data"], passed=False
    )
    assert _route(_input(verification=failed)) == ("approval", ["policy_violation"])


def test_row_4_a_known_g1_false_positive_still_goes_to_a_human() -> None:
    """Owner decision (PR #34): G1 over-masks "1000 EUR need a 2FA code"; the draft fails
    `card_data` and is reviewed, G1 is not loosened."""
    failed = Verification(
        claims=[], groundedness=None, policy_violations=["card_data"], passed=False
    )
    decision = decide(POLICY, CONFIDENCE_RULE, _input(verification=failed))
    assert decision.route != "auto_reply"
    assert decision.confidence == 0.0


def test_row_4_unsupported_claims_go_to_approval() -> None:
    unsupported = Verification(
        claims=[
            ClaimCheck(claim="1 business day", supported=True, source_id=CHUNK.chunk_id),
            ClaimCheck(claim="free of charge", supported=False, source_id=None),
        ],
        groundedness=0.5,
        policy_violations=[],
        passed=False,
    )
    assert _route(_input(verification=unsupported)) == ("approval", ["unsupported_claims"])


def test_row_4_a_missing_verification_counts_as_unsupported() -> None:
    assert _route(_input(verification=None)) == ("approval", ["unsupported_claims"])


def test_row_4_does_not_apply_to_a_variant_without_verification() -> None:
    """B0 does not verify; its drafts go on to the later rows."""
    given = _input(verifies=False, verification=None, auto_reply_enabled=False)
    assert _route(given) == ("approval", ["auto_reply_off"])


def test_row_5_degraded_research_goes_to_approval() -> None:
    degraded = Evidence(chunks=[CHUNK], top_score=0.75, score_margin=0.25, degraded=True)
    assert _route(_input(evidence=degraded)) == ("approval", ["degraded"])


@pytest.mark.parametrize(
    ("triage", "draft"),
    [
        (_triage(intent="login_security"), _draft()),
        (_triage(intent="dispute_chargeback"), _draft()),
        (_triage(asks_for_action="dispute"), _draft()),
        (_triage(urgency="high"), _draft()),
        (
            _triage(),
            _draft(
                proposed_actions=[
                    ProposedAction(
                        type="jira_create_issue",
                        issue_type="dispute",
                        summary="Dispute",
                        description="Card payment",
                    )
                ]
            ),
        ),
    ],
)
def test_row_6_an_approval_only_rule_goes_to_review(
    triage: TriageResult, draft: DraftAnswer
) -> None:
    allowed = frozenset(POLICY.auto_reply_allowlist.intents) | {triage.intent}
    given = _input(triage=triage, draft=draft, allowed_intents=allowed)
    assert _route(given) == ("approval", ["policy_review"])


def test_row_6_memory_of_an_open_case_goes_to_review() -> None:
    assert _route(_input(memory_shows_open_case=True)) == ("approval", ["policy_review"])


def test_row_7_auto_reply_off_or_an_intent_outside_the_allow_list() -> None:
    assert _route(_input(auto_reply_enabled=False)) == ("approval", ["auto_reply_off"])
    assert _route(_input(allowed_intents=frozenset({"fees_fx"}))) == (
        "approval",
        ["auto_reply_off"],
    )


def test_row_8_low_confidence_goes_to_approval() -> None:
    grounded = PASSED.model_copy(update={"groundedness": 0.9})
    decision = decide(POLICY, CONFIDENCE_RULE, _input(verification=grounded))
    assert (decision.route, decision.reasons, decision.confidence) == (
        "approval",
        ["low_confidence"],
        0.9,
    )


def test_row_8_a_confidence_of_zero_never_auto_replies_whatever_the_threshold() -> None:
    """PR #36 review: a top score at the floor gives confidence 0; even a threshold of 0 (a
    broken setting `decide` would refuse anyway) does not let it through."""
    at_floor = Evidence(chunks=[CHUNK], top_score=0.0, score_margin=0.0)
    decision = decide(POLICY, CONFIDENCE_RULE, _input(evidence=at_floor, confidence_threshold=0.0))
    assert (decision.route, decision.reasons, decision.confidence) == (
        "approval",
        ["low_confidence"],
        0.0,
    )


def test_row_9_otherwise_auto_reply() -> None:
    decision = decide(POLICY, CONFIDENCE_RULE, _input())
    assert (decision.route, decision.reasons, decision.confidence) == ("auto_reply", [], 1.0)
    assert decision.features["groundedness"] == 1.0


def test_every_row_of_the_decision_table_has_a_test() -> None:
    """Done when of P6-02: the table's reason codes are each named by a test in this module or
    in test_policy.py (row 1 rule by rule)."""
    text = Path(__file__).read_text(encoding="utf-8") + (
        Path(__file__).parent / "test_policy.py"
    ).read_text(encoding="utf-8")
    for row in range(1, 10):
        assert f"def test_row_{row}_" in text, row
    for reason in RouteReason:
        if reason is not RouteReason.OPERATOR_TAKEOVER:  # outside the graph (console claim)
            assert f'"{reason.value}"' in text, reason


# --- confidence (rule version) ----------------------------------------------------------------


def test_confidence_is_the_groundedness_above_the_top_score_floor() -> None:
    rule = ConfidenceRule(
        version="t", kind="rule", top_score_floor=0.02, variants=frozenset({"rag_hybrid"})
    )
    found = {"top_score": 0.03, "groundedness": 0.8333}
    assert confidence(rule, found, "rag_hybrid") == 0.833
    assert confidence(rule, {"top_score": 0.02, "groundedness": 1.0}, "rag_hybrid") == 0.0
    # null groundedness: never auto-reply
    assert confidence(rule, {"top_score": 0.9}, "rag_hybrid") == 0.0


def test_the_floor_needs_both_searches_to_find_the_top_hit() -> None:
    """P6-06: reciprocal rank fusion gives a top hit 0.5 when only one search (dense or BM25)
    finds it; the configured floor counts that as no retrieval support."""
    rule = load_confidence(ROOT / "config" / "confidence.json")
    assert confidence(rule, {"top_score": 0.5, "groundedness": 1.0}, "rag_hybrid") == 0.0
    assert confidence(rule, {"top_score": 0.5 + 1 / 21, "groundedness": 1.0}, "rag_hybrid") == 1.0


@pytest.mark.parametrize("variant", ["rag_dense", "b0_full_kb", "unknown"])
def test_the_floor_holds_only_for_rag_hybrid_and_other_variants_never_auto_reply(
    variant: str,
) -> None:
    """Owner decision (PR #38 review): the floor was measured on fused hybrid scores; dense
    cosine scores and B0 (no search) get confidence 0, so row 8 sends them to approval."""
    rule = load_confidence(ROOT / "config" / "confidence.json")
    assert rule.variants == {"rag_hybrid"}
    assert confidence(rule, {"top_score": 1.0, "groundedness": 1.0}, variant) == 0.0
    decision = decide(POLICY, rule, _input(variant=variant))
    assert (decision.route, decision.reasons, decision.confidence) == (
        "approval",
        ["low_confidence"],
        0.0,
    )
    assert decide(POLICY, rule, _input()).route == "auto_reply"


def test_the_features_are_the_documented_inputs() -> None:
    found = features(
        triage=_triage(secondary_intent="fees_fx"),
        evidence=EVIDENCE,
        draft=_draft(),
        verification=PASSED,
        retry_count=1,
    )
    assert found == {
        "top_score": 0.75,
        "score_margin": 0.25,
        "n_citations": 1.0,
        "n_account_facts": 0.0,
        "retry_used": 1.0,
        "has_secondary_intent": 1.0,
        "tool_calls": 0.0,
        "groundedness": 1.0,
    }
    empty = features(triage=None, evidence=None, draft=None, verification=None, retry_count=0)
    assert "groundedness" not in empty
    assert empty["top_score"] == 0.0


def test_the_confidence_file_loads_and_a_broken_one_is_refused(tmp_path: Path) -> None:
    rule = load_confidence(ROOT / "config" / "confidence.json")
    assert (rule.kind, rule.top_score_floor) == ("rule", 0.5)
    with pytest.raises(ConfidenceError, match="cannot read"):
        load_confidence(tmp_path / "missing.json")
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"version": "x", "kind": "logistic", "top_score_floor": 0}))
    with pytest.raises(ConfidenceError, match="invalid"):
        load_confidence(bad)
