"""Knowledge-base search (ai-pipeline §4, data model §2; P5-02).

`search(query, visibility, top_k, mode)`:
- `hybrid` (default): Qdrant Query API with two prefetches (dense top 20, sparse BM25 top 20)
  fused with reciprocal rank fusion. Exact tokens (`SWIFT`) come from BM25, loose wording
  (`my money is stuck`) from the dense vector (ADR-0005).
  **Exact error-code lookup** (ADR-0020, P5-02): an error code in the query (`ERR-2003`,
  any case) puts its chunk `error-codes#ERR-2003` first, then the fused hits without it. BM25
  alone cannot do this: FastEmbed's tokenizer splits `ERR-2003` into `err` and `2003`, three
  articles mention the same code, and the small dense model barely tells codes apart, so the
  code chunk ranked 4th to 6th on the real index. A pinned hit gets the top fused score, so
  scores never rise above what the fusion gave and stay in rank order.
- `dense`: the dense vector alone (variant `rag_dense`, ai-pipeline §10), without the lookup.

**Ties** (P5-03): reciprocal rank fusion often gives two chunks the same score (rank 3 in one
list and 4 in the other, or the other way round), and the Qdrant server orders equal scores
differently from run to run. The same ticket would then get a different writer prompt, so
recordings and the evaluation cache would not match. Equal scores are therefore ordered by chunk
id, and the cut to `top_k` is made here, not by the server. Hybrid asks for every fused
candidate (`FUSED_LIMIT`, both prefetch lists), so the order is fully deterministic. Dense asks
for `PREFETCH_LIMIT` hits: a tie group that reaches past the 20th hit is still cut by the
server. That is a bound, not a guarantee, and needs exactly equal cosine scores.

There is no reranker mode (ADR-0018). `visibility="external"` filters every prefetch and the
final query on `visibility = external`, so an internal chunk can never reach a customer
(AGENTS.md never-do 5); `"all"` is for operators (the console search).
"""

import re
from collections.abc import Mapping, Sequence
from typing import Any, Literal

from pydantic import BaseModel
from qdrant_client import AsyncQdrantClient, models

from hub.domain.kb import ERROR_CODES_DOC_ID
from hub.kb.index import ALIAS, DENSE, SPARSE, DenseEmbedder, point_id
from hub.kb.sparse import SparseEncoder

SearchMode = Literal["dense", "hybrid"]
SearchVisibility = Literal["external", "all"]
DEFAULT_TOP_K = 6
PREFETCH_LIMIT = 20
# Every candidate RRF can return: the union of the two prefetch lists.
FUSED_LIMIT = 2 * PREFETCH_LIMIT
MAX_TOP_K = 20
_ERROR_CODE = re.compile(r"(?<![A-Za-z0-9])ERR-\d{4}(?!\d)", re.IGNORECASE)


class KbChunk(BaseModel):
    """One search hit (ai-pipeline §4). `rank` starts at 1. `url` opens the chunk's section
    (`/help/<doc_id>#<anchor>`)."""

    chunk_id: str
    doc_id: str
    title: str
    section: str
    url: str
    text: str
    visibility: str
    score: float
    rank: int


def error_code_chunk_ids(query: str) -> list[str]:
    """`error-codes#ERR-NNNN` for every error code in the query, in order, without repeats."""
    codes = dict.fromkeys(match.upper() for match in _ERROR_CODE.findall(query))
    return [f"{ERROR_CODES_DOC_ID}#{code}" for code in codes]


def visibility_filter(visibility: SearchVisibility) -> models.Filter | None:
    if visibility == "all":
        return None
    return models.Filter(
        must=[models.FieldCondition(key="visibility", match=models.MatchValue(value=visibility))]
    )


