"""`make record-llm`: refresh the recorded model responses for fake mode (docs/operations.md §1).

Reads every request file in `tests/fixtures/llm/requests/*.json`, sends it for real to the role's
model and fallback (and the embedding model), and writes one fixture per model next to the
request files. Needs real API keys and costs money; prints tokens and cost of every call.

A request file: {"role": "triage", "messages": [...], "tools": [...] (optional)}
or {"role": "embedding", "texts": [...]}. Messages must already be masked: the outbound scan
(G2) runs before every call and before every write.

Recording writes no `llm_calls` rows: it runs outside the app, without a database.
"""

import asyncio
import sys
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict

from hub.config import load_settings
from hub.llm.config import load_models
from hub.llm.gateway import FIXTURES_DIR, Gateway, LiteLLMProvider, scan_outbound, write_fixture
from hub.llm.pricing import load_pricing
from hub.llm.schemas import LlmCallRecord, OutboundCardDataError, ProviderRequest


class RequestSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: str
    messages: list[dict[str, Any]] = []
    texts: list[str] = []
    tools: list[dict[str, Any]] | None = None


class _NoRecorder:
    async def record(self, call: LlmCallRecord) -> None:
        return None

    async def record_blocked(self, call: LlmCallRecord, entities: dict[str, int]) -> None:
        return None


def requests_for(gateway: Gateway, spec: RequestSpec) -> list[ProviderRequest]:
    if spec.texts:
        return [gateway.embedding_request(spec.texts, spec.role)]
    return gateway.chat_requests(spec.role, spec.messages, tools=spec.tools)


async def record_all(directory: Path) -> int:
    settings = load_settings()
    models = load_models(settings.models_config)
    pricing = load_pricing(settings.models_config.parent / "pricing.yaml")
    provider = LiteLLMProvider(settings)
    gateway = Gateway(models=models, pricing=pricing, provider=provider, recorder=_NoRecorder())
    written = 0
    for spec_path in sorted((directory / "requests").glob("*.json")):
        spec = RequestSpec.model_validate_json(spec_path.read_bytes())
        for request in requests_for(gateway, spec):
            if scan_outbound(request):
                raise OutboundCardDataError(f"{spec_path.name}: request contains card data")
            send = provider.embed if request.kind == "embedding" else provider.complete
            response = await send(request)
            cost = pricing.cost(request.model, response.usage)
            path = write_fixture(directory, request, response)
            print(
                f"{spec_path.name} {request.model}: {response.usage.input_tokens} in, "
                f"{response.usage.output_tokens} out, ${cost} -> {path.name}"
            )
            written += 1
    return written


def main() -> None:
    written = asyncio.run(record_all(FIXTURES_DIR))
    print(f"record-llm: {written} fixtures written")
    if written == 0:
        sys.exit("record-llm: no request files found")


if __name__ == "__main__":
    main()
