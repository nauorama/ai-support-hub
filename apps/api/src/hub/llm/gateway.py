"""The only way to a model (ADR-0003; AGENTS.md never-do 2). The only module that imports
`litellm` (import-linter contract `only-gateway-imports-litellm`).

`complete` and `embed` ask for a role; `config/models.yaml` names its model and fallback. Every
call: outbound card scan (G2) → model, with a timeout and 2 retries with backoff on transient
errors → fallback model, the same way → `LlmUnavailableError`. Every attempt writes one
`llm_calls` row with tokens, cost, latency, model and prompt version, failures included.

`LLM_MODE=fake` swaps the provider for `FixtureProvider`, which answers from recorded responses
(`make record-llm`) and never makes a real call. Tests pass a scripted provider.
"""

import asyncio
import json
import os
import time
from collections import Counter
from collections.abc import Awaitable, Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel, ValidationError
from uuid_utils.compat import uuid7

from hub.config import Settings
from hub.domain.enums import LlmCallStatus
from hub.guardrails.masking import mask
from hub.llm.config import ConfigError, ModelsConfig, RoleConfig, load_models
from hub.llm.pricing import Pricing, load_pricing
from hub.llm.recorder import CallRecorder
from hub.llm.schemas import (
    CallContext,
    Completion,
    Embeddings,
    FixtureMissingError,
    LlmCallRecord,
    LlmUnavailableError,
    Message,
    OutboundCardDataError,
    ProviderError,
    ProviderRequest,
    ProviderResponse,
    ToolCall,
    ToolsNotAllowedError,
    Usage,
)

# Two switches LiteLLM reads at import (checked in LiteLLM 1.108.6, `litellm/__init__.py` and
# `get_model_cost_map.py`):
# - LITELLM_MODE: in its default "DEV" mode LiteLLM calls `load_dotenv()`, which copies the
#   nearest `.env` (searched upward through parent folders) into `os.environ`. Settings are read
#   only by hub/config.py; a `.env` copied into the environment would override and hide it.
#   Outside the proxy server, "PRODUCTION" changes nothing else.
# - LITELLM_LOCAL_MODEL_COST_MAP: otherwise LiteLLM fetches its price map from GitHub at import.
#   The Hub needs no network at import and takes prices from config/pricing.yaml.
os.environ["LITELLM_MODE"] = "PRODUCTION"
os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"
import litellm  # after the switch above
from litellm import exceptions as litellm_errors

# Request and response bodies must never reach LiteLLM's logging callbacks or debug output.
litellm.turn_off_message_logging = True
litellm.suppress_debug_info = True

RETRIES = 2
BACKOFF_S = 0.5  # waits 0.5 s, then 1 s

FIXTURES_DIR = Path(__file__).resolve().parents[3] / "tests" / "fixtures" / "llm"

# LiteLLM exceptions that are worth another attempt on the same model.
_TRANSIENT: tuple[type[Exception], ...] = (
    litellm_errors.Timeout,
    litellm_errors.RateLimitError,
    litellm_errors.APIConnectionError,
    litellm_errors.ServiceUnavailableError,
    litellm_errors.InternalServerError,
)


class Provider(Protocol):
    async def complete(self, request: ProviderRequest) -> ProviderResponse: ...

    async def embed(self, request: ProviderRequest) -> ProviderResponse: ...


# --- G2: outbound card scan ---------------------------------------------------------------------


def _strings(value: object) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for key, item in value.items():
            yield str(key)
            yield from _strings(item)
    elif isinstance(value, list | tuple):
        for item in value:
            yield from _strings(item)


def scan_outbound(request: ProviderRequest) -> dict[str, int]:
    """Card data (PAN, CVV, PIN, 2FA code) anywhere in what the model would see: messages,
    texts, tool definitions, schema. Each string is scanned on its own, so digits of two
    neighbouring strings never join into one number. Fails closed: a masking error counts."""
    seen = request.model_dump(include={"messages", "texts", "tools", "json_schema"})
    counts: Counter[str] = Counter()
    for text in _strings(seen):
        result = mask(text)
        if result.failed:
            counts["MASKING_FAILED"] += 1
        counts.update(result.counts)
    return {entity: count for entity, count in counts.items() if count}


