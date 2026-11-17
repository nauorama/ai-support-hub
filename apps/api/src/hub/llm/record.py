"""`make record-llm`: refresh the recorded model responses for fake mode (docs/operations.md §1).

Reads every request file in `tests/fixtures/llm/requests/*.json` and sends it through the gateway
in record mode (`Gateway.record_each_model` with a `RecordingProvider`): the role's model and
its fallback are each called once, with the G2 scan, retries and one `llm_calls` row per attempt
(context `record`), and every successful answer is written as a fixture. Needs real API keys and
the database (`make dev`); costs money.

A request file: {"role": "triage", "messages": [...], "tools": [...] (optional)}
or {"role": "embedding", "texts": [...]}. Messages must already be masked. Optional fields:
- "schema": the name of a structured-output schema (`SCHEMAS`, or the registry the caller
  passes: `hub_evals.record` adds the judge schemas); the request is built as
  `complete_structured` builds it.
- "prompt_version": written to the `llm_calls` rows of the recording.
- "models": "all" (default: the model and its fallback) or "primary" (the model only), to keep
  the cost of a recording small.

A model whose fixture already holds a real recording is skipped, so a second run costs nothing;
`--force` (`make record-llm FORCE=1`) records it again. A synthetic fixture is always replaced.
"""

import asyncio
import json
import sys
from collections.abc import Callable, Mapping
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, ValidationInfo, model_validator

from hub.config import load_settings
from hub.db.session import connect
from hub.domain.draft import DraftAnswer
from hub.domain.enums import LlmCallContext, LlmCallMode, LlmNode
from hub.domain.triage import TriageResult
from hub.domain.verification import ClaimsAnswer
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
from hub.llm.schemas import CallContext, ProviderRequest


class ProbeColour(StrEnum):
    RED = "red"
    BLUE = "blue"


class ProbeItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["probe_item"]
    note: str


class SchemaProbe(BaseModel):
    """Only for recording (P3-03): one request whose strict schema has every shape our schemas
    use or will use, so the first real calls show that Anthropic accepts them: `const` (a
    one-value Literal), a `description` next to a `$ref` (an enum and a nested model), a
    nullable `$ref` (`anyOf` with null), a list of models, and limits moved into descriptions."""

    model_config = ConfigDict(extra="forbid")

    marker: Literal["schema_probe"] = Field(description="Always `schema_probe`.")
    colour: ProbeColour = Field(description="The colour the user names.")
    item: ProbeItem = Field(description="One item; `note` repeats the user's word.")
    maybe: ProbeItem | None = Field(description="Null unless the user asks for a second item.")
    items: list[ProbeItem] = Field(max_length=2, description="The items the user lists.")
    count: int = Field(ge=0, le=9, description="The number the user names.")


# Schemas a request file may name. A fixed list: a file never names arbitrary code.
SCHEMAS: dict[str, type[BaseModel]] = {
    "TriageResult": TriageResult,
    "DraftAnswer": DraftAnswer,
    "ClaimsAnswer": ClaimsAnswer,
    "SchemaProbe": SchemaProbe,
}


class RequestSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: str
    messages: list[dict[str, Any]] = []
    texts: list[str] = []
    tools: list[dict[str, Any]] | None = None
    # A name from the schema registry given as validation context (`SCHEMAS` by default), never
    # a path to arbitrary code.
    schema_name: str | None = Field(default=None, alias="schema")
    prompt_version: str | None = None
    models: Literal["all", "primary"] = "all"
    _schema: type[BaseModel] | None = PrivateAttr(default=None)

    @model_validator(mode="after")
    def _known_schema(self, info: ValidationInfo) -> "RequestSpec":
        schemas: Mapping[str, type[BaseModel]] = (info.context or {}).get("schemas", SCHEMAS)
        if self.schema_name is not None:
            if self.schema_name not in schemas:
                raise ValueError(f"unknown schema; known: {', '.join(sorted(schemas))}")
            self._schema = schemas[self.schema_name]
        return self

    @property
    def schema_model(self) -> type[BaseModel] | None:
        return self._schema


