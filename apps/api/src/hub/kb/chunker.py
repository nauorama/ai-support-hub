"""Split knowledge-base files into chunks (ADR-0006). Pure: no I/O, no database.

- Article: one chunk per `## ` section. The chunk text starts with the article title and the
  section heading, so a chunk makes sense without its article. A chunk over
  `MAX_CHUNK_TOKENS` is split at paragraph borders (and, for a paragraph that is still too long,
  at line borders); a single line is never cut. No overlap.
- Text before the first `## ` (data-check refuses it, but the chunker must not drop it) becomes a
  chunk whose section is the article title.
- `error_codes.csv`: one chunk per row, never split.

Chunk ids: `<doc_id>#<ordinal>` for articles, `error-codes#<code>` for error codes, so a code
keeps its id when rows are reordered (data model §1).
"""

import hashlib
import math
import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass

from hub.domain.kb import ERROR_CODES_DOC_ID, ErrorCodeRow

MAX_CHUNK_TOKENS = 400
# Token counts are an estimate, not a tokenizer: about 4 characters per token for English text
# (owner decision, P2-09; no new dependency). It decides only where a long section is split.
CHARS_PER_TOKEN = 4
ERROR_CODES_TITLE = "Error codes"

_HEADING = re.compile(r"^## +(?P<heading>.*?)[ #]*$")
_FENCE = re.compile(r"^(```|~~~)")
_BLANK_LINES = re.compile(r"\n[ \t]*\n+")
# Paragraphs first, then lines inside a paragraph that is too long on its own.
_SEPARATORS = ("\n\n", "\n")


@dataclass(frozen=True, slots=True)
class Chunk:
    id: str
    doc_id: str
    ordinal: int
    section: str
    text: str
    token_count: int
    content_hash: str


def estimate_tokens(text: str) -> int:
    """Approximate token count: `ceil(characters / 4)`."""
    return math.ceil(len(text) / CHARS_PER_TOKEN)


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def chunk_article(doc_id: str, title: str, body: str) -> list[Chunk]:
    """The chunks of one article body (front matter already removed)."""
    texts: list[tuple[str, str]] = []
    for heading, content in sections(body, default_heading=title):
        prefix = f"{title}\n{heading}"

        def fits(piece: str, prefix: str = prefix) -> bool:
            return estimate_tokens(_join(prefix, piece)) <= MAX_CHUNK_TOKENS

        texts.extend((heading, _join(prefix, piece)) for piece in split_text(content, fits))
    return [
        _chunk(f"{doc_id}#{ordinal}", doc_id, ordinal, heading, text)
        for ordinal, (heading, text) in enumerate(texts)
    ]


def chunk_error_codes(rows: Iterable[ErrorCodeRow]) -> list[Chunk]:
    """One chunk per error code, in file order."""
    chunks: list[Chunk] = []
    for ordinal, row in enumerate(rows):
        section = f"{row.code} {row.title}"
        text = _join(
            f"{ERROR_CODES_TITLE}\n{section}",
            f"{row.customer_explanation}\n\n{row.customer_action}",
        )
        chunks.append(
            _chunk(f"{ERROR_CODES_DOC_ID}#{row.code}", ERROR_CODES_DOC_ID, ordinal, section, text)
        )
    return chunks


def sections(body: str, default_heading: str) -> list[tuple[str, str]]:
    """`(heading, content)` for every `## ` section, in order.

    Content before the first heading gets `default_heading`; it is kept only when it has text.
    A `## ` line inside a fenced code block is content, not a heading.
    """
    found: list[tuple[str, list[str]]] = [(default_heading, [])]
    in_fence = False
    for line in body.splitlines():
        if _FENCE.match(line):
            in_fence = not in_fence
        match = None if in_fence else _HEADING.match(line)
        if match:
            found.append((match["heading"].strip(), []))
        else:
            found[-1][1].append(line)
    result = [(heading, "\n".join(lines).strip()) for heading, lines in found]
    preamble = result[0]
    return ([preamble] if preamble[1] else []) + result[1:]


def split_text(
    text: str, fits: Callable[[str], bool], separators: Sequence[str] = _SEPARATORS
) -> list[str]:
    """Greedy packing of paragraphs (then lines) into pieces for which `fits` is true.

    A piece that cannot be split further (one line) is returned whole even when it does not fit:
    cutting inside a line would break a sentence or a table row.
    """
    if fits(text) or not separators:
        return [text]
    separator, finer = separators[0], separators[1:]
    parts = _BLANK_LINES.split(text) if separator == "\n\n" else text.split(separator)
    pieces: list[str] = []
    current = ""
    for part in (p.strip("\n") for p in parts):
        if not part.strip():
            continue
        candidate = f"{current}{separator}{part}" if current else part
        if fits(candidate):
            current = candidate
            continue
        if current:
            pieces.append(current)
        if fits(part):
            current = part
        else:
            pieces.extend(split_text(part, fits, finer))
            current = ""
    if current:
        pieces.append(current)
    return pieces


def _join(prefix: str, content: str) -> str:
    return f"{prefix}\n\n{content}" if content else prefix


def _chunk(chunk_id: str, doc_id: str, ordinal: int, section: str, text: str) -> Chunk:
    return Chunk(
        id=chunk_id,
        doc_id=doc_id,
        ordinal=ordinal,
        section=section,
        text=text,
        token_count=estimate_tokens(text),
        content_hash=sha256(text),
    )
