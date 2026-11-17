# Operations: local setup, configuration, observability, CI/CD, deployment

## 1. Local development

Requirements: Docker, Python 3.13 with `uv`, Node.js LTS with `pnpm`, `make`.

`infra/docker-compose.yml` services: `postgres`, `qdrant`, `api` (hot reload), `mcp` (support-tools server), `web` (Vite dev server). Optional profile `tracing`: self-hosted Langfuse — only if the cloud free tier is not used.

Build state: the services arrive with their phases. Phase 2 has `postgres`, `qdrant` and `api`; `mcp` comes in phase 7 and `web` in phase 10. **Until the MCP server exists (P7), `GET /ready` answers 503** with `mcp` reported as not reachable, while `postgres` and `qdrant` report `ok`; the compose healthcheck of `api` uses `/health` for this reason. Likewise `make dev` gains `migrate`, `seed` and `kb-load` as P2-02, P2-05 and P2-09 build them. **`make dev` needs `SEED_OPERATOR_PASSWORD`, `SESSION_SECRET` and `DEMO_ACCESS_CODE` in `.env`:** `.env.example` leaves them empty on purpose (no shared demo secrets in the repository). The first step of `make dev` (`hub/dev_check.py`) lists every one that is empty in a single message and stops before anything is started or written; set them in `.env` and run `make dev` again. `make seed` on its own still checks `SEED_OPERATOR_PASSWORD` and writes nothing when it is empty. Ports bind to `127.0.0.1` only. Integration tests do not use this stack: testcontainers starts Postgres and Qdrant with the image tags of the compose file.

| Command | Does |
|---|---|
| `make setup` | `uv sync`, `pnpm install`, copy `.env.example` → `.env` if missing, install pre-commit hooks |
| `make up` / `make down` | start / stop the compose stack |
| `make migrate` | Alembic upgrade + LangGraph checkpointer setup |
| `make seed` | load demo customers, operators, settings (idempotent: customers are upserted by `external_ref`, except anonymised ones; operators and settings are inserted only when missing) |
| `make kb-load` | load `data/kb` into Postgres and Qdrant (only changed documents); the Qdrant part arrives in phase 5 |
| `make dev` | `up` + `migrate` + `seed` + `kb-load` |
| `make test` | unit tests (no network, no containers) |
| `make test-int` | integration tests with testcontainers (Postgres, Qdrant) and recorded model responses |
| `make test-e2e` | Playwright against the compose stack with the fake model provider |
| `make lint` | ruff, mypy, import-linter, eslint, tsc |
| `make data-check` | validate `data/` (seed schemas and rules; KB front matter, `error_codes.csv`, internal-only phrases in external content; seed error codes must be in the CSV; evaluation tickets and attacks: schema per line, known customers and external gold documents, composition rules of the [eval plan](eval-plan.md) §3) |
| `make kb-numbers` | report every number in `data/kb/` that is not in the world file (a report, not a gate) |
| `make eval`, `make eval-smoke`, `make eval-attacks` | see [eval plan](eval-plan.md); real model calls, run by hand only when the owner asks (ADR-0018) |
| `make web-types` | generate TypeScript types from OpenAPI |
| `make retention` | run the retention job once |
| `make record-llm` | refresh recorded model responses for tests (real calls); skips models that already have a real recording, `FORCE=1` records them again |
| `make llm-requests` | rewrite the recording requests (no model call): triage from the example tickets, the run tickets (`tests/fixtures/run/tickets.jsonl`) for triage and write with the KB from `data/kb`; run before `make record-llm` after a prompt or KB change (a KB change changes every write request) |
| `make eval-report RUN=<id> BASE=<id>` | compare two evaluation runs; see [eval plan](eval-plan.md) |
| `make leak-scan` | scan database, logs and recorded model requests for card patterns and canary strings |
| `make export-review-candidates` | export rejected and heavily edited drafts as candidates for new eval tickets |
| `make rollback` | send traffic back to the previous deployed revision |

