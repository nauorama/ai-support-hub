"""Schemas of the evaluation files in `data/eval/` (docs/eval-plan.md §3).

One JSON object per line. Unknown fields are refused, so a typo in a field name fails instead of
being ignored. Rules inside one item live here; rules across items and files (unique ids, known
customers and KB documents, counts) are in `hub_evals.data_check`.
"""

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from hub.domain.enums import Intent

Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
CustomerRef = Annotated[str, Field(pattern=r"^CUS-\d{4}$")]
Tag = Annotated[str, Field(pattern=r"^[a-z0-9]+(_[a-z0-9]+)*$")]
# An article id (matched against `kb_chunks.doc_id`) or an error-code chunk id
# (`error-codes#ERR-2003`, matched against `kb_chunks.id`); data model §2.
GoldDocId = Annotated[str, Field(pattern=r"^(?:[a-z0-9]+(-[a-z0-9]+)*|error-codes#ERR-\d{4})$")]

TICKET_COUNT = 20
MIN_HARD_TICKETS = 6
MIN_MUST_HANDOFF = 3
ATTACKS_PER_CATEGORY = 2


class ExpectedReason(StrEnum):
    """Reason codes a dataset author can predict from the ticket alone (AI pipeline §6).

    The codes of decision-table row 1 (hard rules), `no_kb_answer` (row 3) and `policy_review`
    (row 6). Codes that depend on the run rather than the ticket (`budget_exceeded`,
    `system_error`, `degraded`, `unsupported_claims`, `policy_violation`, `auto_reply_off`,
    `low_confidence`) are not labels. P6-02 adds the reason-code enum to the domain; this list
    then becomes a subset of it.
    """

    POLICY_INTENT = "policy_intent"
    CUSTOMER_REQUEST = "customer_request"
    FRAUD_REPORT = "fraud_report"
    COMPLAINT = "complaint"
    INJECTION = "injection"
    ACTION_REQUEST = "action_request"
    LANGUAGE = "language"
    RESTRICTED_ACCOUNT = "restricted_account"
    NO_KB_ANSWER = "no_kb_answer"
    POLICY_REVIEW = "policy_review"


# Row 1 of the decision table: a hard rule sends the ticket to a human (`must_handoff`).
HARD_RULE_REASONS = frozenset(
    {
        ExpectedReason.POLICY_INTENT,
        ExpectedReason.CUSTOMER_REQUEST,
        ExpectedReason.FRAUD_REPORT,
        ExpectedReason.COMPLAINT,
        ExpectedReason.INJECTION,
        ExpectedReason.ACTION_REQUEST,
        ExpectedReason.LANGUAGE,
        ExpectedReason.RESTRICTED_ACCOUNT,
    }
)


class HardCase(StrEnum):
    """The hard cases of eval plan §3. A `hard` ticket carries at least one of these tags."""

    TWO_INTENTS = "two_intents"
    NO_KB_ANSWER = "no_kb_answer"
    CARD_NUMBER = "card_number"
    COMPLAINT = "complaint"
    WANTS_HUMAN = "wants_human"
    FORBIDDEN_ACTION = "forbidden_action"
    NON_ENGLISH = "non_english"
    TYPOS = "typos"
    VERY_SHORT = "very_short"
    FOLLOW_UP = "follow_up"
    ACCOUNT_DATA = "account_data"


class AttackCategory(StrEnum):
    """The 8 attack categories of eval plan §3, exactly 2 attacks each."""

    INSTRUCTION_OVERRIDE = "instruction_override"
    PROMPT_EXTRACTION = "prompt_extraction"
    DATA_EXFILTRATION = "data_exfiltration"
    INTERNAL_EXTRACTION = "internal_extraction"
    FORBIDDEN_PROMISE = "forbidden_promise"
    TOOL_ABUSE = "tool_abuse"
    INDIRECT_INJECTION = "indirect_injection"
    ENCODING_TRICKS = "encoding_tricks"


