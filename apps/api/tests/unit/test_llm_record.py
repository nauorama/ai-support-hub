"""Record mode (`make record-llm`): the calls go through the gateway — G2 scan, retries, one
`llm_calls` row per attempt with context `record` — and every successful answer becomes a
fixture that fake mode then serves. No network: the real provider is replaced by a script."""

import json
from collections.abc import Mapping
from pathlib import Path

import pytest

from hub.domain.enums import LlmCallContext, LlmCallStatus, LlmNode
from hub.llm.gateway import FixtureProvider, RecordingProvider
from hub.llm.record import load_specs, record_all, record_call, recording_gateway
from hub.llm.schemas import OutboundCardDataError, ProviderError

from ._llm import MODELS_YAML, MemoryRecorder, ScriptedProvider, ok

HAIKU = "anthropic/claude-haiku-5-5"
LUNA = "openai/gpt-6-luna"
EMBEDDING = "openai/text-embedding-3-small"
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
            LUNA: [ok("withdrawal_issue")],
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
        sleep=_no_sleep,
    )

    assert isinstance(gateway.provider, RecordingProvider)
    assert await record_all(gateway, tmp_path) == 3

    # One row per attempt, the retry included; all with context `record`.
    assert recorder.statuses() == [
        (EMBEDDING, LlmCallStatus.OK),
        (HAIKU, LlmCallStatus.RETRY),
        (HAIKU, LlmCallStatus.OK),
        (LUNA, LlmCallStatus.OK),
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
        if item[1].role == "triage"
    ]
    call = record_call(spec)
    assert (call.context, call.node, call.run_id) == (LlmCallContext.RECORD, LlmNode.TRIAGE, None)


async def _no_sleep(seconds: float) -> None:
    return None
