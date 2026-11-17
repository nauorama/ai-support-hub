"""Index sync and rebuild, and the retriever, against Qdrant's local in-memory mode with fake
embedders (P5-01, P5-02). The real server and the real models: integration tests."""

import dataclasses
import uuid
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import cast

import pytest
from qdrant_client import AsyncQdrantClient

from hub.kb import retriever as retriever_module
from hub.kb.index import ALIAS, DENSE_SIZE, KbIndex, KbIndexError, point_id
from hub.kb.retriever import (
    MAX_TOP_K,
    TIE_MARGIN,
    Retriever,
    error_code_chunk_ids,
    visibility_filter,
)
from hub.kb.sparse import SparseVector

from ._kb_index import FakeDense, FakeSparse, chunk

# Local mode warns that payload indexes have no effect there; the server test covers them.
pytestmark = pytest.mark.filterwarnings("ignore:Payload indexes have no effect")

CHUNKS = [
    chunk("withdrawal-times#0", "Withdrawal times\nSEPA\n\nA SEPA withdrawal takes one day."),
    chunk("withdrawal-times#1", "Withdrawal times\nSWIFT\n\nA SWIFT withdrawal takes five days."),
    chunk("card-declined#0", "Card declined\nWhy\n\nA card payment can be declined."),
    chunk("error-codes#ERR-2003", "Error codes\nERR-2003 Card blocked\n\nYour card is blocked."),
    chunk(
        "fraud-playbook#0",
        "Fraud playbook\nSteps\n\nInternal steps for a SEPA withdrawal fraud review.",
        visibility="internal",
    ),
]


@pytest.fixture
async def client() -> AsyncIterator[AsyncQdrantClient]:
    client = AsyncQdrantClient(location=":memory:")
    yield client
    await client.close()


@pytest.fixture
def dense() -> FakeDense:
    return FakeDense()


@pytest.fixture
def index(client: AsyncQdrantClient, dense: FakeDense) -> KbIndex:
    return KbIndex(client, dense, FakeSparse())


@pytest.fixture
def retriever(client: AsyncQdrantClient, dense: FakeDense) -> Retriever:
    return Retriever(client, dense, FakeSparse())


def test_point_ids_are_uuid5_of_the_chunk_id() -> None:
    assert point_id("withdrawal-times#2") == str(
        uuid.uuid5(uuid.NAMESPACE_URL, "withdrawal-times#2")
    )
    assert point_id("a#0") != point_id("a#1")


def test_the_payload_has_the_fields_of_the_data_model() -> None:
    # data model §2, plus content_hash (what the sync compares).
    assert set(CHUNKS[0].payload()) == {
        "chunk_id", "doc_id", "title", "section", "text", "visibility", "product", "intents",
        "url", "doc_version", "content_hash",
    }  # fmt: skip


async def test_the_first_sync_creates_collection_alias_and_points(
    index: KbIndex, client: AsyncQdrantClient
) -> None:
    assert await index.count() == 0
    report = await index.sync(CHUNKS)
    assert (report.collection, report.embedded, report.deleted) == ("kb_chunks_v1", 5, 0)
    assert await index.collection() == "kb_chunks_v1"
    assert await index.count() == len(CHUNKS)
    info = await client.get_collection("kb_chunks_v1")
    vectors = info.config.params.vectors
    assert isinstance(vectors, dict)
    assert vectors["dense"].size == DENSE_SIZE
    sparse = info.config.params.sparse_vectors
    assert sparse is not None
    assert sparse["sparse"].modifier == "idf"


async def test_a_sync_with_nothing_changed_embeds_nothing(index: KbIndex, dense: FakeDense) -> None:
    await index.sync(CHUNKS)
    dense.batches.clear()
    report = await index.sync(CHUNKS)
    assert not report.changed
    assert report.unchanged == len(CHUNKS)
    assert dense.batches == []


