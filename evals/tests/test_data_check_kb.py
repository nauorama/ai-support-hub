"""`make data-check` on `data/kb/`: articles, `error_codes.csv`, intent coverage, card data and
internal-only phrases. The rules themselves are tested in apps/api/tests/unit/test_domain_kb.py;
these tests show that data-check runs them on the files.
"""

from pathlib import Path

import pytest

from hub.domain.enums import Intent
from hub.domain.kb import ERROR_CODE_COLUMNS
from hub_evals.data_check import check, check_kb

ROOT = Path(__file__).resolve().parents[2]
WORDS = " ".join(["word"] * 160)
CSV = (
    ",".join(ERROR_CODE_COLUMNS)
    + "\nERR-2015,Name does not match,The name differs.,Use your own account.,external\n"
    + "ERR-5010,Account under review,Your account is under review.,Wait for us.,external\n"
)


def _article(
    slug: str, intents: str, visibility: str = "external", extra: str = "", title: str = "T"
) -> str:
    return (
        f"---\nid: {slug}\ntitle: {title}\nproduct: general\nvisibility: {visibility}\n"
        f"intents: [{intents}]\nupdated_at: 2026-11-14\n---\n## Section\n{WORDS} {extra}\n"
    )


@pytest.fixture
def kb(tmp_path: Path) -> Path:
    """A small valid KB: the CSV and one external article per intent."""
    folder = tmp_path / "data" / "kb"
    folder.mkdir(parents=True)
    (folder / "error_codes.csv").write_text(CSV, encoding="utf-8")
    for intent in Intent:
        slug = intent.value.replace("_", "-")
        (folder / f"{slug}.md").write_text(_article(slug, intent.value), encoding="utf-8")
    return folder


def _write(kb: Path, slug: str, text: str) -> None:
    (kb / f"{slug}.md").write_text(text, encoding="utf-8")


def test_the_repository_kb_passes() -> None:
    assert check_kb(ROOT / "data" / "kb") == []


def test_the_repository_kb_covers_every_intent_with_an_external_article() -> None:
    # Stated again on the real files: the build-plan "Done when" for P2-08.
    assert not [e for e in check(ROOT / "data") if "no external article" in e]


def test_a_small_valid_kb_passes(kb: Path) -> None:
    assert check_kb(kb) == []


def test_an_empty_or_missing_kb_passes(tmp_path: Path) -> None:
    assert check_kb(tmp_path / "nope") == []
    (tmp_path / "kb").mkdir()
    assert check_kb(tmp_path / "kb") == []


def test_articles_without_the_csv_fail(kb: Path) -> None:
    (kb / "error_codes.csv").unlink()
    assert check_kb(kb) == [f"{kb / 'error_codes.csv'}: missing"]


def test_an_invalid_csv_is_reported(kb: Path) -> None:
    (kb / "error_codes.csv").write_text("code\nERR-1\n", encoding="utf-8")
    errors = check_kb(kb)
    assert len(errors) == 1
    assert errors[0].startswith("kb/error_codes.csv: the header must be exactly")


def test_an_intent_without_an_external_article_fails(kb: Path) -> None:
    _write(kb, "limits", _article("limits", "limits", visibility="internal"))
    assert check_kb(kb) == ["kb: no external article for intent limits"]


def test_a_front_matter_error_is_reported_with_the_file(kb: Path) -> None:
    _write(kb, "extra", _article("extra", "refunds"))
    errors = check_kb(kb)
    assert len(errors) == 1
    assert errors[0].startswith("kb/extra.md: intents.0")


def test_an_article_rule_is_reported_with_the_file(kb: Path) -> None:
    _write(kb, "extra", _article("other-name", "other"))
    assert check_kb(kb) == ["kb/extra.md: id 'other-name' does not match the file name 'extra'"]


def test_an_unknown_error_code_in_an_article_fails(kb: Path) -> None:
    _write(kb, "extra", _article("extra", "other", extra="See ERR-2015 and ERR-2999."))
    assert check_kb(kb) == ["kb/extra.md: ERR-2999 is not in error_codes.csv"]


def test_internal_phrase_in_an_external_article_fails(kb: Path) -> None:
    _write(kb, "extra", _article("extra", "other", extra="Our goodwill limit applies."))
    assert check_kb(kb) == [
        "kb/extra.md: external article contains internal-only phrase 'goodwill'"
    ]


def test_internal_phrase_in_an_internal_article_passes(kb: Path) -> None:
    text = _article("extra", "other", visibility="internal", extra="Goodwill; open Jira.")
    _write(kb, "extra", text)
    assert check_kb(kb) == []


def test_internal_phrase_in_an_external_csv_row_fails(kb: Path) -> None:
    row = "ERR-4020,Country not supported,Your country is sanctioned.,None.,external\n"
    with (kb / "error_codes.csv").open("a", encoding="utf-8") as file:
        file.write(row)
    assert check_kb(kb) == [
        "kb/error_codes.csv: ERR-4020: external row contains internal-only phrase 'sanction'"
    ]


@pytest.mark.parametrize("text", ["Card 4111 1111 1111 1111 works.", "My CVV is 123."])
def test_card_data_in_an_article_fails(kb: Path, text: str) -> None:
    _write(kb, "extra", _article("extra", "other", extra=text))
    errors = check_kb(kb)
    assert errors == ["kb/extra.md: text looks like card data (masking would change it)"]
    assert "4111" not in errors[0]


def test_card_data_in_the_csv_fails(kb: Path) -> None:
    row = "ERR-3001,Low balance,Card 4111111111111111 failed.,Top up.,external\n"
    with (kb / "error_codes.csv").open("a", encoding="utf-8") as file:
        file.write(row)
    assert check_kb(kb) == [
        "kb/error_codes.csv: text looks like card data (masking would change it)"
    ]
