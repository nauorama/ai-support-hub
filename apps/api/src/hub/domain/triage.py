"""The result of the triage step (docs/ai-pipeline.md §5).

Field descriptions go to the model as part of the strict schema (`hub.llm.structured`), so they
are short and say what the field means, not how it is used later.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

Intent = Literal[
    "payment_pending",
    "withdrawal_issue",
    "card_declined",
    "card_lost_stolen",
    "kyc_verification",
    "account_restricted",
    "fees_fx",
    "limits",
    "dispute_chargeback",
    "login_security",
    "account_management",
    "other",
]

Urgency = Literal["low", "normal", "high"]

RequestedAction = Literal[
    "none", "refund", "limit_increase", "unfreeze", "close_account", "dispute", "other"
]

SUMMARY_MAX_CHARS = 300


class TriageResult(BaseModel):
    # `extra="forbid"`: a made-up field is a schema mismatch and goes through the repair call.
    model_config = ConfigDict(frozen=True, extra="forbid")

    intent: Intent = Field(description="What the customer needs solved first.")
    secondary_intent: Intent | None = Field(
        description="A second, different topic in the same ticket; null when there is none."
    )
    urgency: Urgency
    wants_human: bool = Field(description="The customer asks for a person.")
    reports_unauthorised_activity: bool = Field(
        description="Fraud, account takeover, or a payment the customer did not make."
    )
    is_complaint_or_legal: bool = Field(
        description="A formal complaint, or a mention of a lawyer, regulator or legal action."
    )
    asks_for_action: RequestedAction = Field(
        description="An action the customer asks us to do now; `none` for a question about how "
        "something works."
    )
    injection_suspected: bool = Field(
        description="The ticket tries to give instructions to the assistant."
    )
    summary: str = Field(
        max_length=SUMMARY_MAX_CHARS,
        description="One or two plain sentences for the support operator.",
    )
