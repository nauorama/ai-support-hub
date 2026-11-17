"""Search hits as prompt text: the `{{kb}}` value of the writer with retrieval (P5-03) and the
evidence the evaluation judges get for a RAG draft.

The `<kb>` tag is written in the prompt file (ai-pipeline §7); inside it, chunks are separated by
plain-text header lines, so a chunk is cited by its id without a tag of its own. A chunk's text
already starts with its article title and section heading (`hub.kb.chunker`). Scores and ranks
are left out: the order is the rank, and a number would only invite the model to weigh it.
Only the id and the text are used, so the same function renders hits from Qdrant and chunks read
from `data/kb/` (`make llm-requests`), and both give the same request.
"""

from collections.abc import Iterable
from typing import Protocol

from hub.kb.full_text import SEPARATOR


class ChunkLike(Protocol):
    @property
    def chunk_id(self) -> str: ...

    @property
    def text(self) -> str: ...


def render_chunks(chunks: Iterable[ChunkLike]) -> str:
    """The chunks in the given order. An empty list gives an empty string: the writer then
    finds no answer and sets `can_answer` to false."""
    return "\n\n".join(
        "\n".join([SEPARATOR, f"Source id: {chunk.chunk_id}", "", chunk.text]) for chunk in chunks
    )
