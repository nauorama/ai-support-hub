"""Verify node (docs/ai-pipeline.md §3, §5; P5-04): code checks first, then the LLM claim check.

1. The code checks of `hub.guardrails.output_checks` run on the draft as the model wrote it. When
   any fails, the LLM check is not called: `Verification` has the violation codes, no claims,
   no groundedness, and `passed = false`.
2. Otherwise one structured call on the `verify` role (`prompts/verify/v1.md`) splits the
   answer into claims and names the source of each. The model's answer is checked in code: a
   source id that is not one the writer was given is dropped (stored as null), and a claim
   marked supported on such an id counts as unsupported, so the model cannot ground a claim on
   a source it invented (nor on none), and only known chunk ids are stored.
3. `groundedness` = supported claims / all claims, and `passed` = every claim supported. A draft
   with an answer but no claim at all gets groundedness 0 and fails: an answer the verifier
   cannot split into claims gives no basis for trust (the rule version of confidence uses
   groundedness, ai-pipeline §6).

Any gateway error is raised to the caller, which hands the ticket to a human.
"""

import uuid
from collections.abc import Collection

from hub.domain.enums import LlmCallContext, LlmNode
from hub.domain.verification import ClaimCheck, ClaimsAnswer, Verification
from hub.llm.gateway import Gateway
from hub.llm.prompts import Prompt, PromptError, cache_system
from hub.llm.schemas import CallContext, Message, Structured

NODE = LlmNode.VERIFY


def verify_messages(
    prompt: Prompt, *, draft: str, kb: str, ticket: str, history: str
) -> list[Message]:
    """The messages of a verify call. `make llm-requests` uses the same function, so the
    recorded requests match what `verify` sends."""
    if prompt.node != NODE.value:
        raise PromptError(f"{prompt.version} is not a verify prompt")
    return cache_system(
        prompt.render({"draft": draft, "kb": kb, "ticket": ticket, "history": history})
    )


async def check_claims(
    gateway: Gateway,
    prompt: Prompt,
    *,
    draft: str,
    kb: str,
    ticket: str,
    history: str,
    run_id: uuid.UUID | None = None,
    context: LlmCallContext = LlmCallContext.ONLINE,
) -> Structured[ClaimsAnswer]:
    call = CallContext(context=context, node=NODE, run_id=run_id, prompt_version=prompt.version)
    messages = verify_messages(prompt, draft=draft, kb=kb, ticket=ticket, history=history)
    return await gateway.complete_structured(prompt.front.role, messages, ClaimsAnswer, call=call)


def failed_checks(violations: list[str]) -> Verification:
    """The result when a code check failed: the LLM check is not called."""
    return Verification(claims=[], groundedness=None, policy_violations=violations, passed=False)


def verification(answer: ClaimsAnswer, known_sources: Collection[str]) -> Verification:
    """The result of the LLM check, after every code check passed."""
    claims = []
    for claim in answer.claims:
        source = claim.source_id if claim.source_id in known_sources else None
        claims.append(
            ClaimCheck(
                claim=claim.claim,
                supported=claim.supported and source is not None,
                source_id=source,
            )
        )
    supported = sum(claim.supported for claim in claims)
    return Verification(
        claims=claims,
        groundedness=round(supported / len(claims), 3) if claims else 0.0,
        policy_violations=[],
        passed=bool(claims) and supported == len(claims),
    )
