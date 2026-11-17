"""The evaluation disk cache (`CachingProvider`, eval plan §4, P4-03): a hit makes no provider
call and is recorded as `fake` (nothing paid); a miss calls the provider, and the gateway stores
the answer only once it passed its check."""

import dataclasses
from pathlib import Path

import pytest
from pydantic import BaseModel

from hub.config import Settings
from hub.domain.enums import LlmCallMode, LlmCallStatus
from hub.llm.gateway import CachingProvider, FixtureProvider, build_gateway
from hub.llm.local_embedding import LocalEmbeddingProvider
from hub.llm.schemas import (
    LlmUnavailableError,
    ProviderError,
    ProviderRequest,
    ProviderResponse,
    Usage,
)

from ._llm import CALL, MODELS_YAML, MemoryRecorder, ScriptedProvider, make_gateway, ok

HAIKU = "anthropic/claude-haiku-5-5"


class Label(BaseModel):
    label: str


SONNET = "anthropic/claude-sonnet-5-5"


def _request(text: str = "hi") -> ProviderRequest:
    return ProviderRequest(
        kind="chat",
        model=HAIKU,
        messages=[{"role": "user", "content": text}],
        max_tokens=400,
        timeout_s=30,
    )


async def test_a_miss_calls_the_provider_and_a_hit_does_not(tmp_path: Path) -> None:
    inner = ScriptedProvider({HAIKU: [ok("first")]})
    cache = CachingProvider(inner, tmp_path / "real")
    first = await cache.complete(_request())
    assert not cache.contains(_request())  # a miss alone stores nothing
    cache.store(_request(), first)
    second = await cache.complete(_request())
    assert len(inner.requests) == 1
    assert (cache.hits, cache.misses) == (1, 1)
    assert first.content == second.content == "first"
    assert second.usage == first.usage
    assert not first.replayed
    assert second.replayed
    assert cache.contains(_request())
    assert not cache.contains(_request("other"))


async def test_the_replayed_flag_is_never_written(tmp_path: Path) -> None:
    cache = CachingProvider(ScriptedProvider({HAIKU: [ok()]}), tmp_path)
    cache.store(_request(), await cache.complete(_request()))
    # A replayed answer is never written again.
    cache.store(_request("other"), ok().model_copy(update={"replayed": True}))
    assert not cache.contains(_request("other"))
    assert "replayed" not in cache.path(_request()).read_text(encoding="utf-8")
    assert list(tmp_path.glob("*.tmp")) == []


async def test_embeddings_are_cached_too(tmp_path: Path) -> None:
    request = ProviderRequest(kind="embedding", model=HAIKU, texts=["a"], timeout_s=5)
    inner = ScriptedProvider({HAIKU: [ProviderResponse(vectors=[[0.1]])]})
    cache = CachingProvider(inner, tmp_path)
    cache.store(request, await cache.embed(request))
    assert (await cache.embed(request)).vectors == [[0.1]]
    assert len(inner.requests) == 1


async def test_an_answer_the_schema_check_rejects_is_not_stored(tmp_path: Path) -> None:
    inner = ScriptedProvider(
        {HAIKU: [ok("not json"), ok('{"label": "fine"}'), ok('{"label": "again"}')]}
    )
    gateway = dataclasses.replace(
        make_gateway(ScriptedProvider({})), provider=CachingProvider(inner, tmp_path)
    )
    messages = [{"role": "user", "content": "q"}]
    first = await gateway.complete_structured("triage", messages, Label, call=CALL)
    assert first.repaired
    # Only the repair answer (valid) was stored; the first request runs again next time.
    assert len(list(tmp_path.glob("*.json"))) == 1
    second = await gateway.complete_structured("triage", messages, Label, call=CALL)
    assert second.value.label == "again"
    assert len(inner.requests) == 3


