"""Knowledge-base file rules (`hub.domain.kb`): article front matter and body, `error_codes.csv`,
and the internal-only phrase check. The real files in `data/kb/` are checked by
evals/tests/test_data_check_kb.py."""

from datetime import date

import pytest

from hub.domain.enums import Intent
from hub.domain.kb import (
    ERROR_CODE_COLUMNS,
    INTERNAL_ONLY_PHRASES,
    MAX_WORDS,
    MIN_WORDS,
    ErrorCodeRow,
    KbFormatError,
    Product,
    Visibility,
    article_errors,
    error_code_errors,
    error_codes_in,
    internal_phrases,
    parse_article,
    parse_error_codes,
    word_count,
)

FRONT = """---
id: withdrawal-times
title: How long does a withdrawal take?
product: withdrawals
visibility: external
intents: [withdrawal_issue]
updated_at: 2026-10-01
---
"""
WORDS = " ".join(["word"] * 200)
BODY = f"## SEPA withdrawals\n{WORDS}\n\n## SWIFT withdrawals\nMore text.\n"
HEADER = ",".join(ERROR_CODE_COLUMNS)


def _article(front: str = FRONT, body: str = BODY) -> str:
    return front + body


# --- parse_article ----------------------------------------------------------------------------


def test_a_valid_article_parses() -> None:
    article = parse_article(_article())
    meta = article.front_matter
    assert meta.id == "withdrawal-times"
    assert meta.product is Product.WITHDRAWALS
    assert meta.visibility is Visibility.EXTERNAL
    assert meta.intents == [Intent.WITHDRAWAL_ISSUE]
    assert meta.updated_at == date(2026, 10, 1)
    assert article.body == BODY
    assert article_errors(article, "withdrawal-times") == []


def _front(old: str, new: str) -> str:
    return _article(front=FRONT.replace(old, new))


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("## No front matter\n", "front matter"),
        ("---\nid: [unclosed\n---\n## A\n", "not valid YAML"),
        ("---\n- a list\n---\n## A\n", "mapping"),
        (_front("product: withdrawals", "product: loans"), "product"),
        (_front("visibility: external", "visibility: public"), "visibility"),
        (_front("[withdrawal_issue]", "[refund]"), "intents"),
        (_front("[withdrawal_issue]", "[]"), "intents"),
        (_front("[withdrawal_issue]", "[limits, limits]"), "must not repeat"),
        (_front("2026-10-01", "yesterday"), "updated_at"),
        (_front("id: withdrawal-times", "id: Withdrawal_Times"), "id"),
        (_front("updated_at: 2026-10-01", "updated_at: 2026-10-01\nauthor: me"), "author"),
        (_front("title: How long does a withdrawal take?\n", ""), "title"),
    ],
)
def test_bad_front_matter_is_refused(text: str, message: str) -> None:
    with pytest.raises(KbFormatError, match=message):
        parse_article(text)


@pytest.mark.parametrize(
    ("body", "message"),
    [
        (f"# Title\n## A\n{WORDS}\n", "'# ' heading"),
        (f"{WORDS}\n", "start with a '## ' section"),
        (f"Intro line.\n\n## A\n{WORDS}\n", "start with a '## ' section"),
        # The heading word "A" counts: these bodies have MIN_WORDS - 1 and MAX_WORDS + 1 words.
        ("## A\n" + " ".join(["w"] * (MIN_WORDS - 2)) + "\n", f"{MIN_WORDS}-{MAX_WORDS}"),
        ("## A\n" + " ".join(["w"] * MAX_WORDS) + "\n", f"{MIN_WORDS}-{MAX_WORDS}"),
    ],
)
def test_body_rules(body: str, message: str) -> None:
    errors = article_errors(parse_article(_article(body=body)), "withdrawal-times")
    assert any(message in error for error in errors), errors


def test_the_word_limits_are_inclusive() -> None:
    low = "## A\n" + " ".join(["w"] * (MIN_WORDS - 1)) + "\n"  # "A" is the 150th word
    high = "## A\n" + " ".join(["w"] * (MAX_WORDS - 1)) + "\n"
    for body in (low, high):
        assert article_errors(parse_article(_article(body=body)), "withdrawal-times") == []


def test_the_id_must_match_the_file_name() -> None:
    errors = article_errors(parse_article(_article()), "withdrawal-time")
    assert errors == ["id 'withdrawal-times' does not match the file name 'withdrawal-time'"]


def test_word_count_skips_markup() -> None:
    assert word_count("## Heading here\n1. Open the app\n- item\n\n**bold** €1.50") == 8


