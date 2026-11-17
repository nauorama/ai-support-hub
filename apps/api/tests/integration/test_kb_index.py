"""The knowledge-base index and search against the real Qdrant server, Postgres and the local
embedding model (P5-01, P5-02). Needs the model files: `make models` (CI runs it first).

Done-when lines covered here:
- P5-01: changing one article re-embeds only its chunks; Postgres and Qdrant chunk counts match
  (and `/ready` shows them); embedding works with the network off; embedding rows in
  `llm_calls` cost 0; every section chunk fits in the model's 512 tokens.
- P5-02: a query with "ERR-2003" returns that error-code chunk first in hybrid mode;
  `visibility=external` never returns an internal chunk (over every chunk, both modes).
"""

import shutil
import socket
from collections.abc import AsyncIterator, Iterator
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from qdrant_client import AsyncQdrantClient, models

from hub.db.session import Database, connect
from hub.domain.enums import ActorType
from hub.kb.index import point_id, read_chunks
from hub.kb.retriever import MAX_TOP_K
from hub.kb.service import (
    EMBEDDING_CALL,
    Actor,
    KbServices,
    build_kb_services,
    index_counts,
    reload_kb,
)
from hub.llm.gateway import build_gateway, embedder
from hub.llm.local_embedding import MAX_INPUT_TOKENS, LocalEmbeddingProvider
from hub.llm.recorder import DbCallRecorder
from hub.llm.schemas import ProviderRequest
from hub.observability.readiness import kb_index_check

from . import _api
from ._api import Urls, run_sql
from ._compose import ROOT
from ._kb import EMBEDDING_MODEL, MODEL_DIR, settings

DATABASE = "hub_kb_index_test"
EDITED = "withdrawal-times"


@pytest.fixture(scope="module")
def urls(postgres_url: str) -> Iterator[Urls]:
    if not (MODEL_DIR / "models--Qdrant--bge-small-en-v1.5-onnx-Q").is_dir():
        pytest.fail(f"the local embedding model is not in {MODEL_DIR}: run `make models`")
    with _api.own_database(postgres_url, DATABASE) as found:
        yield found


@pytest.fixture
async def clean(urls: Urls, qdrant_url: str) -> None:
    run_sql(urls, "TRUNCATE audit_log, llm_calls, kb_chunks, kb_documents RESTART IDENTITY CASCADE")
    client = AsyncQdrantClient(url=qdrant_url, check_compatibility=False)
    for item in (await client.get_collections()).collections:
        await client.delete_collection(item.name)
    await client.close()


@pytest.fixture
async def database(urls: Urls, clean: None) -> AsyncIterator[Database]:
    database = connect(urls[1])
    yield database
    await database.dispose()


@pytest.fixture
async def kb(urls: Urls, qdrant_url: str, database: Database) -> AsyncIterator[KbServices]:
    config = settings(urls, qdrant_url)
    gateway = build_gateway(config, DbCallRecorder(database), None)
    services = build_kb_services(config, embedder(gateway, EMBEDDING_CALL))
    yield services
    await services.close()


@pytest.fixture
def kb_dir(tmp_path: Path) -> Path:
    target = tmp_path / "kb"
    shutil.copytree(ROOT / "data" / "kb", target)
    return target


@pytest.fixture
async def loaded(database: Database, kb: KbServices, kb_dir: Path) -> int:
    report = await reload_kb(database, kb.index, kb_dir)
    return report.index.embedded


# --- P5-01: index ------------------------------------------------------------------------------


async def test_kb_load_fills_qdrant_with_one_point_per_postgres_chunk(
    database: Database, kb: KbServices, loaded: int, urls: Urls
) -> None:
    postgres, qdrant = await index_counts(database, kb.index)
    assert postgres == qdrant == loaded > 100
    assert await kb.index.collection() == "kb_chunks_v1"
    check = await kb_index_check(lambda: index_counts(database, kb.index))()
    assert check == {"postgres_chunks": postgres, "qdrant_points": qdrant}
    # Every embedding call wrote a row: node `embedding`, status ok, cost 0.
    rows = run_sql(
        urls, "SELECT node, status, cost_usd, prompt_version, run_id, context FROM llm_calls"
    )
    assert rows
    assert {row[:5] for row in rows} == {("embedding", "ok", Decimal(0), None, None)}
    assert {row[5] for row in rows} == {"online"}


async def test_a_second_load_embeds_nothing(
    database: Database, kb: KbServices, loaded: int, kb_dir: Path, urls: Urls
) -> None:
    calls = run_sql(urls, "SELECT count(*) FROM llm_calls")[0][0]
    report = await reload_kb(database, kb.index, kb_dir)
    assert not report.load.changed
    assert (report.index.embedded, report.index.payload_updated, report.index.deleted) == (0, 0, 0)
    assert run_sql(urls, "SELECT count(*) FROM llm_calls")[0][0] == calls


async def test_changing_one_article_re_embeds_only_its_chunks(
    database: Database, kb: KbServices, loaded: int, kb_dir: Path
) -> None:
    article = kb_dir / f"{EDITED}.md"
    article.write_text(
        article.read_text(encoding="utf-8").replace(
            "SEPA is for EUR withdrawals to a bank account in the EU.",
            "SEPA is for EUR withdrawals to a bank account in the EU or the EEA.",
        ),
        encoding="utf-8",
    )
    report = await reload_kb(database, kb.index, kb_dir)
    assert report.load.documents_updated == 1
    assert report.index.embedded_chunk_ids == [f"{EDITED}#0"]
    # The other chunks of the article get the new document version, without an embedding.
    async with database.sessions() as session:
        chunks = await read_chunks(session)
    same_doc = sum(1 for chunk in chunks if chunk.doc_id == EDITED)
    assert report.index.payload_updated == same_doc - 1
    postgres, qdrant = await index_counts(database, kb.index)
    assert postgres == qdrant


