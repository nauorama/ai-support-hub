"""Routing: the decision table of docs/ai-pipeline.md §6 as pure functions (P6-02).

Order: hard rules → system state → confidence; the first match decides.

- `gate`: `policy_gate`, before research. Row 2 when intake or triage failed (nothing else can be
  judged without the triage), else row 1 (`hub.guardrails.policy.hard_rules`): every matching
  rule's reason goes into `route_reasons`, in row-1 order, and the first one decides.
- `decide`: rows 2 to 9 after `verify`, one reason. A failed G1 mask on model-written text is
  row 2 (`masking_failed`): the ticket goes to a human, never to auto-reply or approval (P3-05a
  review). Row 4 routes on the verification the run holds in memory: `policy_violation` when a
  code check failed (the codes are in the `verify.done` audit record), `unsupported_claims` when
  the LLM check found any or did not run. Rows 7 and 8 use the console settings, read by the node.

Known false handoffs (owner decision, PR #34): the `card_data` code check reuses G1, which
over-masks a number next to a trigger word or a date-like pair ("Transfers over 1000 EUR need a
2FA code [1]."), so such a correct draft fails row 4 with `policy_violation` and goes to a human.
G1 is not loosened: over-masking is the safe side.
"""

from dataclasses import dataclass, field

from hub.domain.draft import DraftAnswer
from hub.domain.enums import RouteReason
from hub.domain.triage import TriageResult
from hub.domain.verification import Verification
from hub.graph.confidence import ConfidenceRule, confidence, features
from hub.graph.context import failure_reasons
from hub.graph.state import Evidence, InputFlags, RouteName
from hub.guardrails.policy import (
    GateInput,
    Policy,
    RuleMatch,
    hard_rules,
    needs_review,
    reasons,
)

ENGLISH = "en"


@dataclass(frozen=True)
class Decision:
    route: RouteName | None  # None: no hard rule matched, the run continues to research
    reasons: list[str]
    confidence: float | None = None
    features: dict[str, float] = field(default_factory=dict)


def gate_input(triage: TriageResult, message_masked: str, flags: InputFlags | None) -> GateInput:
    return GateInput(
        triage=triage,
        message_masked=message_masked,
        english=None if flags is None or flags.language is None else flags.language == ENGLISH,
        pan_found=bool(flags and flags.pan_found),
        injection_score=flags.injection.score if flags else 0.0,
    )


def gate_matches(
    policy: Policy, triage: TriageResult | None, message_masked: str, flags: InputFlags | None
) -> list[RuleMatch]:
    """Row 1's matches; none without a triage result."""
    if triage is None:
        return []
    return hard_rules(policy, gate_input(triage, message_masked, flags))


def gate(
    policy: Policy,
    *,
    errors: list[str],
    triage: TriageResult | None,
    message_masked: str,
    flags: InputFlags | None,
) -> Decision:
    if errors or triage is None:
        return Decision("handoff", failure_reasons(errors))
    matched = reasons(gate_matches(policy, triage, message_masked, flags))
    return Decision("handoff", matched) if matched else Decision(None, [])


@dataclass(frozen=True)
class DecideInput:
    errors: list[str]
    triage: TriageResult | None
    evidence: Evidence | None
    draft: DraftAnswer | None
    verification: Verification | None
    retry_count: int
    # Whether this variant verifies (B0 does not: row 4 does not apply to it).
    verifies: bool
    # A G1 mask of a model-written string of this run failed.
    mask_failed: bool
    # Console settings (data model, `settings`); `allowed_intents` already limited to the policy.
    auto_reply_enabled: bool
    allowed_intents: frozenset[str]
    confidence_threshold: float
    memory_shows_open_case: bool = False  # P6-05


def _one(route: RouteName, reason: RouteReason, found: dict[str, float], score: float) -> Decision:
    return Decision(route, [reason.value], score, found)


def decide(policy: Policy, rule: ConfidenceRule, given: DecideInput) -> Decision:
    if given.errors:
        return Decision("handoff", failure_reasons(given.errors))
    draft, triage = given.draft, given.triage
    if draft is None or triage is None:
        return Decision("handoff", [RouteReason.SYSTEM_ERROR.value])
    found = features(
        triage=triage,
        evidence=given.evidence,
        draft=draft,
        verification=given.verification,
        retry_count=given.retry_count,
    )
    score = confidence(rule, found)
    if given.mask_failed:
        return _one("handoff", RouteReason.MASKING_FAILED, found, score)
    if not draft.can_answer:
        return _one("handoff", RouteReason.NO_KB_ANSWER, found, score)
    verification = given.verification
    if given.verifies and (verification is None or not verification.passed):
        violated = verification is not None and verification.policy_violations
        reason = RouteReason.POLICY_VIOLATION if violated else RouteReason.UNSUPPORTED_CLAIMS
        return _one("approval", reason, found, score)
    if given.evidence is not None and given.evidence.degraded:
        return _one("approval", RouteReason.DEGRADED, found, score)
    if needs_review(
        policy,
        triage,
        has_proposed_actions=bool(draft.proposed_actions),
        memory_shows_open_case=given.memory_shows_open_case,
    ):
        return _one("approval", RouteReason.POLICY_REVIEW, found, score)
    if not given.auto_reply_enabled or triage.intent not in given.allowed_intents:
        return _one("approval", RouteReason.AUTO_REPLY_OFF, found, score)
    if score < given.confidence_threshold:
        return _one("approval", RouteReason.LOW_CONFIDENCE, found, score)
    return Decision("auto_reply", [], score, found)
