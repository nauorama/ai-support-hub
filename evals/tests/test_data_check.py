"""`make data-check`: the layout rules (fail safe for files without a validator). The seed and
knowledge-base validators are tested in test_data_check_seed.py and test_data_check_kb.py, the
evaluation validators in test_data_check_eval.py."""

from pathlib import Path

import pytest

from hub_evals.data_check import check, check_layout, main

ROOT = Path(__file__).resolve().parents[2]


def _layout(tmp_path: Path, *files: str) -> Path:
    data = tmp_path / "data"
    for folder in ("kb", "seed", "eval"):
        (data / folder).mkdir(parents=True)
        (data / folder / ".gitkeep").touch()
    for name in files:
        (data / name).parent.mkdir(parents=True, exist_ok=True)
        (data / name).write_text("x", encoding="utf-8")
    return data


def test_the_repository_data_folder_passes() -> None:
    assert check_layout(ROOT / "data") == []


def test_empty_known_folders_pass(tmp_path: Path) -> None:
    assert check_layout(_layout(tmp_path)) == []


def test_os_noise_is_ignored(tmp_path: Path) -> None:
    assert check_layout(_layout(tmp_path, ".DS_Store", "kb/.DS_Store")) == []


@pytest.mark.parametrize(
    "name",
    [
        "kb/refunds.txt",
        "kb/error_codes.json",
        "seed/accounts.json",
        "seed/old/customers.json",
        "eval/tickets.test.jsonl",
        "eval/tickets.jsonl",
        "eval/old/tickets.dev.jsonl",
        "kb/nested/deep.md",
    ],
)
def test_a_data_file_without_a_validator_fails(tmp_path: Path, name: str) -> None:
    errors = check_layout(_layout(tmp_path, name))
    assert errors == [f"{tmp_path / 'data' / name}: no validator for this file yet"]


@pytest.mark.parametrize("name", ["kb/refunds.md", "kb/error_codes.csv"])
def test_kb_files_have_a_validator(tmp_path: Path, name: str) -> None:
    assert check_layout(_layout(tmp_path, name)) == []


@pytest.mark.parametrize(
    "name",
    ["eval/tickets.dev.jsonl", "eval/attacks.jsonl", "eval/README.md", "eval/CHANGELOG.md"],
)
def test_eval_files_have_a_validator(tmp_path: Path, name: str) -> None:
    assert check_layout(_layout(tmp_path, name)) == []


def test_the_test_set_is_refused_by_name_and_never_read(tmp_path: Path) -> None:
    # AGENTS.md rule 7: a file that is not valid JSON Lines would give a parse error if read.
    data = _layout(tmp_path, "eval/tickets.test.jsonl")
    path = data / "eval" / "tickets.test.jsonl"
    path.chmod(0)
    try:
        assert check(data) == [f"{path}: no validator for this file yet"]
    finally:
        path.chmod(0o600)


def test_an_unknown_folder_fails(tmp_path: Path) -> None:
    data = _layout(tmp_path)
    (data / "misc").mkdir()
    assert check_layout(data) == [f"{data / 'misc'}: not a known data folder (eval, kb, seed)"]


def test_a_file_at_the_top_level_fails(tmp_path: Path) -> None:
    data = _layout(tmp_path, "notes.txt")
    assert check_layout(data) == [f"{data / 'notes.txt'}: not a known data folder (eval, kb, seed)"]


def test_a_missing_data_folder_fails(tmp_path: Path) -> None:
    assert check_layout(tmp_path / "nope") == [f"{tmp_path / 'nope'}: not a directory"]


def test_main_exit_codes_and_messages(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    good = _layout(tmp_path / "good")
    assert main([str(good)]) == 0
    assert "data-check: OK" in capsys.readouterr().out

    bad = _layout(tmp_path / "bad", "kb/refunds.txt")
    assert main([str(bad)]) == 1
    err = capsys.readouterr().err
    assert "no validator for this file yet" in err
    assert "FAILED, 1 problem(s)" in err

    assert main([]) == 2
    assert "usage:" in capsys.readouterr().err
