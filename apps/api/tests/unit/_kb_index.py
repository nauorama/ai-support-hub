"""Test doubles for the knowledge-base index: word-hash dense vectors and BM25-like sparse
vectors, so index and search logic run without the local model. Qdrant runs in its local
in-memory mode (`location=":memory:"`); the real server is in the integration tests."""

import hashlib
import math
import re
from collections import Counter
from collections.abc import Sequence

from hub.kb.index import DENSE_SIZE, IndexChunk
from hub.kb.sparse import SparseVector

_WORD = re.compile(r"[a-z0-9]+(?:-[0-9]+)?")


def words(text: str) -> list[str]:
    return _WORD.findall(text.lower())


def _bucket(word: str, size: int) -> int:
    return int(hashlib.sha256(word.encode()).hexdigest()[:8], 16) % size


class FakeDense:
    """Normalised bag-of-words vectors of size 384. Records every batch it embeds."""

    def __init__(self) -> None:
        self.batches: list[list[str]] = []

    async def __call__(self, texts: Sequence[str]) -> list[list[float]]:
        self.batches.append(list(texts))
        vectors = []
        for text in texts:
            vector = [0.0] * DENSE_SIZE
            for word in words(text):
                vector[_bucket(word, DENSE_SIZE)] += 1.0
            norm = math.sqrt(sum(v * v for v in vector)) or 1.0
            vectors.append([v / norm for v in vector])
        return vectors

    @property
    def texts(self) -> list[str]:
        return [text for batch in self.batches for text in batch]


class FakeSparse:
    async def documents(self, texts: Sequence[str]) -> list[SparseVector]:
        return [self._vector(text) for text in texts]

    async def query(self, text: str) -> SparseVector:
        return self._vector(text)

    @staticmethod
    def _vector(text: str) -> SparseVector:
        counts = Counter(_bucket(word, 1 << 20) for word in words(text))
        indices = sorted(counts)
        return SparseVector(indices=indices, values=[float(counts[i]) for i in indices])


def chunk(
    chunk_id: str,
    text: str,
    *,
    visibility: str = "external",
    title: str = "Title",
    version: int = 1,
) -> IndexChunk:
    doc_id = chunk_id.partition("#")[0]
    return IndexChunk(
        chunk_id=chunk_id,
        doc_id=doc_id,
        title=title,
        section="Section",
        text=text,
        visibility=visibility,
        product="general",
        intents=(),
        url=f"/help/{doc_id}#section",
        doc_version=version,
        content_hash=hashlib.sha256(text.encode()).hexdigest(),
    )
