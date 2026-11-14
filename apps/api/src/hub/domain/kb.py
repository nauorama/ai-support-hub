"""Schemas and checks for the knowledge-base files in `data/kb/` (docs/data-model.md §3).

Two kinds of file: articles (`<id>.md`, YAML front matter + Markdown body) and
`error_codes.csv` (one row per error code; each row becomes one chunk). `make data-check` runs
these checks; the KB loader (P2-09) parses the same way.

Pure code: no I/O, no imports from the rest of the project except the shared enums.
"""

import csv
import io
import re
from collections import Counter
from datetime import date
from enum import StrEnum
from typing import Annotated

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from hub.domain.enums import Intent

MIN_WORDS = 150
MAX_WORDS = 600
ERROR_CODES_FILE = "error_codes.csv"
ERROR_CODE_COLUMNS = ("code", "title", "customer_explanation", "customer_action", "visibility")

# Phrases that belong only in internal material: restriction reasons (the tipping-off rule,
# security §5 / world file §6) and internal procedures (goodwill limits, escalation channels,
# tools). A cheap backstop for the owner's review (gate H3): it cannot catch paraphrases.
# "anti-money-laundering rules" spelled out is customer-facing (world file §3), so only the
# acronym is listed.
INTERNAL_ONLY_PHRASES = (
    "aml",
    "sanctions screening",
    "suspicious activity report",
    "tipping-off",
    "restriction reason",
    "goodwill",
    "good will",
    "supervisor",
    "support-escalations",
    "slack",
    "jira",
    "on-call",
    "fraud analyst",
)
# Error-code texts are shown in the app: ERR-4020 says only that Tallybridge cannot serve the
# country, never why (owner decision, P2-08).
ERROR_CODE_ONLY_PHRASES = ("sanction",)

_SLUG = r"^[a-z0-9]+(-[a-z0-9]+)*$"
ERROR_CODE_PATTERN = r"^ERR-[1-5]\d{3}$"
_ERROR_CODE_IN_TEXT = re.compile(r"\bERR-\d{4}\b")
_FRONT_MATTER = re.compile(r"\A---\n(?P<yaml>.*?\n)---\n(?P<body>.*)\Z", re.DOTALL)
_H1 = re.compile(r"^# ", re.MULTILINE)
_SECTION = re.compile(r"^## +\S", re.MULTILINE)
_LIST_NUMBER = re.compile(r"^\d+[.)]$")
_PHRASE_WORD_GAP = re.compile(r"[\s_-]+")
_TEXT_WORD_GAP = r"[\s_*-]+"

Text = Annotated[str, Field(min_length=1, max_length=200)]
LongText = Annotated[str, Field(min_length=1, max_length=1000)]


class Product(StrEnum):
    ACCOUNT = "account"
    PAYMENT_REQUESTS = "payment_requests"
    WITHDRAWALS = "withdrawals"
    CARD = "card"
    VERIFICATION = "verification"
    SECURITY = "security"
    FEES = "fees"
    GENERAL = "general"


class Visibility(StrEnum):
    EXTERNAL = "external"
    INTERNAL = "internal"


