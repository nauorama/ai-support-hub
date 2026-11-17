"""The knowledge base as one unit: Postgres load, Qdrant index and search (P5-01, P5-02).

`reload_kb` is what `make kb-load` and `POST /admin/kb/reindex` both run: read and check
`data/kb/`, write the changed documents to Postgres (one transaction, `hub.kb.loader`), then
bring the index in line with Postgres (`KbIndex.sync`, or `rebuild` for a full re-index).
Postgres is committed first: when Qdrant fails, the next run finds the difference and repairs
the index.
"""

import asyncio
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path

from qdrant_client import AsyncQdrantClient

from hub.audit import record
from hub.config import Settings, SettingsError, load_settings
from hub.db.session import Database, connect
from hub.domain.enums import ActorType, LlmCallContext, LlmNode
from hub.kb.index import (
    DenseEmbedder,
    IndexReport,
    KbIndex,
    KbIndexError,
    connect_qdrant,
    postgres_chunk_count,
    read_chunks,
)
from hub.kb.loader import ACTOR, KbLoadError, KbLoadReport, load_kb, read_kb
from hub.kb.retriever import Retriever
from hub.kb.sparse import Bm25Encoder, SparseModelError
from hub.llm.gateway import build_gateway, embedder
from hub.llm.recorder import DbCallRecorder
from hub.llm.schemas import CallContext, LlmUnavailableError

KB_DIR = Path("data/kb")
# Embeddings for the index and for search are `online` calls of node `embedding` (cost 0).
EMBEDDING_CALL = CallContext(context=LlmCallContext.ONLINE, node=LlmNode.EMBEDDING)


@dataclass(frozen=True)
class KbServices:
    client: AsyncQdrantClient
    index: KbIndex
    retriever: Retriever

    async def close(self) -> None:
        await self.client.close()


def build_kb_services(settings: Settings, dense: DenseEmbedder) -> KbServices:
    """One Qdrant client and one BM25 encoder for the index and the retriever. Nothing is
    loaded or connected until the first use."""
    client = connect_qdrant(settings.qdrant_url, settings.qdrant_api_key.get_secret_value())
    sparse = Bm25Encoder(settings.embedding_cache_dir)
    return KbServices(
        client=client,
        index=KbIndex(client, dense, sparse),
        retriever=Retriever(client, dense, sparse),
    )


@dataclass(frozen=True)
class ReloadReport:
    load: KbLoadReport
    index: IndexReport
    full: bool


@dataclass(frozen=True)
class Actor:
    type: ActorType
    id: str | uuid.UUID


async def reload_kb(
    database: Database,
    index: KbIndex,
    kb_dir: Path,
    *,
    full: bool = False,
    audit_as: Actor | None = None,
) -> ReloadReport:
    """Load `kb_dir` into Postgres, then sync the index. `full`: re-chunk unchanged files too
    (a chunker change reaches every file, `load_kb(rechunk=True)`) and rebuild the index in a
    new collection. With `audit_as`, a `kb.reindexed` record names who asked for it. Raises
    `KbLoadError` before anything is written when the files are not valid."""
    files = await asyncio.to_thread(read_kb, kb_dir)
    async with database.sessions() as session, session.begin():
        load = await load_kb(session, files, rechunk=full)
        chunks = await read_chunks(session)
    report = await (index.rebuild(chunks) if full else index.sync(chunks))
    if audit_as is not None:
        async with database.sessions() as session, session.begin():
            await record(
                session,
                actor_type=audit_as.type,
                actor_id=audit_as.id,
                action="kb.reindexed",
                entity_type="kb_index",
                entity_id=index.alias,
                details={
                    "full": full,
                    "collection": report.collection,
                    "chunks": len(chunks),
                    "embedded": report.embedded,
                    "payload_updated": report.payload_updated,
                    "deleted": report.deleted,
                },
            )
    return ReloadReport(load=load, index=report, full=full)


async def index_counts(database: Database, index: KbIndex) -> tuple[int, int]:
    """(chunks in Postgres, points in Qdrant), for `/ready`."""
    async with database.sessions() as session:
        postgres = await postgres_chunk_count(session)
    return postgres, await index.count()


# --- `make kb-load` ------------------------------------------------------------------------


async def run(settings: Settings, kb_dir: Path, *, full: bool) -> ReloadReport:
    database = connect(settings.database_url.get_secret_value())
    # No budget guard: embeddings are local and free (hub.llm.gateway).
    gateway = build_gateway(settings, DbCallRecorder(database), None)
    services = build_kb_services(settings, embedder(gateway, EMBEDDING_CALL))
    try:
        return await reload_kb(
            database,
            services.index,
            kb_dir,
            full=full,
            audit_as=Actor(ActorType.SYSTEM, ACTOR) if full else None,
        )
    finally:
        await services.close()
        await database.dispose()


def main(argv: list[str]) -> int:
    """`python -m hub.kb.service [--full] [kb_dir]`."""
    full = "--full" in argv
    paths = [arg for arg in argv if arg != "--full"]
    kb_dir = Path(paths[0]) if paths else KB_DIR
    try:
        report = asyncio.run(run(load_settings(), kb_dir, full=full))
    except (KbLoadError, SettingsError, KbIndexError, SparseModelError) as error:
        print(f"kb-load: {error}", file=sys.stderr)
        return 1
    except LlmUnavailableError:
        print(
            "kb-load: the local embedding model is not available; run `make models`",
            file=sys.stderr,
        )
        return 1
    load, index = report.load, report.index
    print(
        f"kb-load: documents +{load.documents_added} ~{load.documents_updated} "
        f"={load.documents_unchanged} -{load.documents_removed}; "
        f"chunks written {load.chunks_written}, removed {load.chunks_removed}"
        + ("" if load.changed else " (nothing to do)")
    )
    print(
        f"kb-load: index {index.collection}{' (full re-index)' if report.full else ''}: "
        f"embedded {index.embedded}, payload updated {index.payload_updated}, "
        f"deleted {index.deleted}, unchanged {index.unchanged}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
