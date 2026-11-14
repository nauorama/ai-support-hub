"""Text enums of the data model (docs/data-model.md §1).

The database stores each one as text with a `CHECK` constraint built from these classes
(`hub/db/models.py`), so the code and the schema cannot list different values.
"""

from enum import StrEnum


class TicketStatus(StrEnum):
    PROCESSING = "processing"
    PENDING_APPROVAL = "pending_approval"
    HANDED_OFF = "handed_off"
    ANSWERED = "answered"
    CLOSED = "closed"


class Channel(StrEnum):
    CHAT = "chat"


class Intent(StrEnum):
    """The 12 intents of triage (docs/ai-pipeline.md §5)."""

    PAYMENT_PENDING = "payment_pending"
    WITHDRAWAL_ISSUE = "withdrawal_issue"
    CARD_DECLINED = "card_declined"
    CARD_LOST_STOLEN = "card_lost_stolen"
    KYC_VERIFICATION = "kyc_verification"
    ACCOUNT_RESTRICTED = "account_restricted"
    FEES_FX = "fees_fx"
    LIMITS = "limits"
    DISPUTE_CHARGEBACK = "dispute_chargeback"
    LOGIN_SECURITY = "login_security"
    ACCOUNT_MANAGEMENT = "account_management"
    OTHER = "other"


class Urgency(StrEnum):
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"


class Route(StrEnum):
    AUTO_REPLY = "auto_reply"
    APPROVAL = "approval"
    HANDOFF = "handoff"


class Resolution(StrEnum):
    AI = "ai"
    AI_APPROVED = "ai_approved"
    AI_EDITED = "ai_edited"
    HUMAN = "human"


class CustomerTier(StrEnum):
    STARTER = "starter"
    VERIFIED = "verified"
    BUSINESS = "business"


class OperatorRole(StrEnum):
    OPERATOR = "operator"
    LEAD = "lead"
    COMPLIANCE = "compliance"
    ADMIN = "admin"


class MessageRole(StrEnum):
    CUSTOMER = "customer"
    ASSISTANT = "assistant"
    OPERATOR = "operator"
    SYSTEM = "system"


class FeedbackRating(StrEnum):
    UP = "up"
    DOWN = "down"


class SettingKey(StrEnum):
    """Runtime settings changed from the console (data model §1, `settings`)."""

    AUTO_REPLY_ENABLED = "auto_reply_enabled"
    CONFIDENCE_THRESHOLD = "confidence_threshold"
    DAILY_BUDGET_USD = "daily_budget_usd"
    INTENT_ALLOWLIST = "intent_allowlist"


class ActorType(StrEnum):
    AI = "ai"
    OPERATOR = "operator"
    CUSTOMER = "customer"
    SYSTEM = "system"


class LlmCallContext(StrEnum):
    """Why a model call was made (data model §1, `llm_calls.context`)."""

    ONLINE = "online"
    EVAL = "eval"
    MEMORY = "memory"
    RECORD = "record"  # `make record-llm`: refreshing the fake-mode recordings


class LlmNode(StrEnum):
    """The step that made a model call (data model §1, `llm_calls.node`)."""

    TRIAGE = "triage"
    RESEARCH = "research"
    WRITE = "write"
    VERIFY = "verify"
    HANDOFF_PREP = "handoff_prep"
    MEMORY_SUMMARY = "memory_summary"
    JUDGE = "judge"
    EMBEDDING = "embedding"


class LlmCallStatus(StrEnum):
    """`retry`: this attempt failed and the gateway tries again; `error`: it failed for good
    (the gateway may still go on to the fallback model); `invalid_output`: the answer did not
    match the schema (P3-02)."""

    OK = "ok"
    RETRY = "retry"
    ERROR = "error"
    INVALID_OUTPUT = "invalid_output"
