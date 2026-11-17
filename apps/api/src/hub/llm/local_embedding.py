"""The local embedding backend of the gateway (ADR-0018, P5-01): FastEmbed runs
`BAAI/bge-small-en-v1.5` (ONNX, 384 dimensions) on the CPU. No API, no key, no cost.

Only `hub.llm.gateway` uses `LocalEmbeddingProvider`; every embedding still goes through
`Gateway.embed` (G2 scan, `llm_calls` row with cost 0).

**No network at run time.** `make models` (part of `make setup`) downloads the files into
`EMBEDDING_CACHE_DIR`. At run time the model loads with `local_files_only=True`, so a missing
model is an error, never a download.

**Pinning.** FastEmbed 0.9.1 has no revision option: it always downloads the revision the
Hugging Face repository has now (`model_info(...).sha` in `fastembed.common.model_management`).
The model file is therefore pinned by its SHA-256 (`PINNED_MODELS`), checked after the download
and again at every load: a changed file stops the setup and every embedding call.

Calls run in a worker thread (`asyncio.to_thread`): ONNX inference is CPU work and must not
block the event loop.
"""

import asyncio
import hashlib
import sys
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from hub.config import SettingsError, load_settings
from hub.llm.schemas import ProviderError, ProviderRequest, ProviderResponse, Usage

PROVIDER_PREFIX = "fastembed/"
# The model reads at most this many tokens; longer text is cut (fastembed tokenizer truncation).
MAX_INPUT_TOKENS = 512
BM25_MODEL = "Qdrant/bm25"


@dataclass(frozen=True, slots=True)
class PinnedModel:
    """A local model and the SHA-256 of its weights file, checked at download and load."""

    name: str  # FastEmbed model name
    source_repo: str  # Hugging Face repository FastEmbed downloads from
    model_file: str
    sha256: str
    revision_seen: str  # revision of the repository when the hash was recorded (information)


# Recorded on 2026-11-17 with fastembed 0.9.1 (P5-01).
PINNED_MODELS = {
    "BAAI/bge-small-en-v1.5": PinnedModel(
        name="BAAI/bge-small-en-v1.5",
        source_repo="Qdrant/bge-small-en-v1.5-onnx-Q",
        model_file="model_optimized.onnx",
        sha256="51f1bd0addd6e859e42c2c8021a5e5461385bb676a649f4b269aa445449f2431",
        revision_seen="aa8f8b060edb00e03bfdd08813a2949946c8ba55",
    ),
}


class ModelFileError(Exception):
    """The local model is missing, or its file is not the pinned one."""


class TextModel(Protocol):
    """What the provider needs of a loaded model (FastEmbed `TextEmbedding`; a stub in tests)."""

    def embed(self, texts: Sequence[str]) -> list[list[float]]: ...

    def token_counts(self, texts: Sequence[str]) -> list[int]: ...


class _FastEmbedModel:
    def __init__(self, name: str, cache_dir: Path) -> None:
        # Imported here: loading onnxruntime is slow, and unit tests never need it.
        from fastembed import TextEmbedding

        self._model = TextEmbedding(name, cache_dir=str(cache_dir), local_files_only=True)

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return [vector.tolist() for vector in self._model.embed(list(texts))]

    def token_counts(self, texts: Sequence[str]) -> list[int]:
        """Tokens of each text before truncation: the kept tokens plus every overflow piece
        (each piece repeats the special tokens, so a cut text counts a little high; a text
        that fits is exact)."""
        from fastembed.text.onnx_text_model import OnnxTextModel

        model = self._model.model
        if not isinstance(model, OnnxTextModel):
            raise ModelFileError(f"{type(model).__name__}: no tokenizer")
        encodings = model.tokenize(list(texts))
        return [
            len(encoding.ids) + sum(len(piece.ids) for piece in encoding.overflowing)
            for encoding in encodings
        ]


def snapshot_file(cache_dir: Path, model: PinnedModel) -> Path | None:
    """The model file FastEmbed loads with `local_files_only=True`, or None: the snapshot that
    `refs/main` names in the Hugging Face cache layout."""
    repo = cache_dir / f"models--{model.source_repo.replace('/', '--')}"
    try:
        revision = (repo / "refs" / "main").read_text(encoding="utf-8").strip()
    except OSError:
        return None
    path = repo / "snapshots" / revision / model.model_file
    return path if revision and path.is_file() else None


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def verify_model_file(cache_dir: Path, model: PinnedModel) -> Path:
    """Raises ModelFileError unless the cached copy exists and has the pinned hash."""
    path = snapshot_file(cache_dir, model)
    if path is None:
        raise ModelFileError(f"{model.name}: no cached copy in {cache_dir}; run `make models`")
    if file_sha256(path) != model.sha256:
        raise ModelFileError(f"{model.name}: {model.model_file} does not have the pinned SHA-256")
    return path


