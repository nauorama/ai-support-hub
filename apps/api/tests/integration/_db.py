"""Helpers for database integration tests: run the Alembic migrations, build role URLs.

A plain module, not conftest: pytest advises against importing from conftest files.
"""

from alembic import command
from alembic.config import Config
from sqlalchemy.engine import make_url

from ._compose import ROOT

ALEMBIC_INI = ROOT / "infra" / "alembic.ini"

# Login user of the app role, created by infra/postgres/init/02-app-user.sql (local default).
APP_USER = "hub_api"
APP_PASSWORD = "hub_api"  # noqa: S105 - local test credential from the init script


def alembic_config(url: str) -> Config:
    config = Config(str(ALEMBIC_INI))
    config.attributes["url"] = url
    return config


def migrate(url: str, revision: str = "head") -> None:
    """`alembic upgrade` as the migrator. Sync: env.py runs its own event loop."""
    command.upgrade(alembic_config(url), revision)


def as_user(url: str, user: str, password: str, database: str | None = None) -> str:
    """The same server URL with another login (and optionally another database)."""
    changed = make_url(url).set(username=user, password=password)
    if database is not None:
        changed = changed.set(database=database)
    return changed.render_as_string(hide_password=False)


def app_url(url: str, database: str | None = None) -> str:
    return as_user(url, APP_USER, APP_PASSWORD, database)
