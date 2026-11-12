# AGENTS.md — instructions for coding agents

Enterprise AI Support Hub: AI customer support for Tallybridge, a **fictional** payments company. A LangGraph workflow answers routine tickets with citations and sends everything risky to a human operator.

## Read first

| When you work on | Read |
|---|---|
| anything | `docs/build-plan.md` §1 (how to work) and the task you were given |
| what and why | `docs/prd.md` |
| structure, modules, technology | `docs/design.md` |
| tables, files, formats | `docs/data-model.md` |
| endpoints, SSE, MCP tools | `docs/api.md` |
| workflow, schemas, prompts, routing | `docs/ai-pipeline.md` |
| metrics, datasets, runner | `docs/eval-plan.md` |
| guardrails, auth, audit, privacy | `docs/security.md` |
| commands, config, tracing, CI, deploy | `docs/operations.md` |
| web app | `docs/ui.md` |
| why a decision was made | `docs/adr/` |
| facts about the company | `docs/tallybridge-world.md` |

The documents are the specification. If code and documents disagree, fix the one that is wrong and say so in the pull request. If two documents disagree, stop and ask.

## Roles

The main session is the **builder**: it does one task from the build plan and opens a pull request. It does not review or merge its own work. Three subagents in `.claude/agents/` have narrow jobs: `reviewer` (read-only review of a pull request), `content-author` (files in `data/`), `eval-analyst` (read-only analysis of an evaluation run). How they work together: `docs/build-process.md`.

## Commands

```
make setup        # install everything
make dev          # start stack, migrate, seed, load knowledge base
make lint         # ruff, mypy, import-linter, eslint, tsc
make test         # unit tests (no network)
make test-int     # integration tests (testcontainers, fake models)
make test-e2e     # Playwright (fake models)
make data-check   # validate data/
make eval-smoke   # small real-model evaluation (costs money)
make eval SPLIT=dev
```

Run `make lint test` before you say a task is done. Run `make test-int` when you touched the database, Qdrant, the graph, tools or the API.

## Never do

1. **Never** store, log, trace or send to a model a full card number, CVV, PIN or 2FA code. Mask first (`hub/guardrails/masking.py`). Tests use only public test card numbers.
2. **Never** call a model provider or `litellm` outside `hub/llm/gateway.py`. **Never** call a tool outside `hub/tools/` with its permission check.
3. **Never** let a model choose the customer identity. `customer_ref` comes from the session or the state.
4. **Never** give a model a callable tool that has a side effect (Jira, Slack, anything that writes).
5. **Never** send `internal` knowledge-base content or a restriction reason to a customer.
6. **Never** weaken a policy rule, a guardrail, a red line or a test to make something pass. If a rule looks wrong, stop and ask.
7. **Never** read or tune on `data/eval/tickets.test.jsonl` except in the tasks that say "test set".
8. **Never** commit secrets, `.env`, real personal data, or large result files (`results.jsonl`).
9. **Never** edit an applied Alembic migration or an accepted ADR decision. Add a new one.
10. **Never** write library API calls from memory. Check the current documentation of the library first, then pin the version.
11. **Never** put a fact into a knowledge-base article that is not in `docs/tallybridge-world.md`. Add it to the world file first (ask the owner).
12. **Never** start the next phase without the owner's go-ahead.

## Always do

- One task = one small pull request with: what changed, how it was verified (command output), which "Done when" lines are met, what you were unsure about.
- Tests with the change. Deterministic safety code (masking, policy, routing, permissions, output checks, auth): ≥ 95% line coverage.
- Tests never need an API key: use the fake model mode and recorded fixtures.
- A change to prompts, models, retrieval or the graph: new prompt version file, `make eval-smoke`, numbers in the pull request.
- Every new model call records tokens, cost, latency, model and prompt version. Every decision and action writes an audit record.
- On any error or doubt at run time, the system hands the ticket to a human.
- Keep the module rules (import-linter). `graph` combines modules; other modules do not import each other's internals.
- At the end of a phase write `docs/walkthrough/phase-N.md` (format in the build plan). It is the owner's personal note: written locally, git-ignored, never committed, no pull request.

## Code style

- Python 3.13, full type hints, mypy strict, ruff (line length 100). Pydantic v2 models for all data that crosses a boundary (API, model output, tool result, file).
- Async for I/O. No global state except the app container built in `deps.py`.
- Small pure functions for logic; side effects at the edges. No clever abstractions without a second use.
- Names from the documents: `ticket`, `run`, `draft`, `route`, `handoff`, `evidence`, `chunk`. Do not invent synonyms.
- Comments explain why, not what. English only in code, comments, commits and documents.
- TypeScript strict, no `any`. API types are generated, not written by hand.
- Commits: `<type>(<area>): <summary>` with the task id, e.g. `feat(guardrails): card masking with Luhn check (P2-03)`.

## When to stop and ask the owner

A requirement cannot be met as written · two documents disagree · a red line cannot be reached · a human gate (H1–H9) is reached · a change would alter the public API, the data model, a policy rule or an ADR · a paid service or a new dependency with broad permissions is needed · anything touches real credentials or real personal data.
