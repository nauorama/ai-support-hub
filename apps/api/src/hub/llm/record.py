"""`make record-llm`: refresh the recorded model responses for fake mode (docs/operations.md §1).

Reads every request file in `tests/fixtures/llm/requests/*.json` and sends it through the gateway
in record mode (`Gateway.record_each_model` with a `RecordingProvider`): the role's model and
its fallback are each called once, with the G2 scan, retries and one `llm_calls` row per attempt
(context `record`), and every successful answer is written as a fixture. Needs real API keys and
the database (`make dev`); costs money.

A request file: {"role": "triage", "messages": [...], "tools": [...] (optional)}
or {"role": "embedding", "texts": [...]}. Messages must already be masked.
"""

import asyncio
import sys
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict

from hub.config import load_settings
from hub.db.session import connect
from hub.domain.enums import LlmCallContext, LlmNode
from hub.llm.config import load_models
from hub.llm.gateway import (
    FIXTURES_DIR,
    Gateway,
    LiteLLMProvider,
    Provider,
    RecordingProvider,
)
from hub.llm.pricing import load_pricing
from hub.llm.recorder import CallRecorder, DbCallRecorder
from hub.llm.schemas import CallContext


class RequestSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: str
    messages: list[dict[str, Any]] = []
    texts: list[str] = []
    tools: list[dict[str, Any]] | None = None


def load_specs(directory: Path) -> list[tuple[Path, RequestSpec]]:
    return [
        (path, RequestSpec.model_validate_json(path.read_bytes()))
        for path in sorted((directory / "requests").glob("*.json"))
    ]


def record_call(spec: RequestSpec) -> CallContext:
    """The `llm_calls` context of a recording: context `record`, node named after the role."""
    return CallContext(context=LlmCallContext.RECORD, node=LlmNode(spec.role))


async def record_all(gateway: Gateway, directory: Path) -> int:
    """Record every request file through the gateway. Returns the number of models called."""
    called = 0
    for _, spec in load_specs(directory):
        requests = await gateway.record_each_model(
            spec.role,
            messages=spec.messages,
            texts=spec.texts,
            tools=spec.tools,
            call=record_call(spec),
        )
        called += len(requests)
    return called


def recording_gateway(
    provider: Provider, recorder: CallRecorder, directory: Path, models_config: Path
) -> Gateway:
    models = load_models(models_config)
    pricing = load_pricing(models_config.parent / "pricing.yaml")
    pricing.check_covers(models)
    return Gateway(
        models=models,
        pricing=pricing,
        provider=RecordingProvider(provider, directory),
        recorder=recorder,
    )


async def _main() -> int:
    settings = load_settings()
    database = connect(settings.database_url.get_secret_value())
    try:
        gateway = recording_gateway(
            LiteLLMProvider(settings),
            DbCallRecorder(database),
            FIXTURES_DIR,
            settings.models_config,
        )
        return await record_all(gateway, FIXTURES_DIR)
    finally:
        await database.dispose()


def main() -> None:
    called = asyncio.run(_main())
    if called == 0:
        sys.exit("record-llm: no request files found")
    print(
        f"record-llm: {called} fixtures written; tokens and cost are in llm_calls (context record)"
    )


if __name__ == "__main__":
    main()
