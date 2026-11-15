"""KB loader without a database: reading and checking the files, and the load plan.
The writes are tested against Postgres in tests/integration/test_kb_loader.py."""

import hashlib
from datetime import UTC, datetime
from pathlib import Path

import pytest

from hub.domain.kb import ERROR_CODE_COLUMNS, Product, Visibility
from hub.kb import loader
from hub.kb.loader import KbFile, KbLoadError, parse_kb, plan_load, read_kb

from .conftest import TEST_CARD

ROOT = Path(__file__).resolve().parents[4]
WORDS = " ".join(["word"] * 160)
CSV = (
    ",".join(ERROR_CODE_COLUMNS) + "\n"
    "ERR-2003,Beneficiary bank rejected,The bank returned it.,Check the details.,external\n"
    "ERR-1001,Currency not supported,Only EUR USD GBP.,Ask the payer.,external\n"
).encode()


def _article(
    doc_id: str = "withdrawal-times", visibility: str = "external", body: str = ""
) -> bytes:
    return (
        f"---\nid: {doc_id}\ntitle: How long does a withdrawal take?\nproduct: withdrawals\n"
        f"visibility: {visibility}\nintents: [withdrawal_issue]\nupdated_at: 2026-10-01\n---\n"
        f"{body or f'## SEPA\n{WORDS}\n\n## SWIFT\nUp to five days, see ERR-2003.\n'}"
    ).encode()


def test_articles_and_csv_become_documents() -> None:
    error_codes, article = parse_kb({"withdrawal-times": _article()}, CSV)

    assert error_codes.id == "error-codes"
    assert (error_codes.title, error_codes.product, error_codes.visibility) == (
        "Error codes",
        Product.GENERAL,
        Visibility.EXTERNAL,
    )
    assert error_codes.url == "/help/error-codes"
    assert error_codes.updated_at is None
    assert [c.id for c in error_codes.chunks] == ["error-codes#ERR-2003", "error-codes#ERR-1001"]
    assert error_codes.content_hash == hashlib.sha256(CSV).hexdigest()

    assert article.id == "withdrawal-times"
    assert article.url == "/help/withdrawal-times"
    assert article.intents == ("withdrawal_issue",)
    assert article.updated_at == datetime(2026, 10, 1, tzinfo=UTC)
    assert article.content_hash == hashlib.sha256(_article()).hexdigest()
    assert [c.section for c in article.chunks] == ["SEPA", "SWIFT"]


def test_the_real_knowledge_base_is_read_without_problems() -> None:
    files = read_kb(ROOT / "data" / "kb")
    assert files[0].id == "error-codes"
    assert len({f.id for f in files}) == len(files) > 1
    assert all(f.chunks for f in files)


@pytest.mark.parametrize(
    ("articles", "csv", "message"),
    [
        ({}, CSV, "no articles"),
        ({"other-name": _article()}, CSV, "other-name.md: .*does not match the file name"),
        ({"withdrawal-times": _article(body="Text without sections.\n")}, CSV, "## "),
        ({"withdrawal-times": b"no front matter"}, CSV, "withdrawal-times.md: an article is"),
        ({"withdrawal-times": b"\xff\xfe"}, CSV, "not valid UTF-8"),
        (
            {"withdrawal-times": _article(body=f"## Card\n{TEST_CARD} {WORDS}\n")},
            CSV,
            "card data",
        ),
        (
            {"withdrawal-times": _article(body=f"## Goodwill\n{WORDS}\n")},
            CSV,
            "internal-only phrase 'goodwill'",
        ),
        (
            {"withdrawal-times": _article(body=f"## Code\n{WORDS} ERR-9999 ERR-5999\n")},
            CSV,
            "ERR-5999 is not in error_codes.csv",
        ),
        ({"error-codes": _article(doc_id="error-codes")}, CSV, "belongs to error_codes.csv"),
        ({"withdrawal-times": _article()}, b"code,title\n", "error_codes.csv: the header"),
        (
            {"withdrawal-times": _article()},
            CSV.replace(b"Ask the payer.,external", b"Ask the payer.,internal"),
            "ERR-1001: visibility must be external",
        ),
    ],
)
def test_a_bad_file_stops_the_load(articles: dict[str, bytes], csv: bytes, message: str) -> None:
    with pytest.raises(KbLoadError, match=message):
        parse_kb(articles, csv)


def test_every_problem_is_listed_at_once() -> None:
    with pytest.raises(KbLoadError) as caught:
        parse_kb({"a-doc": _article(), "b-doc": b"broken"}, b"code\n")
    message = str(caught.value)
    assert all(name in message for name in ("error_codes.csv:", "a-doc.md:", "b-doc.md:"))


def test_an_internal_article_may_be_loaded() -> None:
    body = f"## Goodwill\n{WORDS}\n"
    article = parse_kb({"withdrawal-times": _article(visibility="internal", body=body)}, CSV)[1]
    assert article.visibility is Visibility.INTERNAL


def test_read_kb_needs_the_folder_and_the_csv(tmp_path: Path) -> None:
    with pytest.raises(KbLoadError, match="is not a folder"):
        read_kb(tmp_path / "missing")
    (tmp_path / "withdrawal-times.md").write_bytes(_article())
    with pytest.raises(KbLoadError, match=r"cannot read .*error_codes\.csv"):
        read_kb(tmp_path)
    (tmp_path / "error_codes.csv").write_bytes(CSV)
    assert [f.id for f in read_kb(tmp_path)] == ["error-codes", "withdrawal-times"]


# --- plan ------------------------------------------------------------------------------------


def _file(doc_id: str, content_hash: str) -> KbFile:
    return KbFile(
        id=doc_id,
        title="t",
        product=Product.GENERAL,
        visibility=Visibility.EXTERNAL,
        intents=(),
        content_hash=content_hash,
        updated_at=None,
        chunks=(),
    )


def test_the_plan_adds_changes_skips_and_removes_by_hash() -> None:
    stored = {"same": "h1", "edited": "h2", "deleted": "h3"}
    files = [_file("same", "h1"), _file("edited", "h2-new"), _file("new", "h4")]

    plan = plan_load(stored, files)

    assert [f.id for f in plan.added] == ["new"]
    assert [f.id for f in plan.changed] == ["edited"]
    assert plan.unchanged == ("same",)
    assert plan.removed == ("deleted",)


def test_the_plan_of_an_unchanged_knowledge_base_is_empty() -> None:
    files = [_file("a", "h1"), _file("b", "h2")]
    plan = plan_load({"a": "h1", "b": "h2"}, files)
    assert (plan.added, plan.changed, plan.removed) == ((), (), ())


# --- command line ----------------------------------------------------------------------------


def test_main_reports_a_bad_folder_and_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(loader, "load_settings", lambda: None)
    assert loader.main([str(tmp_path / "missing")]) == 1
    assert "kb-load: " in capsys.readouterr().err
