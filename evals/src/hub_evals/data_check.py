"""`make data-check`: validate the files in `data/` (docs/operations.md §1).

The check fails safe: a file passes only when a validator for it exists. `data/seed/` holds the
four seed files, checked by the schemas and cross-file rules of `hub.domain.seed`, plus a scan
that no text in them looks like card data. `data/kb/` holds the articles (`*.md`) and
`error_codes.csv`, checked by `hub.domain.kb`; the seed's error codes must be in that CSV. Any
other file (except `.gitkeep` placeholders) fails with "no validator", so data can never pass CI
unchecked. Evaluation files get their validator in P4-01.

This never reads `data/eval/tickets.test.jsonl` (AGENTS.md, never-do rule 7): files without a
validator are judged by name only.
"""

import json
import re
import sys
from collections.abc import Iterator
from pathlib import Path

from pydantic import ValidationError

from hub.domain.enums import Intent
from hub.domain.kb import (
    ERROR_CODES_FILE,
    KbFormatError,
    Visibility,
    article_errors,
    error_code_errors,
    error_codes_in,
    parse_article,
    parse_error_codes,
)
from hub.domain.seed import SeedData, parse_seed, validate_seed
from hub.guardrails.masking import mask_text

KNOWN_FOLDERS = frozenset({"kb", "seed", "eval"})
PLACEHOLDER = ".gitkeep"
# Written by macOS Finder and git-ignored; not data.
OS_NOISE = frozenset({".DS_Store"})
SEED_FILES = ("customers.json", "cards.json", "transactions.json", "operators.json")
# Stricter than masking, which only catches Luhn-valid numbers: the seed holds no long digit
# runs at all (cards keep only the last 4 digits).
_LONG_DIGIT_RUN = re.compile(r"\d{13,}")


def check_layout(data_dir: Path) -> list[str]:
    """Return one error message per file that has no validator or is in an unknown place."""
    if not data_dir.is_dir():
        return [f"{data_dir}: not a directory"]
    errors: list[str] = []
    for path in sorted(data_dir.rglob("*")):
        relative = path.relative_to(data_dir)
        if path.name in OS_NOISE or (path.is_dir() and len(relative.parts) > 1):
            continue
        if len(relative.parts) == 1:
            if path.is_dir() and path.name in KNOWN_FOLDERS:
                continue
            errors.append(f"{path}: not a known data folder ({', '.join(sorted(KNOWN_FOLDERS))})")
        elif path.name != PLACEHOLDER and not (_is_seed_file(relative) or _is_kb_file(relative)):
            errors.append(f"{path}: no validator for this file yet")
    return errors


def _is_seed_file(relative: Path) -> bool:
    return relative.parts[0] == "seed" and len(relative.parts) == 2 and relative.name in SEED_FILES


def _is_kb_file(relative: Path) -> bool:
    return (
        relative.parts[0] == "kb"
        and len(relative.parts) == 2
        and (relative.suffix == ".md" or relative.name == ERROR_CODES_FILE)
    )


def _strings(value: object) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield str(key)
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)
    elif isinstance(value, int | float) and not isinstance(value, bool):
        yield str(value)


def check_card_data(name: str, raw: bytes) -> list[str]:
    """No text or number in a seed file may look like a card number, CVV, PIN or 2FA code."""
    try:
        document = json.loads(raw)
    except json.JSONDecodeError:
        return []  # the schema check reports invalid JSON
    errors: list[str] = []
    for text in _strings(document):
        if _looks_like_card_data(text):
            errors.append(f"seed/{name}: value looks like card data (masking would change it)")
    return errors


def _looks_like_card_data(text: str) -> bool:
    return bool(_LONG_DIGIT_RUN.search(text)) or mask_text(text) != text