async def test_a_removed_article_loses_its_points(
    database: Database, kb: KbServices, loaded: int, kb_dir: Path
) -> None:
    async with database.sessions() as session:
        removed = sum(1 for chunk in await read_chunks(session) if chunk.doc_id == EDITED)
    (kb_dir / f"{EDITED}.md").unlink()
    report = await reload_kb(database, kb.index, kb_dir)
    assert report.index.deleted == removed
    postgres, qdrant = await index_counts(database, kb.index)
    assert postgres == qdrant == loaded - removed


async def test_a_full_reindex_builds_a_new_collection_and_moves_the_alias(
    database: Database, kb: KbServices, loaded: int, kb_dir: Path, urls: Urls
) -> None:
    report = await reload_kb(
        database, kb.index, kb_dir, full=True, audit_as=Actor(ActorType.SYSTEM, "test")
    )
    assert (report.index.collection, report.index.embedded) == ("kb_chunks_v2", loaded)
    assert await kb.index.collection() == "kb_chunks_v2"
    names = {c.name for c in (await kb.client.get_collections()).collections}
    assert names == {"kb_chunks_v2"}
    postgres, qdrant = await index_counts(database, kb.index)
    assert postgres == qdrant
    (row,) = run_sql(urls, "SELECT entity_id, details FROM audit_log WHERE action = 'kb.reindexed'")
    assert row[0] == "kb_chunks"
    assert row[1]["full"] is True
    assert row[1]["collection"] == "kb_chunks_v2"


async def test_the_index_repairs_itself_after_a_failed_qdrant_step(
    database: Database, kb: KbServices, loaded: int, kb_dir: Path
) -> None:
    # Postgres is committed before Qdrant; a lost point is found and written by the next sync.
    await kb.client.delete(
        kb.index.alias, points_selector=models.PointIdsList(points=[point_id(f"{EDITED}#0")])
    )
    report = await reload_kb(database, kb.index, kb_dir)
    assert report.index.embedded_chunk_ids == [f"{EDITED}#0"]


async def test_embedding_works_with_the_network_off(monkeypatch: pytest.MonkeyPatch) -> None:
    def no_network(*args: Any, **kwargs: Any) -> None:  # noqa: ANN401
        raise OSError("network is off in this test")

    monkeypatch.setattr(socket.socket, "connect", no_network)
    monkeypatch.setattr(socket, "create_connection", no_network)
    monkeypatch.setattr(socket, "getaddrinfo", no_network)

    class NoChat:
        async def complete(self, request: ProviderRequest) -> Any:  # noqa: ANN401
            raise AssertionError("no chat call")

    provider = LocalEmbeddingProvider(NoChat(), MODEL_DIR)
    response = await provider.embed(
        ProviderRequest(kind="embedding", model=EMBEDDING_MODEL, texts=["SEPA", "x"], timeout_s=30)
    )
    assert [len(vector) for vector in response.vectors] == [384, 384]


async def test_every_chunk_fits_in_the_model_input(
    database: Database, kb: KbServices, loaded: int
) -> None:
    async with database.sessions() as session:
        chunks = await read_chunks(session)
    provider = LocalEmbeddingProvider(None, MODEL_DIR)  # type: ignore[arg-type]
    counts = provider.token_counts(EMBEDDING_MODEL, [chunk.text for chunk in chunks])
    too_long = {c.chunk_id: n for c, n in zip(chunks, counts, strict=True) if n > MAX_INPUT_TOKENS}
    assert too_long == {}
    assert max(counts) > 100  # the counter really counts


# --- P5-02: retriever --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "query",
    [
        "ERR-2003",
        "My withdrawal failed with ERR-2003, what does it mean?",
        "i got err-2003 from the app",
    ],
)
async def test_an_error_code_query_returns_that_code_first_in_hybrid_mode(
    kb: KbServices, loaded: int, query: str
) -> None:
    hits = await kb.retriever.search(query, "external")
    assert hits[0].chunk_id == "error-codes#ERR-2003"
    assert hits[0].url == "/help/error-codes#err-2003"
    assert [hit.rank for hit in hits] == list(range(1, len(hits) + 1))
    assert len(hits) == 6


@pytest.mark.parametrize("mode", ["dense", "hybrid"])
async def test_external_search_never_returns_an_internal_chunk(
    database: Database, kb: KbServices, loaded: int, mode: str
) -> None:
    async with database.sessions() as session:
        chunks = await read_chunks(session)
    internal = {chunk.chunk_id for chunk in chunks if chunk.visibility == "internal"}
    assert internal  # the knowledge base has internal articles to leak
    for chunk in chunks:
        hits = await kb.retriever.search(chunk.text, "external", top_k=MAX_TOP_K, mode=mode)  # type: ignore[arg-type]
        assert not {hit.chunk_id for hit in hits} & internal, chunk.chunk_id
        assert {hit.visibility for hit in hits} == {"external"}
    # With visibility "all" the internal chunks are found (console search).
    sample = next(chunk for chunk in chunks if chunk.visibility == "internal")
    hits = await kb.retriever.search(sample.text, "all", top_k=3, mode=mode)  # type: ignore[arg-type]
    assert hits[0].chunk_id == sample.chunk_id