class _Kb(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


class FrontMatter(_Kb):
    id: Annotated[str, Field(pattern=_SLUG, max_length=80)]
    title: Text
    product: Product
    visibility: Visibility
    intents: Annotated[list[Intent], Field(min_length=1)]
    updated_at: date

    @field_validator("intents")
    @classmethod
    def _unique(cls, intents: list[Intent]) -> list[Intent]:
        if len(set(intents)) != len(intents):
            raise ValueError("intents must not repeat")
        return intents


class Article(BaseModel):
    # Not `_Kb`: the body is kept exactly as written (no whitespace stripping).
    model_config = ConfigDict(extra="forbid", frozen=True)

    front_matter: FrontMatter
    body: str


class ErrorCodeRow(_Kb):
    code: Annotated[str, Field(pattern=ERROR_CODE_PATTERN)]
    title: Text
    customer_explanation: LongText
    customer_action: LongText
    visibility: Visibility


class KbFormatError(ValueError):
    """A KB file cannot be parsed. The message lists every problem found."""


def parse_article(text: str) -> Article:
    """Split front matter and body and validate the front matter. Raises KbFormatError."""
    match = _FRONT_MATTER.fullmatch(text)
    if match is None:
        raise KbFormatError("an article is '---', front matter, '---' on its own line, then body")
    try:
        meta = yaml.safe_load(match["yaml"])
    except yaml.YAMLError as error:
        raise KbFormatError(f"front matter is not valid YAML: {error}") from error
    if not isinstance(meta, dict):
        raise KbFormatError("front matter must be a mapping of keys to values")
    try:
        return Article(front_matter=FrontMatter.model_validate(meta), body=match["body"])
    except ValidationError as error:
        raise KbFormatError(_format(error)) from error


def article_errors(article: Article, file_stem: str) -> list[str]:
    """Rules for one article beyond its schema (data model §3)."""
    errors: list[str] = []
    meta = article.front_matter
    if meta.id != file_stem:
        errors.append(f"id {meta.id!r} does not match the file name {file_stem!r}")
    if _H1.search(article.body):
        errors.append("the body has a '# ' heading; the title is in the front matter")
    # Section chunking (ADR-0006): text before the first `## ` would be a chunk without a heading.
    if not _SECTION.match(article.body.lstrip()):
        errors.append("the body must start with a '## ' section")
    words = word_count(article.body)
    if not MIN_WORDS <= words <= MAX_WORDS:
        errors.append(f"{words} words; an article has {MIN_WORDS}-{MAX_WORDS}")
    if meta.visibility is Visibility.EXTERNAL:
        errors.extend(
            f"external article contains internal-only phrase {phrase!r}"
            for phrase in internal_phrases(f"{meta.title}\n{article.body}")
        )
    return errors


def word_count(body: str) -> int:
    """Words of the body, without Markdown heading and list markers."""
    return sum(
        1 for token in body.split() if re.search(r"\w", token) and not _LIST_NUMBER.match(token)
    )


def internal_phrases(text: str, extra: tuple[str, ...] = ()) -> list[str]:
    """The internal-only phrases that occur in the text.

    Case-insensitive. Words of a phrase may be separated by any run of spaces, line breaks,
    hyphens, underscores or Markdown emphasis (`*`), so "restriction reason" also finds
    "restriction_reason", "**restriction** reason" and a phrase split across lines. A phrase
    must start a word (after a letter or digit it does not count) but may continue it
    ("sanction" also finds "sanctioned"), so a plural or another word form does not slip
    through.
    """
    lowered = text.lower()
    return [
        phrase for phrase in (*INTERNAL_ONLY_PHRASES, *extra) if _phrase(phrase).search(lowered)
    ]


def _phrase(phrase: str) -> re.Pattern[str]:
    words = (re.escape(word) for word in _PHRASE_WORD_GAP.split(phrase))
    return re.compile(r"(?<![a-z0-9])" + _TEXT_WORD_GAP.join(words))


def error_codes_in(text: str) -> set[str]:
    """Every `ERR-####` mentioned in a text."""
    return set(_ERROR_CODE_IN_TEXT.findall(text))


def parse_error_codes(text: str) -> list[ErrorCodeRow]:
    """Parse `error_codes.csv`. Raises KbFormatError listing every problem."""
    reader = csv.DictReader(io.StringIO(text, newline=""), strict=True)
    try:
        rows = list(reader)
    except csv.Error as error:
        raise KbFormatError(f"not valid CSV: {error}") from error
    if tuple(reader.fieldnames or ()) != ERROR_CODE_COLUMNS:
        raise KbFormatError(f"the header must be exactly {','.join(ERROR_CODE_COLUMNS)}")
    problems: list[str] = []
    parsed: list[ErrorCodeRow] = []
    for line, row in enumerate(rows, start=2):
        if None in row or any(value is None for value in row.values()):
            problems.append(f"line {line}: expected {len(ERROR_CODE_COLUMNS)} fields")
            continue
        try:
            parsed.append(ErrorCodeRow.model_validate(row))
        except ValidationError as error:
            problems.append(f"line {line}: {_format(error)}")
    duplicates = sorted(c for c, n in Counter(r.code for r in parsed).items() if n > 1)
    problems.extend(f"duplicate code {code}" for code in duplicates)
    if not parsed and not problems:
        problems.append("no rows")
    if problems:
        raise KbFormatError("; ".join(problems))
    return parsed


def error_code_errors(row: ErrorCodeRow) -> list[str]:
    """Rules for one error-code row beyond its schema."""
    if row.visibility is not Visibility.EXTERNAL:
        return []
    text = f"{row.title}\n{row.customer_explanation}\n{row.customer_action}"
    return [
        f"{row.code}: external row contains internal-only phrase {phrase!r}"
        for phrase in internal_phrases(text, ERROR_CODE_ONLY_PHRASES)
    ]


def _format(error: ValidationError) -> str:
    return "; ".join(
        f"{'.'.join(str(p) for p in item['loc']) or 'value'}: {item['msg']}"
        for item in error.errors(include_url=False, include_input=False)
    )
