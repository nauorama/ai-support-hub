"""The whole external knowledge base as one text, for baseline B0 (P3-05, ai-pipeline §10).

B0 has no retrieval: the write prompt holds every `external` document inside one `<kb>` tag.
The tag is written in the prompt file (ai-pipeline §7: data tags are never built in code);
inside it, documents are separated by plain-text header lines, so a document can be cited by
its id without a tag of its own. `internal` documents never get here (AGENTS.md, never do #5).

Two sources give the same text, and a test checks it: Postgres (`kb_documents`, `kb_chunks`)
for a live run, and the files in `data/kb/` (through the loader's parser) for
`make llm-requests`, so a recorded request has the same fixture key as the live one.
The text is deterministic: documents sorted by id, chunks by ordinal.
"""

from collections.abc import Iterable
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from hub.db.models import KbChunk, KbDocument
from hub.domain.kb import Visibility
from hub.kb.loader import KbFile

SEPARATOR = "=" * 40


@dataclass(frozen=True, slots=True)
class KbSource:
    """One external document with the texts of its chunks, in order."""

    id: str
    title: str
    url: str
    chunks: tuple[str, ...]


def sources_from_files(files: Iterable[KbFile]) -> list[KbSource]:
    return sorted(
        (
            KbSource(
                id=file.id,
                title=file.title,
                url=file.url,
                chunks=tuple(chunk.text for chunk in sorted(file.chunks, key=lambda c: c.ordinal)),
            )
            for file in files
            if file.visibility == Visibility.EXTERNAL
        ),
        key=lambda source: source.id,
    )


async def load_external_sources(session: AsyncSession) -> list[KbSource]:
    rows = await session.execute(
        select(KbDocument.id, KbDocument.title, KbDocument.url, KbChunk.text)
        .join(KbChunk, KbChunk.doc_id == KbDocument.id)
        .where(KbDocument.visibility == Visibility.EXTERNAL)
        .order_by(KbDocument.id, KbChunk.ordinal)
    )
    sources: list[KbSource] = []
    for doc_id, title, url, text in rows:
        if sources and sources[-1].id == doc_id:
            last = sources[-1]
            sources[-1] = KbSource(last.id, last.title, last.url, (*last.chunks, text))
        else:
            sources.append(KbSource(doc_id, title, url, (text,)))
    return sources


def render_kb(sources: Iterable[KbSource]) -> str:
    """The value of the `{{kb}}` placeholder of the write prompt."""
    blocks = [
        "\n".join(
            [
                SEPARATOR,
                f"Document id: {source.id}",
                f"Title: {source.title}",
                f"URL: {source.url}",
                "",
                "\n\n".join(source.chunks),
            ]
        )
        for source in sources
    ]
    return "\n\n".join(blocks)
