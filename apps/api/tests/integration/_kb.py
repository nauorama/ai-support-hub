"""Shared settings of the knowledge-base integration tests (a plain module, not conftest)."""

import json

from hub.config import Settings
from hub.kb.retriever import DEFAULT_TOP_K, KbChunk, SearchMode, SearchVisibility

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


class RecordedSearch:
    """A search that returns the recorded hits of the run tickets (`fixtures/run/evidence.json`)
    without Qdrant, for tests that are about something else (the budget). Any other query finds
    nothing. `test_linear_run.py` checks that the real search returns the same hits."""

    def __init__(self) -> None:
        from hub.graph.nodes.intake import intake
        from hub.kb.loader import read_kb

        fixtures = ROOT / "apps" / "api" / "tests" / "fixtures" / "run"
        evidence: dict[str, list[str]] = json.loads((fixtures / "evidence.json").read_text())
        tickets = [
            json.loads(line)
            for line in (fixtures / "tickets.jsonl").read_text().splitlines()
            if line.strip()
        ]
        files = read_kb(ROOT / "data" / "kb")
        chunks = {chunk.id: (file, chunk) for file in files for chunk in file.chunks}
        self._hits: dict[str, list[KbChunk]] = {}
        for ticket in tickets:
            hits = []
            for rank, chunk_id in enumerate(evidence[ticket["id"]], start=1):
                file, chunk = chunks[chunk_id]
                hits.append(
                    KbChunk(
                        chunk_id=chunk.id,
                        doc_id=file.id,
                        title=file.title,
                        section=chunk.section,
                        url=file.url,
                        text=chunk.text,
                        visibility=str(file.visibility),
                        score=0.0,
                        rank=rank,
                    )
                )
            self._hits[intake(ticket["ticket"]).message_for_llm] = hits

    async def search(
        self,
        query: str,
        visibility: SearchVisibility,
        top_k: int = DEFAULT_TOP_K,
        mode: SearchMode = "hybrid",
    ) -> list[KbChunk]:
        return list(self._hits.get(query, []))[:top_k]
