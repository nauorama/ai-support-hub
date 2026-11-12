"""The Makefile and .env.example follow docs/operations.md §1 and §2.

The expected names are read from the document itself, so a new command or variable in the
document fails this test until the repository catches up.

No test here runs a Makefile recipe. The targets are checked in make's own database
(`make -p -n -r <target that does not exist>`), which reads the Makefile and executes nothing.
Several targets cost money or change a deployment (`eval-smoke`, `record-llm`, `rollback`), and a
unit test must never start one by mistake.

Limit: these checks protect against honest mistakes by a later task, such as a target that was
implemented but left in NOT_IMPLEMENTED, a rule with no recipe, or a forgotten tests folder.
They do not protect against hostile Makefile edits: whoever can edit the Makefile can already
run any code through `make test` itself. The text check below only bans the constructs that make
executes while it reads the file, so that reading the database stays free of side effects.
"""

import os
import re
import subprocess
import tomllib
from dataclasses import dataclass
from functools import cache
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
OPERATIONS = (ROOT / "docs" / "operations.md").read_text(encoding="utf-8")
MAKEFILE = (ROOT / "Makefile").read_text(encoding="utf-8")

NO_SUCH_TARGET = "__p0_database_dump_no_such_target__"
STUB_RECIPE = ('@echo "make $@: not implemented"', "@exit 1")

# make 3.81 prints "#  commands to execute (from `Makefile', line 37):",
# make 4.x prints  "#  recipe to execute (from 'Makefile', line 37):". Everything else we read
# (the "# Files" section, "name: prerequisites", "NAME := value") is the same in both.
RECIPE_HEADER = re.compile(r"^#  (?:commands|recipe) to execute \(from [`'].+', line (\d+)\):$")


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
    names: set[str] = set()
    for row in rows:
        _, name_cell, _example, note_cell, *_ = re.split(r"(?<!\\)\|", row)
        names |= set(re.findall(r"`([A-Z][A-Z0-9_]+)`", name_cell + note_cell))
    return names


def _env_example_names() -> set[str]:
    text = (ROOT / ".env.example").read_text(encoding="utf-8")
    return set(re.findall(r"^([A-Z][A-Z0-9_]+)=", text, flags=re.MULTILINE))


# --- make database ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Rule:
    prerequisites: str
    recipe: tuple[str, ...]
    recipe_line: int | None


def _assert_makefile_runs_nothing_while_parsed() -> None:
    # make executes these while it reads the file, so even `make -p -n` would run them.
    forbidden = {
        "$(shell / $(eval": r"\$[({]\s*(?:shell|eval)\b",
        "include / -include / sinclude": r"^\s*(?:-|s)?include\b",
        "BASH_ENV / .SHELLFLAGS": r"\b(?:BASH_ENV|\.SHELLFLAGS)\b",
        "a SHELL other than /bin/bash": (
            r"^\s*(?:export\s+|override\s+)*SHELL\b(?!\s*:=\s*/bin/bash\s*$)"
        ),
    }
    code = "\n".join(x for x in MAKEFILE.splitlines() if not x.lstrip().startswith("#"))
    for what, pattern in forbidden.items():
        assert not re.search(pattern, code, flags=re.MULTILINE), f"Makefile uses {what}"


@cache
def _make_run() -> subprocess.CompletedProcess[str]:
    _assert_makefile_runs_nothing_while_parsed()
    # A clean environment: no MAKEFLAGS from an outer `make test`, and English messages, because
    # make translates the database headers.
    env = {k: v for k, v in os.environ.items() if not k.startswith(("MAKE", "MFLAGS"))}
    env["LC_ALL"] = "C"
    result = subprocess.run(
        ["make", "-p", "-n", "-r", "--no-print-directory", "-C", str(ROOT), NO_SUCH_TARGET],
        capture_output=True,
        text=True,
        check=False,  # exits 2: the target does not exist, which is the point
        env=env,
    )
    # 3.81 quotes `name', 4.x quotes 'name'.
    assert re.search(rf"No rule to make target [`']{NO_SUCH_TARGET}'", result.stderr), result.stderr
    return result


def _make_database() -> str:
    return _make_run().stdout