**Fake model provider.** `LLM_MODE=fake` makes the gateway return deterministic recorded or scripted responses. Unit, integration and e2e tests run with it, so tests need no API key and cost nothing. Recordings live in `apps/api/tests/fixtures/llm/` and are refreshed with `make record-llm` (real calls). The compose `api` service mounts them read-only where the gateway looks (`FIXTURES_DIR`), with `config/` and `prompts/` (the app loads both at start), so `make dev` runs in fake mode; a ticket whose text has no recording is handed to a human (`system_error`). Only the run tickets have recordings; use `LLM_MODE=real` for other texts.

## 2. Configuration

All settings are environment variables read by one Pydantic `Settings` class (`hub/config.py`). `.env.example` lists every variable with a comment. No setting is read anywhere else.

| Variable | Example | Note |
|---|---|---|
| `APP_ENV` | `local` \| `test` \| `demo` | |
| `DATABASE_URL` | `postgresql+psycopg://…` | pooled connection as the **app role** (a login user in group `hub_app`) |
| `DATABASE_URL_DIRECT` | | direct (not pooled) connection as the **migrator** (schema owner) for migrations and checkpointer setup; empty = same as `DATABASE_URL`, which only works when that user may create tables |
| `EVAL_DATABASE_URL` | | separate database for evaluation runs |
| `CORE_ANCHOR_DATE` | empty \| `2026-11-12` | "today" for the core mock and the workflow; empty = real date; fixed in tests and evaluation |
| `JOBS_TOKEN` | random | bearer token for `POST /admin/jobs/*` |
| `QDRANT_URL`, `QDRANT_API_KEY` | | key empty locally |
| `ANTHROPIC_API_KEY` | | read by LiteLLM; the only provider key (ADR-0018) |
| `LLM_MODE` | `real` \| `fake` | |
| `MODELS_CONFIG` | `config/models.yaml` | |
| `PROMPTS_DIR` | `prompts` | prompt files; the active versions are in `MODELS_CONFIG` |
| `MCP_SERVER_URL`, `MCP_SERVER_TOKEN` | `http://mcp:8001/mcp` | |
| `JIRA_MODE`, `SLACK_MODE` | `mock` | only `mock` ([ADR-0019](adr/0019-scope-cuts.md)). The settings still accept `real` together with `JIRA_BASE_URL`, `JIRA_EMAIL`, `JIRA_API_TOKEN`, `JIRA_PROJECT_KEY`, `SLACK_BOT_TOKEN`, `SLACK_CHANNEL_ID`; P7-02 removes them |
| `SESSION_SECRET` | random 32 bytes | |
| `DEMO_ACCESS_CODE` | | for the public chat |
| `SEED_OPERATOR_PASSWORD` | | used by `make seed` |
| `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`, `LANGFUSE_HOST` | | tracing is off when empty |
| `DAILY_BUDGET_USD` | `0.50` | start value of the setting |
| `EVAL_MAX_COST_USD` | `1.00` | hard stop for one evaluation run |
| `EVAL_TRACE_SAMPLE` | `0.2` | share of evaluation runs sent to Langfuse |
| `LOG_LEVEL` | `INFO` | |

**Database roles** (security §4, least privilege). Two connections, two roles:
- **Migrator**: owns the schema and runs `make migrate` through `DATABASE_URL_DIRECT`. Locally `hub`; on Neon the owner role of the database.
- **App**: the API connects through `DATABASE_URL` as a login user that is a member of the group role `hub_app`. Locally `hub_api` (created by `infra/postgres/init/02-app-user.sql`). Migration 0001 creates `hub_app` when it is missing and gives it, through default privileges, read and write access to every table the migrator creates; `audit_log` keeps only `SELECT` and `INSERT`.
- **Deploy setup (once per managed database, before the first deploy):** as the owner role, `CREATE ROLE hub_app NOLOGIN;` (or let migration 0001 do it) and `CREATE ROLE <app user> LOGIN PASSWORD '<secret>' IN ROLE hub_app;`. The password goes into the secret for `DATABASE_URL`, never into the repository.

