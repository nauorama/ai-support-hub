"""A gateway for evaluation unit tests: answers come from a queue or from recorded fixtures, rows
go to memory. No network, no database."""

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import BaseModel

from hub.domain.enums import LlmCallMode
from hub.llm.config import load_models
from hub.llm.gateway import Gateway, Provider
from hub.llm.pricing import load_pricing
from hub.llm.schemas import LlmCallRecord, ProviderRequest, ProviderResponse, Usage

ROOT = Path(__file__).resolve().parents[2]
MODELS_YAML = ROOT / "config" / "models.yaml"


@dataclass
class QueueProvider:
    """Answers every request with the next item: a response, or an exception to raise."""

    answers: list[ProviderResponse | Exception]
    requests: list[ProviderRequest] = field(default_factory=list)

    async def complete(self, request: ProviderRequest) -> ProviderResponse:
        self.requests.append(request)
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    async def embed(self, request: ProviderRequest) -> ProviderResponse:
        raise AssertionError("judges never embed")


@dataclass
class MemoryRecorder:
    rows: list[LlmCallRecord] = field(default_factory=list)

    async def record(self, call: LlmCallRecord) -> None:
        self.rows.append(call)

    async def record_blocked(self, call: LlmCallRecord, entities: dict[str, int]) -> None:
        self.rows.append(call)


def answer(value: BaseModel | dict[str, object], *, replayed: bool = False) -> ProviderResponse:
    content = value.model_dump_json() if isinstance(value, BaseModel) else json.dumps(value)
    return ProviderResponse(
        content=content,
        usage=Usage(input_tokens=1000, output_tokens=100),
        replayed=replayed,
    )


def make_gateway(
    provider: Provider,
    recorder: MemoryRecorder | None = None,
    mode: LlmCallMode = LlmCallMode.FAKE,
) -> Gateway:
    models = load_models(MODELS_YAML)
    return Gateway(
        models=models,
        pricing=load_pricing(MODELS_YAML.parent / "pricing.yaml"),
        provider=provider,
        recorder=recorder or MemoryRecorder(),
        mode=mode,
        budget=None,
    )


def queue(*answers: ProviderResponse | Exception) -> QueueProvider:
    return QueueProvider(answers=list(answers))


def no_calls() -> QueueProvider:
    return QueueProvider(answers=[])


def requests_of(provider: QueueProvider) -> Sequence[ProviderRequest]:
    return provider.requests
