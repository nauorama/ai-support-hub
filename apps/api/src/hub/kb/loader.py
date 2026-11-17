"""Load `data/kb/` into `kb_documents` and `kb_chunks` (the Postgres part of `make kb-load`,
P2-09; the command and the Qdrant part are in `hub.kb.service`, P5-01).

One document per article (`<id>.md`) and one for `error_codes.csv` (`error-codes`). In one
transaction, under an advisory lock so two loads cannot interleave:
- a file whose SHA-256 equals the stored `content_hash` is skipped: its row, `version` and
  chunks stay as they are;
- a changed file gets `version + 1`; of its chunks only those whose id, place or text changed are
  written, and chunks it no longer has are deleted;
- a document whose file is gone is deleted with its chunks.

The files are checked first with the rules of `make data-check` (`hub.domain.kb`, and no text
that masking would change): any problem stops the load before anything is written. A folder
with no articles is refused, so a wrong path cannot empty the knowledge base.
Every document added, changed or removed gets an audit record; a load that changes nothing
writes none. The Qdrant index is built from these rows (`hub.kb.index`).
"""

import hashlib
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, time
from pathlib import Path
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from hub.audit import record
from hub.db.models import KbChunk, KbDocument
from hub.domain.enums import ActorType
from hub.domain.kb import (
    ERROR_CODES_DOC_ID,
    ERROR_CODES_FILE,
    KbFormatError,
    Product,
    Visibility,
    article_errors,
    error_code_errors,
    error_codes_in,
    parse_article,
    parse_error_codes,
)
from hub.guardrails.masking import mask_text
from hub.kb.chunker import ERROR_CODES_TITLE, Chunk, chunk_article, chunk_error_codes

ACTOR = "make-kb-load"
# Any constant different from the audit lock (`hub.audit.log.AUDIT_LOCK_KEY`); "kbload" in hex.
KB_LOAD_LOCK_KEY = 0x6B626C6F6164


@dataclass(frozen=True, slots=True)
class KbFile:
    """One parsed and checked KB file, ready to be written."""

    id: str
    title: str
    product: Product
    visibility: Visibility
    intents: tuple[str, ...]
    content_hash: str
    # From the front matter; None for `error-codes`, which takes the time of the load.
    updated_at: datetime | None
    chunks: tuple[Chunk, ...]

    @property
    def url(self) -> str:
        return f"/help/{self.id}"


@dataclass(frozen=True, slots=True)
class LoadPlan:
    added: tuple[KbFile, ...]
    changed: tuple[KbFile, ...]
    unchanged: tuple[str, ...]
    removed: tuple[str, ...]


@dataclass
class KbLoadReport:
    documents_added: int = 0
    documents_updated: int = 0
    documents_unchanged: int = 0
    documents_removed: int = 0
    chunks_written: int = 0
    chunks_removed: int = 0

    @property
    def changed(self) -> bool:
        return bool(self.documents_added or self.documents_updated or self.documents_removed)


@dataclass
class _ChunkSync:
    written: int = 0
    removed: int = 0
    kept: int = 0


class KbLoadError(Exception):
    """The KB files cannot be loaded. Nothing was written. The message lists every problem."""


# --- reading the files (pure apart from reading the folder) ---------------------------------


def read_kb(kb_dir: Path) -> list[KbFile]:
    """Read and check every KB file. Raises KbLoadError listing all problems."""
    if not kb_dir.is_dir():
        raise KbLoadError(f"{kb_dir} is not a folder")
    try:
        csv_bytes = (kb_dir / ERROR_CODES_FILE).read_bytes()
        articles = {path.stem: path.read_bytes() for path in sorted(kb_dir.glob("*.md"))}
    except OSError as error:
        raise KbLoadError(f"cannot read {error.filename}: {error.strerror}") from error
    return parse_kb(articles, csv_bytes)


def parse_kb(articles: Mapping[str, bytes], error_codes_csv: bytes) -> list[KbFile]:
    """Turn file contents (article file stem → bytes, and the CSV) into checked KbFiles."""
    if not articles:
        raise KbLoadError("no articles (*.md); refusing to load an empty knowledge base")
    problems: list[str] = []
    files: list[KbFile] = []
    codes: frozenset[str] = frozenset()
    try:
        error_codes, codes = _parse_error_codes(error_codes_csv)
    except KbFormatError as error:
        problems.append(f"{ERROR_CODES_FILE}: {error}")
    else:
        files.append(error_codes)
    for stem, data in sorted(articles.items()):
        try:
            files.append(_parse_article(stem, data, codes))
        except KbFormatError as error:
            problems.append(f"{stem}.md: {error}")
    if problems:
        raise KbLoadError("; ".join(problems))
    return files


