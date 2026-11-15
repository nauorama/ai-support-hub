"""Section chunking (ADR-0006): sections, long-section split, error-code rows, ids and hashes."""

import hashlib

from hypothesis import given
from hypothesis import strategies as st

from hub.domain.kb import ErrorCodeRow, Visibility
from hub.kb.chunker import (
    MAX_CHUNK_TOKENS,
    chunk_article,
    chunk_error_codes,
    estimate_tokens,
    sections,
    split_text,
)

TITLE = "How long does a withdrawal take?"
_WORD_CHARS = st.characters(categories=["L", "N"])


def _words(text: str) -> list[str]:
    return text.split()


# --- token estimate --------------------------------------------------------------------------


def test_tokens_are_characters_divided_by_four_rounded_up() -> None:
    assert [estimate_tokens("x" * n) for n in (0, 1, 4, 5, 1600, 1601)] == [0, 1, 1, 2, 400, 401]


# --- sections --------------------------------------------------------------------------------


def test_one_chunk_per_section_with_title_and_heading_on_top() -> None:
    body = "## SEPA withdrawals\nOne business day.\n\n## SWIFT withdrawals\nUp to 5 days.\n"
    chunks = chunk_article("withdrawal-times", TITLE, body)
    assert [(c.id, c.ordinal, c.section) for c in chunks] == [
        ("withdrawal-times#0", 0, "SEPA withdrawals"),
        ("withdrawal-times#1", 1, "SWIFT withdrawals"),
    ]
    assert chunks[0].text == f"{TITLE}\nSEPA withdrawals\n\nOne business day."
    assert all(c.doc_id == "withdrawal-times" for c in chunks)


def test_a_body_without_sections_is_one_chunk_under_the_title() -> None:
    chunks = chunk_article("no-sections", TITLE, "Just a paragraph.\n\nAnd another one.\n")
    assert [(c.id, c.section, c.text) for c in chunks] == [
        ("no-sections#0", TITLE, f"{TITLE}\n{TITLE}\n\nJust a paragraph.\n\nAnd another one.")
    ]


def test_an_empty_body_has_no_chunks() -> None:
    assert chunk_article("empty", TITLE, "\n  \n") == []


def test_text_before_the_first_section_is_kept_as_its_own_chunk() -> None:
    assert sections("Intro.\n## A\nText A.\n", default_heading=TITLE) == [
        (TITLE, "Intro."),
        ("A", "Text A."),
    ]


def test_a_section_with_only_a_heading_is_the_heading_alone() -> None:
    (chunk,) = chunk_article("doc", TITLE, "## Empty section\n")
    assert chunk.text == f"{TITLE}\nEmpty section"


def test_subheadings_and_closing_hashes() -> None:
    body = "## Steps ##\n### Step one\nDo it.\n#### Detail\nMore.\n"
    assert sections(body, default_heading=TITLE) == [
        ("Steps", "### Step one\nDo it.\n#### Detail\nMore.")
    ]


def test_a_heading_inside_a_code_fence_is_content() -> None:
    body = "## Real\n```\n## not a heading\n```\nAfter.\n"
    assert sections(body, default_heading=TITLE) == [("Real", "```\n## not a heading\n```\nAfter.")]


# --- long sections ---------------------------------------------------------------------------


def _paragraph(n: int, chars: int = 600) -> str:
    return (f"Paragraph {n} " + "word " * chars)[:chars].strip()


def test_a_long_section_is_split_at_paragraph_borders_under_the_limit() -> None:
    paragraphs = [_paragraph(n) for n in range(6)]  # about 3,600 characters, 900 tokens
    body = "## Long\n" + "\n\n".join(paragraphs) + "\n\n## Short\nDone.\n"
    chunks = chunk_article("long", TITLE, body)

    long_chunks = [c for c in chunks if c.section == "Long"]
    assert len(long_chunks) == 3
    assert all(c.token_count <= MAX_CHUNK_TOKENS for c in chunks)
    # Every piece repeats title and heading, keeps whole paragraphs and loses nothing.
    assert all(c.text.startswith(f"{TITLE}\nLong\n\n") for c in long_chunks)
    pieces = [c.text.removeprefix(f"{TITLE}\nLong\n\n") for c in long_chunks]
    assert [p for piece in pieces for p in piece.split("\n\n")] == paragraphs
    # Ordinals and ids continue across the split pieces and the next section.
    assert [c.id for c in chunks] == [f"long#{n}" for n in range(4)]
    assert chunks[-1].section == "Short"