Runtime settings that the lead changes in the console (auto-reply, threshold, budget, allow-list) live in the `settings` table; environment variables give only the first values.

## 3. Observability

**Three layers, each with one job.**

| Layer | Tool | Answers the question |
|---|---|---|
| Structured logs | `structlog`, JSON to stdout, masked | What happened in the service? Each line has `ticket_id`, `run_id`, `node`, `request_id`. |
| Traces | Langfuse via OpenTelemetry | What did this run do, step by step? One trace per run; one span per node; one generation per model call (masked input and output, tokens, cost, prompt version); spans for retrieval (query, chunk ids, scores) and tool calls. Trace id stored in `runs.trace_id`; the console links to it. |
| Business and cost data | Postgres: `runs`, `llm_calls`, `drafts`, `feedback` | How is the service doing? Source for the dashboard and for cost. Does not depend on the tracing vendor. |

Trace attributes on every run: `ticket_id`, `intent`, `route`, `route_reasons`, `confidence`, `graph_version`, `prompt_versions`, `variant`, `context` (`online` / `eval`). Eval runs are traced with the tag `eval` and the run id, so a bad eval ticket can be opened as a trace.

**Dashboard numbers** (from Postgres, see [API](api.md) §5): tickets by route, automation share, route reasons, draft outcomes and edit ratio, latency p50/p95, cost per ticket and by node, feedback, budget used, last eval summary with pass/fail against the red lines.

**Alerts** are not built: they were part of phase 9, removed by [ADR-0019](adr/0019-scope-cuts.md). The design, kept for reference: a daily summary and these conditions logged and posted to the Slack mock: budget above 80%; error rate of runs above 10% in an hour; p95 latency above the red line in an hour; audit chain check failed.

**Runbook** (`docs/runbook.md`): not written, phase 9 was removed (ADR-0019). Its planned content: what to do when — auto-replies are wrong (switch off, find the traces, add eval tickets); the provider is down (fallback model, handoff mode); the budget is reached; a run is stuck; a card number is found in storage (incident steps).

## 4. Cost control

- Costs are recorded in USD, the currency of the provider price lists. Targets in the PRD are in EUR; reports convert with one fixed rate from `config/pricing.yaml` and print the rate.
- Prices per model in `config/pricing.yaml` (input, output, cached input; date of the price). The gateway computes `cost_usd` for every call.
- Prompt caching for the stable system part of every prompt (provider feature; the builder checks the current rules and minimum sizes).
- One small model for every role: Claude Haiku 5.5, fallback Claude Sonnet 5.5 ([ADR-0018](adr/0018-budget-mode.md)). Embeddings run locally (FastEmbed, `BAAI/bge-small-en-v1.5`) and cost nothing.
- Project budget: about $5 of model calls in total. The owner sets a $5 spending limit in the Anthropic console. Paid evaluation runs only by hand, when the owner asks; none in CI.
- Limits: input 4,000 characters, 10 history turns, 8 chunks, `max_tokens` per role.
- Daily budget guard (G8). Evaluation has its own limit (`EVAL_MAX_COST_USD`) and a disk cache that is on by default.

## 5. CI (GitHub Actions)

| Workflow | Trigger | Jobs |
|---|---|---|
| `ci.yml` | pull request, push to main | `lint` (ruff, mypy, import-linter, eslint, tsc) · `test` (unit tests, coverage gate for safety modules) · `test-int` (integration with testcontainers; builds the api dev image) · `web` (vitest, build) · `e2e` (compose + Playwright, fake models) · `security` (gitleaks, pip-audit, pnpm audit) · `data-check` |
| `deploy.yml` | push to main after `ci` is green; manual | build images → push → migrate → deploy → smoke check |

No workflow runs an evaluation or any other real model call, except the post-deploy canary ticket (§6, under one cent). `make eval-smoke` runs by hand, only when the owner asks ([ADR-0018](adr/0018-budget-mode.md)).

Branch rule: changes reach `main` only through pull requests, and CI must be green. Until phase 12 the repository is private on the free GitHub plan, which has no branch protection, so the rule holds by process (only the owner merges). The owner turns branch protection on when the repository becomes public; the README section "CI and branch protection" lists the settings.