def _parse_error_codes(data: bytes) -> tuple[KbFile, frozenset[str]]:
    """The `error-codes` document and the codes it defines."""
    text = _decode(data)
    rows = parse_error_codes(text)
    errors = [e for row in rows for e in error_code_errors(row)]
    if errors:
        raise KbFormatError("; ".join(errors))
    document = KbFile(
        id=ERROR_CODES_DOC_ID,
        title=ERROR_CODES_TITLE,
        product=Product.GENERAL,
        visibility=Visibility.EXTERNAL,
        # No intent of its own: codes come up in tickets of every intent.
        intents=(),
        content_hash=_file_hash(data),
        updated_at=None,
        chunks=tuple(chunk_error_codes(rows)),
    )
    return document, frozenset(row.code for row in rows)


def _parse_article(stem: str, data: bytes, codes: frozenset[str]) -> KbFile:
    if stem == ERROR_CODES_DOC_ID:
        raise KbFormatError(f"the id {ERROR_CODES_DOC_ID!r} belongs to {ERROR_CODES_FILE}")
    text = _decode(data)
    article = parse_article(text)
    errors = article_errors(article, stem)
    errors.extend(
        f"{code} is not in {ERROR_CODES_FILE}"
        for code in sorted(error_codes_in(article.body) - codes)
        if codes
    )
    if errors:
        raise KbFormatError("; ".join(errors))
    meta = article.front_matter
    return KbFile(
        id=meta.id,
        title=meta.title,
        product=meta.product,
        visibility=meta.visibility,
        intents=tuple(str(intent) for intent in meta.intents),
        content_hash=_file_hash(data),
        updated_at=datetime.combine(meta.updated_at, time(), UTC),
        chunks=tuple(chunk_article(meta.id, meta.title, article.body)),
    )


def _decode(data: bytes) -> str:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise KbFormatError("not valid UTF-8") from error
    # The KB is written by us and must hold no card data (AGENTS.md never-do 1); same check as
    # `make data-check`.
    if mask_text(text) != text:
        raise KbFormatError("text looks like card data (masking would change it)")
    return text


def _file_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# --- deciding what to write (pure) -----------------------------------------------------------


def plan_load(stored: Mapping[str, str], files: Sequence[KbFile]) -> LoadPlan:
    """Compare the files with the stored `{doc_id: content_hash}`."""
    current = {file.id for file in files}
    return LoadPlan(
        added=tuple(f for f in files if f.id not in stored),
        changed=tuple(f for f in files if f.id in stored and stored[f.id] != f.content_hash),
        unchanged=tuple(f.id for f in files if stored.get(f.id) == f.content_hash),
        removed=tuple(sorted(doc_id for doc_id in stored if doc_id not in current)),
    )


# --- writing ---------------------------------------------------------------------------------


async def load_kb(
    session: AsyncSession,
    files: Sequence[KbFile],
    *,
    now: datetime | None = None,
    rechunk: bool = False,
) -> KbLoadReport:
    """Write the KB inside the caller's transaction (the caller commits).

    `rechunk` (a full re-index, P5-01): the chunks of files whose hash is unchanged are compared
    with what the chunker gives now, so a change to the chunker or to `MAX_CHUNK_TOKENS` reaches
    every file. Only a document whose chunks really differ is written, gets `version + 1` and a
    `kb.document_updated` record; the others stay as they are."""
    now = now or datetime.now(UTC)
    await session.execute(select(func.pg_advisory_xact_lock(KB_LOAD_LOCK_KEY)))
    rows = await session.execute(select(KbDocument.id, KbDocument.content_hash))
    stored = {doc_id: content_hash for doc_id, content_hash in rows}
    plan = plan_load(stored, files)
    if rechunk:
        plan = await _with_rechunked(session, plan, files)
    report = KbLoadReport(documents_unchanged=len(plan.unchanged))
    for file in plan.added:
        session.add(_document(file, version=1, now=now))
        await session.flush()
        sync = await _sync_chunks(session, file.id, file.chunks)
        await _audit(
            session, "kb.document_added", file.id, {"version": 1, "chunks": len(file.chunks)}
        )
        report.documents_added += 1
        report.chunks_written += sync.written
    for file in plan.changed:
        document = await session.get_one(KbDocument, file.id)
        version = document.version + 1
        _update_document(document, file, version=version, now=now)
        await session.flush()
        sync = await _sync_chunks(session, file.id, file.chunks)
        details = {
            "version": version,
            "chunks": len(file.chunks),
            "chunks_written": sync.written,
            "chunks_removed": sync.removed,
        }
        await _audit(session, "kb.document_updated", file.id, details)
        report.documents_updated += 1
        report.chunks_written += sync.written
        report.chunks_removed += sync.removed
    for doc_id in plan.removed:
        removed = await session.scalar(
            select(func.count()).select_from(KbChunk).where(KbChunk.doc_id == doc_id)
        )
        # The chunks go with the document (ON DELETE CASCADE, migration 0006).
        await session.execute(delete(KbDocument).where(KbDocument.id == doc_id))
        await _audit(session, "kb.document_removed", doc_id, {"chunks_removed": removed or 0})
        report.documents_removed += 1
        report.chunks_removed += removed or 0
    return report


