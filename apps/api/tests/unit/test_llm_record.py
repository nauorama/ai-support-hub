"""Record mode (`make record-llm`): the calls go through the gateway — G2 scan, retries, one
`llm_calls` row per attempt with context `record` — and every successful answer becomes a
fixture that fake mode then serves. No network: the real provider is replaced by a script."""

import json
import sys
from collections.abc import Mapping
from pathlib import Path

import pytest
from pydantic import ValidationError

from hub.domain.enums import LlmCallContext, LlmCallMode, LlmCallStatus, LlmNode
from hub.llm.gateway import FixtureProvider, Gateway, RecordingProvider
from hub.llm.record import (
    SCHEMAS,
    RequestSpec,
    SchemaProbe,
    has_real_recording,
    load_specs,
    main,
    record_all,
    record_call,
    recording_gateway,
    spec_requests,
)
from hub.llm.schemas import CallContext, OutboundCardDataError, ProviderError
from hub.llm.structured import provider_schema

from ._llm import MODELS_YAML, MemoryRecorder, ScriptedProvider, ok

HAIKU = "anthropic/claude-haiku-5-5"
SONNET = "anthropic/claude-sonnet-5-5"
EMBEDDING = "fastembed/BAAI/bge-small-en-v1.5"
RATE_LIMIT = ProviderError("RateLimitError", transient=True, status_code=429)


def _write_spec(directory: Path, name: str, spec: Mapping[str, object]) -> None:
    (directory / "requests").mkdir(exist_ok=True)
    (directory / "requests" / f"{name}.json").write_text(json.dumps(spec), encoding="utf-8")


TRIAGE_SPEC = {
    "role": "triage",
    "messages": [{"role": "user", "content": "<ticket>Where is my SEPA withdrawal?</ticket>"}],
}


async def test_recording_calls_model_and_fallback_through_the_gateway(tmp_path: Path) -> None:
    _write_spec(tmp_path, "triage", TRIAGE_SPEC)
    _write_spec(tmp_path, "embedding", {"role": "embedding", "texts": ["a", "b"]})
    from hub.llm.schemas import ProviderResponse, Usage

    vectors = ProviderResponse(vectors=[[0.1], [0.2]], usage=Usage(input_tokens=4))
    inner = ScriptedProvider(
        {
            HAIKU: [RATE_LIMIT, ok("withdrawal_issue")],
            SONNET: [ok("withdrawal_issue")],
            EMBEDDING: [vectors],
        }
    )
    recorder = MemoryRecorder()
    gateway = recording_gateway(inner, recorder, tmp_path, MODELS_YAML)
    gateway = type(gateway)(
        models=gateway.models,
        pricing=gateway.pricing,
        provider=gateway.provider,
        recorder=recorder,
        mode=gateway.mode,
        budget=None,
        sleep=_no_sleep,
    )

    assert isinstance(gateway.provider, RecordingProvider)
    assert await record_all(gateway, tmp_path) == 3

    # One row per attempt, the retry included; all with context `record`.
    assert recorder.statuses() == [
        (EMBEDDING, LlmCallStatus.OK),
        (HAIKU, LlmCallStatus.RETRY),
        (HAIKU, LlmCallStatus.OK),
        (SONNET, LlmCallStatus.OK),
    ]
    assert {row.context for row in recorder.rows} == {LlmCallContext.RECORD}
    assert [row.node for row in recorder.rows] == [LlmNode.EMBEDDING] + [LlmNode.TRIAGE] * 3

    # Fake mode serves exactly what was recorded, for the model and the fallback.
    fake = FixtureProvider(tmp_path)
    [model, fallback] = gateway.chat_requests("triage", TRIAGE_SPEC["messages"])  # type: ignore[arg-type]
    assert (await fake.complete(model)).content == "withdrawal_issue"
    assert (await fake.complete(fallback)).content == "withdrawal_issue"
    assert (await fake.embed(gateway.embedding_request(["a", "b"]))).vectors == [[0.1], [0.2]]


async def test_recording_a_request_with_card_data_is_blocked_and_writes_nothing(
    tmp_path: Path,
) -> None:
    _write_spec(
        tmp_path,
        "leak",
        {"role": "triage", "messages": [{"role": "user", "content": "card 4111 1111 1111 1111"}]},
    )
    inner = ScriptedProvider({})
    recorder = MemoryRecorder()
    gateway = recording_gateway(inner, recorder, tmp_path, MODELS_YAML)
    with pytest.raises(OutboundCardDataError):
        await record_all(gateway, tmp_path)
    assert inner.requests == []
    assert len(recorder.blocked) == 1
    assert sorted(path.name for path in tmp_path.iterdir()) == ["requests"]


def test_record_call_uses_the_record_context_and_the_role_as_node() -> None:
    [(_, spec)] = [
        item
        for item in load_specs(Path(__file__).parents[1] / "fixtures" / "llm")
        if item[0].name == "triage_01.json"
    ]
    call = record_call(spec)
    assert (call.context, call.node, call.run_id) == (LlmCallContext.RECORD, LlmNode.TRIAGE, None)
    assert call.prompt_version == "triage/v1"


async def _no_sleep(seconds: float) -> None:
    return None


# --- structured requests, primary-only, skipping real recordings (P3-03) ----------------------

PROBE_SPEC = {
    "role": "triage",
    "schema": "SchemaProbe",
    "prompt_version": "triage/v1",
    "models": "primary",
    "messages": [{"role": "user", "content": "Colour: blue."}],
}


