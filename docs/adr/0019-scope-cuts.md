# ADR-0019: Scope cuts — no phase 9, one review round, Jira and Slack as mocks

- Status: accepted
- Date: 2026-11-16
- Supersedes: parts of [ADR-0014](0014-observability.md) (Langfuse tracing) and [ADR-0018](0018-budget-mode.md) (experiment E9 and human gate H7)

## Context
After gate 3 the owner reviewed the remaining plan against the time she has. Phase 9 (LLMOps) adds tracing, a confidence threshold, a cost check, alerts and a runbook. None of it changes what the demo shows except the metrics API and the milestone run, which later phases need. Two review rounds per pull request double the owner's waiting time for findings that are usually small. Real Jira and Slack connections need accounts and tokens (H8) for a step the demo never shows to a stranger.

## Decision
1. **Phase 9 is removed.** Two tasks move because later phases need them:
   - P9-02 (metrics and settings API) becomes **P10-09**. The dashboard and settings pages (P10-04) and the kill switch in the demo script (P12-03) use it.
   - P9-06 (milestone run) becomes **P12-00**. The README numbers (P12-01) use it.
   - **Not built:**
     - P9-01: tracing with Langfuse. The `llm_calls` table from ADR-0014 stays and is the only record of model calls.
     - P9-03: the confidence threshold, experiment E9 and human gate H7. Auto-reply stays **off** in the demo.
     - P9-04: the cost check. Cost per ticket is still a metric of every evaluation report.
     - P9-05: alerts, the runbook and `make export-review-candidates`.
2. **One review round per pull request** (was at most two). The builder fixes the reviewer's findings in the same pull request and lists each fix. There is no second review. If the findings are large or unclear, the owner looks herself.
3. **Jira and Slack are mocks only.** The adapters store calls in a local table and log. There is no real mode. H8 no longer covers Jira, Slack or Langfuse; it keeps the cloud accounts for the deploy (P10-07).

Also decided, without an ADR because it is process only: small neighbouring tasks may share one pull request when the owner agrees, and the builder names them in its plan. The `reviewer` and `content-author` subagents run on Sonnet.

## Alternatives considered
- **Keep a reduced phase 9 (tracing only).** Langfuse traces help debugging, but nothing in the remaining build depends on them, and they need another account.
- **Keep E9 and H7.** Twenty tickets give at most a handful of auto-replies, so a threshold chosen from them would carry an interval too wide to justify turning auto-reply on.
- **Keep the real Jira and Slack mode as optional.** It still needs code, tests and a hand test with real tokens, for no visible gain in the demo.

## Consequences
- The demo always routes to approval or handoff, never to auto-reply. The automation share in the reports is 0 by design. The README and the case study say so.
- Debugging a run uses `runs`, `llm_calls`, `tool_calls` and the audit log, not a trace view. The demo script shows no trace.
- Wrong review findings that the fixes miss reach the owner at merge time, not in a second review.
- Several documents still describe Langfuse tracing as built: design, operations, security, data model `runs.trace_id`, README and `.env.example`. ADR-0014's tracing part no longer applies. The `JIRA_MODE` / `SLACK_MODE` settings still accept `real` until P7-02 removes that value.