def load_text_model(name: str, cache_dir: Path) -> TextModel:
    pinned = PINNED_MODELS.get(name)
    if pinned is None:
        raise ModelFileError(f"{name}: not a pinned local model")
    verify_model_file(cache_dir, pinned)
    return _FastEmbedModel(name, cache_dir)


class ChatProvider(Protocol):
    """The chat half of `hub.llm.gateway.Provider` (kept here so the modules do not import
    each other)."""

    async def complete(self, request: ProviderRequest) -> ProviderResponse: ...


class LocalEmbeddingProvider:
    """Chat calls go to `inner` (LiteLLM, fixtures); embedding calls to a local model.

    Used in every `LLM_MODE`: the local model is free and makes no network call, so fake mode
    needs no recorded vectors. A model is loaded on its first call and kept. Any failure to
    load is a `ProviderError` that is not retried (`LocalEmbeddingNotAvailable`): the gateway
    records it and raises `LlmUnavailableError`.
    """

    def __init__(
        self,
        inner: ChatProvider,
        cache_dir: Path,
        loader: Callable[[str, Path], TextModel] = load_text_model,
    ) -> None:
        self._inner = inner
        self._cache_dir = cache_dir
        self._loader = loader
        self._models: dict[str, TextModel] = {}
        # A threading lock: models are loaded inside worker threads.
        self._lock = threading.Lock()

    @property
    def inner(self) -> ChatProvider:
        return self._inner

    def _model(self, provider_model: str) -> TextModel:
        if not provider_model.startswith(PROVIDER_PREFIX):
            raise ProviderError("NotALocalModel", transient=False)
        name = provider_model.removeprefix(PROVIDER_PREFIX)
        with self._lock:
            model = self._models.get(name)
            if model is None:
                try:
                    model = self._loader(name, self._cache_dir)
                except Exception:
                    # Only the class reaches the row (data model: error class only).
                    raise ProviderError("LocalEmbeddingNotAvailable", transient=False) from None
                self._models[name] = model
            return model

    def token_counts(self, provider_model: str, texts: Sequence[str]) -> list[int]:
        """Tokens per text for `provider_model`, before the model's truncation. Blocking."""
        return self._model(provider_model).token_counts(texts)

    async def complete(self, request: ProviderRequest) -> ProviderResponse:
        return await self._inner.complete(request)

    async def embed(self, request: ProviderRequest) -> ProviderResponse:
        def run() -> ProviderResponse:
            model = self._model(request.model)
            vectors = model.embed(request.texts)
            tokens = sum(model.token_counts(request.texts))
            return ProviderResponse(vectors=vectors, usage=Usage(input_tokens=tokens))

        return await asyncio.to_thread(run)


# --- `make models`: download once, with the network ----------------------------------------


def download(cache_dir: Path) -> list[str]:
    """Download the dense model and the BM25 files into `cache_dir`, then check the pins.
    Returns one line per model for the console."""
    from fastembed import SparseTextEmbedding, TextEmbedding

    cache_dir.mkdir(parents=True, exist_ok=True)
    lines = []
    for pinned in PINNED_MODELS.values():
        TextEmbedding(pinned.name, cache_dir=str(cache_dir))
        path = verify_model_file(cache_dir, pinned)
        lines.append(
            f"{pinned.name}: {path.name} sha256 {pinned.sha256} (revision {path.parent.name})"
        )
    # BM25 has no weights, only stop-word lists; loading it fetches them.
    SparseTextEmbedding(BM25_MODEL, cache_dir=str(cache_dir))
    lines.append(f"{BM25_MODEL}: stop-word files cached")
    return lines


def main(argv: list[str]) -> int:
    try:
        cache_dir = Path(argv[0]) if argv else load_settings().embedding_cache_dir
        for line in download(cache_dir):
            print(f"models: {line}")
    except (SettingsError, ModelFileError) as error:
        print(f"models: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