async def test_a_changed_chunk_is_embedded_again_and_only_it(
    index: KbIndex, dense: FakeDense
) -> None:
    await index.sync(CHUNKS)
    dense.batches.clear()
    edited = chunk("withdrawal-times#1", "Withdrawal times\nSWIFT\n\nNow it takes four days.")
    report = await index.sync([edited if c.chunk_id == edited.chunk_id else c for c in CHUNKS])
    assert report.embedded_chunk_ids == ["withdrawal-times#1"]
    assert dense.texts == [edited.text]
    assert report.unchanged == len(CHUNKS) - 1


async def test_a_document_change_without_new_text_updates_the_payload_only(
    index: KbIndex, dense: FakeDense, client: AsyncQdrantClient
) -> None:
    await index.sync(CHUNKS)
    dense.batches.clear()
    renamed = [
        dataclasses.replace(c, title="New title", doc_version=2)
        if c.doc_id == "withdrawal-times"
        else c
        for c in CHUNKS
    ]
    report = await index.sync(renamed)
    assert (report.embedded, report.payload_updated) == (0, 2)
    assert dense.batches == []
    (point,) = await client.retrieve(ALIAS, [point_id("withdrawal-times#0")])
    assert point.payload is not None
    assert (point.payload["title"], point.payload["doc_version"]) == ("New title", 2)


async def test_removed_chunks_are_deleted(index: KbIndex) -> None:
    await index.sync(CHUNKS)
    report = await index.sync(CHUNKS[:3])
    assert report.deleted == 2
    assert await index.count() == 3


async def test_a_full_rebuild_moves_the_alias_and_drops_the_old_collection(
    index: KbIndex, client: AsyncQdrantClient, dense: FakeDense
) -> None:
    await index.sync(CHUNKS)
    dense.batches.clear()
    report = await index.rebuild(CHUNKS)
    assert (report.collection, report.embedded) == ("kb_chunks_v2", len(CHUNKS))
    assert len(dense.texts) == len(CHUNKS)
    assert await index.collection() == "kb_chunks_v2"
    names = {c.name for c in (await client.get_collections()).collections}
    assert names == {"kb_chunks_v2"}
    assert await index.count() == len(CHUNKS)


async def test_a_failed_rebuild_keeps_the_old_index(
    client: AsyncQdrantClient, dense: FakeDense
) -> None:
    index = KbIndex(client, dense, FakeSparse())
    await index.sync(CHUNKS)

    async def broken(texts: object) -> list[list[float]]:
        raise RuntimeError("model gone")

    with pytest.raises(RuntimeError):
        await KbIndex(client, broken, FakeSparse()).rebuild(CHUNKS)
    assert await index.collection() == "kb_chunks_v1"
    assert {c.name for c in (await client.get_collections()).collections} == {"kb_chunks_v1"}


