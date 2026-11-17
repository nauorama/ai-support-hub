"""The Qdrant index of the knowledge base (data model §2, ADR-0005; P5-01).

Postgres (`kb_documents`, `kb_chunks`, written by `make kb-load`) is the source of truth; the
index is built from it and can always be rebuilt.

- Collection `kb_chunks_v<N>` behind the alias `kb_chunks`. Named vectors: `dense` (the local
  embedding model through the gateway, cosine) and `sparse` (BM25 from FastEmbed, IDF modifier
  on). Keyword payload indexes on `visibility`, `product` and `doc_id`.
- Point id: UUID v5 of the chunk id, so a chunk keeps its point.
- `sync` compares Postgres with the points and touches only what differs: a chunk whose text
  changed (`content_hash`) is embedded again, a chunk whose document data changed (title,
  version, …) gets its payload replaced without an embedding call, and a point whose chunk is
  gone is deleted. A sync that finds nothing to do embeds nothing. Because it compares state,
  not a list of changed files, a sync after a failed one repairs the index.
- `rebuild` embeds everything into a new collection `kb_chunks_v<N+1>`, moves the alias in one
  atomic operation, then deletes the old collection: searches never see a half-built index.
"""

import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from qdrant_client import AsyncQdrantClient, models
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from hub.db.models import KbChunk, KbDocument
from hub.kb.sections import StoredChunk, chunk_anchors, section_url
from hub.kb.sparse import SparseEncoder

ALIAS = "kb_chunks"
COLLECTION_PREFIX = "kb_chunks_v"
DENSE = "dense"
SPARSE = "sparse"
# Size of the local model's vectors (`BAAI/bge-small-en-v1.5`, ai-pipeline §8).
DENSE_SIZE = 384
# Texts per embedding call: one gateway call (and `llm_calls` row) per batch.
EMBED_BATCH = 32
SCROLL_PAGE = 256
KEYWORD_INDEXES = ("visibility", "product", "doc_id")
# Point ids are UUID v5 of the chunk id in this namespace (data model §2).
POINT_NAMESPACE = uuid.NAMESPACE_URL


# Dense vectors for texts. The app passes the gateway (`hub.llm.gateway.embedder`).
type DenseEmbedder = Callable[[Sequence[str]], Awaitable[list[list[float]]]]


class KbIndexError(Exception):
    """The index cannot be used as it is (wrong vector size, no collection behind the alias)."""


@dataclass(frozen=True, slots=True)
class IndexChunk:
    """One chunk with the document data its point carries (the payload of data model §2)."""

    chunk_id: str
    doc_id: str
    title: str
    section: str
    text: str
    visibility: str
    product: str
    intents: tuple[str, ...]
    url: str
    doc_version: int
    content_hash: str

    def payload(self) -> dict[str, Any]:
        return {
            "chunk_id": self.chunk_id,
            "doc_id": self.doc_id,
            "title": self.title,
            "section": self.section,
            "text": self.text,
            "visibility": self.visibility,
            "product": self.product,
            "intents": list(self.intents),
            "url": self.url,
            "doc_version": self.doc_version,
            "content_hash": self.content_hash,
        }


@dataclass
class IndexReport:
    collection: str = ""
    embedded: int = 0
    payload_updated: int = 0
    deleted: int = 0
    unchanged: int = 0
    embedded_chunk_ids: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.embedded or self.payload_updated or self.deleted)


def point_id(chunk_id: str) -> str:
    return str(uuid.uuid5(POINT_NAMESPACE, chunk_id))


