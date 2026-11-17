"""`make llm-requests`: write one recording request per judge test case (P4-05).

A plain module, imported by `test_judges.py` and run as a script by the Make target. Each case in
`evals/tests/fixtures/judges/cases.jsonl` (a pass and a fail per judge) becomes
`evals/tests/fixtures/llm/requests/judge_<case id>.json`, built with the same message functions
the judges use and the active judge prompts, so the recordings match what a judge sends. A unit
test fails when a file is missing or out of date, e.g. after a prompt change; then run
`make llm-requests` and `make record-llm` (real calls, by hand, only when the owner agrees).

Only the role's own model is recorded: fake mode never falls back. The evidence of a case names
knowledge-base documents; their text comes from `data/kb/` as the runner renders it. A
faithfulness or citation case has the customer's `ticket` (and optionally `history`, a list of
`[role, text]`), prepared by `ticket_text` as the runner prepares it.
"""

import json
import re
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from hub.kb.full_text import KbSource, render_kb, sources_from_files
from hub.kb.loader import read_kb
from hub.llm.config import load_models
from hub.llm.prompts import Prompt
from hub.llm.schemas import Message
from hub_evals.judges import (
    Judges,
    TicketText,
    attack_messages,
    citation_messages,
    faithfulness_messages,
    key_facts_messages,
    load_judges,
    ticket_text,
)

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "evals" / "tests" / "fixtures"
CASES = FIXTURES / "judges" / "cases.jsonl"
LLM_FIXTURES = FIXTURES / "llm"
REQUESTS = LLM_FIXTURES / "requests"
GENERATED = re.compile(r"judge_[a-z_]+\.json")
SCHEMA = {
    "key_facts": "KeyFactsVerdict",
    "faithfulness": "FaithfulnessVerdict",
    "citation": "CitationVerdict",
    "attack": "AttackVerdict",
}


class Case(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    judge: Literal["key_facts", "faithfulness", "citation", "attack"]
    case: str
    input: dict[str, Any]
    expected: dict[str, Any]


def load_cases() -> list[Case]:
    lines = CASES.read_text(encoding="utf-8").splitlines()
    return [Case.model_validate_json(line) for line in lines if line.strip()]


def judges() -> Judges:
    return load_judges(ROOT / "prompts", load_models(ROOT / "config" / "models.yaml"))


def kb_sources() -> dict[str, KbSource]:
    return {source.id: source for source in sources_from_files(read_kb(ROOT / "data" / "kb"))}


def evidence(doc_ids: list[str]) -> str:
    sources = kb_sources()
    return render_kb([sources[doc_id] for doc_id in doc_ids])


def case_ticket(case: Case) -> TicketText:
    history = [(role, text) for role, text in case.input.get("history", [])]
    return ticket_text(case.input["ticket"], history)


def prompt_of(judge_set: Judges, case: Case) -> Prompt:
    return {
        "key_facts": judge_set.key_facts,
        "faithfulness": judge_set.faithfulness,
        "citation": judge_set.citation,
        "attack": judge_set.attack,
    }[case.judge]


def case_messages(judge_set: Judges, case: Case) -> list[Message]:
    """The messages the judge sends for this case."""
    data = case.input
    prompt = prompt_of(judge_set, case)
    match case.judge:
        case "key_facts":
            return key_facts_messages(prompt, draft=data["draft"], key_facts=data["key_facts"])
        case "faithfulness":
            return faithfulness_messages(
                prompt,
                draft=data["draft"],
                evidence=evidence(data["evidence_doc_ids"]),
                ticket=case_ticket(case),
            )
        case "citation":
            return citation_messages(
                prompt,
                sentence=data["sentence"],
                source=evidence([data["source_doc_id"]]),
                ticket=case_ticket(case),
            )
        case "attack":
            return attack_messages(
                prompt,
                goal=data["goal"],
                rubric=data["rubric"],
                transcript=[(role, text) for role, text in data["transcript"]],
                tool_calls=data["tool_calls"],
            )


def request_specs() -> dict[str, dict[str, Any]]:
    """File name -> request file content, for every case."""
    judge_set = judges()
    specs: dict[str, dict[str, Any]] = {}
    for case in load_cases():
        name = f"judge_{case.id}.json"
        if not GENERATED.fullmatch(name):
            raise ValueError(f"{case.id}: ids are lowercase words joined by `_`")
        prompt = prompt_of(judge_set, case)
        specs[name] = {
            "role": prompt.front.role,
            "schema": SCHEMA[case.judge],
            "prompt_version": prompt.version,
            "models": "primary",
            "messages": case_messages(judge_set, case),
        }
    return specs


def render(spec: dict[str, Any]) -> str:
    return json.dumps(spec, indent=2, ensure_ascii=False) + "\n"


def main() -> None:
    REQUESTS.mkdir(parents=True, exist_ok=True)
    specs = request_specs()
    for stale in REQUESTS.glob("judge_*.json"):
        if stale.name not in specs:
            stale.unlink()
    for name, spec in specs.items():
        (REQUESTS / name).write_text(render(spec), encoding="utf-8")
    print(f"llm-requests: {len(specs)} judge request files in {REQUESTS.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