async def test_a_cache_that_cannot_write_does_not_fail_the_paid_call(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    blocker = tmp_path / "file"
    blocker.write_text("x", encoding="utf-8")
    recorder = MemoryRecorder()
    inner = ScriptedProvider({HAIKU: [ok("paid", input_tokens=100, output_tokens=10)]})
    # The cache folder is under a file: mkdir fails with an OSError.
    gateway = dataclasses.replace(
        make_gateway(ScriptedProvider({}), recorder),
        provider=CachingProvider(inner, blocker / "cache"),
    )
    completion = await gateway.complete("triage", [{"role": "user", "content": "q"}], call=CALL)
    assert completion.content == "paid"
    assert not completion.fallback_used
    (row,) = recorder.rows
    assert row.status == LlmCallStatus.OK
    assert row.cost_usd > 0
    assert "could not store" in caplog.text


async def test_a_broken_cache_file_calls_the_model_again(tmp_path: Path) -> None:
    inner = ScriptedProvider({HAIKU: [ok("fresh")]})
    cache = CachingProvider(inner, tmp_path)
    cache.path(_request()).write_text("{not json", encoding="utf-8")
    assert (await cache.complete(_request())).content == "fresh"
    assert cache.misses == 1


async def test_a_hit_is_recorded_as_fake_with_the_stored_usage(tmp_path: Path) -> None:
    inner = ScriptedProvider({HAIKU: [ok("answer", input_tokens=1000, output_tokens=100)]})
    recorder = MemoryRecorder()
    gateway = dataclasses.replace(
        make_gateway(ScriptedProvider({}), recorder), provider=CachingProvider(inner, tmp_path)
    )
    await gateway.complete("triage", [{"role": "user", "content": "q"}], call=CALL)
    await gateway.complete("triage", [{"role": "user", "content": "q"}], call=CALL)
    paid, replayed = recorder.rows
    assert paid.mode == LlmCallMode.REAL
    assert replayed.mode == LlmCallMode.FAKE
    assert replayed.status == LlmCallStatus.OK
    # The stored usage and its cost are kept (like a recording); `mode` says nothing was paid.
    assert (replayed.input_tokens, replayed.output_tokens) == (1000, 100)
    assert replayed.cost_usd == paid.cost_usd > 0
    assert len(inner.requests) == 1


async def test_a_missed_call_still_gets_retries_and_the_fallback(tmp_path: Path) -> None:
    error = ProviderError("Overloaded", transient=True)
    inner = ScriptedProvider({HAIKU: [error, error, error], SONNET: [ok("fallback")]})
    gateway = dataclasses.replace(
        make_gateway(ScriptedProvider({})), provider=CachingProvider(inner, tmp_path)
    )
    completion = await gateway.complete("triage", [{"role": "user", "content": "q"}], call=CALL)
    assert completion.fallback_used
    # A raised provider error is never stored; only the fallback's answer is.
    # Only the answer that came is stored, under the fallback model's request.
    assert len(list(tmp_path.glob("*.json"))) == 1


async def test_all_failures_raise_and_store_nothing(tmp_path: Path) -> None:
    error = ProviderError("Bad", transient=False)
    inner = ScriptedProvider({HAIKU: [error], SONNET: [error]})
    gateway = dataclasses.replace(
        make_gateway(ScriptedProvider({})), provider=CachingProvider(inner, tmp_path)
    )
    with pytest.raises(LlmUnavailableError):
        await gateway.complete("triage", [{"role": "user", "content": "q"}], call=CALL)
    assert list(tmp_path.iterdir()) == []


def test_build_gateway_wraps_the_provider_in_one_folder_per_mode(tmp_path: Path) -> None:
    fake = build_gateway(
        Settings(_env_file=None, models_config=MODELS_YAML),
        MemoryRecorder(),
        None,
        cache_dir=tmp_path,
    )
    assert isinstance(fake.provider, CachingProvider)
    assert fake.provider.path(_request()).parent == tmp_path / "fake"
    real = build_gateway(
        Settings(_env_file=None, models_config=MODELS_YAML, llm_mode="real"),
        MemoryRecorder(),
        None,
        cache_dir=tmp_path,
    )
    assert isinstance(real.provider, CachingProvider)
    assert real.provider.path(_request()).parent == tmp_path / "real"
    # Without a cache folder (the app) there is no cache: only the local embeddings in front.
    plain = build_gateway(
        Settings(_env_file=None, models_config=MODELS_YAML), MemoryRecorder(), None
    )
    assert isinstance(plain.provider, LocalEmbeddingProvider)
    assert isinstance(plain.provider.inner, FixtureProvider)


def test_usage_survives_the_round_trip(tmp_path: Path) -> None:
    response = ProviderResponse(content="x", usage=Usage(input_tokens=5, cached_tokens=2))
    assert response.model_copy(update={"replayed": True}).usage == response.usage
