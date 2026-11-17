"""Sections of a stored knowledge-base document, and their anchors. Pure.

A citation link points to one section: `/help/<doc_id>#<anchor>` (api.md §2). The same function
gives the anchors to the index (the `url` of every Qdrant point) and to `GET /help/{doc_id}`, so
a link from an answer always opens the right section.

- Article: one section per `##` heading. The pieces of a split section (ADR-0006) share their
  heading and are joined again. The anchor is the heading in lower case, words joined by `-`;
  a repeated heading gets `-1`, `-2`, … (as GitHub does), so every anchor of a page is unique.
- `error-codes`: one section per code, anchor = the code in lower case (`err-2003`).
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass

from hub.domain.kb import ERROR_CODES_DOC_ID

_NOT_WORD = re.compile(r"[^\w\- ]+")
_SPACES = re.compile(r"[\s\-]+")


@dataclass(frozen=True, slots=True)
class StoredChunk:
    """The columns of `kb_chunks` this module needs, in ordinal order."""

    id: str
    section: str
    text: str


@dataclass(frozen=True, slots=True)
class Section:
    anchor: str
    heading: str
    text: str
    chunk_ids: tuple[str, ...]


def slug(heading: str) -> str:
    """`How long does it take?` → `how-long-does-it-take`; empty headings become `section`."""
    words = _SPACES.sub("-", _NOT_WORD.sub("", heading.lower()).strip()).strip("-")
    return words or "section"


def _body(title: str, chunk: StoredChunk) -> str:
    """The chunk text without the `title`/heading prefix that the chunker adds (ADR-0006)."""
    prefix = f"{title}\n{chunk.section}"
    if chunk.text == prefix:
        return ""
    if chunk.text.startswith(prefix + "\n\n"):
        return chunk.text[len(prefix) + 2 :]
    return chunk.text


def sections(doc_id: str, title: str, chunks: Sequence[StoredChunk]) -> list[Section]:
    """Group the chunks of one document (in ordinal order) into its sections."""
    groups: list[tuple[str, list[StoredChunk]]] = []
    for chunk in chunks:
        if doc_id != ERROR_CODES_DOC_ID and groups and groups[-1][0] == chunk.section:
            groups[-1][1].append(chunk)
        else:
            groups.append((chunk.section, [chunk]))
    used: set[str] = set()
    result: list[Section] = []
    for heading, members in groups:
        if doc_id == ERROR_CODES_DOC_ID:
            base = slug(members[0].id.partition("#")[2])
        else:
            base = slug(heading)
        anchor, count = base, 0
        # Also skips a suffix that a real heading already took (`Fees`, `Fees`, `Fees 1`).
        while anchor in used:
            count += 1
            anchor = f"{base}-{count}"
        used.add(anchor)
        text = "\n\n".join(body for body in (_body(title, m) for m in members) if body)
        result.append(Section(anchor, heading, text, tuple(m.id for m in members)))
    return result


def chunk_anchors(doc_id: str, title: str, chunks: Sequence[StoredChunk]) -> dict[str, str]:
    """`{chunk_id: anchor}` for every chunk of one document."""
    return {
        chunk_id: section.anchor
        for section in sections(doc_id, title, chunks)
        for chunk_id in section.chunk_ids
    }


def section_url(doc_id: str, anchor: str) -> str:
    return f"/help/{doc_id}#{anchor}"
