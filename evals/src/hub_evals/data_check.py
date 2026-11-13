"""`make data-check`: validate the files in `data/` (docs/operations.md §1).

The check fails safe: a file passes only when a validator for it exists. `data/seed/` holds the
four seed files, checked by the schemas and cross-file rules of `hub.domain.seed`, plus a scan
that no text in them looks like card data. Any other file (except `.gitkeep` placeholders) fails
with "no validator", so data can never pass CI unchecked. Knowledge-base articles get their
validator in P2-08, evaluation files in P4-01.

This never reads `data/eval/tickets.test.jsonl` (AGENTS.md, never-do rule 7): files without a
validator are judged by name only.
"""

import json
import re
import sys
from collections.abc import Iterator
from pathlib import Path

from pydantic import ValidationError

from hub.domain.seed import parse_seed, validate_seed
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
        elif path.name != PLACEHOLDER and not _is_seed_file(relative):
            errors.append(f"{path}: no validator for this file yet")
    return errors


def _is_seed_file(relative: Path) -> bool:
    return relative.parts[0] == "seed" and len(relative.parts) == 2 and relative.name in SEED_FILES


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
        if _LONG_DIGIT_RUN.search(text) or mask_text(text) != text:
            errors.append(f"seed/{name}: value looks like card data (masking would change it)")
    return errors


def check_seed(seed_dir: Path) -> list[str]:
    """Validate the seed files. A missing `data/seed/` folder or empty folder is valid."""
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
    return [*errors, *(f"seed: {message}" for message in validate_seed(seed))]


def _format_validation(error: ValidationError) -> list[str]:
    return [
        f"seed ({error.title}): {'.'.join(str(p) for p in item['loc'])}: {item['msg']}"
        for item in error.errors(include_url=False, include_input=False)
    ]


def check(data_dir: Path) -> list[str]:
    errors = check_layout(data_dir)
    if data_dir.is_dir():
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
