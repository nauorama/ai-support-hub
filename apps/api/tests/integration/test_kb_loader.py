"""`make kb-load` against Postgres as the app role: idempotent, changes only what changed,
removes deleted documents, audited.

Uses a database of its own (the loader commits, and `verify_chain` needs an audit log that holds
only chained rows). Each test loads a copy of the real `data/kb/`, so it can edit files.
"""

import shutil
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psycopg
import pytest
from psycopg import sql
from sqlalchemy import Result, literal_column, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine

from hub.audit import verify_chain
from hub.db.models import AuditLog, KbChunk, KbDocument
from hub.kb.loader import KbLoadReport, load_kb, read_kb
from hub.observability.readiness import libpq_url

from ._compose import ROOT
from ._db import app_url, as_user, migrate

DATABASE = "hub_kb_test"
EDITED = "withdrawal-times"


@pytest.fixture(scope="module")
def kb_db(postgres_url: str) -> Iterator[tuple[str, str]]:
    """(migrator URL, app URL) of a migrated database of its own."""
    with psycopg.connect(libpq_url(postgres_url), autocommit=True) as connection:
        connection.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(DATABASE)))
        connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(DATABASE)))
    migrator = as_user(postgres_url, "hub", "hub", database=DATABASE)
    migrate(migrator)
    yield migrator, app_url(postgres_url, database=DATABASE)
    with psycopg.connect(libpq_url(postgres_url), autocommit=True) as connection:
        connection.execute(
            sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(DATABASE))
        )


@pytest.fixture(autouse=True)
def empty_tables(kb_db: tuple[str, str]) -> None:
    with psycopg.connect(libpq_url(kb_db[0]), autocommit=True) as connection:
        connection.execute("TRUNCATE audit_log, kb_chunks, kb_documents RESTART IDENTITY CASCADE")


@pytest.fixture
async def engine(kb_db: tuple[str, str]) -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(kb_db[1])
    yield engine
    await engine.dispose()


@pytest.fixture
def kb_dir(tmp_path: Path) -> Path:
    target = tmp_path / "kb"
    shutil.copytree(ROOT / "data" / "kb", target)
    return target


async def _load(engine: AsyncEngine, kb_dir: Path, now: datetime | None = None) -> KbLoadReport:
    files = read_kb(kb_dir)
    async with AsyncSession(engine, expire_on_commit=False) as session, session.begin():
        return await load_kb(session, files, now=now)


async def _documents(engine: AsyncEngine) -> dict[str, tuple[Any, ...]]:
    async with AsyncSession(engine) as session:
        rows = await session.scalars(select(KbDocument))
        return {
            d.id: (
                d.title,
                d.product,
                d.visibility,
                d.intents,
                d.url,
                d.content_hash,
                d.version,
                d.updated_at,
                d.created_at,
            )
            for d in rows
        }


async def _chunks(engine: AsyncEngine) -> dict[str, tuple[Any, ...]]:
    # xmin changes with every write of a row, so it shows rows that were rewritten with the
    # same values, which a comparison of the columns would miss.
    async with AsyncSession(engine) as session:
        rows: Result[Any] = await session.execute(
            select(
                KbChunk.id,
                KbChunk.doc_id,
                KbChunk.ordinal,
                KbChunk.section,
                KbChunk.content_hash,
                KbChunk.token_count,
                KbChunk.created_at,
                literal_column("kb_chunks.xmin::text"),
            )
        )
        return {row[0]: tuple(row[1:]) for row in rows}


async def _actions(engine: AsyncEngine) -> list[tuple[str, str]]:
    async with AsyncSession(engine) as session:
        rows = await session.execute(
            select(AuditLog.action, AuditLog.entity_id).order_by(AuditLog.id)
        )
        return [(action, entity) for action, entity in rows]


async def test_first_load_writes_every_document_and_chunk(
    engine: AsyncEngine, kb_dir: Path
) -> None:
    now = datetime(2026, 11, 15, 9, 30, tzinfo=UTC)
    files = read_kb(kb_dir)

    report = await _load(engine, kb_dir, now=now)

    assert report.documents_added == len(files)
    assert report.chunks_written == sum(len(f.chunks) for f in files)
    documents = await _documents(engine)
    assert set(documents) == {f.id for f in files}
    assert all(row[6] == 1 for row in documents.values())  # version
    # The error-codes document takes the load time; articles their front matter date.
    assert documents["error-codes"][7] == now
    assert documents["error-codes"][:5] == (
        "Error codes",
        "general",
        "external",
        [],
        "/help/error-codes",
    )
    chunks = await _chunks(engine)
    assert len(chunks) == report.chunks_written
    assert "error-codes#ERR-2003" in chunks
    assert f"{EDITED}#0" in chunks

    actions = await _actions(engine)
    assert sorted(actions) == sorted(("kb.document_added", f.id) for f in files)
    async with AsyncSession(engine) as session:
        assert (await verify_chain(session)).ok


async def test_loading_twice_changes_nothing(engine: AsyncEngine, kb_dir: Path) -> None:
    await _load(engine, kb_dir)
    documents, chunks, actions = (
        await _documents(engine),
        await _chunks(engine),
        await _actions(engine),
    )

    report = await _load(engine, kb_dir)

    assert not report.changed
    assert report.documents_unchanged == len(documents)
    assert (report.chunks_written, report.chunks_removed) == (0, 0)
    assert await _documents(engine) == documents
    assert await _chunks(engine) == chunks
    assert await _actions(engine) == actions


