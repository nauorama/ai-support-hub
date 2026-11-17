"""LLM judges (eval plan §5, P4-05): key facts, faithfulness, citation, attack.

Each judge is one structured call on the `judge` role (Claude Haiku 5.5, the same model family
as the writer, ADR-0018) with its own prompt `prompts/judge_<name>/v<N>.md`. The rules of eval
plan §5: binary questions, not scores; one call checks one thing; the judge must quote.

**Quotes are checked in code.** A verdict in favour of the draft ("present", "supported",
"supports") counts only when its quote is really in the text it claims to quote (the draft for
key facts, the evidence for faithfulness and citations), compared after folding case, quote
marks, dashes, Markdown emphasis, `[n]` markers and white space. A verdict whose quote is not
found is turned into the stricter one and marked `quote_found = false`, so a judge that invents
a quote cannot make a draft look better. The model's own verdict is kept next to it (`judge_*`).

No validation set exists (H5 removed, ADR-0018): reports mark every judge metric "unvalidated,
same model family". A wrong verdict found later becomes a unit test with a recorded response
(`evals/tests/test_judges.py`).

Judge calls are recorded in `llm_calls` with context `eval`, node `judge`, no `run_id` (data
model §1) and the prompt version. Any gateway error is raised to the caller.
"""

import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from hub.domain.enums import LlmCallContext, LlmNode
from hub.llm.config import ModelsConfig
from hub.llm.gateway import Gateway
from hub.llm.prompts import Prompt, PromptError, active_prompt
from hub.llm.schemas import CallContext, LlmError, Message

KEY_FACTS = "judge_key_facts"
FAITHFULNESS = "judge_faithfulness"
CITATION = "judge_citation"
ATTACK = "judge_attack"
JUDGE_PROMPTS = (KEY_FACTS, FAITHFULNESS, CITATION, ATTACK)
# Shown in the tool-calls part of an attack transcript without tool calls.
NO_TOOL_CALLS = "(none)"


class JudgeOutputError(LlmError):
    """A judge answer passed the schema but cannot be used (e.g. a key fact left out). Names the
    judge only, never model text."""


# --- what the model fills in (strict schemas, ai-pipeline §5) ------------------------------------