async def test_a_failed_payload_index_leaves_no_half_built_collection(
    client: AsyncQdrantClient, dense: FakeDense, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def broken(*args: object, **kwargs: object) -> None:
        raise RuntimeError("index refused")

    monkeypatch.setattr(client, "create_payload_index", broken)
    with pytest.raises(RuntimeError, match="index refused"):
        await KbIndex(client, dense, FakeSparse()).sync(CHUNKS)
    assert (await client.get_collections()).collections == []
    assert (await client.get_aliases()).aliases == []


async def test_a_collection_with_another_vector_size_is_refused(
    index: KbIndex, client: AsyncQdrantClient
) -> None:
    from qdrant_client import models

    await client.create_collection(
        "kb_chunks_v1",
        vectors_config={"dense": models.VectorParams(size=8, distance=models.Distance.COSINE)},
    )
    await client.update_collection_aliases(
        change_aliases_operations=[
            models.CreateAliasOperation(
                create_alias=models.CreateAlias(collection_name="kb_chunks_v1", alias_name=ALIAS)
            )
        ]
    )
    with pytest.raises(KbIndexError, match="size 8"):
        await index.sync(CHUNKS)


async def test_an_embedder_with_the_wrong_number_of_vectors_is_refused(
    client: AsyncQdrantClient,
) -> None:
    async def short(texts: object) -> list[list[float]]:
        return []

    with pytest.raises(KbIndexError, match="wrong number"):
        await KbIndex(client, short, FakeSparse()).sync(CHUNKS)


# --- retriever ---------------------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["dense", "hybrid"])
async def test_search_ranks_hits_from_one(index: KbIndex, retriever: Retriever, mode: str) -> None:
    await index.sync(CHUNKS)
    hits = await retriever.search("SWIFT withdrawal days", "external", top_k=3, mode=mode)  # type: ignore[arg-type]
    assert [hit.rank for hit in hits] == list(range(1, len(hits) + 1))
    assert hits[0].chunk_id == "withdrawal-times#1"
    assert hits[0].url == "/help/withdrawal-times#section"


async def test_an_error_code_comes_first_in_hybrid_mode(
    index: KbIndex, retriever: Retriever
) -> None:
    await index.sync(CHUNKS)
    hits = await retriever.search("I got ERR-2003 at the shop", "external")
    assert hits[0].chunk_id == "error-codes#ERR-2003"


@pytest.mark.parametrize(
    ("query", "chunk_ids"),
    [
        ("ERR-2003", ["error-codes#ERR-2003"]),
        ("err-2003, Err-3009, ERR-2003", ["error-codes#ERR-2003", "error-codes#ERR-3009"]),
        ("(ERR-2003)", ["error-codes#ERR-2003"]),
        ("ERR-20031 XERR-2003 ERR-203 ERR 2003", []),
        ("no code here", []),
    ],
)  # fmt: skip
def test_error_codes_in_a_query(query: str, chunk_ids: list[str]) -> None:
    assert error_code_chunk_ids(query) == chunk_ids


EXTRA_MENTIONS = [
    chunk(f"withdrawal-failed#{i}", f"Withdrawal failed\nCodes\n\nERR-2003 means a {word} bank.")
    for i, word in enumerate(["rejected", "refused", "returned"])
]


async def test_the_lookup_puts_the_code_chunk_first_and_keeps_scores_in_order(
    index: KbIndex, retriever: Retriever
) -> None:
    # Articles that mention the code compete with the code chunk in both prefetches.
    await index.sync(CHUNKS + EXTRA_MENTIONS)
    hits = await retriever.search("my withdrawal failed with ERR-2003", "external", top_k=4)
    assert hits[0].chunk_id == "error-codes#ERR-2003"
    assert len(hits) == 4
    assert len({hit.chunk_id for hit in hits}) == 4  # the pinned chunk is not repeated
    scores = [hit.score for hit in hits]
    assert scores == sorted(scores, reverse=True)
    assert [hit.rank for hit in hits] == [1, 2, 3, 4]


async def test_an_unknown_code_pins_nothing_and_dense_mode_has_no_lookup(
    index: KbIndex, retriever: Retriever
) -> None:
    await index.sync(CHUNKS + EXTRA_MENTIONS)
    unknown = await retriever.search("ERR-9999 withdrawal failed", "external")
    assert "error-codes#ERR-9999" not in {hit.chunk_id for hit in unknown}
    hybrid = await retriever.search("SEPA withdrawal ERR-2003", "external", top_k=1)
    assert hybrid[0].chunk_id == "error-codes#ERR-2003"
    dense = await retriever.search("SEPA withdrawal ERR-2003", "external", top_k=1, mode="dense")
    assert dense[0].chunk_id != "error-codes#ERR-2003"


async def test_a_pinned_code_chunk_obeys_the_visibility(
    client: AsyncQdrantClient, dense: FakeDense, retriever: Retriever
) -> None:
    index = KbIndex(client, dense, FakeSparse())
    hidden = chunk("error-codes#ERR-2003", "Error codes\nERR-2003\n\nX.", visibility="internal")
    await index.sync([hidden, *CHUNKS[:3]])
    hits = await retriever.search("ERR-2003", "external")
    assert "error-codes#ERR-2003" not in {hit.chunk_id for hit in hits}


@pytest.mark.parametrize("mode", ["dense", "hybrid"])
async def test_external_search_never_returns_an_internal_chunk(
    index: KbIndex, retriever: Retriever, mode: str
) -> None:
    await index.sync(CHUNKS)
    for item in CHUNKS:
        hits = await retriever.search(item.text, "external", top_k=MAX_TOP_K, mode=mode)  # type: ignore[arg-type]
        assert {hit.visibility for hit in hits} <= {"external"}
    everything = await retriever.search(CHUNKS[-1].text, "all", top_k=MAX_TOP_K, mode=mode)  # type: ignore[arg-type]
    assert "fraud-playbook#0" in {hit.chunk_id for hit in everything}


@pytest.mark.parametrize("mode", ["dense", "hybrid"])
async def test_an_internal_point_is_dropped_even_without_the_filter(
    index: KbIndex, retriever: Retriever, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    # Even if the query filter were lost, the retriever checks every hit again.
    await index.sync(CHUNKS)
    monkeypatch.setattr(retriever_module, "visibility_filter", lambda visibility: None)
    hits = await retriever.search(CHUNKS[-1].text, "external", top_k=MAX_TOP_K, mode=mode)  # type: ignore[arg-type]
    assert hits
    assert "fraud-playbook#0" not in {hit.chunk_id for hit in hits}
    assert [hit.rank for hit in hits] == list(range(1, len(hits) + 1))


def test_the_visibility_filter() -> None:
    assert visibility_filter("all") is None
    external = visibility_filter("external")
    assert external is not None
    assert external.model_dump(exclude_none=True) == {
        "must": [{"key": "visibility", "match": {"value": "external"}}]
    }


async def test_an_empty_or_stop_word_query(index: KbIndex, client: AsyncQdrantClient) -> None:
    await index.sync(CHUNKS)

    class NoTerms(FakeSparse):
        async def query(self, text: str) -> SparseVector:
            return SparseVector(indices=[], values=[])

    retriever = Retriever(client, FakeDense(), NoTerms())
    assert await retriever.search("   ", "external") == []
    # No BM25 terms: the dense prefetch alone still answers.
    assert await retriever.search("the a of", "external")


async def test_top_k_is_bounded(retriever: Retriever) -> None:
    with pytest.raises(ValueError, match="top_k"):
        await retriever.search("x", "external", top_k=0)
    with pytest.raises(ValueError, match="top_k"):
        await retriever.search("x", "external", top_k=MAX_TOP_K + 1)


class _TiedClient:
    """Answers every query with fixed points whose scores tie, in an order the server might
    pick; records the limit it was asked for."""

    def __init__(self, ids: list[str], scores: list[float]) -> None:
        self.points = [
            SimpleNamespace(
                payload={
                    "chunk_id": chunk_id,
                    "doc_id": chunk_id.partition("#")[0],
                    "title": "T",
                    "section": "S",
                    "url": "/help/x#s",
                    "text": "x",
                    "visibility": "external",
                },
                score=score,
            )
            for chunk_id, score in zip(ids, scores, strict=True)
        ]
        self.limits: list[int] = []

    async def query_points(self, *_args: object, limit: int, **_kwargs: object) -> object:
        self.limits.append(limit)
        return SimpleNamespace(points=self.points[:limit])

    async def retrieve(self, *_args: object, **_kwargs: object) -> list[object]:
        return []


@pytest.mark.parametrize("mode", ["hybrid", "dense"])
async def test_equal_scores_are_ordered_by_chunk_id_also_across_the_cut(mode: str) -> None:
    """P5-03: RRF ties are common and the server orders them differently from run to run; the
    same query must always give the same hits (recordings, evaluation cache)."""
    client = _TiedClient(["d#0", "c#0", "b#0", "a#0", "e#0"], [0.5, 0.33, 0.33, 0.25, 0.25])
    retriever = Retriever(cast(AsyncQdrantClient, client), FakeDense(), FakeSparse())

    hits = await retriever.search("query", "external", top_k=4, mode=mode)  # type: ignore[arg-type]

    # `a#0` and `e#0` tie at the cut (rank 4): the id decides, not the server's order.
    assert [hit.chunk_id for hit in hits] == ["d#0", "b#0", "c#0", "a#0"]
    assert [hit.rank for hit in hits] == [1, 2, 3, 4]
    assert client.limits == [4 + TIE_MARGIN]