# --- providers -----------------------------------------------------------------------------------


class LiteLLMProvider:
    """Real calls through the LiteLLM SDK. Keys come from the settings, never from the request
    or the environment, and LiteLLM's own retries are off: the gateway retries.

    `api_bases` exists for tests only (a local server that captures what LiteLLM sends); it is
    set in code, never from a config file or a request."""

    def __init__(self, settings: Settings, api_bases: Mapping[str, str] | None = None) -> None:
        self._keys = {
            "anthropic": settings.anthropic_api_key.get_secret_value(),
            "openai": settings.openai_api_key.get_secret_value(),
        }
        self._api_bases = dict(api_bases or {})

    def _connection(self, model: str) -> dict[str, str]:
        provider = model.split("/", 1)[0]
        base = self._api_bases.get(provider)
        return {"api_key": self._key(model), **({"api_base": base} if base else {})}

    def _key(self, model: str) -> str:
        provider = model.split("/", 1)[0]
        key = self._keys.get(provider, "")
        if not key:
            raise ProviderError("MissingApiKey", transient=False)
        return key

    async def complete(self, request: ProviderRequest) -> ProviderResponse:
        kwargs: dict[str, Any] = {
            "model": request.model,
            "messages": request.messages,
            "max_tokens": request.max_tokens,
            "timeout": request.timeout_s,
            "num_retries": 0,
            "max_retries": 0,
            **self._connection(request.model),
            **request.params,
        }
        if request.json_schema is not None:
            kwargs["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": request.schema_name,
                    "schema": request.json_schema,
                    "strict": True,
                },
            }
        if request.tools:
            kwargs["tools"] = request.tools
        try:
            response = await litellm.acompletion(**kwargs)
        except Exception as exc:
            raise _provider_error(exc) from None
        message = response.choices[0].message
        return ProviderResponse(
            content=message.content,
            tool_calls=[
                ToolCall(id=call.id, name=call.function.name, arguments=call.function.arguments)
                for call in (message.tool_calls or [])
            ],
            usage=_usage(response.usage),
        )

    async def embed(self, request: ProviderRequest) -> ProviderResponse:
        try:
            response = await litellm.aembedding(
                model=request.model,
                input=request.texts,
                timeout=request.timeout_s,
                num_retries=0,
                max_retries=0,
                **self._connection(request.model),
            )
        except Exception as exc:
            raise _provider_error(exc) from None
        return ProviderResponse(
            vectors=[item["embedding"] for item in response.data],
            usage=_usage(response.usage),
        )


def _provider_error(exc: Exception) -> ProviderError:
    """Only the class and status code: provider messages can echo the request."""
    if isinstance(exc, ProviderError):
        return exc
    return ProviderError(
        type(exc).__name__,
        transient=isinstance(exc, _TRANSIENT),
        status_code=getattr(exc, "status_code", None),
    )


def _usage(usage: object) -> Usage:
    """LiteLLM's OpenAI-style usage. `prompt_tokens` includes cache reads and writes (LiteLLM
    adds Anthropic's `cache_read_input_tokens` and `cache_creation_input_tokens` to it)."""
    if usage is None:
        return Usage()
    details = getattr(usage, "prompt_tokens_details", None)
    return Usage(
        input_tokens=getattr(usage, "prompt_tokens", 0) or 0,
        output_tokens=getattr(usage, "completion_tokens", 0) or 0,
        cached_tokens=getattr(details, "cached_tokens", 0) or 0,
        cache_write_tokens=getattr(details, "cache_creation_tokens", 0) or 0,
    )


class Fixture(BaseModel):
    """A recorded response. `request` is kept for people reading the file; the key is the
    file name."""

    request: dict[str, Any]
    response: ProviderResponse
    recorded: str  # date of the real call, or "synthetic" for a hand-written response


