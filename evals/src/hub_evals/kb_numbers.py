"""`make kb-numbers`: list every number in `data/kb/` that is not in the world file.

Build-plan P2-08: every fact in the knowledge base comes from docs/tallybridge-world.md. This
script finds the numbers that break that rule mechanically; the list it prints must be empty, or
each item is explained in the pull request. It is a report, not a gate: a number can be fine (for
example "Step 7" in a long list), so it always exits 0 when it could read the files.

What counts as a number: digits, optionally with thousands commas and a decimal part
("€30,000", "2.9%", "0.30"). Equal values match ("15.00" = "15", "30,000" = "30000"). Not
counted: error codes (`ERR-2015`, checked against the CSV by `make data-check`), numbered-list
markers at the start of a line ("1. Open the app"), digits inside a word ("2FA", "3-D" counts
its 3), and the front matter (only `updated_at` has numbers there).
"""

import re
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

_ERROR_CODE = re.compile(r"\bERR-\d{4}\b")
_LIST_MARKER = re.compile(r"^(\s*)\d+\.(?=\s)", re.MULTILINE)
_NUMBER = re.compile(r"(?<![\w.,])(\d{1,3}(?:,\d{3})+|\d+)(\.\d+)?(?![\w])")
_FRONT_MATTER = re.compile(r"\A---\n.*?\n---\n", re.DOTALL)


@dataclass(frozen=True)
class Finding:
    value: str
    location: str


def numbers(text: str) -> Iterator[tuple[int, str]]:
    """(line number, value as written) for each counted number in the text."""
    text = _LIST_MARKER.sub(r"\1", _ERROR_CODE.sub("", text))
    for line_no, line in enumerate(text.splitlines(), start=1):
        for match in _NUMBER.finditer(line):
            yield line_no, match.group(0)


def normalise(value: str) -> Decimal:
    return Decimal(value.replace(",", "")).normalize()


def unknown_numbers(world: str, files: dict[str, str]) -> list[Finding]:
    """Numbers in the files whose value never occurs in the world file."""
    known = {normalise(value) for _, value in numbers(world)}
    findings: list[Finding] = []
    for name, text in sorted(files.items()):
        offset = 0
        front = _FRONT_MATTER.match(text)
        if front is not None:
            offset = front.group(0).count("\n")
            text = text[front.end() :]
        for line_no, value in numbers(text):
            if normalise(value) not in known:
                findings.append(Finding(value, f"{name}:{line_no + offset}"))
    return findings


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: python -m hub_evals.kb_numbers <kb dir> <world file>", file=sys.stderr)
        return 2
    kb_dir, world_path = Path(argv[0]), Path(argv[1])
    try:
        world = world_path.read_text(encoding="utf-8")
        files = {
            str(path): path.read_text(encoding="utf-8")
            for path in sorted(kb_dir.iterdir())
            if path.suffix in {".md", ".csv"}
        }
    except OSError as error:
        print(f"kb-numbers: cannot read: {error}", file=sys.stderr)
        return 2
    findings = unknown_numbers(world, files)
    for finding in findings:
        print(f"{finding.location}: {finding.value}")
    print(f"kb-numbers: {len(findings)} number(s) not in {world_path} ({len(files)} files)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
