"""Data that crosses the gateway boundary: requests to a provider, answers, the call record.

The gateway works with these models only; `litellm` types never leave `hub/llm/gateway.py`.
"""

import hashlib
import json
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from hub.domain.enums import LlmCallContext, LlmCallStatus, LlmNode

type Message = dict[str, Any]


class CallContext(BaseModel):
    """Who makes a call. Given by the caller; written to every `llm_calls` row of the call."""

    model_config = ConfigDict(frozen=True)

    context: LlmCallContext
    node: LlmNode
    run_id: uuid.UUID | None = None
    prompt_version: str | None = None


class ProviderRequest(BaseModel):
    """One request to one model. Holds no API key: the key is added by the real provider."""

    model_config = ConfigDict(frozen=True)

    kind: Literal["chat", "embedding"]
    model: str
    messages: list[Message] = Field(default_factory=list)
    texts: list[str] = Field(default_factory=list)
    max_tokens: int | None = None
    timeout_s: float
    params: dict[str, JsonValue] = Field(default_factory=dict)
    schema_name: str | None = None
    json_schema: dict[str, Any] | None = None
    tools: list[dict[str, Any]] | None = None

    def fixture_key(self) -> str:
        """Stable id of the request for recorded responses: everything the model sees, and the
        model and parameters, but not the timeout."""
        content = self.model_dump(mode="json", exclude={"timeout_s"})
        canonical = json.dumps(content, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]


class Usage(BaseModel):
    """Token counts of one answer. `input_tokens` counts every prompt token, cache reads and
    cache writes included; `cached_tokens` are the cache reads."""

    model_config = ConfigDict(frozen=True)

    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    cached_tokens: int = Field(default=0, ge=0)
    cache_write_tokens: int = Field(default=0, ge=0)


class ToolCall(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    name: str
    arguments: str  # JSON text as the model wrote it; checked by the caller


class ProviderResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    content: str | None = None
    tool_calls: list[ToolCall] = Field(default_factory=list)
    vectors: list[list[float]] = Field(default_factory=list)
    usage: Usage = Field(default_factory=Usage)


class Completion(BaseModel):
    """The answer of `complete`. `fallback_used` is true when the fallback model answered."""

    model_config = ConfigDict(frozen=True)

    content: str | None
    tool_calls: list[ToolCall]
    model: str
    usage: Usage
    cost_usd: Decimal
    latency_ms: int
    fallback_used: bool


@dataclass(frozen=True)
class Structured[T: BaseModel]:
    """The answer of `complete_structured`: the validated value, and tokens, cost and latency
    summed over the first call and the repair call. `model` is the model of the valid answer."""

    value: T
    model: str
    usage: Usage
    cost_usd: Decimal
    latency_ms: int
    fallback_used: bool
    repaired: bool


class Embeddings(BaseModel):
    model_config = ConfigDict(frozen=True)

    vectors: list[list[float]]
    model: str
    usage: Usage
    cost_usd: Decimal
    latency_ms: int


class LlmCallRecord(BaseModel):
    """One `llm_calls` row, as the gateway hands it to the recorder."""

    model_config = ConfigDict(frozen=True)

    id: uuid.UUID
    run_id: uuid.UUID | None
    context: LlmCallContext
    node: LlmNode
    model: str
    prompt_version: str | None
    input_tokens: int
    output_tokens: int
    cached_tokens: int
    cost_usd: Decimal
    latency_ms: int
    status: LlmCallStatus
    error: str | None


# --- errors -----------------------------------------------------------------------------------


class LlmError(Exception):
    """Base of the gateway errors. Messages name roles and models, never request content."""


class ProviderError(LlmError):
    """A provider call failed. `transient` errors (timeout, rate limit, connection, 5xx) are
    retried; others go straight to the fallback model."""

    def __init__(self, kind: str, *, transient: bool, status_code: int | None = None) -> None:
        self.kind = kind
        self.transient = transient
        self.status_code = status_code
        super().__init__(self.summary)

    @property
    def summary(self) -> str:
        """What `llm_calls.error` stores: the error class and status, never provider text."""
        return self.kind if self.status_code is None else f"{self.kind} ({self.status_code})"


class OutboundBlockedError(LlmError):
    """G2 blocked the call and audited it."""


class OutboundCardDataError(OutboundBlockedError):
    """G2: the request contains card data. The call was blocked and audited."""


class OutboundIbanError(OutboundBlockedError):
    """G2: the request contains a raw IBAN, which G3 should have replaced. The call was blocked
    and audited; no card data was found."""


BlockedReason = Literal["card_data", "iban"]


def blocked_reason(entities: Mapping[str, int]) -> BlockedReason:
    """Why G2 blocked a call: card data whenever anything other than an IBAN was found (a
    failed scan counts as card data), so an IBAN never hides a card hit."""
    return "iban" if set(entities) == {"IBAN"} else "card_data"


def blocked_error(reason: BlockedReason) -> str:
    """What `llm_calls.error` stores for a G2 block."""
    return "blocked: G2" if reason == "card_data" else "blocked: G2 IBAN"


class LlmUnavailableError(LlmError):
    """Every attempt on the model and its fallback failed."""


class StructuredOutputError(LlmError):
    """The answer and the one repair answer both failed the schema (ADR-0004). The node hands
    the ticket to a human. Carries the schema name and the error count, never model text."""

    def __init__(self, schema_name: str, errors: int) -> None:
        self.schema_name = schema_name
        self.errors = errors
        super().__init__(f"{schema_name}: invalid after one repair ({errors} errors)")


class OutputCheckError(LlmError):
    """The check of an answer crashed (e.g. a custom validator raised something Pydantic does
    not wrap). A programming error; the paid call is recorded with status `error`. Names the
    exception class only, never its message, which may quote the answer."""


class ToolsNotAllowedError(LlmError):
    """Tools were passed to a role that is not configured for tools."""


class FixtureMissingError(LlmError):
    """Fake mode has no recorded response for this request. Never falls back to a real call."""
