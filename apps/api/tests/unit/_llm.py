"""Helpers for gateway tests: a scripted provider, an in-memory recorder, the real config files.

A plain module, not conftest: pytest advises against importing from conftest files.
"""

import asyncio
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from hub.domain.enums import LlmCallContext, LlmCallMode, LlmCallStatus, LlmNode
from hub.llm.budget import BudgetCheck
from hub.llm.config import ModelsConfig, load_models
from hub.llm.gateway import Gateway, Provider
from hub.llm.pricing import Pricing, load_pricing
from hub.llm.schemas import (
    CallContext,
    LlmCallRecord,
    ProviderRequest,
    ProviderResponse,
    Usage,
)

ROOT = Path(__file__).resolve().parents[4]
MODELS_YAML = ROOT / "config" / "models.yaml"
PRICING_YAML = ROOT / "config" / "pricing.yaml"

CALL = CallContext(context=LlmCallContext.ONLINE, node=LlmNode.TRIAGE, prompt_version="v1")

type Step = ProviderResponse | Exception | float  # a float: hang for that many seconds


def ok(content: str = "fine", **usage: int) -> ProviderResponse:
    return ProviderResponse(
        content=content, usage=Usage(**({"input_tokens": 100, "output_tokens": 20} | usage))
    )


@dataclass
class ScriptedProvider:
    """Answers each request with the next step for its model. Records every request it got."""

    script: dict[str, list[Step]]
    requests: list[ProviderRequest] = field(default_factory=list)

    async def _next(self, request: ProviderRequest) -> ProviderResponse:
        self.requests.append(request)
        step = self.script[request.model].pop(0)
        if isinstance(step, Exception):
            raise step
        if isinstance(step, float):
            await asyncio.sleep(step)
            raise AssertionError("the timeout should have stopped this call")
        return step

    async def complete(self, request: ProviderRequest) -> ProviderResponse:
        return await self._next(request)

    async def embed(self, request: ProviderRequest) -> ProviderResponse:
        return await self._next(request)


@dataclass
class MemoryRecorder:
    rows: list[LlmCallRecord] = field(default_factory=list)
    blocked: list[tuple[LlmCallRecord, dict[str, int]]] = field(default_factory=list)

    async def record(self, call: LlmCallRecord) -> None:
        self.rows.append(call)

    async def record_blocked(self, call: LlmCallRecord, entities: dict[str, int]) -> None:
        self.rows.append(call)
        self.blocked.append((call, entities))

    def statuses(self) -> list[tuple[str, LlmCallStatus]]:
        return [(row.model, row.status) for row in self.rows]


@dataclass
class Sleeps:
    waits: list[float] = field(default_factory=list)

    async def __call__(self, seconds: float) -> None:
        self.waits.append(seconds)


def real_models() -> ModelsConfig:
    return load_models(MODELS_YAML)


def real_pricing() -> Pricing:
    return load_pricing(PRICING_YAML)


def make_gateway(
    provider: Provider,
    recorder: MemoryRecorder | None = None,
    sleeps: Sleeps | None = None,
    models: ModelsConfig | None = None,
    clock: Sequence[float] | None = None,
    mode: LlmCallMode = LlmCallMode.REAL,
    budget: BudgetCheck | None = None,
) -> Gateway:
    ticks = iter(clock) if clock is not None else None
    return Gateway(
        models=models or real_models(),
        pricing=real_pricing(),
        provider=provider,
        recorder=recorder or MemoryRecorder(),
        mode=mode,
        budget=budget,
        sleep=sleeps or Sleeps(),
        clock=(lambda: next(ticks)) if ticks is not None else time.perf_counter,
    )
