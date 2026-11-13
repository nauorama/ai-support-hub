"""Testcontainers fixtures for integration tests (`make test-int`, needs Docker).

The containers use the image tags of infra/docker-compose.yml, read from the parsed compose file,
so tests and the local stack cannot run different versions. Each container starts once per test
session; tests that change data must clean up after themselves or use their own schema.
"""

import json
import shutil
import subprocess
from collections.abc import Iterator
from functools import cache
from pathlib import Path
from typing import Any

import pytest
from testcontainers.community.postgres import PostgresContainer
from testcontainers.community.qdrant import QdrantContainer

ROOT = Path(__file__).resolve().parents[4]
COMPOSE_FILE = ROOT / "infra" / "docker-compose.yml"
POSTGRES_INIT_DIR = ROOT / "infra" / "postgres" / "init"


@cache
def compose_config() -> dict[str, Any]:
    """The compose file as Docker Compose itself resolves it (validates the file, too)."""
    docker = shutil.which("docker")
    if docker is None:
        pytest.fail("integration tests need Docker: `docker` is not on PATH", pytrace=False)
    result = subprocess.run(
        [docker, "compose", "-f", str(COMPOSE_FILE), "config", "--format", "json"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        pytest.fail(f"docker compose config failed:\n{result.stderr}", pytrace=False)
    config: dict[str, Any] = json.loads(result.stdout)
    return config


def compose_image(service: str) -> str:
    image: str = compose_config()["services"][service]["image"]
    return image


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
    with QdrantContainer(compose_image("qdrant")) as container:
        yield f"http://{container.rest_host_address}"
