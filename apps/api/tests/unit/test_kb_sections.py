"""Sections and anchors of stored KB documents (`hub/kb/sections.py`): the targets of citation
links and the sections of `GET /help/{doc_id}`."""

from pathlib import Path

import pytest

from hub.kb.chunker import chunk_article
from hub.kb.loader import read_kb
from hub.kb.sections import StoredChunk, chunk_anchors, section_url, sections, slug

ROOT = Path(__file__).resolve().parents[4]


@pytest.mark.parametrize(
    ("heading", "anchor"),
    [
        ("SEPA withdrawals", "sepa-withdrawals"),
        ("How long does it take?", "how-long-does-it-take"),
        ("Fees & limits (2026)", "fees-limits-2026"),
        ("  Spaces  -- and dashes ", "spaces-and-dashes"),
        ("ERR-2003", "err-2003"),
        ("???", "section"),
    ],
)
def test_slug(heading: str, anchor: str) -> None:
    assert slug(heading) == anchor


def _stored(doc_id: str, title: str, body: str) -> list[StoredChunk]:
    return [StoredChunk(c.id, c.section, c.text) for c in chunk_article(doc_id, title, body)]


def test_sections_strip_the_chunk_prefix_and_keep_the_order() -> None:
    stored = _stored("a", "Title", "## First\n\nOne.\n\n## Second\n\nTwo.")
    result = sections("a", "Title", stored)
    assert [(s.anchor, s.heading, s.text) for s in result] == [
        ("first", "First", "One."),
        ("second", "Second", "Two."),
    ]
    assert chunk_anchors("a", "Title", stored) == {"a#0": "first", "a#1": "second"}
    assert section_url("a", "second") == "/help/a#second"


def test_the_pieces_of_a_split_section_are_one_section() -> None:
    paragraphs = "\n\n".join(f"Paragraph {i}" + " word" * 120 for i in range(4))
    stored = _stored("a", "Title", f"## Long\n\n{paragraphs}\n\n## Short\n\nEnd.")
    assert len(stored) > 2  # the long section was split
    result = sections("a", "Title", stored)
    assert [s.anchor for s in result] == ["long", "short"]
    assert result[0].text == paragraphs
    assert result[0].chunk_ids == tuple(c.id for c in stored[:-1])


def test_repeated_headings_get_unique_anchors() -> None:
    stored = _stored(
        "a", "T", "## Fees\n\nA.\n\n## Other\n\nB.\n\n## Fees\n\nC.\n\n## Fees 1\n\nD."
    )
    assert [s.anchor for s in sections("a", "T", stored)] == ["fees", "other", "fees-1", "fees-1-1"]


def test_a_section_without_text() -> None:
    stored = _stored("a", "T", "## Empty\n\n## Full\n\nText.")
    assert [(s.anchor, s.text) for s in sections("a", "T", stored)] == [
        ("empty", ""),
        ("full", "Text."),
    ]


def test_a_chunk_without_the_usual_prefix_is_kept_whole() -> None:
    result = sections("a", "T", [StoredChunk("a#0", "S", "free text")])
    assert result[0].text == "free text"


def test_error_codes_use_the_code_as_anchor() -> None:
    stored = [
        StoredChunk(
            f"error-codes#{code}", f"{code} {name}", f"Error codes\n{code} {name}\n\n{text}"
        )
        for code, name, text in [("ERR-2003", "Card blocked", "X."), ("ERR-2004", "Other", "Y.")]
    ]
    result = sections("error-codes", "Error codes", stored)
    assert [(s.anchor, s.text) for s in result] == [("err-2003", "X."), ("err-2004", "Y.")]


def test_every_real_document_has_unique_anchors_and_covers_every_chunk() -> None:
    for file in read_kb(ROOT / "data" / "kb"):
        stored = [StoredChunk(c.id, c.section, c.text) for c in file.chunks]
        anchors = chunk_anchors(file.id, file.title, stored)
        assert set(anchors) == {c.id for c in file.chunks}, file.id
        found = sections(file.id, file.title, stored)
        assert len({s.anchor for s in found}) == len(found), file.id
