"""Alembic environment (based on the `async` template of Alembic 1.20).

The URL is the migrator connection: `Settings.database_url_for_migrations` (DATABASE_URL_DIRECT,
falling back to DATABASE_URL). `-x db=eval` migrates the evaluation database instead
(`Settings.eval_database_url_for_migrations`; `make migrate` runs both, P4-03): default privileges
are per database, so the app role gets its grants in `hub_eval` only from these migrations.
Tests pass their own URL in `config.attributes["url"]`; an attribute and not `sqlalchemy.url`,
because the ini parser would read `%` in a password.
"""

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine

from hub.config import load_settings
from hub.db.models import Base

config = context.config

# Only when run from the command line: inside pytest the test's logging setup stays as it is.
if config.config_file_name is not None and not config.attributes.get("url"):
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def _url() -> str:
    url = config.attributes.get("url")
    if isinstance(url, str) and url:
        return url
    settings = load_settings()
    target = context.get_x_argument(as_dictionary=True).get("db", "app")
    if target == "app":
        return settings.database_url_for_migrations.get_secret_value()
    if target == "eval":
        eval_url = settings.eval_database_url_for_migrations.get_secret_value()
        if not eval_url:
            raise SystemExit("EVAL_DATABASE_URL is empty: no evaluation database to migrate")
        return eval_url
    raise SystemExit(f"unknown -x db={target}; use app or eval")


def run_migrations_offline() -> None:
    """`alembic upgrade --sql`: print the SQL instead of running it."""
    context.configure(
        url=_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    # compare_server_default: `alembic check` also compares server defaults (now(), '[]'::jsonb,
    # true, 1), which it skips by default; CHECK constraints are still not compared
    # (tests/integration/test_schema_checks.py does that).
    context.configure(
        connection=connection, target_metadata=target_metadata, compare_server_default=True
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    engine = create_async_engine(_url(), poolclass=pool.NullPool)
    async with engine.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await engine.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