## 6. Deployment (demo, free tier)

Decision and alternatives: [ADR-0015](adr/0015-free-tier-deployment.md).

```mermaid
flowchart LR
    user([Browser]) --> run_api["Cloud Run: hub-api<br/>API + built web app<br/>scales to zero"]
    run_api --> run_mcp["Cloud Run: support-tools<br/>MCP server, internal"]
    run_api --> neon[("Neon Postgres<br/>free plan")]
    run_api --> qcloud[("Qdrant Cloud<br/>free cluster")]
    run_api --> llm[(LLM providers)]
    run_api --> lf[(Langfuse Cloud<br/>free plan)]
    gha["GitHub Actions"] -- "build, migrate, deploy" --> run_api
    gha --> run_mcp
```

| Part | Service | Cost | Note |
|---|---|---|---|
| API + web | Google Cloud Run, 1 service, min instances 0, max 2, 1 vCPU / 1 GiB, request-based billing | inside the always-free allowance for demo traffic | Needs a billing account with a card. A budget alert at €1 is set. Cold start a few seconds — acceptable for a demo; the README says so. |
| MCP server | Cloud Run, second service, min 0 | free | "Require authentication" is on: only the service account of `hub-api` has the invoker role. The bearer token is a second check. |
| Postgres | Neon free plan | free | Checked 2026-11: 100 compute-hours per month, about 1 GB storage, the database sleeps after 5 minutes without queries (first query after that is slow). Pooled connection string for the app — the builder checks that psycopg prepared statements and the LangGraph checkpointer work through the pooler; direct connection for migrations. |
| Vector search | Qdrant Cloud free cluster (1 GB RAM) | free | Checked 2026-11: a free cluster is suspended after 1 week without use and deleted after 4 weeks. A scheduled GitHub Action calls the `ping` job every 3 days. If the cluster is lost, `make kb-load` rebuilds it in minutes. |
| Tracing | Langfuse Cloud free plan | free | Checked 2026-11: 50,000 units per month, 30 days of data. Full evaluation runs can use this up — tracing of eval runs is sampled (`EVAL_TRACE_SAMPLE`, default 0.2). |
| Images | Artifact Registry or GitHub Container Registry | free at this size | |
| Model calls | Anthropic API (the only provider, ADR-0018) | pay per use | The only real cost. About $5 for the whole project; daily budget guard default $0.50. Embeddings run inside the API container (the image contains the model). |

Free tiers change. **Before phase 10 the builder checks the current limits on the official pricing pages and updates this table.** Everything runs in standard containers with standard Postgres and Qdrant, so the fallback is simple: one small VPS with the same `docker-compose.yml` plus Caddy for HTTPS (`infra/compose.prod.yml` is kept working for this).

Deployment rules:
- Images: multi-stage builds, non-root user, only runtime dependencies. `Dockerfile.api` also builds the web app and copies the static files.
- Secrets in Google Secret Manager (or Cloud Run secrets), never in the image or the repository.
- Migrations run as a separate step before the new revision gets traffic. Migrations must be backward compatible with the running version.
- After deploy: smoke check — `/ready`, then one fixed canary ticket through the real pipeline (cost under one cent). A failed check triggers the rollback.
- Rollback: previous Cloud Run revision gets the traffic again (`make rollback`).
- Because Cloud Run gives CPU only during a request, all work happens inside requests ([ADR-0007](adr/0007-in-request-processing-sse.md)). Scheduled work is triggered by GitHub Actions schedules that call `POST /admin/jobs/{name}` with `JOBS_TOKEN`: `close-stale` and `recover-runs` every hour, `retention` every night, `ping` every 3 days. No evaluation runs in GitHub Actions (ADR-0018).

## 7. Backups

Demo data is synthetic and can be rebuilt with `make migrate seed kb-load`, so there is no backup duty. The README states what a real system would add: point-in-time recovery for Postgres, Qdrant snapshots, restore drills.
