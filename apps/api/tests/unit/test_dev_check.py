"""`make dev` stops first when a required setting is empty, and names all of them at once."""

import pytest

from hub import dev_check
from hub.config import Settings, SettingsError

ALL_SET = {
    "seed_operator_password": "pw-for-tests",
    "session_secret": "secret-for-tests",
    "demo_access_code": "code-for-tests",
}


def _settings(**values: str) -> Settings:
    return Settings(_env_file=None, **values)  # type: ignore[arg-type]


def test_nothing_missing_when_all_are_set() -> None:
    assert dev_check.missing_settings(_settings(**ALL_SET)) == []


def test_all_missing_names_are_listed_in_order() -> None:
    assert dev_check.missing_settings(_settings()) == [
        "SEED_OPERATOR_PASSWORD",
        "SESSION_SECRET",
        "DEMO_ACCESS_CODE",
    ]


def test_main_lists_every_missing_name_and_fails(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    only_password = {"seed_operator_password": "pw-for-tests"}
    monkeypatch.setattr(dev_check, "load_settings", lambda: _settings(**only_password))
    assert dev_check.main() == 1
    err = capsys.readouterr().err
    assert "SESSION_SECRET, DEMO_ACCESS_CODE" in err
    assert "SEED_OPERATOR_PASSWORD" not in err
    assert ".env" in err
    assert "Nothing was started or written." in err


def test_main_passes_when_all_are_set(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(dev_check, "load_settings", lambda: _settings(**ALL_SET))
    assert dev_check.main() == 0
    assert capsys.readouterr().err == ""


def test_main_reports_invalid_settings(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def broken() -> Settings:
        raise SettingsError("invalid settings: DATBASE_URL: Extra inputs are not permitted")

    monkeypatch.setattr(dev_check, "load_settings", broken)
    assert dev_check.main() == 1
    assert "DATBASE_URL" in capsys.readouterr().err


def test_values_are_never_printed(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    partial = {"session_secret": "do-not-print-me"}
    monkeypatch.setattr(dev_check, "load_settings", lambda: _settings(**partial))
    dev_check.main()
    assert "do-not-print-me" not in capsys.readouterr().err
