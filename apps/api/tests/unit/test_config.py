"""Settings: one class, every variable of .env.example, unknown names in .env stop the start."""

import re
from datetime import date
from pathlib import Path

import pytest
from pydantic import ValidationError

from hub.config import Settings, SettingsError
from hub.main import create_app

ROOT = Path(__file__).resolve().parents[4]
ENV_EXAMPLE = ROOT / ".env.example"


def _env_example_names() -> set[str]:
    text = ENV_EXAMPLE.read_text(encoding="utf-8")
    return set(re.findall(r"^([A-Z][A-Z0-9_]*)=", text, flags=re.MULTILINE))


def _write_env(tmp_path: Path, text: str) -> Path:
    env = tmp_path / ".env"
    env.write_text(text, encoding="utf-8")
    return env


def test_env_example_and_settings_name_the_same_variables() -> None:
    fields = {name.upper() for name in Settings.model_fields}
    assert _env_example_names() == fields


def test_env_example_loads_as_settings() -> None:
    settings = Settings(_env_file=ENV_EXAMPLE)
    assert settings.app_env == "local"
    assert settings.core_anchor_date is None  # `CORE_ANCHOR_DATE=` means "not set"


def test_unknown_name_in_env_file_fails(tmp_path: Path) -> None:
    env = _write_env(tmp_path, "DATBASE_URL=postgresql+psycopg://x@y/z\n")
    with pytest.raises(ValidationError, match="datbase_url"):
        Settings(_env_file=env)


def test_unknown_name_in_env_file_stops_app_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_env(tmp_path, "LOG_LEVL=INFO\nANTHROPIC_KEY=sk-not-a-real-key\nAPP_ENV=prod\n")
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SettingsError) as raised:
        create_app()
    message = str(raised.value)
    assert "LOG_LEVL: Extra inputs are not permitted" in message
    assert "ANTHROPIC_KEY: Extra inputs are not permitted" in message
    assert "APP_ENV: " in message
    # The value under a misspelled name may be a secret: it must not be in the error.
    assert "sk-not-a-real-key" not in message
    assert "prod" not in message
    assert raised.value.__cause__ is None
    assert raised.value.__suppress_context__


def test_app_starts_with_a_valid_env_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_env(tmp_path, "APP_ENV=test\nCORE_ANCHOR_DATE=2026-11-12\n")
    monkeypatch.chdir(tmp_path)
    app = create_app()
    assert app.state.container.settings.core_anchor_date == date(2026, 11, 12)


@pytest.mark.parametrize(
    ("line", "field"),
    [
        ("APP_ENV=prod", "app_env"),
        ("LLM_MODE=maybe", "llm_mode"),
        ("JIRA_MODE=live", "jira_mode"),
        ("EVAL_TRACE_SAMPLE=1.5", "eval_trace_sample"),
        ("DAILY_BUDGET_USD=-1", "daily_budget_usd"),
        ("CORE_ANCHOR_DATE=yesterday", "core_anchor_date"),
        ("LOG_LEVEL=LOUD", "log_level"),
    ],
)
def test_invalid_values_fail(tmp_path: Path, line: str, field: str) -> None:
    with pytest.raises(ValidationError, match=field):
        Settings(_env_file=_write_env(tmp_path, line + "\n"))


def test_log_level_is_case_insensitive() -> None:
    assert Settings(_env_file=None, log_level="debug").log_level == "DEBUG"


def test_secrets_are_hidden_in_repr() -> None:
    settings = Settings(_env_file=None, session_secret="not-a-real-secret")  # noqa: S106 - placeholder
    assert "not-a-real-secret" not in repr(settings)
    assert settings.session_secret.get_secret_value() == "not-a-real-secret"


def test_migrations_use_the_direct_url_when_set() -> None:
    pooled = Settings(_env_file=None, database_url="postgresql+psycopg://a@pooled/db")
    assert pooled.database_url_for_migrations == "postgresql+psycopg://a@pooled/db"
    direct = pooled.model_copy(update={"database_url_direct": "postgresql+psycopg://a@direct/db"})
    assert direct.database_url_for_migrations == "postgresql+psycopg://a@direct/db"


def test_settings_are_frozen() -> None:
    settings = Settings(_env_file=None)
    with pytest.raises(ValidationError):
        settings.app_env = "demo"  # type: ignore[misc]
