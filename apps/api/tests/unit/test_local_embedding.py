"""The local embedding backend (`hub/llm/local_embedding.py`, ADR-0018, P5-01) with a stub
model: loading, pinning, errors, and the gateway rows. The real model runs in the integration
tests (`test_kb_index.py`)."""

import hashlib
from collections.abc import Sequence
from decimal import Decimal
from pathlib import Path

import pytest
import yaml

from hub.domain.enums import LlmCallContext, LlmCallStatus, LlmNode
from hub.llm import local_embedding
from hub.llm.config import parse_models
from hub.llm.gateway import embedder
from hub.llm.local_embedding import (
    LocalEmbeddingProvider,
    ModelFileError,
    PinnedModel,
    load_text_model,
    snapshot_file,
    verify_model_file,
)
from hub.llm.schemas import (
    BudgetExceededError,
    CallContext,
    LlmUnavailableError,
    ProviderError,
    ProviderRequest,
)

from ._llm import MODELS_YAML, MemoryRecorder, ScriptedProvider, make_gateway

MODEL = "fastembed/BAAI/bge-small-en-v1.5"


class StubModel:
    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return [[float(len(text)), 1.0] for text in texts]

    def token_counts(self, texts: Sequence[str]) -> list[int]:
        return [len(text.split()) + 2 for text in texts]


class Loader:
    def __init__(self, fail: bool = False) -> None:
        self.calls: list[tuple[str, Path]] = []
        self.fail = fail

    def __call__(self, name: str, cache_dir: Path) -> StubModel:
        self.calls.append((name, cache_dir))
        if self.fail:
            raise ModelFileError("missing")
        return StubModel()


def _request(texts: list[str], model: str = MODEL) -> ProviderRequest:
    return ProviderRequest(kind="embedding", model=model, texts=texts, timeout_s=5)


async def test_embeddings_come_from_the_local_model_which_loads_once(tmp_path: Path) -> None:
    loader = Loader()
    provider = LocalEmbeddingProvider(ScriptedProvider({}), tmp_path, loader)
    first = await provider.embed(_request(["a b", "c"]))
    await provider.embed(_request(["d"]))
    assert first.vectors == [[3.0, 1.0], [1.0, 1.0]]
    assert first.usage.input_tokens == 4 + 3
    assert loader.calls == [("BAAI/bge-small-en-v1.5", tmp_path)]
    assert provider.token_counts(MODEL, ["one two"]) == [4]


async def test_chat_calls_go_to_the_inner_provider(tmp_path: Path) -> None:
    inner = ScriptedProvider({})
    provider = LocalEmbeddingProvider(inner, tmp_path, Loader())
    assert provider.inner is inner


async def test_a_model_that_cannot_load_is_not_retried(tmp_path: Path) -> None:
    provider = LocalEmbeddingProvider(ScriptedProvider({}), tmp_path, Loader(fail=True))
    with pytest.raises(ProviderError) as raised:
        await provider.embed(_request(["a"]))
    assert (raised.value.kind, raised.value.transient) == ("LocalEmbeddingNotAvailable", False)


async def test_a_provider_model_is_refused(tmp_path: Path) -> None:
    provider = LocalEmbeddingProvider(ScriptedProvider({}), tmp_path, Loader())
    with pytest.raises(ProviderError) as raised:
        await provider.embed(_request(["a"], model="anthropic/claude-haiku-5-5"))
    assert raised.value.kind == "NotALocalModel"


async def test_the_gateway_records_a_free_row_and_skips_the_budget_guard(tmp_path: Path) -> None:
    class Refuses:
        async def check(self) -> None:
            raise BudgetExceededError(Decimal("0.5"), Decimal("0.5"))

    recorder = MemoryRecorder()
    gateway = make_gateway(
        LocalEmbeddingProvider(ScriptedProvider({}), tmp_path, Loader()),
        recorder=recorder,
        budget=Refuses(),
    )
    call = CallContext(context=LlmCallContext.ONLINE, node=LlmNode.EMBEDDING)
    result = await gateway.embed(["a", "b"], call=call)
    assert result.cost_usd == Decimal(0)
    (row,) = recorder.rows
    assert (row.node, row.status, row.cost_usd, row.prompt_version) == (
        LlmNode.EMBEDDING, LlmCallStatus.OK, Decimal(0), None,
    )  # fmt: skip
    # A chat call through the same gateway is still refused by the guard.
    with pytest.raises(BudgetExceededError):
        await gateway.complete(
            "triage",
            [{"role": "user", "content": "x"}],
            call=CallContext(context=LlmCallContext.ONLINE, node=LlmNode.TRIAGE),
        )