class _Item(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Flags(_Item):
    wants_human: bool
    reports_unauthorised_activity: bool
    is_complaint_or_legal: bool


class HistoryMessage(_Item):
    """An earlier message of the conversation; `message` is the newest customer message."""

    # Plain strings, not enum members: the error message then lists the values themselves.
    role: Literal["customer", "assistant", "operator"]
    text: Text


class Ticket(_Item):
    id: Annotated[str, Field(pattern=r"^T-\d{4}$")]
    customer_ref: CustomerRef
    message: Text
    history: list[HistoryMessage]
    intent: Intent
    secondary_intent: Intent | None
    flags: Flags
    gold_doc_ids: list[GoldDocId]
    needs_account_data: bool
    key_facts: list[Text]
    should_escalate: bool
    must_handoff: bool
    expected_reason: ExpectedReason | None
    difficulty: Literal["normal", "hard"]
    tags: list[Tag]

    @model_validator(mode="after")
    def _consistent(self) -> "Ticket":
        problems = _ticket_problems(self)
        if problems:
            raise ValueError("; ".join(problems))
        return self


def _ticket_problems(ticket: Ticket) -> list[str]:
    problems: list[str] = []
    if ticket.secondary_intent == ticket.intent:
        problems.append("secondary_intent must differ from intent")
    for field, values in (
        ("gold_doc_ids", ticket.gold_doc_ids),
        ("key_facts", ticket.key_facts),
        ("tags", ticket.tags),
    ):
        if len(set(values)) != len(values):
            problems.append(f"{field} must not repeat")
    if ticket.must_handoff and not ticket.should_escalate:
        problems.append("must_handoff requires should_escalate")
    if ticket.should_escalate != (ticket.expected_reason is not None):
        problems.append("expected_reason is set exactly when should_escalate is true")
    if (
        ticket.must_handoff
        and ticket.expected_reason is not None
        and ticket.expected_reason not in HARD_RULE_REASONS
    ):
        problems.append(
            f"a must_handoff ticket needs a hard-rule reason, not {ticket.expected_reason}"
        )
    # Row 1 is checked first and always hands off, so a hard-rule reason means must_handoff.
    if not ticket.must_handoff and ticket.expected_reason in HARD_RULE_REASONS:
        problems.append(f"hard-rule reason {ticket.expected_reason} requires must_handoff")
    if ticket.difficulty == "hard" and not set(ticket.tags) & set(HardCase):
        problems.append(f"a hard ticket needs a hard-case tag ({', '.join(sorted(HardCase))})")
    if HardCase.FOLLOW_UP in ticket.tags and not ticket.history:
        problems.append("tag follow_up requires history")
    if HardCase.TWO_INTENTS in ticket.tags and ticket.secondary_intent is None:
        problems.append("tag two_intents requires secondary_intent")
    if HardCase.NO_KB_ANSWER in ticket.tags and not ticket.should_escalate:
        problems.append("tag no_kb_answer requires should_escalate")
    return problems


class CanaryCheck(_Item):
    """Success when this string reaches the attacker (planted in P8-01)."""

    type: Literal["canary"]
    value: Text


class ToolCallCheck(_Item):
    """Success when this tool was called during the run."""

    type: Literal["tool_call"]
    tool: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]*$")]


class AutoReplyPhraseCheck(_Item):
    """Success when the route is auto-reply and the answer contains this phrase."""

    type: Literal["auto_reply_phrase"]
    value: Text


class JudgeCheck(_Item):
    """No deterministic check exists: the attack judge decides with this strict rubric."""

    type: Literal["judge"]
    rubric: Text


SuccessCheck = Annotated[
    CanaryCheck | ToolCallCheck | AutoReplyPhraseCheck | JudgeCheck,
    Field(discriminator="type"),
]


class Attack(_Item):
    id: Annotated[str, Field(pattern=r"^A-\d{3}$")]
    customer_ref: CustomerRef
    message: Text
    category: AttackCategory
    goal: Text
    success_check: SuccessCheck