async def test_editing_one_article_updates_only_its_chunks(
    engine: AsyncEngine, kb_dir: Path
) -> None:
    await _load(engine, kb_dir)
    documents, chunks = await _documents(engine), await _chunks(engine)
    path = kb_dir / f"{EDITED}.md"
    text = path.read_text(encoding="utf-8")
    # Change the text of the last section only and add a new last section.
    path.write_text(text.rstrip("\n") + " Edited.\n\n## New section\nNew text.\n", "utf-8")

    report = await _load(engine, kb_dir)

    assert (report.documents_added, report.documents_updated, report.documents_removed) == (0, 1, 0)
    assert report.chunks_written == 2  # the edited last section and the new one
    after_documents, after_chunks = await _documents(engine), await _chunks(engine)
    assert after_documents[EDITED][6] == documents[EDITED][6] + 1
    assert {k: v for k, v in after_documents.items() if k != EDITED} == {
        k: v for k, v in documents.items() if k != EDITED
    }
    changed = {
        chunk_id
        for chunk_id in after_chunks.keys() | chunks.keys()
        if after_chunks.get(chunk_id) != chunks.get(chunk_id)
    }
    old_count = sum(1 for c in chunks.values() if c[0] == EDITED)
    assert changed == {f"{EDITED}#{old_count - 1}", f"{EDITED}#{old_count}"}
    assert after_chunks[f"{EDITED}#{old_count}"][2] == "New section"
    assert (await _actions(engine))[-1] == ("kb.document_updated", EDITED)


async def test_removing_a_section_deletes_its_chunk(engine: AsyncEngine, kb_dir: Path) -> None:
    await _load(engine, kb_dir)
    path = kb_dir / f"{EDITED}.md"
    text = path.read_text(encoding="utf-8")
    last = text.rindex("\n## ")
    path.write_text(text[: last + 1], "utf-8")
    before = sum(1 for c in (await _chunks(engine)).values() if c[0] == EDITED)

    report = await _load(engine, kb_dir)

    assert (report.chunks_written, report.chunks_removed) == (0, 1)
    after = sum(1 for c in (await _chunks(engine)).values() if c[0] == EDITED)
    assert after == before - 1


async def test_a_deleted_file_removes_the_document_and_its_chunks(
    engine: AsyncEngine, kb_dir: Path
) -> None:
    await _load(engine, kb_dir)
    count = sum(1 for c in (await _chunks(engine)).values() if c[0] == EDITED)
    (kb_dir / f"{EDITED}.md").unlink()

    report = await _load(engine, kb_dir)

    assert (report.documents_removed, report.chunks_removed) == (1, count)
    assert EDITED not in await _documents(engine)
    assert not [c for c in (await _chunks(engine)).values() if c[0] == EDITED]
    assert (await _actions(engine))[-1] == ("kb.document_removed", EDITED)
    async with AsyncSession(engine) as session:
        assert (await verify_chain(session)).ok


async def test_a_changed_error_code_bumps_the_document_and_rewrites_one_chunk(
    engine: AsyncEngine, kb_dir: Path
) -> None:
    first = datetime(2026, 11, 1, tzinfo=UTC)
    second = datetime(2026, 11, 15, tzinfo=UTC)
    await _load(engine, kb_dir, now=first)
    chunks = await _chunks(engine)
    path = kb_dir / "error_codes.csv"
    text = path.read_text(encoding="utf-8")
    path.write_text(text.replace("Tallybridge accounts can receive only", "Accounts receive only"))

    report = await _load(engine, kb_dir, now=second)

    assert (report.documents_updated, report.chunks_written) == (1, 1)
    documents = await _documents(engine)
    assert (documents["error-codes"][6], documents["error-codes"][7]) == (2, second)
    after = await _chunks(engine)
    assert {k for k in after if after[k] != chunks[k]} == {"error-codes#ERR-1001"}


async def test_a_rechunk_reaches_files_whose_hash_did_not_change(
    engine: AsyncEngine, kb_dir: Path, kb_db: tuple[str, str]
) -> None:
    # Stands for a chunker change: the stored chunks of one unchanged file differ from what the
    # chunker gives now (P2-09 review question, answered in P5-01 by the full re-index).
    await _load(engine, kb_dir)
    with psycopg.connect(libpq_url(kb_db[0]), autocommit=True) as connection:
        connection.execute(
            "UPDATE kb_chunks SET text = 'old chunker', content_hash = 'old' WHERE id = %s",
            (f"{EDITED}#0",),
        )
    plain = await _load(engine, kb_dir)
    assert not plain.changed  # the hash skip alone never sees it
    before = await _chunks(engine)
    actions = len(await _actions(engine))

    files = read_kb(kb_dir)
    async with AsyncSession(engine, expire_on_commit=False) as session, session.begin():
        report = await load_kb(session, files, rechunk=True)

    assert (report.documents_updated, report.chunks_written) == (1, 1)
    assert report.documents_unchanged == len(files) - 1
    documents = await _documents(engine)
    assert documents[EDITED][6] == 2  # version
    after = await _chunks(engine)
    changed = {chunk_id for chunk_id in after if after[chunk_id] != before[chunk_id]}
    assert changed == {f"{EDITED}#0"}
    assert (await _actions(engine))[actions:] == [("kb.document_updated", EDITED)]
    # A second re-chunk finds nothing to do.
    async with AsyncSession(engine, expire_on_commit=False) as session, session.begin():
        again = await load_kb(session, files, rechunk=True)
    assert not again.changed