class _Verdict(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class FactCheck(_Verdict):
    fact: int = Field(ge=1, description="The number of the key fact, as listed.")
    quote: str = Field(
        description="The words of the draft that state the fact, copied exactly; empty when the "
        "draft does not state it."
    )
    present: bool = Field(description="True only when the draft states the fact.")


class KeyFactsVerdict(_Verdict):
    facts: list[FactCheck] = Field(description="One item for every key fact, in order.")


class ClaimCheck(_Verdict):
    claim: str = Field(description="One claim of the draft, in a few words.")
    evidence_quote: str = Field(
        description="The words of the evidence that support the claim, copied exactly; empty "
        "when no part of the evidence supports it."
    )
    supported: bool = Field(description="True only when the evidence states the claim.")


class FaithfulnessVerdict(_Verdict):
    claims: list[ClaimCheck] = Field(description="Every claim of the draft.")


class CitationVerdict(_Verdict):
    quote: str = Field(
        description="The words of the source that support the sentence, copied exactly; empty "
        "when no part of the source supports it."
    )
    supports: bool = Field(description="True only when the source states what the sentence says.")


class AttackVerdict(_Verdict):
    reason: str = Field(description="One or two sentences: what decides the result.")
    succeeded: bool = Field(description="True when the assistant reached the goal of the attack.")


# Schemas a recording request may name (`hub_evals.record`).
JUDGE_SCHEMAS: dict[str, type[BaseModel]] = {
    "KeyFactsVerdict": KeyFactsVerdict,
    "FaithfulnessVerdict": FaithfulnessVerdict,
    "CitationVerdict": CitationVerdict,
    "AttackVerdict": AttackVerdict,
}


# --- what the evaluation keeps (results.jsonl) ---------------------------------------------------


class _Result(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class FactResult(_Result):
    fact: int
    present: bool  # counted: the judge said present and its quote is in the draft
    judge_present: bool
    quote_found: bool


class KeyFactsResult(_Result):
    facts: list[FactResult]

    @property
    def coverage(self) -> float:
        """Share of the key facts present in the draft; 0 for an empty list."""
        return sum(f.present for f in self.facts) / len(self.facts) if self.facts else 0.0


class ClaimResult(_Result):
    claim: str
    supported: bool  # counted: the judge said supported and its quote is in the evidence
    judge_supported: bool
    quote_found: bool


class FaithfulnessResult(_Result):
    claims: list[ClaimResult]

    @property
    def unsupported(self) -> int:
        return sum(not claim.supported for claim in self.claims)

    @property
    def faithful(self) -> bool:
        return self.unsupported == 0


class CitationResult(_Result):
    marker: int
    source_id: str
    supports: bool  # counted: the judge said it supports and its quote is in the source
    judge_supports: bool
    quote_found: bool


class AttackResult(_Result):
    succeeded: bool
    reason: str


# --- quotes --------------------------------------------------------------------------------------

_MARKER = re.compile(r"\[\d+\]")
_SPACE = re.compile(r"\s+")
_FOLD = str.maketrans(
    {
        **dict.fromkeys("\u2018\u2019\u201a\u201b\u2032", "'"),
        **dict.fromkeys("\u201c\u201d\u201e\u201f\u2033", '"'),
        **dict.fromkeys("\u2010\u2011\u2012\u2013\u2014\u2015\u2212", "-"),
        "\u00a0": " ",
        # Markdown emphasis and code marks of the KB articles: a judge quotes the words, not
        # the `**` around them.
        "*": None,
        "`": None,
    }
)
# Cut from both ends of a quote: a judge often adds or drops the final full stop.
_EDGE = " .,;:!?\"'"


def _fold(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).translate(_FOLD).casefold()
    return _SPACE.sub(" ", _MARKER.sub(" ", text)).strip()


def quote_in(quote: str, text: str) -> bool:
    """True when `quote` is a non-empty part of `text`, ignoring case, quote marks, dashes,
    Markdown emphasis (`*`, backticks), `[n]` markers, white space and the punctuation at its
    two ends."""
    folded = _fold(quote).strip(_EDGE)
    return bool(folded) and folded in _fold(text)


# --- sentences of a draft ------------------------------------------------------------------------

# A sentence ends at `.`, `!` or `?` followed by white space or the end, and takes the `[n]`
# markers written after its full stop (the write prompt puts them "right after the sentence").
# A full stop inside a number (`1.5%`) is not an end.
_SENTENCE = re.compile(r"\S.*?(?:[.!?]+(?:\s*\[\d+\])*(?=\s|\Z)|\Z)", re.DOTALL)


def sentences(text: str) -> list[str]:
    """The sentences of a draft, each with its markers; paragraphs never join."""
    found: list[str] = []
    for paragraph in text.splitlines():
        found.extend(match.group(0).strip() for match in _SENTENCE.finditer(paragraph))
    return [sentence for sentence in found if sentence]


def marked_sentences(text: str) -> list[tuple[int, str]]:
    """(marker, sentence) for every `[n]` marker in the draft, in order of appearance."""
    pairs: list[tuple[int, str]] = []
    for sentence in sentences(text):
        for number in dict.fromkeys(int(m) for m in re.findall(r"\[(\d+)\]", sentence)):
            pairs.append((number, sentence))
    return pairs


# --- the judges ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class Judges:
    """The prompts of the judges, loaded once per run."""

    key_facts: Prompt
    faithfulness: Prompt
    citation: Prompt
    attack: Prompt

    @property
    def prompt_versions(self) -> dict[str, str]:
        return {
            prompt.node: prompt.front.version
            for prompt in (self.key_facts, self.faithfulness, self.citation, self.attack)
        }

    @property
    def prompts(self) -> tuple[Prompt, ...]:
        return (self.key_facts, self.faithfulness, self.citation, self.attack)


def load_judges(prompts_dir: Path, models: ModelsConfig) -> "Judges":
    return Judges(
        key_facts=_checked(active_prompt(prompts_dir, models, KEY_FACTS), KEY_FACTS),
        faithfulness=_checked(active_prompt(prompts_dir, models, FAITHFULNESS), FAITHFULNESS),
        citation=_checked(active_prompt(prompts_dir, models, CITATION), CITATION),
        attack=_checked(active_prompt(prompts_dir, models, ATTACK), ATTACK),
    )


def _checked(prompt: Prompt, node: str) -> Prompt:
    if prompt.node != node or prompt.front.role != "judge":
        raise PromptError(f"{prompt.version} is not a {node} prompt on the judge role")
    return prompt


def _call(prompt: Prompt) -> CallContext:
    return CallContext(
        context=LlmCallContext.EVAL, node=LlmNode.JUDGE, prompt_version=prompt.version
    )


def numbered(items: Sequence[str]) -> str:
    return "\n".join(f"{number}. {item}" for number, item in enumerate(items, start=1))


def key_facts_messages(prompt: Prompt, *, draft: str, key_facts: Sequence[str]) -> list[Message]:
    return _checked(prompt, KEY_FACTS).render({"draft": draft, "key_facts": numbered(key_facts)})


def faithfulness_messages(prompt: Prompt, *, draft: str, evidence: str) -> list[Message]:
    return _checked(prompt, FAITHFULNESS).render({"draft": draft, "evidence": evidence})


def citation_messages(prompt: Prompt, *, sentence: str, source: str) -> list[Message]:
    return _checked(prompt, CITATION).render({"sentence": sentence, "source": source})


def attack_messages(
    prompt: Prompt,
    *,
    goal: str,
    rubric: str,
    transcript: Sequence[tuple[str, str]],
    tool_calls: Sequence[str],
) -> list[Message]:
    return _checked(prompt, ATTACK).render(
        {
            "goal": goal,
            "rubric": rubric,
            "transcript": "\n\n".join(f"{role}: {text}" for role, text in transcript),
            "tool_calls": "\n".join(tool_calls) or NO_TOOL_CALLS,
        }
    )


async def judge_key_facts(
    gateway: Gateway, prompt: Prompt, *, draft: str, key_facts: Sequence[str]
) -> KeyFactsResult:
    """Which key facts the draft states. An empty draft states none (no model call)."""
    if not key_facts:
        return KeyFactsResult(facts=[])
    if not draft.strip():
        return KeyFactsResult(
            facts=[
                FactResult(fact=n, present=False, judge_present=False, quote_found=False)
                for n in range(1, len(key_facts) + 1)
            ]
        )
    answer = await gateway.complete_structured(
        prompt.front.role,
        key_facts_messages(prompt, draft=draft, key_facts=key_facts),
        KeyFactsVerdict,
        call=_call(prompt),
    )
    checks = answer.value.facts
    if sorted(check.fact for check in checks) != list(range(1, len(key_facts) + 1)):
        raise JudgeOutputError(f"{prompt.version}: not one verdict per key fact")
    results = []
    for check in sorted(checks, key=lambda c: c.fact):
        found = quote_in(check.quote, draft)
        results.append(
            FactResult(
                fact=check.fact,
                present=check.present and found,
                judge_present=check.present,
                quote_found=found,
            )
        )
    return KeyFactsResult(facts=results)


async def judge_faithfulness(
    gateway: Gateway, prompt: Prompt, *, draft: str, evidence: str
) -> FaithfulnessResult:
    """The claims of the draft, each supported by the evidence or not."""
    answer = await gateway.complete_structured(
        prompt.front.role,
        faithfulness_messages(prompt, draft=draft, evidence=evidence),
        FaithfulnessVerdict,
        call=_call(prompt),
    )
    results = []
    for check in answer.value.claims:
        found = quote_in(check.evidence_quote, evidence)
        results.append(
            ClaimResult(
                claim=check.claim,
                supported=check.supported and found,
                judge_supported=check.supported,
                quote_found=found,
            )
        )
    return FaithfulnessResult(claims=results)


async def judge_citation(
    gateway: Gateway,
    prompt: Prompt,
    *,
    marker: int,
    source_id: str,
    sentence: str,
    source: str,
) -> CitationResult:
    """Whether the cited source supports the sentence that carries the marker."""
    answer = await gateway.complete_structured(
        prompt.front.role,
        citation_messages(prompt, sentence=sentence, source=source),
        CitationVerdict,
        call=_call(prompt),
    )
    found = quote_in(answer.value.quote, source)
    return CitationResult(
        marker=marker,
        source_id=source_id,
        supports=answer.value.supports and found,
        judge_supports=answer.value.supports,
        quote_found=found,
    )


async def judge_attack(
    gateway: Gateway,
    prompt: Prompt,
    *,
    goal: str,
    rubric: str,
    transcript: Sequence[tuple[str, str]],
    tool_calls: Sequence[str],
) -> AttackResult:
    """Whether an attack reached its goal (eval plan §3, `success_check` of type `judge`).
    `transcript`: (role, text) pairs, oldest first; `tool_calls`: one line per call."""
    answer = await gateway.complete_structured(
        prompt.front.role,
        attack_messages(
            prompt, goal=goal, rubric=rubric, transcript=transcript, tool_calls=tool_calls
        ),
        AttackVerdict,
        call=_call(prompt),
    )
    return AttackResult(succeeded=answer.value.succeeded, reason=answer.value.reason)
