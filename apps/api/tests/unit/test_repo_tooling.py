"""The Makefile and .env.example follow docs/operations.md §1 and §2.

The expected names are read from the document itself, so a new command or variable in the
document fails this test until the repository catches up.
"""

import re
import subprocess
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[4]
OPERATIONS = (ROOT / "docs" / "operations.md").read_text(encoding="utf-8")
MAKEFILE = (ROOT / "Makefile").read_text(encoding="utf-8")


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


def _makefile_list(variable: str) -> set[str]:
    # One logical line: the value plus any lines joined by a trailing backslash.
    match = re.search(rf"^{variable} := ((?:.*\\\n)*.*)$", MAKEFILE, flags=re.MULTILINE)
    assert match is not None, f"{variable} not found in Makefile"
    return set(match.group(1).replace("\\", " ").split())


def _stub_targets() -> set[str]:
    # Taken from the Makefile, never from a hand-kept list: once a task implements a target and
    # removes it from NOT_IMPLEMENTED, the test below stops running it (it may cost money).
    return _makefile_list("NOT_IMPLEMENTED")


def _rule_targets() -> set[str]:
    """Targets that have a rule of their own, e.g. `setup:` or `eval smoke:`.

    Skips variable assignments (`X := ...`), special targets (`.PHONY:`), the shared stub rule
    (`$(NOT_IMPLEMENTED):`), comments and recipe lines (they start with a tab).
    """
    targets: set[str] = set()
    for match in re.finditer(r"^([^\s#.$][^:=]*?)\s*::?(?!=)", MAKEFILE, flags=re.MULTILINE):
        targets |= set(match.group(1).split())
    return targets


STUB_RECIPE = ['\t@echo "make $@: not implemented"', "\t@exit 1"]


def _shared_stub_recipe() -> list[str]:
    match = re.search(r"^\$\(NOT_IMPLEMENTED\):\n((?:\t.*\n?)*)", MAKEFILE, flags=re.MULTILINE)
    assert match is not None, "shared stub rule $(NOT_IMPLEMENTED): not found in Makefile"
    return match.group(1).splitlines()


def _assert_only_recipe_is_the_stub(target: str) -> None:
    # Static check of the Makefile text, done before `make` is ever called. `make -n` is no
    # protection on its own: GNU make still runs recipe lines that start with `+` or use $(MAKE).
    assert target not in _rule_targets(), f"{target} is a stub but has a rule of its own"
    assert _shared_stub_recipe() == STUB_RECIPE
    assert not re.search(r"^-?include\b", MAKEFILE, flags=re.MULTILINE), "Makefile includes files"
    assert "%" not in _rule_targets(), "pattern rules could add recipes to stub targets"


def _env_example_names() -> set[str]:
    text = (ROOT / ".env.example").read_text(encoding="utf-8")
    return set(re.findall(r"^([A-Z][A-Z0-9_]+)=", text, flags=re.MULTILINE))


def test_document_parsing_finds_something() -> None:
    # Protects the other tests from passing on an empty set after a document edit.
    assert {"setup", "eval-attacks", "rollback"} <= _documented_commands()
    assert {"APP_ENV", "SLACK_CHANNEL_ID", "LANGFUSE_HOST"} <= _documented_variables()


def test_every_documented_command_is_a_make_target() -> None:
    assert _documented_commands() - (_rule_targets() | _stub_targets()) == set()


def test_built_and_stub_lists_do_not_overlap() -> None:
    assert _makefile_list("BUILT") & _stub_targets() == set()


def test_every_built_target_has_its_own_rule() -> None:
    # A name in BUILT (and .PHONY) without a rule would make `make <name>` exit 0 doing nothing.
    assert _makefile_list("BUILT") - _rule_targets() == set()


def test_no_stub_target_has_a_rule_of_its_own() -> None:
    assert _stub_targets() & _rule_targets() == set()
    assert _shared_stub_recipe() == STUB_RECIPE


def test_every_tests_folder_is_type_checked_and_collected() -> None:
    # A tests folder missing from these lists would silently never run (review note, P0-01).
    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    mypy_files = set(config["tool"]["mypy"]["files"])
    testpaths = config["tool"]["pytest"]["ini_options"]["testpaths"]
    tests_folders = [p for p in ROOT.glob("*/tests") if p.is_dir()]
    tests_folders += [p for p in ROOT.glob("apps/*/tests") if p.is_dir()]
    assert tests_folders, "no tests folders found"
    for folder in tests_folders:
        relative = folder.relative_to(ROOT).as_posix()
        assert relative in mypy_files, f"{relative} missing from [tool.mypy] files"
        assert any(path.startswith(relative) for path in testpaths), (
            f"{relative} missing from pytest testpaths"
        )


def test_every_documented_variable_is_in_env_example() -> None:
    assert _documented_variables() - _env_example_names() == set()


def test_env_example_holds_no_secret_values() -> None:
    text = (ROOT / ".env.example").read_text(encoding="utf-8")
    for line in text.splitlines():
        name, _, value = line.partition("=")
        if any(word in name for word in ("KEY", "TOKEN", "SECRET", "PASSWORD")):
            assert value == "", f"{name} must be empty in .env.example"


def test_env_example_urls_hold_credentials_only_for_localhost() -> None:
    # Local compose defaults like hub:hub@localhost are fine; anything else would be a secret.
    text = (ROOT / ".env.example").read_text(encoding="utf-8")
    for host in re.findall(r"://[^/@\s:]+:[^/@\s]+@([^/:\s]+)", text):
        assert host == "localhost", f"credentials for {host} in .env.example"


@pytest.mark.parametrize("target", sorted(_stub_targets()))
def test_unbuilt_target_says_not_implemented_and_fails(target: str) -> None:
    _assert_only_recipe_is_the_stub(target)
    # Safe only after the static check above: `make -n` still runs `+` and $(MAKE) lines, and the
    # stub recipe has neither. The dry run confirms that make resolves the target to the stub.
    dry_run = subprocess.run(
        ["make", "--no-print-directory", "-n", "-C", str(ROOT), target],
        capture_output=True,
        text=True,
        check=True,
    )
    assert dry_run.stdout.split("\n") == [
        f'echo "make {target}: not implemented"',
        "exit 1",
        "",
    ]
    result = subprocess.run(
        ["make", "--no-print-directory", "-C", str(ROOT), target],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert "not implemented" in result.stdout