async def _with_rechunked(
    session: AsyncSession, plan: LoadPlan, files: Sequence[KbFile]
) -> LoadPlan:
    """Move unchanged files whose stored chunks differ from the chunker's output to `changed`."""
    unchanged = set(plan.unchanged)
    stored: dict[str, set[tuple[str, int, str, str, int, str]]] = {}
    for row in await session.scalars(select(KbChunk).where(KbChunk.doc_id.in_(unchanged))):
        stored.setdefault(row.doc_id, set()).add(
            (row.id, row.ordinal, row.section, row.text, row.token_count, row.content_hash)
        )
    rechunked = tuple(
        file
        for file in files
        if file.id in unchanged
        and stored.get(file.id, set())
        != {
            (c.id, c.ordinal, c.section, c.text, c.token_count, c.content_hash) for c in file.chunks
        }
    )
    moved = {file.id for file in rechunked}
    return LoadPlan(
        added=plan.added,
        changed=plan.changed + rechunked,
        unchanged=tuple(doc_id for doc_id in plan.unchanged if doc_id not in moved),
        removed=plan.removed,
    )


def _document(file: KbFile, *, version: int, now: datetime) -> KbDocument:
    document = KbDocument(id=file.id)
    _update_document(document, file, version=version, now=now)
    return document


def _update_document(document: KbDocument, file: KbFile, *, version: int, now: datetime) -> None:
    document.title = file.title
    document.product = file.product
    document.visibility = file.visibility
    document.intents = list(file.intents)
    document.url = file.url
    document.content_hash = file.content_hash
    document.version = version
    document.updated_at = file.updated_at or now


async def _sync_chunks(session: AsyncSession, doc_id: str, chunks: Iterable[Chunk]) -> _ChunkSync:
    """Make the stored chunks of one document equal `chunks`, writing only what differs."""
    existing = {
        row.id: row
        for row in await session.scalars(select(KbChunk).where(KbChunk.doc_id == doc_id))
    }
    sync = _ChunkSync()
    wanted: set[str] = set()
    for chunk in chunks:
        wanted.add(chunk.id)
        fields = _chunk_fields(chunk)
        row = existing.get(chunk.id)
        if row is None:
            session.add(KbChunk(id=chunk.id, doc_id=doc_id, **fields))
            sync.written += 1
        elif any(getattr(row, name) != value for name, value in fields.items()):
            for name, value in fields.items():
                setattr(row, name, value)
            sync.written += 1
        else:
            sync.kept += 1
    stale = sorted(set(existing) - wanted)
    if stale:
        await session.execute(delete(KbChunk).where(KbChunk.id.in_(stale)))
        sync.removed = len(stale)
    await session.flush()
    return sync


def _chunk_fields(chunk: Chunk) -> dict[str, Any]:
    return {
        "ordinal": chunk.ordinal,
        "section": chunk.section,
        "text": chunk.text,
        "token_count": chunk.token_count,
        "content_hash": chunk.content_hash,
    }


async def _audit(session: AsyncSession, action: str, doc_id: str, details: dict[str, Any]) -> None:
    await record(
        session,
        actor_type=ActorType.SYSTEM,
        actor_id=ACTOR,
        action=action,
        entity_type="kb_document",
        entity_id=doc_id,
        details=details,
    )
