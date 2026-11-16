"""`make llm-requests`: write the recording requests of the phase-3 run tickets (P3-05).

A plain module, imported by `test_linear_run.py` and run as a script by the Make target; it
imports only from `hub`. Each ticket in `tests/fixtures/run/tickets.jsonl` becomes two request
files, `run_NN_triage.json` and `run_NN_write.json`, built with the same functions the run uses
(`intake`, `triage_messages`, `write_messages`, the KB text from `data/kb/`), so a ticket sent
through the API in fake mode (integration tests, `make dev`) finds its recordings. A unit test
fails when a file is missing or out of date, e.g. after a prompt or KB change; then run
`make llm-requests` and `make record-llm`.

Only the role's own model is recorded (no fallback recording): one write request is about 18k
input tokens, and fake mode never falls back.
"""

import json
import re
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict

from hub.graph.nodes.intake import intake
from hub.graph.nodes.triage import triage_messages
from hub.graph.nodes.write import write_messages
from hub.guardrails.masking import mask
from hub.kb.full_text import render_kb, sources_from_files
from hub.kb.loader import read_kb
from hub.llm.config import load_models
from hub.llm.prompts import active_prompt

ROOT = Path(__file__).resolve().parents[4]
FIXTURES = ROOT / "apps" / "api" / "tests" / "fixtures"
TICKETS = FIXTURES / "run" / "tickets.jsonl"
REQUESTS = FIXTURES / "llm" / "requests"
GENERATED = re.compile(r"run_[0-9]{2}_(triage|write)\.json")


class RunTicket(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    case: str
    ticket: str


def load_tickets() -> list[RunTicket]:
    lines = TICKETS.read_text(encoding="utf-8").splitlines()
    return [RunTicket.model_validate_json(line) for line in lines if line.strip()]


def kb_text() -> str:
    return render_kb(sources_from_files(read_kb(ROOT / "data" / "kb")))


def request_specs() -> dict[str, dict[str, Any]]:
    """File name -> request file content, for every run ticket."""
    models = load_models(ROOT / "config" / "models.yaml")
    triage_prompt = active_prompt(ROOT / "prompts", models, "triage")
    write_prompt = active_prompt(ROOT / "prompts", models, "write")
    kb = kb_text()
    specs: dict[str, dict[str, Any]] = {}
    for ticket in load_tickets():
        if not re.fullmatch(r"run_[0-9]{2}", ticket.id):
            raise ValueError(f"{ticket.id}: ids are run_NN")
        if mask(ticket.ticket).text != ticket.ticket:
            raise ValueError(f"{ticket.id}: the ticket is not masked")
        result = intake(ticket.ticket)
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


def main() -> None:
    specs = request_specs()
    for stale in REQUESTS.glob("run_*.json"):
        if GENERATED.fullmatch(stale.name) and stale.name not in specs:
            stale.unlink()
    for name, spec in specs.items():
        (REQUESTS / name).write_text(render(spec), encoding="utf-8")
    print(f"llm-requests: {len(specs)} run requests written to {REQUESTS}")


if __name__ == "__main__":
    main()