def check_seed(seed_dir: Path) -> list[str]:
    """Validate the seed files. A missing `data/seed/` folder or empty folder is valid.

    Error codes are checked against `error_codes.csv` in the `kb` folder next to `seed_dir`.
    """
    present = [name for name in SEED_FILES if (seed_dir / name).is_file()]
    if not present:
        return []
    missing = [name for name in SEED_FILES if name not in present]
    if missing:
        return [f"{seed_dir / name}: missing (the seed has four files)" for name in missing]
    files = {name: (seed_dir / name).read_bytes() for name in SEED_FILES}
    errors: list[str] = []
    for name, raw in files.items():
        errors.extend(check_card_data(name, raw))
    try:
        seed = parse_seed(files)
    except ValidationError as error:
        return [*errors, *_format_validation(error)]
    codes = _read_error_codes(seed_dir.parent / "kb" / ERROR_CODES_FILE)
    if codes is None:
        errors.append(f"seed: error codes cannot be checked: {ERROR_CODES_FILE} missing or invalid")
        codes = _codes_used(seed)  # report the other seed rules anyway
    return [*errors, *(f"seed: {message}" for message in validate_seed(seed, codes))]


def _read_error_codes(path: Path) -> frozenset[str] | None:
    try:
        return frozenset(r.code for r in parse_error_codes(path.read_text(encoding="utf-8")))
    except (OSError, KbFormatError):
        return None


def _codes_used(seed: SeedData) -> frozenset[str]:
    codes = {t.error_code for t in seed.transactions}
    codes |= {c.upgrade.error_code for c in seed.customers if c.upgrade is not None}
    return frozenset(code for code in codes if code is not None)


def check_kb(kb_dir: Path) -> list[str]:
    """Validate the articles and `error_codes.csv`. A missing or empty `data/kb/` is valid.

    Once there is any KB file, the CSV must exist and every intent needs an external article.
    """
    articles = sorted(kb_dir.glob("*.md")) if kb_dir.is_dir() else []
    csv_path = kb_dir / ERROR_CODES_FILE
    if not articles and not csv_path.is_file():
        return []
    errors: list[str] = []
    codes: frozenset[str] = frozenset()
    if not csv_path.is_file():
        errors.append(f"{csv_path}: missing")
    else:
        csv_errors, codes = _check_error_codes(csv_path)
        errors.extend(csv_errors)
    covered: set[Intent] = set()
    for path in articles:
        text = path.read_text(encoding="utf-8")
        if _looks_like_card_data(text):
            errors.append(f"kb/{path.name}: text looks like card data (masking would change it)")
        try:
            article = parse_article(text)
        except KbFormatError as error:
            errors.append(f"kb/{path.name}: {error}")
            continue
        errors.extend(f"kb/{path.name}: {e}" for e in article_errors(article, path.stem))
        errors.extend(
            f"kb/{path.name}: {code} is not in {ERROR_CODES_FILE}"
            for code in sorted(error_codes_in(article.body) - codes)
            if codes
        )
        if article.front_matter.visibility is Visibility.EXTERNAL:
            covered.update(article.front_matter.intents)
    if articles:
        errors.extend(
            f"kb: no external article for intent {intent}"
            for intent in Intent
            if intent not in covered
        )
    return errors


def _check_error_codes(path: Path) -> tuple[list[str], frozenset[str]]:
    text = path.read_text(encoding="utf-8")
    errors: list[str] = []
    if _looks_like_card_data(text):
        errors.append(f"kb/{path.name}: text looks like card data (masking would change it)")
    try:
        rows = parse_error_codes(text)
    except KbFormatError as error:
        return [*errors, f"kb/{path.name}: {error}"], frozenset()
    for row in rows:
        errors.extend(f"kb/{path.name}: {e}" for e in error_code_errors(row))
    return errors, frozenset(row.code for row in rows)


def _format_validation(error: ValidationError) -> list[str]:
    return [
        f"seed ({error.title}): {'.'.join(str(p) for p in item['loc'])}: {item['msg']}"
        for item in error.errors(include_url=False, include_input=False)
    ]


def check(data_dir: Path) -> list[str]:
    errors = check_layout(data_dir)
    if data_dir.is_dir():
        errors.extend(check_kb(data_dir / "kb"))
        errors.extend(check_seed(data_dir / "seed"))
    return errors


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print("usage: python -m hub_evals.data_check <data dir>", file=sys.stderr)
        return 2
    data_dir = Path(argv[0])
    errors = check(data_dir)
    for error in errors:
        print(f"data-check: {error}", file=sys.stderr)
    if errors:
        print(f"data-check: FAILED, {len(errors)} problem(s)", file=sys.stderr)
        return 1
    print(f"data-check: OK ({data_dir})")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