async def read_chunks(session: AsyncSession) -> list[IndexChunk]:
    """Every chunk in Postgres with its document, in document and ordinal order."""
    rows = await session.execute(
        select(KbChunk, KbDocument)
        .join(KbDocument, KbChunk.doc_id == KbDocument.id)
        .order_by(KbChunk.doc_id, KbChunk.ordinal)
    )
    by_doc: dict[str, tuple[KbDocument, list[KbChunk]]] = {}
    for chunk, document in rows:
        by_doc.setdefault(document.id, (document, []))[1].append(chunk)
    result: list[IndexChunk] = []
    for doc_id, (document, chunks) in by_doc.items():
        anchors = chunk_anchors(
            doc_id, document.title, [StoredChunk(c.id, c.section, c.text) for c in chunks]
        )
        result.extend(
            IndexChunk(
                chunk_id=chunk.id,
                doc_id=doc_id,
                title=document.title,
                section=chunk.section,
                text=chunk.text,
                visibility=str(document.visibility),
                product=str(document.product),
                intents=tuple(str(intent) for intent in document.intents),
                url=section_url(doc_id, anchors[chunk.id]),
                doc_version=document.version,
                content_hash=chunk.content_hash,
            )
            for chunk in chunks
        )
    return result


async def postgres_chunk_count(session: AsyncSession) -> int:
    return int(await session.scalar(select(func.count()).select_from(KbChunk)) or 0)