class FixtureProvider:
    """Fake mode: answers from `<dir>/<fixture key>.json`. A missing file is an error, never a
    real call."""

    def __init__(self, directory: Path) -> None:
        self._directory = directory

    def path(self, request: ProviderRequest) -> Path:
        return self._directory / f"{request.fixture_key()}.json"

    def _load(self, request: ProviderRequest) -> ProviderResponse:
        path = self.path(request)
        try:
            return Fixture.model_validate_json(path.read_bytes()).response
        except FileNotFoundError:
            raise FixtureMissingError(
                f"no recorded response for {request.model} (key {request.fixture_key()}); "
                "add the request to tests/fixtures/llm/requests and run `make record-llm`"
            ) from None
        except ValidationError:
            raise FixtureMissingError(f"recorded response {path.name} is invalid") from None

    async def complete(self, request: ProviderRequest) -> ProviderResponse:
        return self._load(request)

    async def embed(self, request: ProviderRequest) -> ProviderResponse:
        return self._load(request)


def write_fixture(directory: Path, request: ProviderRequest, response: ProviderResponse) -> Path:
    """Used by `make record-llm`. The request is scanned again before it is written."""
    if scan_outbound(request):
        raise OutboundCardDataError("refusing to record a request with card data")
    fixture = Fixture(
        request=request.model_dump(mode="json", exclude={"timeout_s"}),
        response=response,
        recorded=time.strftime("%Y-%m-%d"),
    )
    path = directory / f"{request.fixture_key()}.json"
    path.write_text(
        json.dumps(fixture.model_dump(mode="json"), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return path


# --- the gateway ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Outcome:
    request: ProviderRequest
    response: ProviderResponse
    cost_usd: Decimal
    latency_ms: int
    fallback_used: bool


@dataclass(frozen=True)
class Gateway:
    models: ModelsConfig
    pricing: Pricing
    provider: Provider
    recorder: CallRecorder
    sleep: Callable[[float], Awaitable[None]] = field(default=asyncio.sleep)
    clock: Callable[[], float] = field(default=time.perf_counter)

    async def complete(
        self,
        role: str,
        messages: Sequence[Message],
        schema: type[BaseModel] | None = None,
        tools: Sequence[dict[str, Any]] | None = None,
        *,
        call: CallContext,
    ) -> Completion:
        requests = self.chat_requests(role, messages, schema, tools)
        outcome = await self._run(requests, call, self.provider.complete)
        return Completion(
            content=outcome.response.content,
            tool_calls=outcome.response.tool_calls,
            model=outcome.request.model,
            usage=outcome.response.usage,
            cost_usd=outcome.cost_usd,
            latency_ms=outcome.latency_ms,
            fallback_used=outcome.fallback_used,
        )

    async def embed(
        self, texts: Sequence[str], *, call: CallContext, role: str = "embedding"
    ) -> Embeddings:
        request = self.embedding_request(texts, role)
        outcome = await self._run([request], call, self.provider.embed)
        if len(outcome.response.vectors) != len(texts):
            raise LlmUnavailableError(f"role {role}: wrong number of vectors")
        return Embeddings(
            vectors=outcome.response.vectors,
            model=request.model,
            usage=outcome.response.usage,
            cost_usd=outcome.cost_usd,
            latency_ms=outcome.latency_ms,
        )

    def chat_requests(
        self,
        role: str,
        messages: Sequence[Message],
        schema: type[BaseModel] | None = None,
        tools: Sequence[dict[str, Any]] | None = None,
    ) -> list[ProviderRequest]:
        """The request to the role's model, then to its fallback. `make record-llm` uses the
        same function, so recorded fixtures match what `complete` sends."""
        config = self._role(role, "chat")
        if tools and not config.tools:
            raise ToolsNotAllowedError(f"role {role} is not configured for tools")
        return [
            ProviderRequest(
                kind="chat",
                model=choice.model,
                messages=[dict(message) for message in messages],
                max_tokens=config.max_tokens,
                timeout_s=config.timeout_s,
                params=choice.params,
                schema_name=schema.__name__ if schema is not None else None,
                json_schema=schema.model_json_schema() if schema is not None else None,
                tools=[dict(tool) for tool in tools] if tools else None,
            )
            for choice in config.choices()
        ]

    def embedding_request(self, texts: Sequence[str], role: str = "embedding") -> ProviderRequest:
        config = self._role(role, "embedding")
        return ProviderRequest(
            kind="embedding", model=config.model, texts=list(texts), timeout_s=config.timeout_s
        )

    def _role(self, role: str, kind: str) -> RoleConfig:
        config = self.models.role(role)
        if config.kind != kind:
            raise ConfigError(f"role {role} is not a {kind} role")
        return config

    async def _run(
        self,
        requests: list[ProviderRequest],
        call: CallContext,
        send: Callable[[ProviderRequest], Awaitable[ProviderResponse]],
    ) -> _Outcome:
        # The model and fallback requests differ only in model and parameters: one scan covers
        # what every model would see.
        entities = scan_outbound(requests[0])
        if entities:
            await self.recorder.record_blocked(
                self._row(call, requests[0].model, LlmCallStatus.ERROR, error="blocked: G2"),
                entities,
            )
            raise OutboundCardDataError(f"G2 blocked a call of node {call.node.value}")

        for index, request in enumerate(requests):
            for attempt in range(RETRIES + 1):
                start = self.clock()
                try:
                    async with asyncio.timeout(request.timeout_s):
                        response = await send(request)
                except TimeoutError:
                    error = ProviderError("Timeout", transient=True)
                except ProviderError as exc:
                    error = exc
                else:
                    latency_ms = self._elapsed_ms(start)
                    cost = self.pricing.cost(request.model, response.usage)
                    await self.recorder.record(
                        self._row(
                            call,
                            request.model,
                            LlmCallStatus.OK,
                            usage=response.usage,
                            cost=cost,
                            latency_ms=latency_ms,
                        )
                    )
                    return _Outcome(request, response, cost, latency_ms, index > 0)
                will_retry = error.transient and attempt < RETRIES
                await self.recorder.record(
                    self._row(
                        call,
                        request.model,
                        LlmCallStatus.RETRY if will_retry else LlmCallStatus.ERROR,
                        latency_ms=self._elapsed_ms(start),
                        error=error.summary,
                    )
                )
                if not will_retry:
                    break
                await self.sleep(BACKOFF_S * 2**attempt)
        raise LlmUnavailableError(f"node {call.node.value}: the model and its fallback failed")

    def _elapsed_ms(self, start: float) -> int:
        return max(0, round((self.clock() - start) * 1000))

    @staticmethod
    def _row(
        call: CallContext,
        model: str,
        status: LlmCallStatus,
        *,
        usage: Usage | None = None,
        cost: Decimal = Decimal(0),
        latency_ms: int = 0,
        error: str | None = None,
    ) -> LlmCallRecord:
        usage = usage or Usage()
        return LlmCallRecord(
            id=uuid7(),
            run_id=call.run_id,
            context=call.context,
            node=call.node,
            model=model,
            prompt_version=call.prompt_version,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cached_tokens=usage.cached_tokens,
            cost_usd=cost,
            latency_ms=latency_ms,
            status=status,
            error=error,
        )


def build_gateway(settings: Settings, recorder: CallRecorder) -> Gateway:
    """The gateway of the app: config files next to each other, provider from LLM_MODE."""
    models = load_models(settings.models_config)
    pricing = load_pricing(settings.models_config.parent / "pricing.yaml")
    pricing.check_covers(models)
    provider: Provider = (
        LiteLLMProvider(settings) if settings.llm_mode == "real" else FixtureProvider(FIXTURES_DIR)
    )
    return Gateway(models=models, pricing=pricing, provider=provider, recorder=recorder)
