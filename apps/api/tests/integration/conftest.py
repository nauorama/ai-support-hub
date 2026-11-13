"""Testcontainers fixtures for integration tests (`make test-int`, needs Docker).

The containers use the image tags of infra/docker-compose.yml, read from the parsed compose file,
so tests and the local stack cannot run different versions. Each container starts once per test
session; tests that change data must clean up after themselves or use their own schema.
"""

from collections.abc import AsyncIterator, Iterator

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from testcontainers.community.postgres import PostgresContainer
from testcontainers.community.qdrant import QdrantContainer
from testcontainers.core.wait_strategies import HttpWaitStrategy

from ._compose import ROOT, compose_config, compose_image
from ._db import app_url, migrate

POSTGRES_INIT_DIR = ROOT / "infra" / "postgres" / "init"


@pytest.fixture(scope="session")
def postgres_container() -> Iterator[PostgresContainer]:
    # Same image, user, database and init scripts as compose (so hub_eval exists here too).
    env = compose_config()["services"]["postgres"]["environment"]
    container = PostgresContainer(
        compose_image("postgres"),
        username=env["POSTGRES_USER"],
        password=env["POSTGRES_PASSWORD"],
        dbname=env["POSTGRES_DB"],
        driver="psycopg",
    ).with_volume_mapping(str(POSTGRES_INIT_DIR), "/docker-entrypoint-initdb.d", "ro")
    with container:
        yield container


@pytest.fixture(scope="session")
def postgres_url(postgres_container: PostgresContainer) -> str:
    """SQLAlchemy URL (`postgresql+psycopg://…`), the same form as DATABASE_URL."""
    return postgres_container.get_connection_url()


@pytest.fixture(scope="session")
def qdrant_url() -> Iterator[str]:
    """REST URL of a Qdrant container, the same form as QDRANT_URL. Used from phase 5 on."""
    # Wait for /readyz (what the readiness check and the compose healthcheck use), not for the
    # default start-up log line, which can appear before Qdrant serves requests.
    container = QdrantContainer(compose_image("qdrant")).waiting_for(
        HttpWaitStrategy(6333, "/readyz").for_status_code(200)
    )
    with container:
        yield f"http://{container.rest_host_address}"


@pytest.fixture(scope="session")
def migrated_db_url(postgres_url: str) -> str:
    """Migrator URL of the `hub` database after `alembic upgrade head`."""
    migrate(postgres_url)
    return postgres_url


@pytest.fixture(scope="session")
def app_db_url(migrated_db_url: str) -> str:
    """The migrated database as the app role (`hub_api`, member of `hub_app`)."""
    return app_url(migrated_db_url)


@pytest.fixture
async def app_session(app_db_url: str) -> AsyncIterator[AsyncSession]:
    """A session as the app role inside one outer transaction that is rolled back at the end,
    so tests leave no rows behind (SQLAlchemy recipe "Joining a Session into an External
    Transaction"). Commits inside the test become savepoints."""
    engine = create_async_engine(app_db_url)
    async with engine.connect() as connection:
        transaction = await connection.begin()
        session = AsyncSession(
            bind=connection, join_transaction_mode="create_savepoint", expire_on_commit=False
        )
        try:
            yield session
        finally:
            await session.close()
            await transaction.rollback()
    await engine.dispose()
