"""`make data-check`: validate the files in `data/` (docs/operations.md §1).

No validator exists yet, so the check fails safe: `data/` may hold only the folders of
docs/design.md §8 and their `.gitkeep` placeholders. Any other file fails with "no validator",
so data can never pass CI unchecked. The tasks that add data add its validator here:
seed data in P2-05, knowledge-base articles in P2-08, evaluation files in P4-01.

Only names are checked; no file is opened. In particular this never reads
`data/eval/tickets.test.jsonl` (AGENTS.md, never-do rule 7).
"""

import sys
from pathlib import Path

KNOWN_FOLDERS = frozenset({"kb", "seed", "eval"})
PLACEHOLDER = ".gitkeep"
# Written by macOS Finder and git-ignored; not data.
OS_NOISE = frozenset({".DS_Store"})


def check_layout(data_dir: Path) -> list[str]:
    """Return one error message per problem; an empty list means the layout is valid."""
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
        elif path.name != PLACEHOLDER:
            errors.append(f"{path}: no validator for this file yet")
    return errors


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print("usage: python -m hub_evals.data_check <data dir>", file=sys.stderr)
        return 2
    data_dir = Path(argv[0])
    errors = check_layout(data_dir)
    for error in errors:
        print(f"data-check: {error}", file=sys.stderr)
    if errors:
        print(f"data-check: FAILED, {len(errors)} problem(s)", file=sys.stderr)
        return 1
    print(f"data-check: OK ({data_dir} holds only known folders and placeholders)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