def _gateway(inner: ScriptedProvider, recorder: MemoryRecorder, directory: Path) -> Gateway:
    gateway = recording_gateway(inner, recorder, directory, MODELS_YAML)
    return Gateway(
        models=gateway.models,
        pricing=gateway.pricing,
        provider=gateway.provider,
        recorder=recorder,
        mode=gateway.mode,
        budget=None,
        sleep=_no_sleep,
    )


async def test_a_structured_spec_is_recorded_as_complete_structured_sends_it(
    tmp_path: Path,
) -> None:
    _write_spec(tmp_path, "probe", PROBE_SPEC)
    inner = ScriptedProvider({HAIKU: [ok('{"marker": "schema_probe"}')]})
    recorder = MemoryRecorder()
    gateway = _gateway(inner, recorder, tmp_path)

    assert await record_all(gateway, tmp_path) == 1
    # Only the role's own model ("primary"), with the strict schema and the prompt version.
    [sent] = inner.requests
    assert sent.model == HAIKU
    assert sent.schema_name == "SchemaProbe"
    assert sent.json_schema == provider_schema(SchemaProbe)
    assert [row.prompt_version for row in recorder.rows] == ["triage/v1"]
    [model, _] = gateway.chat_requests("triage", PROBE_SPEC["messages"], SchemaProbe)  # type: ignore[arg-type]
    assert sent == model
    assert (await FixtureProvider(tmp_path).complete(model)).content == '{"marker": "schema_probe"}'


async def test_a_second_run_skips_real_recordings_and_force_records_again(tmp_path: Path) -> None:
    _write_spec(tmp_path, "triage", TRIAGE_SPEC)
    inner = ScriptedProvider({HAIKU: [ok("a"), ok("c")], SONNET: [ok("b"), ok("d")]})
    gateway = _gateway(inner, MemoryRecorder(), tmp_path)

    assert await record_all(gateway, tmp_path) == 2
    assert await record_all(gateway, tmp_path) == 0  # nothing paid twice
    assert len(inner.requests) == 2
    assert await record_all(gateway, tmp_path, force=True) == 2
    [model, fallback] = gateway.chat_requests("triage", TRIAGE_SPEC["messages"])  # type: ignore[arg-type]
    assert (await FixtureProvider(tmp_path).complete(model)).content == "c"
    assert (await FixtureProvider(tmp_path).complete(fallback)).content == "d"


async def test_a_synthetic_or_broken_fixture_is_recorded_again(tmp_path: Path) -> None:
    _write_spec(tmp_path, "triage", {**TRIAGE_SPEC, "models": "primary"})
    inner = ScriptedProvider({HAIKU: [ok("real"), ok("again")]})
    gateway = _gateway(inner, MemoryRecorder(), tmp_path)
    [model, _] = gateway.chat_requests("triage", TRIAGE_SPEC["messages"])  # type: ignore[arg-type]
    path = tmp_path / f"{model.fixture_key()}.json"

    path.write_text('{"recorded": "synthetic"}', encoding="utf-8")
    assert not has_real_recording(tmp_path, model)
    assert await record_all(gateway, tmp_path) == 1
    assert has_real_recording(tmp_path, model)

    path.write_text("not json", encoding="utf-8")
    assert not has_real_recording(tmp_path, model)
    assert await record_all(gateway, tmp_path) == 1


def test_a_spec_names_only_a_known_schema(tmp_path: Path) -> None:
    _write_spec(tmp_path, "bad", {**PROBE_SPEC, "schema": "os.system"})
    with pytest.raises(ValidationError):
        load_specs(tmp_path)


async def test_record_mode_writes_real_rows_that_count_against_the_budget(
    tmp_path: Path,
) -> None:
    """`make record-llm` makes real, paid calls: its rows must be `real`, or the daily budget
    would stop counting them (owner decision, P3-01)."""
    recorder = MemoryRecorder()
    inner = ScriptedProvider({HAIKU: [ok()]})
    gateway = recording_gateway(inner, recorder, tmp_path, MODELS_YAML)
    assert gateway.mode == LlmCallMode.REAL
    await gateway.complete(
        "triage",
        [{"role": "user", "content": "hi"}],
        call=CallContext(context=LlmCallContext.RECORD, node=LlmNode.TRIAGE),
    )
    assert [row.mode for row in recorder.rows] == [LlmCallMode.REAL]


def test_spec_requests_match_the_gateway_requests() -> None:
    gateway = recording_gateway(ScriptedProvider({}), MemoryRecorder(), Path("."), MODELS_YAML)
    spec = RequestSpec.model_validate({**PROBE_SPEC, "models": "all"})
    assert spec_requests(gateway, spec) == gateway.chat_requests(
        "triage",
        spec.messages,
        SchemaProbe,
    )
    assert (
        spec_requests(gateway, spec.model_copy(update={"models": "primary"}))
        == (gateway.chat_requests("triage", spec.messages, SchemaProbe)[:1])
    )


def test_the_schema_probe_has_the_shapes_the_first_real_calls_must_confirm() -> None:
    """`const`, a description next to a `$ref`, a nullable `$ref`, and a moved limit."""
    schema = provider_schema(SchemaProbe)
    properties = schema["properties"]
    assert properties["marker"]["const"] == "schema_probe"
    assert set(properties["colour"]) == {"$ref", "description"}
    assert set(properties["item"]) == {"$ref", "description"}
    assert {"type": "null"} in properties["maybe"]["anyOf"]
    assert "at most 9" in properties["count"]["description"]
    assert set(SCHEMAS) == {"TriageResult", "DraftAnswer", "SchemaProbe"}


def test_the_command_takes_only_force(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "argv", ["record", "--everything"])
    with pytest.raises(SystemExit, match="usage"):
        main()
