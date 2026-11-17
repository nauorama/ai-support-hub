"""`make llm-requests`: write the recording requests of the run tickets (P3-05, retrieval P5-03).

A plain module, imported by `test_linear_run.py` and run as a script by the Make target; it
imports only from `hub`. Each ticket in `tests/fixtures/run/tickets.jsonl` becomes two request
files, `run_NN_triage.json` and `run_NN_write.json`, built with the same functions the run uses
(`intake`, `triage_messages`, `write_messages`, `rag_evidence`), so a ticket sent through the API
in fake mode (integration tests, `make dev`) finds its recordings. A unit test fails when a file
is missing or out of date, e.g. after a prompt or KB change; then run `make llm-requests` and
`make record-llm`.

**The search hits** of each ticket are kept in `tests/fixtures/run/evidence.json` (chunk ids in
rank order), and the write request is built from those chunks' texts in `data/kb/`. So the unit
test needs neither Qdrant nor the embedding model. `--search` (part of `make llm-requests`)
writes `evidence.json` again: it indexes `data/kb/` in Qdrant's in-memory mode with the real
local models and runs the run's search. The integration test `test_linear_run.py` checks that
the real Qdrant server returns the same ids, so a recording cannot drift from the live search.

Only the role's own model is recorded (no fallback recording): fake mode never falls back.
"""

import asyncio
import json
import re
import sys
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict

from hub.domain.enums import LlmCallContext, LlmNode
from hub.graph.linear import SEARCH_MODES, SEARCH_TOP_K, VARIANT, rag_evidence
from hub.graph.nodes.intake import intake
from hub.graph.nodes.triage import triage_messages
from hub.graph.nodes.write import write_messages
from hub.guardrails.masking import mask
from hub.kb.loader import KbFile, read_kb
from hub.kb.retriever import KbChunk
from hub.llm.config import load_models
from hub.llm.prompts import active_prompt

ROOT = Path(__file__).resolve().parents[4]
FIXTURES = ROOT / "apps" / "api" / "tests" / "fixtures"
TICKETS = FIXTURES / "run" / "tickets.jsonl"
EVIDENCE = FIXTURES / "run" / "evidence.json"
REQUESTS = FIXTURES / "llm" / "requests"
KB_DIR = ROOT / "data" / "kb"
GENERATED = re.compile(r"run_[0-9]{2}_(triage|write)\.json")


