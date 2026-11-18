"""Confidence, rule version (docs/ai-pipeline.md §6, ADR-0009 layer 2 replaced by ADR-0018).

`confidence = groundedness` when the run's variant is one of `variants` and the top retrieval
score is above `top_score_floor` in `config/confidence.json`, else 0. The floor was measured on
the hybrid search's fused scores (P6-06), so it holds only for the variants listed (`rag_hybrid`);
any other variant (dense cosine scores, B0 without a search) gets 0 and is never auto-replied
(owner decision, PR #38 review); a null groundedness (a code check failed, the LLM check did not
run) gives 0, so such a draft is never auto-replied. No model is trained in budget mode: the
number is uncalibrated, and auto-reply is off by default.

The features are the inputs a trained model would use (ai-pipeline §6), stored with the run and
in `route.decided` for later analysis. Length features need a tokenizer and are left out.
"""

import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from hub.domain.draft import DraftAnswer
from hub.domain.triage import TriageResult
from hub.domain.verification import Verification
from hub.graph.state import Evidence


class ConfidenceError(Exception):
    """`config/confidence.json` cannot be read or is invalid."""


class ConfidenceRule(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    version: str
    kind: Literal["rule"]
    top_score_floor: float = Field(ge=0)
    # The variants whose scores the floor was measured on; any other variant gets confidence 0.
    variants: frozenset[str] = Field(min_length=1)
    note: str = ""


def load_confidence(path: Path) -> ConfidenceRule:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ConfidenceError(f"cannot read {path}: {type(exc).__name__}") from exc
    try:
        return ConfidenceRule.model_validate(raw)
    except ValidationError as exc:
        raise ConfidenceError(f"{path} is invalid: {exc}") from exc


def features(
    *,
    triage: TriageResult | None,
    evidence: Evidence | None,
    draft: DraftAnswer | None,
    verification: Verification | None,
    retry_count: int,
) -> dict[str, float]:
    found: dict[str, float] = {
        "top_score": evidence.top_score if evidence else 0.0,
        "score_margin": evidence.score_margin if evidence else 0.0,
        "n_citations": float(len(draft.citations)) if draft else 0.0,
        "n_account_facts": 0.0,  # account facts come with the research tools (P7-04)
        "retry_used": 1.0 if retry_count > 0 else 0.0,
        "has_secondary_intent": 1.0 if triage and triage.secondary_intent else 0.0,
        "tool_calls": float(evidence.tool_calls) if evidence else 0.0,
    }
    if verification is not None and verification.groundedness is not None:
        found["groundedness"] = verification.groundedness
    return found


def confidence(rule: ConfidenceRule, found: dict[str, float], variant: str) -> float:
    groundedness = found.get("groundedness")
    if variant not in rule.variants:
        return 0.0
    if groundedness is None or found["top_score"] <= rule.top_score_floor:
        return 0.0
    return round(groundedness, 3)
