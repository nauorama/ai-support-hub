## Task
<!-- Task id and link, e.g. P2-03 -->

## What changed
<!-- Short list. Why, when it is not obvious. -->

## Done when — evidence
<!-- Copy the "Done when" lines of the task and mark each with the proof (test name, command output, number). -->
- [ ]

## Verification output
```
<!-- paste: make lint test (and test-int when relevant; eval-smoke only when the owner asked for it) -->
```

## Evaluation (only for changes to prompts, models, retrieval, graph, guardrails)
<!-- Only when the owner asked for a paid run (ADR-0018): metrics table before / after from make eval-smoke or make eval-report. -->

## Security checklist (P8-06; required for guardrails/, tools/permissions.py, auth/, config/policy.yaml, logging, api/, privacy, audit; tick or write "n/a" with a reason)
Data
- [ ] No card number, CVV, PIN or 2FA code is stored, logged, traced or sent to a model; text is masked (`hub/guardrails/masking.py`) before any of that. Tests use public test card numbers only
- [ ] No secret, `.env`, real personal data or large result file (`results.jsonl`) in code, tests, fixtures or docs
- [ ] No request body logged before masking; no ticket text in an exception message, log line, `runs.error` or `llm_calls.error` (error class only)
- [ ] `internal` knowledge-base content and restriction reasons never reach a customer response
Models and tools
- [ ] Every new model call goes through `hub/llm/gateway.py` and records tokens, cost, latency, model and prompt version
- [ ] Every new tool call goes through `hub/tools/permissions.py`; no side-effect tool is callable by a model
- [ ] `customer_ref`, `ticket_id` and idempotency keys come from the session or the state, never from model output
API, auth, audit
- [ ] Every new endpoint declares its role (or the job token) and has an authorisation test (401 / 403 / 404 for foreign data)
- [ ] State-changing console and admin routes check the CSRF token; input lengths are limited
- [ ] Every decision and action writes an audit record; nothing changes or deletes `audit_log` rows; `GET /console/audit/verify` still passes
- [ ] On any runtime error or doubt the ticket goes to a human
Process
- [ ] No policy rule, guardrail, red line or test was weakened to make something pass
- [ ] New dependencies are pinned, checked against their current documentation, and pass `pip-audit` / `pnpm audit`
- [ ] Deterministic safety code keeps >= 95% line coverage
- [ ] Attack suite still passes (`make eval-attacks`, only when the owner asks: it costs money) and `make leak-scan` is clean
- [ ] Threat model table (`docs/security.md` §2) updated, with the test that covers each changed threat

## Documents updated
<!-- Which docs changed, or "none needed". New ADR? -->

## Unsure about
<!-- Anything the reviewer should look at closely. -->