@cache
def _rules() -> dict[str, Rule]:
    database = _make_database()
    files = database.split("\n# Files\n", 1)[1].split("\n# files hash-table stats:", 1)[0]
    rules: dict[str, Rule] = {}
    for block in files.split("\n\n"):
        lines = block.strip("\n").split("\n")
        if not lines[0] or lines[0].startswith("#"):  # "# Not a target:" and other notes
            continue
        name, _, prerequisites = lines[0].partition(":")
        recipe_line = None
        recipe: list[str] = []
        for i, line in enumerate(lines):
            if header := RECIPE_HEADER.match(line):
                recipe_line = int(header.group(1))
                # One tab + the line as written; 3.81 adds an empty trailing line.
                recipe = [x[1:] for x in lines[i + 1 :] if x.startswith("\t") and x.strip()]
        rules[name] = Rule(prerequisites.lstrip(":").strip(), tuple(recipe), recipe_line)
    assert {"setup", "lint", "test"} <= rules.keys(), "could not read the make database"
    return rules


def _make_list(variable: str) -> set[str]:
    # The final value after all assignments, as make itself sees it.
    match = re.search(rf"^{variable} :?= (.*)$", _make_database(), flags=re.MULTILINE)
    assert match is not None, f"{variable} not found in the make database"
    return set(match.group(1).split())


# --- tests -----------------------------------------------------------------------------------


def test_document_parsing_finds_something() -> None:
    # Protects the other tests from passing on an empty set after a document edit.
    assert {"setup", "eval-attacks", "rollback"} <= _documented_commands()
    assert {"APP_ENV", "SLACK_CHANNEL_ID", "LANGFUSE_HOST"} <= _documented_variables()


def test_every_documented_command_is_built_or_a_stub() -> None:
    known = _make_list("BUILT") | _make_list("NOT_IMPLEMENTED")
    assert _documented_commands() - known == set()


def test_built_and_stub_lists_do_not_overlap() -> None:
    assert _make_list("BUILT") & _make_list("NOT_IMPLEMENTED") == set()


def test_every_built_target_has_a_recipe() -> None:
    # A name without a recipe would make `make <name>` exit 0 doing nothing.
    rules = _rules()
    for target in sorted(_make_list("BUILT")):
        assert target in rules, f"{target} is in BUILT but has no rule"
        assert rules[target].recipe, f"{target} is in BUILT but its rule has no recipe"


def test_every_stub_target_only_says_not_implemented_and_fails() -> None:
    # The recipe make would really run, after all rules, overrides and includes: exactly the
    # shared stub, from one place in the Makefile, with no prerequisites that would run first.
    rules = _rules()
    stub_lines = set()
    for target in sorted(_make_list("NOT_IMPLEMENTED")):
        assert target in rules, f"{target} is in NOT_IMPLEMENTED but has no rule"
        rule = rules[target]
        assert rule.recipe == STUB_RECIPE, f"{target} has a recipe other than the stub"
        assert rule.prerequisites == "", f"{target} has prerequisites: {rule.prerequisites}"
        stub_lines.add(rule.recipe_line)
    assert len(stub_lines) == 1, f"stub recipes come from several places: {stub_lines}"


def test_no_rule_overrides_another() -> None:
    # A real rule above the stub rule is silently replaced by the stub; make only warns.
    # 3.81: "warning: overriding commands for target", 4.x: "warning: overriding recipe ...".
    assert "warning: overriding" not in _make_run().stderr, _make_run().stderr


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


def test_every_tests_folder_is_type_checked_and_collected() -> None:
    # A tests folder missing from these lists would silently never run. Covers the layout of
    # design §8 (`<dir>/tests` and `apps/<app>/tests`); deeper folders are not searched.
    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    mypy_files = set(config["tool"]["mypy"]["files"])
    testpaths = config["tool"]["pytest"]["ini_options"]["testpaths"]
    tests_folders = [p for p in ROOT.glob("*/tests") if p.is_dir()]
    tests_folders += [p for p in ROOT.glob("apps/*/tests") if p.is_dir()]
    assert tests_folders, "no tests folders found"
    for folder in tests_folders:
        relative = folder.relative_to(ROOT).as_posix()
        assert relative in mypy_files, f"{relative} missing from [tool.mypy] files"
        assert any(path == relative or path.startswith(relative + "/") for path in testpaths), (
            f"{relative} missing from pytest testpaths"
        )
