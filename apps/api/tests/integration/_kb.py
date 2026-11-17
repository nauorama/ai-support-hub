"""Shared settings of the knowledge-base integration tests (a plain module, not conftest)."""

from hub.config import Settings

from ._api import Urls
from ._compose import ROOT

# `make models` downloads the local embedding model here (EMBEDDING_CACHE_DIR's default).
MODEL_DIR = ROOT / ".cache" / "models"
EMBEDDING_MODEL = "fastembed/BAAI/bge-small-en-v1.5"


def settings(urls: Urls, qdrant_url: str) -> Settings:
    return Settings(
        _env_file=None,
        app_env="test",
        database_url=urls[1],
        qdrant_url=qdrant_url,
        embedding_cache_dir=MODEL_DIR,
        models_config=ROOT / "config" / "models.yaml",
        prompts_dir=ROOT / "prompts",
    )
