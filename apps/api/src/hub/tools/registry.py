"""The tools of the `support-tools` MCP server as the Hub sees them (API §7, ai-pipeline §4).

Each tool has an argument model with only the arguments a caller may choose. The identity
arguments (`customer_ref`, `ticket_id`) are never in it: `hub.tools.permissions` adds them from
the run state (ADR-0011). The argument models forbid extra fields, so a caller that sends an
identity argument anyway fails validation; permissions checks for that first and records it.

Only the four read tools of the core mock are given to the model (`MODEL_TOOLS`), under the
names of ai-pipeline §4. `search_kb` is not an MCP tool; the research loop adds it (P7-04).
"""

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from hub.domain.draft import IssueType
from hub.domain.enums import Urgency
from hub.domain.seed import TransactionRef, TransactionStatus, TransactionType

# Same bound as the server (`MAX_TRANSACTIONS` in support_tools.adapters.core).
MAX_TRANSACTIONS = 20


class Identity(StrEnum):
    """Arguments that only the Hub sets, from the run state."""

    CUSTOMER_REF = "customer_ref"
    TICKET_ID = "ticket_id"


IDENTITY_ARGUMENTS = frozenset(item.value for item in Identity)
# Set by the code that runs a side-effect tool, never by a caller's arguments.
IDEMPOTENCY_KEY = "idempotency_key"


class _Arguments(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class NoArguments(_Arguments):
    pass


class ListTransactionsArguments(_Arguments):
    type: TransactionType | None = Field(default=None, description="Only this transaction type")
    status: TransactionStatus | None = Field(default=None, description="Only this status")
    limit: int | None = Field(
        default=None, ge=1, le=MAX_TRANSACTIONS, description="How many, newest first (max 20)"
    )


class GetTransactionArguments(_Arguments):
    reference: TransactionRef = Field(description="Transaction reference, e.g. TX-000001")


class GetIssueArguments(_Arguments):
    key: str = Field(pattern=r"^TB-[1-9]\d{0,8}$")


class CreateIssueArguments(_Arguments):
    # Lengths as on the server (`jira_create_issue`).
    issue_type: IssueType
    summary: str = Field(min_length=1, max_length=255)
    description: str = Field(min_length=1, max_length=5000)


class SlackAlertArguments(_Arguments):
    urgency: Urgency
    text: str = Field(min_length=1, max_length=3000)


@dataclass(frozen=True)
class ToolSpec:
    name: str  # the MCP tool name
    arguments: type[_Arguments]
    injects: frozenset[Identity]
    side_effect: bool
    # The name and description the model sees; None: never given to a model.
    model_name: str | None = None
    model_description: str = ""


def _spec(
    name: str,
    arguments: type[_Arguments],
    injects: set[Identity],
    *,
    side_effect: bool = False,
    model_name: str | None = None,
    model_description: str = "",
) -> ToolSpec:
    return ToolSpec(
        name=name,
        arguments=arguments,
        injects=frozenset(injects),
        side_effect=side_effect,
        model_name=model_name,
        model_description=model_description,
    )


_CUSTOMER = {Identity.CUSTOMER_REF}

TOOLS: Mapping[str, ToolSpec] = {
    spec.name: spec
    for spec in (
        _spec(
            "core_get_customer",
            NoArguments,
            _CUSTOMER,
            model_name="get_account",
            model_description=(
                "The customer's account: tier, country, verification status, whether the "
                "account is restricted, limits and how much of each is used."
            ),
        ),
        _spec(
            "core_list_transactions",
            ListTransactionsArguments,
            _CUSTOMER,
            model_name="get_transactions",
            model_description=(
                "The customer's newest transactions, optionally filtered by type and status."
            ),
        ),
        _spec(
            "core_get_transaction",
            GetTransactionArguments,
            _CUSTOMER,
            model_name="get_transaction",
            model_description=(
                "One transaction of the customer with its status history and expected date."
            ),
        ),
        _spec(
            "core_get_cards",
            NoArguments,
            _CUSTOMER,
            model_name="get_cards",
            model_description="The customer's cards: last 4 digits, type, status, delivery.",
        ),
        _spec("jira_get_issue", GetIssueArguments, set()),
        _spec(
            "jira_create_issue",
            CreateIssueArguments,
            {Identity.CUSTOMER_REF, Identity.TICKET_ID},
            side_effect=True,
        ),
        _spec(
            "slack_post_escalation",
            SlackAlertArguments,
            {Identity.TICKET_ID},
            side_effect=True,
        ),
    )
}

MODEL_TOOLS: Mapping[str, ToolSpec] = {
    spec.model_name: spec for spec in TOOLS.values() if spec.model_name is not None
}


def model_tool_schema(spec: ToolSpec) -> dict[str, Any]:
    """The OpenAI-style function schema the model sees (through LiteLLM). It is built from the
    argument model, so it can never list an identity argument."""
    if spec.model_name is None:
        raise ValueError(f"{spec.name} is not a model tool")
    parameters = spec.arguments.model_json_schema()
    parameters.pop("title", None)
    return {
        "type": "function",
        "function": {
            "name": spec.model_name,
            "description": spec.model_description,
            "parameters": parameters,
        },
    }


def model_tool_schemas(names: frozenset[str]) -> list[dict[str, Any]]:
    """Schemas of the given model tool names, in a stable order (good for prompt caching)."""
    return [model_tool_schema(MODEL_TOOLS[name]) for name in sorted(names)]