class RunTicket(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    case: str
    ticket: str


def load_tickets() -> list[RunTicket]:
    lines = TICKETS.read_text(encoding="utf-8").splitlines()
    return [RunTicket.model_validate_json(line) for line in lines if line.strip()]


def load_evidence() -> dict[str, list[str]]:
    """Run ticket id -> chunk ids of its search, in rank order."""
    data: dict[str, list[str]] = json.loads(EVIDENCE.read_text(encoding="utf-8"))
    return data


def query(ticket: RunTicket) -> str:
    """The search query and the writer's ticket text: the masked message after intake."""
    return intake(ticket.ticket).message_for_llm


def hits_from_files(chunk_ids: list[str], files: list[KbFile]) -> list[KbChunk]:
    """The chunks of `data/kb/` with these ids, as search hits. Only the id, the text and the
    visibility reach the request; the other fields are filled for the type."""
    by_id = {chunk.id: (file, chunk) for file in files for chunk in file.chunks}
    hits = []
    for rank, chunk_id in enumerate(chunk_ids, start=1):
        file, chunk = by_id[chunk_id]
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
    return hits


def request_specs() -> dict[str, dict[str, Any]]:
    """File name -> request file content, for every run ticket."""
    models = load_models(ROOT / "config" / "models.yaml")
    triage_prompt = active_prompt(ROOT / "prompts", models, "triage")
    write_prompt = active_prompt(ROOT / "prompts", models, "write")
    files = read_kb(KB_DIR)
    evidence = load_evidence()
    specs: dict[str, dict[str, Any]] = {}
    for ticket in load_tickets():
        if not re.fullmatch(r"run_[0-9]{2}", ticket.id):
            raise ValueError(f"{ticket.id}: ids are run_NN")
        if mask(ticket.ticket).text != ticket.ticket:
            raise ValueError(f"{ticket.id}: the ticket is not masked")
        result = intake(ticket.ticket)
        kb = rag_evidence(VARIANT, hits_from_files(evidence[ticket.id], files)).kb
        specs[f"{ticket.id}_triage.json"] = {
            "role": triage_prompt.front.role,
            "schema": "TriageResult",
            "prompt_version": triage_prompt.version,
            "models": "primary",
            "messages": triage_messages(triage_prompt, result.message_for_llm),
        }
        specs[f"{ticket.id}_write.json"] = {
            "role": write_prompt.front.role,
            "schema": "DraftAnswer",
            "prompt_version": write_prompt.version,
            "models": "primary",
            "messages": write_messages(
                write_prompt,
                kb=kb,
                ticket=result.message_for_llm,
                history=result.history_for_llm,
            ),
        }
    return specs


def render(spec: dict[str, Any]) -> str:
    return json.dumps(spec, indent=2, ensure_ascii=False) + "\n"


# --- `--search`: the hits of the real models, in Qdrant's in-memory mode ----------------------


class _NoRecorder:
    """Embedding rows are not stored: nothing here is a run."""

    async def record(self, call: object) -> None:
        return None

    async def record_blocked(self, call: object, entities: dict[str, int]) -> None:
        return None


async def search_evidence() -> dict[str, list[str]]:
    from qdrant_client import AsyncQdrantClient

    from hub.config import load_settings
    from hub.kb.index import IndexChunk, KbIndex
    from hub.kb.retriever import Retriever
    from hub.kb.sections import StoredChunk, chunk_anchors, section_url
    from hub.kb.sparse import Bm25Encoder
    from hub.llm.gateway import build_gateway, embedder
    from hub.llm.schemas import CallContext

    settings = load_settings()
    gateway = build_gateway(settings, _NoRecorder(), None)
    dense = embedder(gateway, CallContext(context=LlmCallContext.RECORD, node=LlmNode.EMBEDDING))
    sparse = Bm25Encoder(settings.embedding_cache_dir)
    chunks: list[IndexChunk] = []
    for file in sorted(read_kb(KB_DIR), key=lambda f: f.id):
        ordered = sorted(file.chunks, key=lambda c: c.ordinal)
        anchors = chunk_anchors(
            file.id, file.title, [StoredChunk(c.id, c.section, c.text) for c in ordered]
        )
        chunks.extend(
            IndexChunk(
                chunk_id=c.id,
                doc_id=file.id,
                title=file.title,
                section=c.section,
                text=c.text,
                visibility=str(file.visibility),
                product=str(file.product),
                intents=file.intents,
                url=section_url(file.id, anchors[c.id]),
                doc_version=1,
                content_hash=c.content_hash,
            )
            for c in ordered
        )
    client = AsyncQdrantClient(location=":memory:")
    try:
        await KbIndex(client, dense, sparse).rebuild(chunks)
        retriever = Retriever(client, dense, sparse)
        return {
            ticket.id: [
                hit.chunk_id
                for hit in await retriever.search(
                    query(ticket), "external", top_k=SEARCH_TOP_K, mode=SEARCH_MODES[VARIANT]
                )
            ]
            for ticket in load_tickets()
        }
    finally:
        await client.close()


def main(argv: list[str]) -> None:
    if "--search" in argv:
        found = asyncio.run(search_evidence())
        EVIDENCE.write_text(json.dumps(found, indent=2) + "\n", encoding="utf-8")
        print(f"llm-requests: search hits of {len(found)} run tickets written to {EVIDENCE}")
    specs = request_specs()
    for stale in REQUESTS.glob("run_*.json"):
        if GENERATED.fullmatch(stale.name) and stale.name not in specs:
            stale.unlink()
    for name, spec in specs.items():
        (REQUESTS / name).write_text(render(spec), encoding="utf-8")
    print(f"llm-requests: {len(specs)} run requests written to {REQUESTS}")


if __name__ == "__main__":
    main(sys.argv[1:])