class Retriever:
    def __init__(
        self,
        client: AsyncQdrantClient,
        dense: DenseEmbedder,
        sparse: SparseEncoder,
        *,
        alias: str = ALIAS,
    ) -> None:
        self._client = client
        self._dense = dense
        self._sparse = sparse
        self._alias = alias

    async def search(
        self,
        query: str,
        visibility: SearchVisibility,
        top_k: int = DEFAULT_TOP_K,
        mode: SearchMode = "hybrid",
    ) -> list[KbChunk]:
        if not 1 <= top_k <= MAX_TOP_K:
            raise ValueError(f"top_k must be 1..{MAX_TOP_K}")
        if not query.strip():
            return []
        where = visibility_filter(visibility)
        (dense,) = await self._dense([query])
        if mode == "dense":
            response = await self._client.query_points(
                self._alias, query=dense, using=DENSE, query_filter=where, limit=PREFETCH_LIMIT
            )
            ranked = _by_score([(p.payload or {}, p.score) for p in response.points])
            return _hits(ranked, visibility)[:top_k]
        prefetch = [models.Prefetch(query=dense, using=DENSE, filter=where, limit=PREFETCH_LIMIT)]
        sparse = await self._sparse.query(query)
        # A query of stop words only has no BM25 terms; an empty sparse query is not sent.
        if sparse.indices:
            prefetch.append(
                models.Prefetch(
                    query=models.SparseVector(indices=sparse.indices, values=sparse.values),
                    using=SPARSE,
                    filter=where,
                    limit=PREFETCH_LIMIT,
                )
            )
        response = await self._client.query_points(
            self._alias,
            prefetch=prefetch,
            query=models.FusionQuery(fusion=models.Fusion.RRF),
            query_filter=where,
            limit=FUSED_LIMIT,
        )
        fused = _by_score([(point.payload or {}, point.score) for point in response.points])
        pinned = await self._error_code_payloads(query)
        if not pinned:
            return _hits(fused, visibility)[:top_k]
        top_score = max((score for _, score in fused), default=0.0)
        ids = {payload.get("chunk_id") for payload in pinned}
        first: list[tuple[Mapping[str, Any], float]] = [(payload, top_score) for payload in pinned]
        rest = [(payload, score) for payload, score in fused if payload.get("chunk_id") not in ids]
        return _hits(first + rest, visibility)[:top_k]

    async def _error_code_payloads(self, query: str) -> list[Mapping[str, Any]]:
        """Payloads of the error-code chunks named in the query that exist in the index."""
        chunk_ids = error_code_chunk_ids(query)
        if not chunk_ids:
            return []
        records = await self._client.retrieve(
            self._alias, ids=[point_id(chunk_id) for chunk_id in chunk_ids], with_payload=True
        )
        found = {str((r.payload or {}).get("chunk_id")): r.payload or {} for r in records}
        return [found[chunk_id] for chunk_id in chunk_ids if chunk_id in found]


def _by_score(
    points: Sequence[tuple[Mapping[str, Any], float]],
) -> list[tuple[Mapping[str, Any], float]]:
    """Highest score first; equal scores by chunk id, so the order never depends on the
    server."""
    return sorted(points, key=lambda point: (-point[1], str(point[0].get("chunk_id"))))


def _hits(
    points: Sequence[tuple[Mapping[str, Any], float]], visibility: SearchVisibility
) -> list[KbChunk]:
    """`(payload, score)` in rank order → hits. A point of another visibility is dropped here
    as well, so the rule does not rest on the query filter alone."""
    hits: list[KbChunk] = []
    for payload, score in points:
        if visibility != "all" and payload.get("visibility") != visibility:
            continue
        hits.append(
            KbChunk(
                chunk_id=str(payload["chunk_id"]),
                doc_id=str(payload["doc_id"]),
                title=str(payload["title"]),
                section=str(payload["section"]),
                url=str(payload["url"]),
                text=str(payload["text"]),
                visibility=str(payload["visibility"]),
                score=float(score),
                rank=len(hits) + 1,
            )
        )
    return hits