def spec_requests(gateway: Gateway, spec: RequestSpec) -> list[ProviderRequest]:
    """The requests a spec records: what the gateway sends for it, model then fallback, or
    only the model for `"models": "primary"`."""
    if spec.texts:
        requests = [gateway.embedding_request(spec.texts, spec.role)]
    else:
        requests = gateway.chat_requests(spec.role, spec.messages, spec.schema_model, spec.tools)
    return requests[:1] if spec.models == "primary" else requests


def has_real_recording(directory: Path, request: ProviderRequest) -> bool:
    path = directory / f"{request.fixture_key()}.json"
    try:
        recorded = json.loads(path.read_bytes()).get("recorded")
    except (OSError, ValueError, AttributeError):
        return False
    return isinstance(recorded, str) and recorded != "synthetic"


def load_specs(
    directory: Path, schemas: Mapping[str, type[BaseModel]] = SCHEMAS
) -> list[tuple[Path, RequestSpec]]:
    return [
        (path, RequestSpec.model_validate_json(path.read_bytes(), context={"schemas": schemas}))
        for path in sorted((directory / "requests").glob("*.json"))
    ]


def record_call(spec: RequestSpec) -> CallContext:
    """The `llm_calls` context of a recording: context `record`, node named after the role."""
    return CallContext(
        context=LlmCallContext.RECORD, node=LlmNode(spec.role), prompt_version=spec.prompt_version
    )


def _selector(
    gateway: Gateway, spec: RequestSpec, directory: Path, force: bool
) -> Callable[[ProviderRequest], bool]:
    wanted = {request.fixture_key() for request in spec_requests(gateway, spec)}

    def select(request: ProviderRequest) -> bool:
        key = request.fixture_key()
        return key in wanted and (force or not has_real_recording(directory, request))

    return select


async def record_all(
    gateway: Gateway,
    directory: Path,
    *,
    force: bool = False,
    schemas: Mapping[str, type[BaseModel]] = SCHEMAS,
) -> int:
    """Record every request file through the gateway. Returns the number of models called."""
    called = 0
    for _, spec in load_specs(directory, schemas):
        requests = await gateway.record_each_model(
            spec.role,
            messages=spec.messages,
            texts=spec.texts,
            tools=spec.tools,
            schema=spec.schema_model,
            call=record_call(spec),
            select=_selector(gateway, spec, directory, force),
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
        # Record mode makes real, paid calls: its rows count against the daily budget.
        mode=LlmCallMode.REAL,
        budget=None,
    )


async def _main(force: bool, directory: Path, schemas: Mapping[str, type[BaseModel]]) -> int:
    settings = load_settings()
    database = connect(settings.database_url.get_secret_value())
    try:
        gateway = recording_gateway(
            LiteLLMProvider(settings),
            DbCallRecorder(database),
            directory,
            settings.models_config,
        )
        return await record_all(gateway, directory, force=force, schemas=schemas)
    finally:
        await database.dispose()


def main(
    directory: Path = FIXTURES_DIR,
    schemas: Mapping[str, type[BaseModel]] = SCHEMAS,
    *,
    command: str = "hub.llm.record",
) -> None:
    """The command line of a recording. `hub_evals.record` calls it with its own fixture folder
    and the judge schemas: the app never imports the evaluation package."""
    args = sys.argv[1:]
    if args not in ([], ["--force"]):
        sys.exit(f"usage: python -m {command} [--force]")
    if not load_specs(directory, schemas):
        sys.exit("record-llm: no request files found")
    called = asyncio.run(_main(args == ["--force"], directory, schemas))
    print(
        f"record-llm: {called} fixtures written (models that already had a real recording were "
        "skipped); tokens and cost are in llm_calls (context record)"
    )


if __name__ == "__main__":
    main()
