"""`make llm-requests`: write one recording request per triage example (P3-03).

A plain module, imported by `test_triage.py` and run as a script by the Make target; it
imports only from `hub`. Each example in `tests/fixtures/triage/examples.jsonl` becomes
`tests/fixtures/llm/requests/triage_NN.json`, built with the same functions `triage()` uses
(active prompt, `triage_messages`), so the recordings match what the node sends. A unit test
fails when a file is missing or out of date, e.g. after a prompt change; then run
`make llm-requests` and `make record-llm`.

Only the first example is recorded on the fallback model too; the others only on the role's own
model, to keep the cost of a recording small (P3-03 plan).
"""

import json
import re
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from hub.domain.triage import Intent, RequestedAction, Urgency
from hub.graph.nodes.triage import triage_messages
from hub.guardrails.masking import mask
from hub.llm.config import load_models
from hub.llm.prompts import active_prompt

ROOT = Path(__file__).resolve().parents[4]
FIXTURES = ROOT / "apps" / "api" / "tests" / "fixtures"
EXAMPLES = FIXTURES / "triage" / "examples.jsonl"
REQUESTS = FIXTURES / "llm" / "requests"
PREFIX = "triage_"
# Only the generated files: `triage_smoke.json` and other hand-written requests are kept.
GENERATED = re.compile(r"triage_[0-9]{2}\.json")
WITH_FALLBACK = frozenset({"triage_01"})


class Expected(BaseModel):
    model_config = ConfigDict(extra="forbid")

    intent: Intent
    secondary_intent: Intent | None
    wants_human: bool
    reports_unauthorised_activity: bool
    is_complaint_or_legal: bool
    injection_suspected: bool
    asks_for_action: RequestedAction
    urgency: Urgency | None = None  # checked only where the example states it


class Example(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    case: str
    ticket: str
    expected: Expected


def load_examples() -> list[Example]:
    lines = EXAMPLES.read_text(encoding="utf-8").splitlines()
    return [Example.model_validate_json(line) for line in lines if line.strip()]


def request_specs() -> dict[str, dict[str, Any]]:
    """File name -> request file content, for every example."""
    prompt = active_prompt(ROOT / "prompts", load_models(ROOT / "config" / "models.yaml"), "triage")
    specs: dict[str, dict[str, Any]] = {}
    for example in load_examples():
        if not GENERATED.fullmatch(f"{example.id}.json"):
            raise ValueError(f"{example.id}: ids are {PREFIX}NN")
        if mask(example.ticket).text != example.ticket:
            raise ValueError(f"{example.id}: the ticket is not masked")
        models: Literal["all", "primary"] = "all" if example.id in WITH_FALLBACK else "primary"
        specs[f"{example.id}.json"] = {
            "role": prompt.front.role,
            "schema": "TriageResult",
            "prompt_version": prompt.version,
            "models": models,
            "messages": triage_messages(prompt, example.ticket),
        }
    return specs


def render(spec: dict[str, Any]) -> str:
    return json.dumps(spec, indent=2, ensure_ascii=False) + "\n"


def main() -> None:
    specs = request_specs()
    for stale in REQUESTS.glob(f"{PREFIX}*.json"):
        if GENERATED.fullmatch(stale.name) and stale.name not in specs:
            stale.unlink()
    for name, spec in specs.items():
        (REQUESTS / name).write_text(render(spec), encoding="utf-8")
    print(f"llm-requests: {len(specs)} triage requests written to {REQUESTS}")


if __name__ == "__main__":
    main()
