"""BM25 sparse vectors for the knowledge-base index (ADR-0005), from FastEmbed's `Qdrant/bm25`.

BM25 is no model call: it stems and counts words (stop-word lists only, no weights), so it runs
here and not through the gateway (build plan P5-01: "BM25 through FastEmbed"). Documents get
term-frequency weights; queries get weight 1 per term. Qdrant applies the IDF part at query time
(the `sparse` vector is created with `modifier: idf`, data model §2).

The files come from `make models` like the dense model; they load with `local_files_only=True`,
so a missing file is an error, never a download. Work runs in a worker thread.
"""

import asyncio
import threading
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from fastembed import SparseEmbedding, SparseTextEmbedding

BM25_MODEL = "Qdrant/bm25"


@dataclass(frozen=True, slots=True)
class SparseVector:
    indices: list[int]
    values: list[float]


class SparseEncoder(Protocol):
    async def documents(self, texts: Sequence[str]) -> list[SparseVector]: ...

    async def query(self, text: str) -> SparseVector: ...


class SparseModelError(Exception):
    """The BM25 files are missing from the model folder (`make models`)."""


class Bm25Encoder:
    """FastEmbed BM25, loaded on first use and kept."""

    def __init__(self, cache_dir: Path) -> None:
        self._cache_dir = cache_dir
        self._model: SparseTextEmbedding | None = None
        self._lock = threading.Lock()

    def _load(self) -> "SparseTextEmbedding":
        with self._lock:
            if self._model is None:
                # Imported here: fastembed loads onnxruntime, slow and unused in unit tests.
                from fastembed import SparseTextEmbedding

                try:
                    self._model = SparseTextEmbedding(
                        BM25_MODEL, cache_dir=str(self._cache_dir), local_files_only=True
                    )
                except Exception:
                    raise SparseModelError(
                        f"{BM25_MODEL} is not in {self._cache_dir}; run `make models`"
                    ) from None
            return self._model

    async def documents(self, texts: Sequence[str]) -> list[SparseVector]:
        def run() -> list[SparseVector]:
            model = self._load()
            return [_vector(item) for item in model.embed(list(texts))]

        return await asyncio.to_thread(run)

    async def query(self, text: str) -> SparseVector:
        def run() -> SparseVector:
            model = self._load()
            return _vector(next(iter(model.query_embed(text))))

        return await asyncio.to_thread(run)


def _vector(embedding: "SparseEmbedding") -> SparseVector:
    return SparseVector(
        indices=[int(i) for i in embedding.indices],
        values=[float(v) for v in embedding.values],
    )
