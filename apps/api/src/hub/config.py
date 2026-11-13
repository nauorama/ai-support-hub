"""All settings of the Hub, read from the environment and `.env` (docs/operations.md §2).

No other module reads environment variables. An unknown name in `.env` (a typo such as
`DATBASE_URL`) stops the app at start, so a setting is never silently left at its default.
Process environment variables have no common prefix, so pydantic-settings cannot tell an
unknown one from `PATH` and ignores it; only `.env` is checked for unknown names.
"""

from datetime import date
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, ValidationError, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="forbid",
        # `KEY=` in .env means "not set": the default applies (e.g. CORE_ANCHOR_DATE= → None).
        env_ignore_empty=True,
        frozen=True,
    )

    app_env: Literal["local", "test", "demo"] = "local"
    log_level: LogLevel = "INFO"

    database_url: str = "postgresql+psycopg://hub:hub@localhost:5432/hub"
    database_url_direct: str = ""
    eval_database_url: str = ""

    core_anchor_date: date | None = None
    jobs_token: SecretStr = SecretStr("")

    qdrant_url: str = "http://localhost:6333"
    qdrant_api_key: SecretStr = SecretStr("")

    anthropic_api_key: SecretStr = SecretStr("")
    openai_api_key: SecretStr = SecretStr("")
    llm_mode: Literal["real", "fake"] = "fake"
    models_config: Path = Path("config/models.yaml")

    mcp_server_url: str = "http://localhost:8001/mcp"
    mcp_server_token: SecretStr = SecretStr("")

    jira_mode: Literal["mock", "real"] = "mock"
    jira_base_url: str = ""
    jira_email: str = ""
    jira_api_token: SecretStr = SecretStr("")
    jira_project_key: str = ""
    slack_mode: Literal["mock", "real"] = "mock"
    slack_bot_token: SecretStr = SecretStr("")
    slack_channel_id: str = ""

    session_secret: SecretStr = SecretStr("")
    demo_access_code: SecretStr = SecretStr("")
    seed_operator_password: SecretStr = SecretStr("")

    langfuse_public_key: str = ""
    langfuse_secret_key: SecretStr = SecretStr("")
    langfuse_host: str = ""

    daily_budget_usd: float = Field(default=3.0, ge=0)
    eval_max_cost_usd: float = Field(default=8.0, ge=0)
    eval_trace_sample: float = Field(default=0.2, ge=0, le=1)

    @field_validator("log_level", mode="before")
    @classmethod
    def _upper_log_level(cls, value: object) -> object:
        return value.upper() if isinstance(value, str) else value

    @property
    def database_url_for_migrations(self) -> str:
        """Direct (not pooled) connection; falls back to the app connection when not set."""
        return self.database_url_direct or self.database_url


class SettingsError(Exception):
    """Settings could not be loaded. Names the variables, never their values."""


def load_settings() -> Settings:
    """Read the settings at start. A wrong value may be a secret under a misspelled name,
    so the error names each variable and the problem, and drops pydantic's `input_value`."""
    try:
        return Settings()
    except ValidationError as exc:
        problems = [
            f"{'.'.join(str(part) for part in error['loc']).upper()}: {error['msg']}"
            for error in exc.errors(include_input=False, include_url=False)
        ]
        raise SettingsError("invalid settings: " + "; ".join(problems)) from None
