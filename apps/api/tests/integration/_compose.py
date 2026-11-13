"""Read infra/docker-compose.yml as Docker Compose resolves it, for fixtures and tests.

A plain module, not conftest: pytest advises against importing from conftest files.
"""

import json
import shutil
import subprocess
from functools import cache
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[4]
COMPOSE_FILE = ROOT / "infra" / "docker-compose.yml"


@cache
def compose_config() -> dict[str, Any]:
    """The parsed compose file (this also validates it).

    `--no-env-resolution` keeps `.env` (an `env_file` of the api service) out of the result,
    so provider keys and other secrets never sit in test memory or in a failure report.
    """
    docker = shutil.which("docker")
    if docker is None:
        pytest.fail("integration tests need Docker: `docker` is not on PATH", pytrace=False)
    result = subprocess.run(
        [
            docker,
            "compose",
            "-f",
            str(COMPOSE_FILE),
            "config",
            "--format",
            "json",
            "--no-env-resolution",
        ],
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