def test_a_too_long_paragraph_is_split_at_line_borders() -> None:
    lines = [f"{n}. " + "step " * 60 for n in range(1, 11)]  # one list, about 3,000 characters
    (first, *rest) = chunk_article("steps", TITLE, "## Steps\n" + "\n".join(lines))
    assert rest
    assert all(c.token_count <= MAX_CHUNK_TOKENS for c in (first, *rest))
    joined = "\n".join(c.text.removeprefix(f"{TITLE}\nSteps\n\n") for c in (first, *rest))
    assert [x.rstrip() for x in joined.split("\n")] == [x.rstrip() for x in lines]


def test_a_single_line_over_the_limit_is_kept_whole() -> None:
    line = "x" * (MAX_CHUNK_TOKENS * 5)
    (chunk,) = chunk_article("one-line", TITLE, f"## Big\n{line}\n")
    assert chunk.text.endswith(line)
    assert chunk.token_count > MAX_CHUNK_TOKENS


def test_split_text_keeps_text_that_fits() -> None:
    assert split_text("a\n\nb", lambda _: True) == ["a\n\nb"]


def test_split_text_drops_extra_blank_lines_between_paragraphs() -> None:
    assert split_text("a\n\n\n \nb", lambda piece: len(piece) <= 1) == ["a", "b"]


@given(
    st.lists(
        st.text(alphabet=_WORD_CHARS | st.just(" "), min_size=1, max_size=900)
        .map(str.strip)
        .filter(bool),
        min_size=1,
        max_size=12,
    )
)
def test_splitting_never_loses_or_reorders_words(paragraphs: list[str]) -> None:
    body = "## Section\n" + "\n\n".join(paragraphs)
    chunks = chunk_article("prop", TITLE, body)
    content = [c.text.removeprefix(f"{TITLE}\nSection\n\n") for c in chunks]
    assert _words(" ".join(content)) == _words(" ".join(paragraphs))


# --- error codes -----------------------------------------------------------------------------


def _row(code: str, title: str = "Beneficiary bank rejected") -> ErrorCodeRow:
    return ErrorCodeRow(
        code=code,
        title=title,
        customer_explanation="The receiving bank returned the payment.",
        customer_action="Check the bank details and try again.",
        visibility=Visibility.EXTERNAL,
    )


def test_one_chunk_per_error_code_row_with_the_code_in_the_id() -> None:
    chunks = chunk_error_codes([_row("ERR-2003"), _row("ERR-1001", "Currency not supported")])
    assert [(c.id, c.doc_id, c.ordinal, c.section) for c in chunks] == [
        ("error-codes#ERR-2003", "error-codes", 0, "ERR-2003 Beneficiary bank rejected"),
        ("error-codes#ERR-1001", "error-codes", 1, "ERR-1001 Currency not supported"),
    ]
    assert chunks[0].text == (
        "Error codes\nERR-2003 Beneficiary bank rejected\n\n"
        "The receiving bank returned the payment.\n\nCheck the bank details and try again."
    )


def test_an_error_code_keeps_its_id_when_rows_are_reordered() -> None:
    forward = chunk_error_codes([_row("ERR-1001"), _row("ERR-2003")])
    backward = chunk_error_codes([_row("ERR-2003"), _row("ERR-1001")])
    assert {c.id: c.content_hash for c in forward} == {c.id: c.content_hash for c in backward}


# --- hashes and determinism ------------------------------------------------------------------


def test_chunks_carry_the_sha256_and_token_estimate_of_their_text() -> None:
    (chunk,) = chunk_article("doc", TITLE, "## A\nText.\n")
    assert chunk.content_hash == hashlib.sha256(chunk.text.encode()).hexdigest()
    assert chunk.token_count == estimate_tokens(chunk.text)


def test_the_same_input_gives_the_same_chunks() -> None:
    body = "## A\n" + "\n\n".join(_paragraph(n) for n in range(5))
    assert chunk_article("doc", TITLE, body) == chunk_article("doc", TITLE, body)
