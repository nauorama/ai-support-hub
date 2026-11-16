"""Rules of the operator review (P3-05b): edit ratio and citation markers of an edited text."""

from decimal import Decimal

import pytest

from hub.domain.review import citations_kept, edit_ratio, markers_in, unknown_markers

CITATIONS = [
    {"marker": 1, "source_id": "withdrawal-times", "title": "T1", "url": "/help/withdrawal-times"},
    {"marker": 2, "source_id": "error-codes", "title": "T2", "url": "/help/error-codes"},
]


def test_an_unchanged_text_has_ratio_zero() -> None:
    assert edit_ratio(
        "Your withdrawal arrives in 1-2 days [1].", "Your withdrawal arrives in 1-2 days [1]."
    ) == Decimal("0.000")


def test_a_text_with_nothing_in_common_has_ratio_one() -> None:
    assert edit_ratio("aaaa", "zzzz") == Decimal("1.000")


def test_an_empty_draft_replaced_by_text_has_ratio_one() -> None:
    # A draft with can_answer = false has an empty body; the operator writes the whole answer.
    assert edit_ratio("", "We will check this for you.") == Decimal("1.000")


def test_a_small_edit_has_a_small_ratio_rounded_to_three_decimals() -> None:
    ratio = edit_ratio(
        "Your withdrawal arrives in 1-2 days.", "Your withdrawal arrives in 1-3 days."
    )
    assert Decimal("0") < ratio < Decimal("0.1")
    assert ratio == ratio.quantize(Decimal("0.001"))


def test_the_ratio_does_not_depend_on_the_autojunk_heuristic() -> None:
    # Over 200 characters difflib's autojunk would treat frequent characters (spaces) as junk
    # and report almost no match for a one-word change.
    base = "word " * 100
    ratio = edit_ratio(base, base.replace("word", "term", 1))
    assert ratio < Decimal("0.05")


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("No markers.", []),
        ("A [1] and B [2] and A again [1].", [1, 2]),
        ("Card [CARD ****1111] and [IBAN DE89 …3000] are not markers [3].", [3]),
        ("Brackets [ 1 ] and [x] are not markers.", []),
    ],
)
def test_markers_in(body: str, expected: list[int]) -> None:
    assert markers_in(body) == expected


def test_unknown_markers_are_those_without_a_citation() -> None:
    assert unknown_markers("See [1] and [3] and [4].", CITATIONS) == [3, 4]
    assert unknown_markers("See [2].", CITATIONS) == []
    assert unknown_markers("Any [1].", []) == [1]


def test_only_citations_whose_marker_is_still_in_the_text_are_kept() -> None:
    assert citations_kept("Only the second one [2].", CITATIONS) == [CITATIONS[1]]
    assert citations_kept("No marker left.", CITATIONS) == []
    assert citations_kept("[2] then [1]", CITATIONS) == CITATIONS
