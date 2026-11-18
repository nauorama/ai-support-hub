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

In the graph (`run`, P6-01) a failed LLM check counts as an unsupported draft (ai-pipeline §3):
no claims, groundedness 0, `passed = false`, and the error class is kept in `verify_error` for
the `verify.done` record. A budget stop (G8) is not a failed check: it ends the run like any
budget stop, with a handoff. B0 has no verify step.
"""

import logging
import uuid
from collections.abc import Collection

from hub.domain.enums import LlmCallContext, LlmNode
from hub.domain.verification import ClaimCheck, ClaimsAnswer, Verification
from hub.graph.context import GraphContext, ids
from hub.graph.draft import draft_to_check, evidence_for_writer, internal_text
from hub.graph.nodes.intake import intake_result
from hub.graph.state import TicketState
from hub.guardrails.output_checks import check_draft
from hub.llm.gateway import Gateway
from hub.llm.prompts import Prompt, PromptError, cache_system
from hub.llm.schemas import BudgetExceededError, CallContext, Message, Structured

NODE = LlmNode.VERIFY

logger = logging.getLogger(__name__)


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


def unavailable() -> Verification:
    """The result when the LLM check failed: the draft counts as unsupported."""
    return Verification(claims=[], groundedness=0.0, policy_violations=[], passed=False)


async def run(state: TicketState, ctx: GraphContext) -> TicketState:
    pipeline = ctx.pipeline
    answer = state.get("draft")
    if not pipeline.verifies or answer is None or pipeline.verify_prompt is None:
        return TicketState()
    _, run_id = ids(state)
    research = state.get("evidence")
    evidence = await evidence_for_writer(pipeline, research.chunks if research else ())
    intake = intake_result(state)
    violations = check_draft(
        draft_to_check(answer, evidence, intake),
        promises=pipeline.policy.promises,
        internal=await internal_text(pipeline),
    )
    if violations:
        return TicketState(verification=failed_checks(violations))
    try:
        checked = await check_claims(
            pipeline.gateway,
            pipeline.verify_prompt,
            draft=answer.answer,
            kb=evidence.kb,
            ticket=intake.message_for_llm,
            history=intake.history_for_llm,
            run_id=run_id,
            context=pipeline.context,
        )
    except BudgetExceededError:
        raise
    except Exception as exc:
        error = type(exc).__name__
        logger.warning("verify check failed", extra={"run_id": str(run_id), "error": error})
        return TicketState(verification=unavailable(), verify_error=error)
    return TicketState(
        verification=verification(checked.value, evidence.citable.keys()),
        models={**state.get("models", {}), "verify": checked.model},
    )
