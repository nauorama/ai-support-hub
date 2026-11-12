"""The Makefile and .env.example follow docs/operations.md §1 and §2.

The expected names are read from the document itself, so a new command or variable in the
document fails this test until the repository catches up.
"""

import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[4]
OPERATIONS = (ROOT / "docs" / "operations.md").read_text(encoding="utf-8")
MAKEFILE = (ROOT / "Makefile").read_text(encoding="utf-8")
BUILT_TARGETS = {"setup", "lint", "test"}


def _section(text: str, heading: str) -> str:
    start = text.index(f"## {heading}")
    end = text.find("\n## ", start + 1)
    return text[start : end if end != -1 else None]


def _documented_commands() -> set[str]:
    section = _section(OPERATIONS, "1. Local development")
    return set(re.findall(r"`make ([a-z][a-z0-9-]*)", section))


def _documented_variables() -> set[str]:
    section = _section(OPERATIONS, "2. Configuration")
    rows = [line for line in section.splitlines() if line.startswith("| `")]
    # Names are in the first column (several per cell, e.g. `QDRANT_URL`, `QDRANT_API_KEY`);
    # the note column adds more (Jira and Slack), the example column holds values like `INFO`.
    names = set()
    for row in rows:
        _, name_cell, _example, note_cell, *_ = re.split(r"(?<!\\)\|", row)
        names |= set(re.findall(r"`([A-Z][A-Z0-9_]+)`", name_cell + note_cell))
    return names


def _makefile_targets() -> set[str]:
    targets = set(re.findall(r"^([a-z][a-z0-9-]*):", MAKEFILE, flags=re.MULTILINE))
    stub_block = re.search(r"NOT_IMPLEMENTED := (.*?)\n\n", MAKEFILE, flags=re.DOTALL)
    assert stub_block is not None
    targets |= set(stub_block.group(1).replace("\\", " ").split())
    return targets


def _env_example_names() -> set[str]:
    text = (ROOT / ".env.example").read_text(encoding="utf-8")
    return set(re.findall(r"^([A-Z][A-Z0-9_]+)=", text, flags=re.MULTILINE))


def test_document_parsing_finds_something() -> None:
    # Protects the other tests from passing on an empty set after a document edit.
    assert {"setup", "eval-attacks", "rollback"} <= _documented_commands()
    assert {"APP_ENV", "SLACK_CHANNEL_ID", "LANGFUSE_HOST"} <= _documented_variables()


def test_every_documented_command_is_a_make_target() -> None:
    assert _documented_commands() - _makefile_targets() == set()


def test_every_documented_variable_is_in_env_example() -> None:
    assert _documented_variables() - _env_example_names() == set()


def test_env_example_holds_no_secret_values() -> None:
    text = (ROOT / ".env.example").read_text(encoding="utf-8")
    for line in text.splitlines():
        name, _, value = line.partition("=")
        if any(word in name for word in ("KEY", "TOKEN", "SECRET", "PASSWORD")):
            assert value == "", f"{name} must be empty in .env.example"


@pytest.mark.parametrize("target", sorted(_documented_commands() - BUILT_TARGETS))
def test_unbuilt_target_says_not_implemented_and_fails(target: str) -> None:
    result = subprocess.run(
        ["make", "--no-print-directory", "-C", str(ROOT), target],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert "not implemented" in result.stdout