async def test_a_missing_model_ends_as_llm_unavailable(tmp_path: Path) -> None:
    recorder = MemoryRecorder()
    gateway = make_gateway(
        LocalEmbeddingProvider(ScriptedProvider({}), tmp_path, Loader(fail=True)),
        recorder=recorder,
    )
    call = CallContext(context=LlmCallContext.ONLINE, node=LlmNode.EMBEDDING)
    with pytest.raises(LlmUnavailableError):
        await gateway.embed(["a"], call=call)
    assert [row.error for row in recorder.rows] == ["LocalEmbeddingNotAvailable"]


# --- pinning -------------------------------------------------------------------------------------


def _cache(tmp_path: Path, content: bytes, revision: str = "abc") -> PinnedModel:
    repo = tmp_path / "models--Org--model-onnx"
    (repo / "refs").mkdir(parents=True)
    (repo / "refs" / "main").write_text(revision)
    (repo / "snapshots" / revision).mkdir(parents=True)
    (repo / "snapshots" / revision / "model.onnx").write_bytes(content)
    return PinnedModel(
        name="Org/model",
        source_repo="Org/model-onnx",
        model_file="model.onnx",
        sha256=hashlib.sha256(b"pinned").hexdigest(),
        revision_seen=revision,
    )


def test_the_pinned_file_passes(tmp_path: Path) -> None:
    pinned = _cache(tmp_path, b"pinned")
    assert verify_model_file(tmp_path, pinned) == snapshot_file(tmp_path, pinned)


def test_a_changed_file_fails(tmp_path: Path) -> None:
    pinned = _cache(tmp_path, b"changed upstream")
    with pytest.raises(ModelFileError, match="pinned SHA-256"):
        verify_model_file(tmp_path, pinned)


def test_a_missing_model_fails_without_a_download(tmp_path: Path) -> None:
    pinned = _cache(tmp_path / "other", b"pinned")
    with pytest.raises(ModelFileError, match="make models"):
        verify_model_file(tmp_path, pinned)
    with pytest.raises(ModelFileError, match="make models"):
        load_text_model("BAAI/bge-small-en-v1.5", tmp_path)


def test_refs_main_naming_a_missing_snapshot(tmp_path: Path) -> None:
    pinned = _cache(tmp_path, b"pinned")
    (tmp_path / "models--Org--model-onnx" / "refs" / "main").write_text("gone")
    assert snapshot_file(tmp_path, pinned) is None


def test_only_pinned_models_load(tmp_path: Path) -> None:
    with pytest.raises(ModelFileError, match="not a pinned"):
        load_text_model("some/other-model", tmp_path)


def test_the_pin_is_the_configured_model() -> None:
    (pinned,) = local_embedding.PINNED_MODELS.values()
    assert f"fastembed/{pinned.name}" == MODEL
    assert len(pinned.sha256) == 64


def test_make_models_reports_a_bad_download(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(cache_dir: Path) -> list[str]:
        raise ModelFileError("BAAI/bge-small-en-v1.5: model.onnx does not have the pinned SHA-256")

    monkeypatch.setattr(local_embedding, "download", broken)
    assert local_embedding.main([str(tmp_path)]) == 1
    assert "pinned SHA-256" in capsys.readouterr().err

    monkeypatch.setattr(local_embedding, "download", lambda cache_dir: ["ok line"])
    assert local_embedding.main([str(tmp_path)]) == 0
    assert "models: ok line" in capsys.readouterr().out


async def test_the_embedder_helper_calls_the_gateway_with_its_context(tmp_path: Path) -> None:
    recorder = MemoryRecorder()
    gateway = make_gateway(
        LocalEmbeddingProvider(ScriptedProvider({}), tmp_path, Loader()), recorder=recorder
    )
    call = CallContext(context=LlmCallContext.EVAL, node=LlmNode.EMBEDDING)
    assert await embedder(gateway, call)(["abc"]) == [[3.0, 1.0]]
    assert [row.context for row in recorder.rows] == [LlmCallContext.EVAL]


async def test_an_embedding_model_that_is_not_local_is_still_budget_checked(
    tmp_path: Path,
) -> None:
    class Refuses:
        async def check(self) -> None:
            raise BudgetExceededError(Decimal("0.5"), Decimal("0.5"))

    data = yaml.safe_load(MODELS_YAML.read_text(encoding="utf-8"))
    data["models"]["voyage/voyage-3"] = {"tools": False}
    data["roles"]["embedding"]["model"] = "voyage/voyage-3"
    recorder = MemoryRecorder()
    gateway = make_gateway(
        LocalEmbeddingProvider(ScriptedProvider({}), tmp_path, Loader()),
        recorder=recorder,
        models=parse_models(data),
        budget=Refuses(),
    )
    call = CallContext(context=LlmCallContext.ONLINE, node=LlmNode.EMBEDDING)
    with pytest.raises(BudgetExceededError):
        await gateway.embed(["a"], call=call)
    assert recorder.rows == []  # refused before any attempt