# --- internal-only phrases --------------------------------------------------------------------


@pytest.mark.parametrize("phrase", INTERNAL_ONLY_PHRASES)
def test_every_internal_phrase_fails_an_external_article(phrase: str) -> None:
    body = f"## A\n{WORDS} {phrase.upper()} here.\n"
    errors = article_errors(parse_article(_article(body=body)), "withdrawal-times")
    assert errors == [f"external article contains internal-only phrase {phrase!r}"]


def test_the_title_is_checked_too() -> None:
    front = FRONT.replace("How long does a withdrawal take?", "Goodwill credits")
    errors = article_errors(parse_article(_article(front=front)), "withdrawal-times")
    assert errors == ["external article contains internal-only phrase 'goodwill'"]


def test_an_internal_article_may_use_internal_phrases() -> None:
    front = FRONT.replace("visibility: external", "visibility: internal")
    body = f"## A\n{WORDS} Goodwill up to the limit; post in #support-escalations; open Jira.\n"
    assert article_errors(parse_article(_article(front=front, body=body)), "withdrawal-times") == []


@pytest.mark.parametrize(
    ("text", "found"),
    [
        ("An AML review", ["aml"]),
        ("Ask a supervisor.", ["supervisor"]),
        ("Two supervisors approve", ["supervisor"]),
        ("the #support-escalations channel", ["support-escalations"]),
        ("EU anti-money-laundering rules", []),
        ("A formal complaint", []),
        ("Our support team replies", []),
        ("example", []),  # "aml" inside a word is not a hit
    ],
)
def test_internal_phrases(text: str, found: list[str]) -> None:
    assert internal_phrases(text) == found


def test_error_codes_in_text() -> None:
    assert error_codes_in("See ERR-2015 and ERR-3001; not ERR-12 or XERR-1234x.") == {
        "ERR-2015",
        "ERR-3001",
    }


# --- error_codes.csv --------------------------------------------------------------------------


def _row(code: str = "ERR-2015", visibility: str = "external", text: str = "Name differs.") -> str:
    return f'{code},Name does not match,"{text}","Use an account in your own name.",{visibility}'


def test_a_valid_csv_parses() -> None:
    rows = parse_error_codes(f"{HEADER}\n{_row()}\n{_row('ERR-3001', text='Too low, sorry.')}\n")
    assert [r.code for r in rows] == ["ERR-2015", "ERR-3001"]
    assert rows[1].customer_explanation == "Too low, sorry."  # the quoted comma stays
    assert all(error_code_errors(r) == [] for r in rows)


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("code,title\nERR-1001,x\n", "header must be exactly"),
        (f"{HEADER}\n", "no rows"),
        (f"{HEADER}\n{_row('ERR-201')}\n", "line 2: code"),
        (f"{HEADER}\n{_row('ERR-6001')}\n", "line 2: code"),
        (f"{HEADER}\n{_row(visibility='public')}\n", "line 2: visibility"),
        (f"{HEADER}\n{_row()}\n{_row()}\n", "duplicate code ERR-2015"),
        (f"{HEADER}\nERR-2015,Title,Text\n", "line 2: expected 5 fields"),
        (f"{HEADER}\n{_row()},extra\n", "line 2: expected 5 fields"),
        (f"{HEADER}\nERR-2015,Title,,Action,external\n", "customer_explanation"),
        (f'{HEADER}\nERR-2015,"unclosed\n', "not valid CSV|expected 5 fields"),
    ],
)
def test_a_bad_csv_is_refused(text: str, message: str) -> None:
    with pytest.raises(KbFormatError, match=message):
        parse_error_codes(text)


def test_an_external_row_with_internal_wording_fails() -> None:
    row = ErrorCodeRow.model_validate(
        {
            "code": "ERR-4020",
            "title": "Country not supported",
            "customer_explanation": "Your country is under EU sanctions.",
            "customer_action": "Ask a supervisor.",
            "visibility": "external",
        }
    )
    assert error_code_errors(row) == [
        "ERR-4020: external row contains internal-only phrase 'supervisor'",
        "ERR-4020: external row contains internal-only phrase 'sanction'",
    ]


def test_an_internal_row_is_not_checked_for_phrases() -> None:
    row = ErrorCodeRow.model_validate(
        {
            "code": "ERR-5010",
            "title": "Restricted",
            "customer_explanation": "Goodwill.",
            "customer_action": "Jira.",
            "visibility": "internal",
        }
    )
    assert error_code_errors(row) == []
