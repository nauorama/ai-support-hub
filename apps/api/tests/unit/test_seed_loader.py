"""`hub.seed` without a database: reading the files, default settings, the command line."""

import json
import shutil
from pathlib import Path

import pytest

from hub.domain.enums import Intent, SettingKey
from hub.seed import SEED_FILES, SeedError, default_settings, main, read_seed

SEED_DIR = Path(__file__).resolve().parents[4] / "data" / "seed"


@pytest.fixture
def seed_dir(tmp_path: Path) -> Path:
    return Path(shutil.copytree(SEED_DIR, tmp_path / "seed"))


def test_read_seed_reads_the_repository_files() -> None:
    assert len(read_seed(SEED_DIR).customers) == 40


def test_a_missing_folder_is_refused(tmp_path: Path) -> None:
    with pytest.raises(SeedError, match="cannot read"):
        read_seed(tmp_path / "nope")


def test_an_invalid_file_is_refused(seed_dir: Path) -> None:
    (seed_dir / "cards.json").write_text("[{}]", encoding="utf-8")
    with pytest.raises(SeedError, match="invalid"):
        read_seed(seed_dir)


def test_inconsistent_files_are_refused(seed_dir: Path) -> None:
    path = seed_dir / "cards.json"
    cards = json.loads(path.read_text(encoding="utf-8"))
    cards[0]["customer_ref"] = "CUS-9999"
    path.write_text(json.dumps(cards), encoding="utf-8")
    with pytest.raises(SeedError, match="inconsistent"):
        read_seed(seed_dir)


def test_default_settings() -> None:
    settings = default_settings(2.5)
    assert set(settings) == set(SettingKey)
    assert settings[SettingKey.AUTO_REPLY_ENABLED] is False
    assert settings[SettingKey.CONFIDENCE_THRESHOLD] == 0.95
    assert settings[SettingKey.DAILY_BUDGET_USD] == 2.5
    allowlist = settings[SettingKey.INTENT_ALLOWLIST]
    # Never the intents that always go to a human (docs/ai-pipeline.md §6).
    assert not {Intent.ACCOUNT_RESTRICTED, Intent.CARD_LOST_STOLEN, Intent.OTHER} & set(allowlist)
    assert len(allowlist) == 7


def test_main_reports_a_seed_error_without_a_traceback(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main([str(tmp_path)]) == 1
    assert capsys.readouterr().err.startswith("seed: cannot read")


def test_the_loader_and_data_check_know_the_same_files() -> None:
    from hub_evals.data_check import SEED_FILES as CHECKED

    assert SEED_FILES == CHECKED