class KbIndex:
    def __init__(
        self,
        client: AsyncQdrantClient,
        dense: DenseEmbedder,
        sparse: SparseEncoder,
        *,
        alias: str = ALIAS,
        prefix: str = COLLECTION_PREFIX,
    ) -> None:
        self._client = client
        self._dense = dense
        self._sparse = sparse
        self.alias = alias
        self._prefix = prefix

    async def collection(self) -> str | None:
        """The collection behind the alias, or None."""
        response = await self._client.get_aliases()
        for item in response.aliases:
            if item.alias_name == self.alias:
                return item.collection_name
        return None

    async def ensure(self) -> str:
        """The collection behind the alias; creates `<prefix>1` (or the next free number) and
        the alias when there is none."""
        current = await self.collection()
        if current is not None:
            await self._check_size(current)
            return current
        name = await self._new_collection()
        await self._move_alias(name, had_alias=False)
        return name

    async def sync(self, chunks: Sequence[IndexChunk]) -> IndexReport:
        """Make the points of the aliased collection equal `chunks`, changing only what
        differs (module docstring)."""
        name = await self.ensure()
        report = IndexReport(collection=name)
        stored = await self._stored_payloads(name)
        wanted = {chunk.chunk_id for chunk in chunks}
        to_embed: list[IndexChunk] = []
        for chunk in chunks:
            payload = stored.get(chunk.chunk_id)
            if payload is None or payload.get("content_hash") != chunk.content_hash:
                to_embed.append(chunk)
            elif payload != chunk.payload():
                await self._client.overwrite_payload(
                    name,
                    payload=chunk.payload(),
                    points=models.PointIdsList(points=[point_id(chunk.chunk_id)]),
                )
                report.payload_updated += 1
            else:
                report.unchanged += 1
        await self._upsert(name, to_embed)
        report.embedded = len(to_embed)
        report.embedded_chunk_ids = [chunk.chunk_id for chunk in to_embed]
        stale = sorted(chunk_id for chunk_id in stored if chunk_id not in wanted)
        if stale:
            await self._client.delete(
                name, points_selector=models.PointIdsList(points=[point_id(c) for c in stale])
            )
            report.deleted = len(stale)
        return report

    async def rebuild(self, chunks: Sequence[IndexChunk]) -> IndexReport:
        """Embed every chunk into a new collection, move the alias to it, drop the old one."""
        old = await self.collection()
        name = await self._new_collection()
        try:
            await self._upsert(name, chunks)
        except BaseException:
            await self._client.delete_collection(name)
            raise
        await self._move_alias(name, had_alias=old is not None)
        if old is not None:
            await self._client.delete_collection(old)
        return IndexReport(
            collection=name,
            embedded=len(chunks),
            embedded_chunk_ids=[chunk.chunk_id for chunk in chunks],
        )

    async def count(self) -> int:
        """Points behind the alias (exact); 0 when there is no index yet."""
        if await self.collection() is None:
            return 0
        return (await self._client.count(self.alias, exact=True)).count

    # --- helpers ---------------------------------------------------------------------------

    async def _new_collection(self) -> str:
        existing = (await self._client.get_collections()).collections
        numbers = [
            int(item.name.removeprefix(self._prefix))
            for item in existing
            if item.name.startswith(self._prefix) and item.name.removeprefix(self._prefix).isdigit()
        ]
        name = f"{self._prefix}{max(numbers, default=0) + 1}"
        await self._client.create_collection(
            name,
            vectors_config={
                DENSE: models.VectorParams(size=DENSE_SIZE, distance=models.Distance.COSINE)
            },
            sparse_vectors_config={SPARSE: models.SparseVectorParams(modifier=models.Modifier.IDF)},
        )
        try:
            for field_name in KEYWORD_INDEXES:
                await self._client.create_payload_index(
                    name, field_name=field_name, field_schema=models.PayloadSchemaType.KEYWORD
                )
        except BaseException:
            # No half-built collection is left behind.
            await self._client.delete_collection(name)
            raise
        return name

    async def _move_alias(self, name: str, *, had_alias: bool) -> None:
        operations: list[models.AliasOperations] = []
        if had_alias:
            operations.append(
                models.DeleteAliasOperation(delete_alias=models.DeleteAlias(alias_name=self.alias))
            )
        operations.append(
            models.CreateAliasOperation(
                create_alias=models.CreateAlias(collection_name=name, alias_name=self.alias)
            )
        )
        # One request: Qdrant applies the alias changes atomically.
        await self._client.update_collection_aliases(change_aliases_operations=operations)

    async def _check_size(self, name: str) -> None:
        info = await self._client.get_collection(name)
        vectors = info.config.params.vectors
        size = vectors[DENSE].size if isinstance(vectors, dict) and DENSE in vectors else None
        if size != DENSE_SIZE:
            raise KbIndexError(
                f"collection {name}: dense vector size {size}, expected {DENSE_SIZE}; "
                "run a full re-index"
            )

    async def _stored_payloads(self, name: str) -> dict[str, dict[str, Any]]:
        stored: dict[str, dict[str, Any]] = {}
        offset: models.ExtendedPointId | None = None
        while True:
            points, offset = await self._client.scroll(
                name, limit=SCROLL_PAGE, offset=offset, with_payload=True, with_vectors=False
            )
            for point in points:
                payload = dict(point.payload or {})
                chunk_id = payload.get("chunk_id")
                if isinstance(chunk_id, str):
                    stored[chunk_id] = payload
            if offset is None:
                return stored

    async def _upsert(self, name: str, chunks: Sequence[IndexChunk]) -> None:
        for start in range(0, len(chunks), EMBED_BATCH):
            batch = chunks[start : start + EMBED_BATCH]
            texts = [chunk.text for chunk in batch]
            dense = await self._dense(texts)
            sparse = await self._sparse.documents(texts)
            if len(dense) != len(batch) or len(sparse) != len(batch):
                raise KbIndexError("the embedder returned a wrong number of vectors")
            await self._client.upsert(
                name,
                points=[
                    models.PointStruct(
                        id=point_id(chunk.chunk_id),
                        vector={
                            DENSE: vector,
                            SPARSE: models.SparseVector(indices=bm25.indices, values=bm25.values),
                        },
                        payload=chunk.payload(),
                    )
                    for chunk, vector, bm25 in zip(batch, dense, sparse, strict=True)
                ],
            )


def connect_qdrant(url: str, api_key: str) -> AsyncQdrantClient:
    """No version check at construction (`check_compatibility`): it would be a network call
    while the app container is built. The client and the server image are pinned together."""
    return AsyncQdrantClient(url=url, api_key=api_key or None, check_compatibility=False)
