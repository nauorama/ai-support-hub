# Build Plan and Backlog

This is the work order for the coding agent that builds the project. Phases follow the modules of the Applied AI Program (phase N = module N), so the finished code can be studied module by module later.

**Budget mode ([ADR-0018](adr/0018-budget-mode.md), 2026-11-16).** About $5 of real model spend for the rest of the project, Anthropic only. Every role runs on Claude Haiku 5.5 (fallback Sonnet 5.5), embeddings come from a local model, phase 4 is a small set of 20 dev tickets plus deterministic checks, the experiments E1–E8 and phase 11 are removed, and `make eval-smoke` runs only by hand when the owner asks, never in CI. The tasks below already follow it.

## 1. How to work

How the sessions, agent roles and reviews are organised: [build process](build-process.md).

**Before any task:** read `AGENTS.md`, then the documents named in the task. The documents in `docs/` are the specification. If a task and a document disagree, the document wins; if two documents disagree, stop and ask.

**Loop for one task**
1. Read the task and its references. Write a short plan (files to touch, tests to add).
2. Check the current documentation of every library you will use. Do not write API calls from memory.
3. Write the tests, then the code. Keep the change small: one task = one pull request.
4. Content tasks (files in `data/`) go to the `content-author` subagent.
5. Run `make lint test` (and `make test-int` when the task touches the database, Qdrant, the graph or the MCP server).
6. Check the "Done when" list line by line. Run the "Verify" command and paste its output into the pull request.
7. Update documents that the change made wrong. A changed decision needs a new ADR.
8. Open the pull request and stop. The `reviewer` subagent checks it; the owner merges.

**Phase gate.** A phase is finished when all its tasks are done, the gate check passes, and `docs/walkthrough/phase-N.md` exists. Then stop and report to Viktoriia; do not start the next phase without her go-ahead.

**Walkthrough file** (one per phase, max 2 pages) — this is how the owner learns the system after it is built. It is the owner's personal note: write it to `docs/walkthrough/phase-N.md` locally. The folder is git-ignored; never commit the file, and it needs no pull request.
- what was built, with paths to the 5–10 most important files;
- how a request flows through the new code (short diagram or numbered list);
- how to run and see it;
- decisions made during the phase and why; what differs from the plan;
- 5 questions to check understanding (with answers at the end);
- what went wrong during the build and how it was found.

**Human gates** — only Viktoriia can do these. Ask early; continue with other tasks meanwhile.

| Gate | Needed for |
|---|---|
| H1. Create the GitHub repository; add secrets | P0 |
| H2. Anthropic API key; a $5 spending limit in the Anthropic console (ADR-0018) | P3 |
| H3. Review the knowledge-base articles | P2-08 |
| H6. Review the attack set | P8-01 |
| H7. Decide whether auto-reply goes on in the demo | P9-03 |
| H8. Accounts: Google Cloud (billing + budget alert), Neon, Qdrant Cloud, Langfuse; optional Jira and Slack | P9-01, P10-07 |
| H9. Record the demo video; publish (make the repository public and turn on branch protection for `main`, see the README) | P12 |

**Rules that never bend:** see `AGENTS.md` ("Never do").

Size: S = under half a day of agent work with review, M = about a day, L = several days.

## 2. Phases at a glance

| Phase | Module | Result |
|---|---|---|
| 0 | — | Repository with tooling and CI |
| 1 | Product and design | This design package (done before the build) |
| 2 | Python and backend | Service skeleton, database, masking, audit, ticket API, knowledge base in Postgres |
| 3 | LLM and prompts | Model gateway, triage, first cited draft (whole KB in the prompt) |
| 4 | Evals | 20 labelled tickets, runner, deterministic checks, judges, first numbers |
| 5 | RAG | Hybrid search, citations to sections, verification, retrieval experiments |
| 6 | Agents and LangGraph | Graph, routing, pause for the operator, memory |
| 7 | Tools and MCP | MCP server, account tools, Jira and Slack actions |
| 8 | Security | Attack suite, hardening, privacy functions |
| 9 | LLMOps | Tracing, dashboard data, confidence model, threshold, runbook |
| 10 | Deployment | Web app, containers, public demo, CI/CD |
| 12 | Interview preparation | README with numbers, case study, demo script |

Dependency notes: phases run in order. Exception: web tasks P10-01 to P10-04 need only the APIs of phases 2, 6, 7 and 9 and may be built in parallel with phases 8 and 9.

---

## Phase 0 — Bootstrap

| ID | Task | Size | Done when | Verify |
|---|---|---|---|---|
| P0-01 | **Repository skeleton.** Copy this design package into the new repository. Create the uv workspace (`apps/api`, `apps/mcp_server`, `evals`), the folder layout from [design](design.md) §8, `Makefile` with the commands from [operations](operations.md) §1 (not-yet-built commands print "not implemented" and exit with an error), `.env.example`, `.gitignore`, MIT licence, pre-commit (ruff, gitleaks). | S | `make setup` works on a clean clone. `uv run python -c "import hub"` works. | `make setup && make lint` |
| P0-02 | **CI skeleton.** `ci.yml` with lint, test, security, data-check jobs. ruff, mypy strict, import-linter contracts from design §8. Issue and pull-request templates. Branch protection described in the README. | S | CI is green on an empty project. A wrong import between modules fails CI (prove with a test commit, then remove it). | CI run link |

**Gate 0:** clean clone → `make setup lint test` passes; CI green.

---

## Phase 2 — Backend foundation

References: [design](design.md), [data model](data-model.md), [API](api.md) §1–3, [security](security.md) §3–5.

| ID | Task | Size | Done when | Verify |
|---|---|---|---|---|
| P2-01 | **App skeleton.** `config.py` (one `Settings` class), FastAPI app factory, request id middleware, structlog JSON logging with a masking processor (uses P2-03 when ready), problem-details error handler, `/health`, `/ready`. | S | Unknown names in `.env` fail at start. Errors follow RFC 9457. | `make test` |
| P2-02 | **Database.** SQLAlchemy models and Alembic migrations for `customers`, `operators`, `tickets`, `messages`, `settings`, `feedback`, `audit_log`. Status and enum `CHECK` constraints. Ticket status transition function in `domain/` (pure, tested). Repositories. Separate database roles: app (no UPDATE/DELETE on `audit_log`) and migrator. | M | Every allowed and forbidden status transition from the data model has a test. An UPDATE on `audit_log` with the app role fails in an integration test. | `make test-int` |
| P2-03 | **Card-data masking (G1).** `guardrails/masking.py` as specified in security §3. Pure functions, no dependencies on the rest. Remove the temporary P2-01 mask and wire in `masking.py`. | M | Unit tests with formats: spaces, dashes, no separators, in the middle of words, several numbers, 13–19 digits, numbers that fail Luhn (not masked), CVV/PIN/2FA next to trigger words. Hypothesis property test: any Luhn-valid number in random text is masked. Coverage ≥ 95%. | `make test` |
| P2-04 | **Audit log.** `audit/` with `record(...)`, hash chain, `verify_chain()`. Details pass through masking. | S | Changing one stored row by hand makes `verify_chain()` report that row. | `make test-int` |
| P2-05 | **Seed data.** Author `data/seed/*.json`: 40 customers across tiers and countries; cards; transactions that cover every scenario of the world file (pending, held for review, failed with each main error code, late SWIFT, card not delivered, restricted account, limits nearly used). Dates are offsets from an anchor date (data model §3). Pydantic schemas + `make data-check`. `make seed` loads customers, operators (password from env) and default settings. | M | Each of the 12 intents has at least 3 customers whose data fits a ticket of that intent. `make seed` is idempotent. With a fixed `CORE_ANCHOR_DATE` two loads give the same dates. | `make data-check && make seed` |
| P2-06 | **Ticket API without AI.** Demo session, `POST /tickets`, `POST /tickets/{id}/messages`, `GET` endpoints, feedback, request-human. Order inside the handler: validate → mask → save → audit. SSE response with `ticket.created`, a placeholder system message and `done`. Idempotency keys. Rate limits. Input size limit before masking: message body max 4,000 characters (security T9) and a cap on the whole request body, because masking (G1) is synchronous CPU work and a large digit-heavy input takes seconds (P2-03 review). Customer isolation (404 for foreign tickets). **From the P2-02 review:** `TicketRepository.save()` flushes any change, so code that sets `ticket.status` and calls `save()` skips the transition check; make status changes go only through `change_status` (e.g. `save` refuses a changed status, or becomes private) before the API uses it. | M | A message with a card number is stored masked; the raw number is not in the database or the logs (test greps both). A message over 4,000 characters is rejected with a problem-details error before it is masked. Second POST with the same key returns the same message. A test shows that changing the status any other way than `change_status` is refused. | `make test-int` + curl example in the PR |
| P2-07 | **Operator auth and console API (no drafts yet).** Login, logout, `me`, roles as dependencies, CSRF. Console: list with filters, ticket detail, claim, release, reply, close. Every action writes an audit record. **From P2-06 (must do in this task):** (1) polling with `GET /tickets/{id}/messages?after=` can miss a message: `messages.created_at` is the transaction start, so a message saved in a transaction that started earlier but committed later sorts before an anchor the client already has (e.g. an operator reply overlapping a customer message); a `clock_timestamp()` or a sequence value alone does not fix this, because both are taken at INSERT, not at commit. Either serialise message writes per ticket (every transaction that writes a message first locks the ticket row with `SELECT … FOR UPDATE`, and only then takes the timestamp or sequence value), or let the client re-read an overlap window and drop duplicates by message id. Test it with two overlapping transactions. (2) In phase 2 a new ticket, and an AI-owned follow-up, stays in `processing` with no run and no owner, so it never reaches the operator queue although the placeholder says the support team will reply; decide how such tickets reach operators until phase 6 (for example: list `processing` tickets in the queue, or hand them off), and note that `processing → answered` is labelled "auto-reply" in the state diagram. (3) The ticket status guard (`hub/db/models.py`, mapper `before_update`) does not see ORM bulk statements: `session.execute(update(Ticket).values(status=...))` changes the status without `change_status`. Refuse such statements (a `do_orm_execute` listener) or provide a checked bulk transition, with a test. | M | A test walks all `/console` and `/admin` routes and fails if a route has no role. Operator reply reaches the customer through `GET /tickets/{id}/messages`. | `make test-int` |
| P2-08 | **Knowledge-base content.** Write about 50 articles in `data/kb/` (about 40 external, 10 internal) and `error_codes.csv` (about 40 codes) following data model §3. Every fact must come from [the world file](tallybridge-world.md). Cover all 12 intents. Internal articles: escalation procedures, goodwill rules, restricted-account wording, fraud checklist. Front-matter validation in `make data-check`. **Human gate H3.** **From P2-05 (deferred):** `hub/domain/seed.py` checks seed error codes against `KNOWN_ERROR_CODES`, the 19 codes named in the world file §7; switch it to read the codes from `error_codes.csv`, so the seed can use every code and a code missing from the CSV fails `make data-check`. | L | `make data-check` passes. A script lists every number in the articles that is not in the world file — the list is empty or each item is explained. | `make data-check` |
| P2-09 | **KB loader.** `kb/loader.py` + `kb/chunker.py`: files → `kb_documents` and `kb_chunks` (section chunking, [ADR-0006](adr/0006-section-chunking.md)). Skips unchanged files by hash, bumps `version` on change, removes deleted documents. `make kb-load` (Postgres part). | S | Loading twice changes nothing. Editing one article updates only its chunks. Chunker tests cover: no sections, long section split, error-code rows. | `make kb-load && make test-int` |
| P2-10 | **Compose and integration setup.** `infra/docker-compose.yml`, dev Dockerfiles, `make dev`. Testcontainers fixtures for Postgres (and Qdrant, used later). **Note:** `make dev` needs `SEED_OPERATOR_PASSWORD` (since P2-05), `SESSION_SECRET` and `DEMO_ACCESS_CODE` (since P2-06) in `.env`; its first step lists every empty one and stops before anything starts. | S | Clean clone → `make dev` → the curl example from P2-06 works. | `make dev` |

**Must do before gate H2 (real API keys), as separate small PRs that change only masking:**

1. **Masking gap, found in the P3-01 review:** a 15-digit card number preceded by two capital letters can pass the IBAN rule and is left unmasked: `mask("YZ178162840989986")` returns the text unchanged with no counts (the IBAN keep rule in `guardrails/masking.py` takes the run when the mod-97 check happens to pass). G1 and the gateway's outbound scan (G2) use the same function, so both miss it. Hypothesis found it in `test_audit_chain.py::test_a_card_number_anywhere_in_details_never_reaches_the_hashed_row`; the example is kept in the local `.hypothesis` database, so the test fails on that machine until fixed. Fix the rule (a Luhn-valid card number wins over an IBAN candidate, or a stricter IBAN check) and add this example as an explicit test. **Done** (ADR-0017): stricter IBAN check (registry country and length) and every valid IBAN is partly masked (`[IBAN DE89 …3000]`); G2 blocks a raw IBAN with its own reason. The example is `test_masking.py::test_card_after_two_capital_letters_is_masked_even_when_mod_97_passes`; a property test hides a card anywhere in a generated IBAN.
2. The follow-up from P2-08 below. **Done** for masking: `ERR-` + 4 digits (uppercase) is not a PIN or 2FA code (security §3, `test_masking.py::test_error_codes_next_to_pin_or_2fa_words_are_kept`). The KB wording restore is a separate PR (content-author).

**Follow-up from P2-08 (separate small PR):** masking must not redact an exact `ERR-\d{4}` code as a PIN or 2FA code; tests: 'ERR-3007 wrong PIN' keeps the code, 'my PIN is 3007' is still masked. Found while writing the KB: `mask_text("ERR-3007 wrong PIN")` returns `ERR-[REDACTED] wrong PIN` (also ERR-3003, ERR-5003, ERR-5004), so triage would lose the code. When the fix lands, restore the world-file wording in `data/kb/` (e.g. ERR-3007 "Wrong PIN" instead of "Incorrect card PIN", ERR-3003, ERR-5003, ERR-5004 titles, and the articles `card-pin.md`, `two-factor-authentication.md`, `card-declined.md` that keep codes away from PIN/2FA words).

**Open question for P5-01 (from the P2-09 review):** `make kb-load` skips a file whose SHA-256 is unchanged, so a change to the chunker or to `MAX_CHUNK_TOKENS` does not reach unchanged files: their old chunks stay in Postgres, and from P5 in Qdrant, until each file is edited. P5-01 needs a way to force a full reload (for example a `--force` option, or a chunker version stored with each document that triggers a reload when it differs).

**From the P2-09 review (deferred):** a first `##` heading indented by 1–3 spaces passes `make data-check` (`article_errors` checks `body.lstrip()`), but the chunker (`hub/kb/chunker.py`, `_HEADING`) does not take it as a heading, so that section is cited under the article title (`sections('   ## Indented\nx\n## A\ny', 'T')` gives `[('T', '## Indented\nx'), ('A', 'y')]`); make the two agree (the chunker accepts the indent, or data-check refuses it). No article in `data/kb` has one today.

**Gate 2:** with `make dev`, a customer message with a card number is stored masked, an operator answers it through the API, the audit chain verifies, CI is green. Walkthrough written.

---

## Phase 3 — LLM and prompts

References: [AI pipeline](ai-pipeline.md) §5, §7, §8; ADR-0003, 0004, 0013.

| ID | Task | Size | Done when | Verify |
|---|---|---|---|---|
| P3-01 | **LLM gateway.** `llm/gateway.py`: `complete(role, messages, schema=None, tools=None)` and `embed(texts)`. Roles from `config/models.yaml`. Timeouts, 2 retries with backoff, fallback model. Outbound card scan (G2). Cost from `config/pricing.yaml`. One `llm_calls` row per call (also for failures). Fake mode with recorded fixtures and `make record-llm`. Fill model ids and prices from the current provider pages. | L | A request containing a card number is blocked and audited (test). A provider error triggers retry then fallback (test with a stub). Cost of a known token count matches the pricing file. No other module imports `litellm` (import-linter). | `make test` |
| P3-02 | **Structured output.** Schema → provider structured output → Pydantic validation → one repair call. | S | Invalid first answer + valid repair = success; two invalid = typed error. | `make test` |
| P3-03 | **Prompt loader and triage.** `llm/prompts.py` (front matter, `# System` / `# User`, placeholders, missing placeholder = error). `prompts/triage/v1.md`. `triage()` returns `TriageResult`. | M | 20 hand-written example tickets (in tests, recorded responses) get the expected intent and flags. Prompt version is in `llm_calls`. The first real calls (`make record-llm`) confirm that Anthropic accepts our strict schemas from `provider_schema` (P3-02), including `const` and a `description` next to a `$ref`. | `make test` |
| P3-04 | **PII placeholders (G3) and injection heuristics (G4).** G1 already partly masks IBANs (ADR-0017): G3 replaces the masked form `[IBAN DE89 …3000]` and restores that masked form, never a full IBAN. **From the P3-03 review:** ticket text is put into the prompt as-is (`prompts/triage/v1.md`, `<ticket>{{ticket}}</ticket>`), so a `</ticket>` inside it closes the data tag early. Neutralise a closing data tag (`</ticket>`, and the other data tags of ai-pipeline §7) inside ticket text before it reaches a prompt; test it. | S | Email, phone, IBAN are replaced and can be restored. Known injection phrases score high; normal tickets score low (table test with 30 examples). | `make test` |
| P3-05 | **First draft, baseline B0.** `prompts/write/v1.md`: all external articles in the system part (cached), ticket in the user part. Output `DraftAnswer` with citations to document ids. Table `runs` and `drafts`; the same migration adds the foreign key `llm_calls.run_id → runs.id` (P3-01 created `run_id` as a nullable uuid without it, because `runs` did not exist yet). **Deferred from P3-01:** fake mode reads its recordings from `apps/api/tests/fixtures/llm/` (`FIXTURES_DIR` in `llm/gateway.py`), but `infra/Dockerfile.api` copies only `apps/api/src`, and `LLM_MODE` defaults to `fake`; once the graph calls the gateway, every model call in a compose or e2e container fails with `FixtureMissingError`. Make the fixture folder reach the container (a setting for the path, or copy the fixtures into the dev image) before the first ticket runs through it. A simple linear function `intake → triage → write` is called by the ticket endpoints; **every** ticket ends as `pending_approval` (no auto-reply yet). Console endpoints approve / edit / reject with `ticket_version`. **From the P3-03 review:** `triage()` (`graph/nodes/triage.py`) writes no audit record yet; when the linear function calls it, write `triage.done` (security §5 events) for its decision (intent, flags, prompt version, never the ticket text). P6-01 keeps this record when triage becomes a graph node (ai-pipeline §3: every node writes an audit record). | L | A ticket about SEPA withdrawal time produces a draft that cites the right article; a question with no answer in the KB gives `can_answer = false`. Approve sends the message with the "AI assistant · reviewed by" label data. | `make test-int`; manual run with real keys shown in the PR |
| P3-06 | **Budget guard (G8).** Daily sum from `llm_calls`; refusal → handoff with reason `budget_exceeded`. The daily sum includes rows with context `record` (`make record-llm`): recording spend counts against the daily budget (owner decision, P3-01). | S | With the budget set to 0, a new ticket is handed off and no model call is made. | `make test-int` |

**Gate 3:** real ticket → triage + cited draft visible through the console API; cost and prompt versions recorded; all tests run without API keys (fake mode).

---

## Phase 4 — Evals

References: [eval plan](eval-plan.md) (all).

| ID | Task | Size | Done when | Verify |
|---|---|---|---|---|
| P4-01 | **Schemas and checks.** `hub_evals/schemas.py`, `make data-check` for eval files (schema, references to KB documents and customers, every intent present, attack categories). There is no test file and no dev/test split (ADR-0018). | S | A broken file fails with a clear message per line. | `make data-check` |
| P4-02 | **Ticket dataset.** Exactly 20 tickets in `data/eval/tickets.dev.jsonl`, written by the `content-author` subagent from the world file, KB and seed data (no paid model call): every intent at least once, at least 6 tagged hard cases, at least 3 `must_handoff`. No human gate (H4 removed by ADR-0018): the reviewer checks every label against the world file and the KB. `data/eval/README.md` describes the process. | S | Exactly 20 tickets pass `make data-check`; the composition rules of eval plan §3 hold. | `make data-check` |
| P4-03 | **Runner.** `hub_evals/runner.py` + CLI: concurrency, resume, disk cache, cost estimate and limit, outputs as in eval plan §4. Runs the pipeline through one function `run_ticket(ticket, variant)` that the API also uses. **From the P2-02 review:** the evaluation database (`EVAL_DATABASE_URL`, `hub_eval`) is not migrated yet, so the app role has no grants there (default privileges are per database); add a migrate step for it (e.g. `make migrate` also upgrades `hub_eval`, with its own migrator URL) before the runner writes to it. | M | An interrupted run continues without repeating finished tickets. The disk cache is on by default; `--no-cache` turns it off. Re-running with the cache makes no model calls. The runner writes to `hub_eval` as the app role after the migrate step. | `LLM_MODE=fake make eval LIMIT=10` (fake mode, no paid call; a real-mode run only when the owner asks) |
| P4-04 | **Deterministic metrics.** Triage accuracy, confusion matrix, flag recall; routing metrics; retrieval metrics (hit@k, MRR — used from phase 5); cost and latency percentiles; counts with Wilson intervals. Pure functions with tests on small hand-made inputs. | M | Each metric has a test with a known answer. | `make test` |
| P4-05 | **Judges.** Prompts `prompts/judge_*/v1.md` (key facts, faithfulness, citation, attack) on the `judge` role (Haiku 5.5, ADR-0018); judge functions. No validation set (H5 removed): reports mark judge metrics "unvalidated, same model family", and every wrong verdict found later becomes a unit test with a recorded response. | M | Each judge has unit tests with recorded responses for a pass and a fail. | `make test` |
| P4-06 | **First report.** One run of the current pipeline (baseline B0) on the 20 tickets, only when the owner asks (costs about $0.10). `report.md` with the metric table against targets and red lines, marked "demo check, not a proof". Error analysis of every failure by category. Start `docs/experiments.md`. | M | Report committed. The README shows the first numbers table with the notes "synthetic data" and "demo check". | report file |

**Gate 4:** first numbers exist and are honest, with counts, intervals and the "unvalidated judge" mark; CI makes no real model call.

---

## Phase 5 — RAG

References: [data model](data-model.md) §2; [AI pipeline](ai-pipeline.md) §4–5; ADR-0005, 0006.

| ID | Task | Size | Done when | Verify |
|---|---|---|---|---|
| P5-01 | **Index.** Qdrant collection with named dense and sparse vectors, payload indexes, alias. **Local embedding backend in the gateway** (ADR-0018): `fastembed==0.9.1` with `BAAI/bge-small-en-v1.5` (384 dimensions), loaded with `local_files_only=True`; a setting for the model cache folder; `make setup` downloads the model; record the SHA-256 of the model file and check whether the Hugging Face revision can be pinned; check that every section chunk fits in 512 tokens. `kb/index.py`: embed changed chunks (dense through the gateway, BM25 through FastEmbed), upsert, delete removed; full re-index builds a new collection and moves the alias. `make kb-load` now also updates Qdrant. `POST /admin/kb/reindex`. | M | Changing one article re-embeds only its chunks. Postgres and Qdrant chunk counts match (check in `/ready` details). Embedding works with the network off; embedding rows in `llm_calls` cost 0. | `make test-int` |
| P5-02 | **Retriever.** `kb/retriever.py`: `search(query, visibility, top_k, mode)` with modes `dense` and `hybrid` (default). No reranker mode: experiment E3 was removed (ADR-0018). Returns chunks with scores and ranks. Console KB search and public `/help/{doc_id}` endpoints. | M | A query with "ERR-2003" returns that error-code chunk first in hybrid mode. `visibility=external` never returns an internal chunk (test over all chunks). | `make test-int` |
| P5-03 | **Writer with retrieval.** `prompts/write/v2.md`: evidence in `<kb>` with source ids; citations point to chunk ids; server builds citation links (title + section URL). | M | Draft citations resolve to existing chunks; the customer message shows article links. | `make test-int` |
| P5-04 | **Verification.** Code checks from AI pipeline §5 in `guardrails/output_checks.py`; `prompts/verify/v1.md` and the LLM claim check. | M | Each code check has a failing and a passing test. A draft with an invented fee is marked unsupported (recorded example). Coverage ≥ 95% for `output_checks.py`. | `make test` |

**Gate 5:** on the 20 tickets, hit@5 ≥ 0.80 and at most 2 answered tickets with an unsupported claim (faithfulness; demo check, judge unvalidated). B0 against RAG is not measured (E1 removed); the README says so.

---

## Phase 6 — Agents and LangGraph

References: [AI pipeline](ai-pipeline.md) §1–3, §6, §9; ADR-0002, 0007, 0008, 0009, 0012.

| ID | Task | Size | Done when | Verify |
|---|---|---|---|---|
| P6-01 | **Graph.** `graph/state.py`, `graph/build.py`, one file per node in `graph/nodes/`. Postgres checkpointer (psycopg 3), setup in migrations. Move the linear pipeline into the graph. SSE `run.status` events from node starts. Node timeouts and error → handoff. `research` is still "first search only". **From the P2-06 review (deferred):** FastAPI's SSE keep-alive sends `: ping` every 15 s (`fastapi.sse._PING_INTERVAL`) while API §4 says 10 s; when the long stream arrives, send the ping every 10 s or change API §4. **From the P2-06 review (deferred):** after a failed mask, (a) a replay of the same request (same `Idempotency-Key`) streams no `ticket.status` event, unlike the first response, and (b) the audit order differs: `create_ticket` writes `ticket.created`, `guardrail.masked`, `route.decided`, while `add_message` writes `route.decided` before `message.received` and `guardrail.masked`. Make replays carry the ticket's current status, and use one order. | L | The graph image generated from code matches AI pipeline §1. Each node has a unit test with a fake gateway. | `make test-int` |
| P6-02 | **Policy and routing.** `config/policy.yaml`, `guardrails/policy.py`, `policy_gate`, `decide` with the rule version of confidence, `handoff_prep` with fixed notice templates and the operator summary. Reason codes as an enum. **From P2-05 (deferred):** `make seed` (`hub/seed.py`, `DEFAULT_INTENT_ALLOWLIST`) hard-codes the first value of the `intent_allowlist` setting as the 7 intents of `auto_reply_allowlist` in AI pipeline §6, because `config/policy.yaml` did not exist yet; read it from `policy.yaml` instead, so the two cannot drift. | M | Table test: every row of the decision table in AI pipeline §6 has at least one test. Coverage ≥ 95% for `policy.py` and `routing.py`. A lost-card ticket gets the freeze-card instruction and a handoff notice within the stream. | `make test` |
| P6-03 | **Human review.** `human_review` with `interrupt()`; console approve/edit/reject resume the run with `Command(resume=…)`; `ticket_version` conflict → 409; ownership rule for follow-up messages; first operator reply on a handed-off ticket resumes the run (`ReviewDecision.kind = reply`); `POST /admin/runs/recover` and lazy recovery of stuck runs. **From P2-07 (deferred):** a customer message does not increase `tickets.version` when the status stays the same (e.g. a message on a `handed_off` or `pending_approval` ticket), so it does not make an operator's `ticket_version` stale, although the conflict rule in [API](api.md) §3 names "the customer wrote again" as its example. Make every new customer message change the version (in the same transaction as the message, under the ticket row lock of `MessageRepository.add`), so approve, edit and reject of a draft the customer has since written to answer `409 stale_draft`; test it. | L | Integration test: start a run → it pauses → restart the app (new process, same database) → approve → the message is sent and the run completes. Stale draft returns 409. | `make test-int` |
| P6-04 | **Retry loop.** `verify → research` with unsupported claims as hints, max one retry. (Experiment E7 removed by ADR-0018.) | S | A test proves the loop cannot run twice. | `make test` |
| P6-05 | **Memory.** `memory/`: summary when the ticket is closed (`prompts/memory_summary/v1.md`), load in `intake`, `memory_shows_open_case` rule, expiry. Shown in the console ticket detail. | M | A second ticket of the same customer has the first one's summary in context. Summaries contain no card data (masking test). | `make test-int` |
| P6-06 | **Routing evaluation.** Routing metrics in the report; a run on the 20 tickets, only when the owner asks (ADR-0018). | S | Hard-handoff recall = 1.0 and at most 1 missed escalation on the 20 tickets (demo check). | `make eval` |

**Gate 6:** the pause survives a restart; routing red lines hold on the 20 tickets (when the owner asks for a run); auto-reply still off by default.

---

## Phase 7 — Tools and MCP

References: [API](api.md) §7; [AI pipeline](ai-pipeline.md) §4; ADR-0011; [security](security.md) T4, T8, G6.

| ID | Task | Size | Done when | Verify |
|---|---|---|---|---|
| P7-01 | **MCP server with the core adapter.** `apps/mcp_server`: FastMCP server, streamable HTTP with bearer token and stdio; tools `core_*` reading `data/seed`; output schemas; argument validation. README section "Connect to Claude Code". **From P2-05 (deferred):** the Pydantic schemas of the seed files live in `hub/domain/seed.py`, which `support_tools` must not import (import-linter: `hub` and `support_tools` are independent). The core adapter needs its own schemas for `data/seed/` (or a small shared package both may import, a design change for the owner); a test must keep them in agreement with `hub.domain.seed`. The adapter resolves date offsets against `CORE_ANCHOR_DATE` (data model §3), with a test that the tools give the same dates on two runs with a fixed `CORE_ANCHOR_DATE` (P2-05 could check this only for the offset function, because `make seed` loads no dates), and must never return `support_scenarios` or `restriction_reason`. **From P2-05 (deferred):** count business days (Monday–Friday without TARGET closing days, world file §5) when the adapter computes expected dates; the seed data does not check the anchor weekday, so a 1–2-day wait that started at `-1d` (e.g. a SEPA withdrawal before the cut-off) may span a weekend. | M | Tools can be listed and called from a test client and from the MCP Inspector. `core_get_customer` never returns a restriction reason. | `make test` (mcp) |
| P7-02 | **Jira and Slack adapters.** Mock (default, stores calls) and real implementations behind one interface; idempotency keys; per-ticket limits. | M | Same idempotency key twice = one issue. Real mode tested by hand once, with a screenshot in the PR (optional, needs H8). | `make test` |
| P7-03 | **Hub tool client and permissions (G6).** `tools/mcp_client.py`, `tools/registry.py`, `tools/permissions.py`, table `tool_calls`. Identity injection; allow-list per node; side-effect tools need an approval object. | M | Tests: the model-facing schema has no customer argument; a call to a tool outside the allow-list is denied and recorded; a side-effect tool without approval is denied. Coverage ≥ 95% for `permissions.py`. | `make test` |
| P7-04 | **Research tool loop.** `prompts/research/v1.md`; loop with the limits from AI pipeline §4; account facts in `Evidence`; `prompts/write/v3.md` cites account facts; `intake` loads the profile through the tool. | L | A ticket "where is my withdrawal" for a customer with a failed withdrawal gets an answer that names the real status and error code with citations. The loop stops at 5 calls (test). | `make test-int`; real run in the PR |
| P7-05 | **Actions.** `ProposedAction` in drafts and handoff; `act` node runs approved Jira actions; `handoff_prep` sends the Slack alert for high-urgency handoffs (before the pause); `POST /console/tickets/{id}/actions`. | M | Dispute ticket → approval route with a draft and a prepared Jira issue → operator approves → issue key stored in `tool_calls.external_ref` and shown on the ticket. | `make test-int` |
| P7-06 | **Run on the 20 tickets** with tools, only when the owner asks. (Experiment E6 removed by ADR-0018.) | S | Report committed; tickets with `needs_account_data` use account facts. | `make eval` |

**Gate 7:** account-specific answers work; no tool call can reach another customer's data (tests); the MCP server works from Claude Code.

---

## Phase 8 — Security

References: [security](security.md) (all); eval plan §3 (attacks).

| ID | Task | Size | Done when | Verify |
|---|---|---|---|---|
| P8-01 | **Attack suite.** `data/eval/attacks.jsonl` (16 items: 8 categories × 2), canary strings in internal articles and in the seed data of other customers (**from P2-05, deferred:** this is the next task that changes `data/seed/`; close two gaps in the seed validator `hub/domain/seed.py` first: check every `status_history` step against allowed transitions, not only the first and last entry and the order, and check daily limit totals per customer and day (all withdrawals incl. pending, USD/GBP and mass payouts; card and ATM daily limits; days counted in CET, when limits reset)), `make eval-attacks`, attack judge. Include the known limitations of card masking (security §3, "Known limitations of G1") as attack cases. **Human gate H6.** | M | Report with successes per category (counts). | `make eval-attacks` |
| P8-02 | **Hardening.** Fix every successful attack with a control in code where possible (not only prompt text). Complete G7 (forbidden promises, prompt fragments, canaries). | M | At most 1 of 16 attacks succeeds (red line) and that case is explained; target 0. | `make eval-attacks` |
| P8-03 | **Leak scan.** After an eval run: scan the database, logs and recorded model requests for card patterns and canaries (`make leak-scan`). Log and trace filter G9. **From the P2-04 review (deferred):** card masking (`guardrails/masking.py`) changes about 1 in 100 UUID strings, because dashes count as card separators and a Luhn-valid run of 13+ digits can appear across them (example: `0b74d664-3640-4097-8741-e5b0ef08c44e` becomes `0b74d[CARD ****8741]-e5b0ef08c44e`, see `test_text_masking_would_change_this_uuid` in [test_audit_chain.py](../apps/api/tests/unit/test_audit_chain.py)). The audit log keeps ids given as `uuid.UUID` objects (P2-04) and request ids avoid such values (P2-01), but ticket, customer and operator ids in log records and traces are still masked like this, so searching logs by id misses those lines. Decide with the owner how G1/G9 treat UUIDs in logs and traces (a change to a guardrail). | S | Scan result: 0. A planted leak in a test is found. A log record with the example UUID keeps the id readable, or the decision not to is written in security §3. | `make leak-scan` |
| P8-04 | **Web and auth hardening.** Security headers and CSP, cookie flags, CSRF tests, lockout, rate-limit tests, input length limits, safe Markdown rendering rule for the web app. | M | Tests for each control. | `make test-int` |
| P8-05 | **Privacy functions and scheduled jobs.** `POST /admin/jobs/{name}` with the job token: `retention`, `close-stale`, `recover-runs`, `ping`. **From P2-06:** `close-stale` changes the status of many tickets; a bulk `UPDATE` skips the status guard (must-do (3) of P2-07), so close them through the checked path and test that a bulk status change is refused. Erasure endpoint with report, privacy text endpoint. | M | After erasure no row contains the customer's messages, memory or name; the audit chain still verifies. | `make test-int` |
| P8-06 | **Supply chain and process.** pip-audit, pnpm audit, gitleaks, Dependabot; pin the Go version that the gitleaks pre-commit hook downloads on machines without Go (`language_version` in `.pre-commit-config.yaml`; today pre-commit takes the latest Go from go.dev); security checklist in the PR template; threat model table updated with the test that covers each threat. | S | CI jobs green; each threat T1–T15 names its test. | CI run link |

**Gate 8:** PAN leakage 0; internal leak 0; attack success 0 of 16 (or 1 documented exception).

---

## Phase 9 — LLMOps

References: [operations](operations.md) §3–4; [AI pipeline](ai-pipeline.md) §6; ADR-0009, 0014.

| ID | Task | Size | Done when | Verify |
|---|---|---|---|---|
| P9-01 | **Tracing.** OpenTelemetry + Langfuse: trace per run, spans per node, retrieval, tool and model call; attributes from operations §3; masked payloads; trace id in `runs`; off when keys are empty. | M | A run shows the full tree in Langfuse; a card number in a test ticket is masked in the trace. | screenshot in the PR |
| P9-02 | **Metrics and settings API.** `/console/metrics/*`, `/console/evals/latest`, `/admin/settings` with audit of changes; kill switch honoured by `decide`. | M | Numbers match SQL checks on seeded data. Switching auto-reply off sends the next ticket to approval. | `make test-int` |
| P9-03 | **Confidence (rule score).** No trained model (ADR-0018 replaces layer 2 of ADR-0009): `config/confidence.json` keeps the rule version from AI pipeline §6 (`confidence = groundedness` when `top_score` is above a fixed value, else 0). Experiment E9 in reduced form: for a few candidate thresholds, how many of the 20 tickets would be auto-replied and which of those were wrong, as counts with intervals. Auto-reply stays off by default. **Human gate H7:** the owner decides whether auto-reply goes on in the demo. | S | E9 row with the counts; the decision recorded in the experiments file. Runtime score matches the rule on hand-made rows (test). | report + `make test` |
| P9-04 | **Cost check.** Prompt caching check (cache reads in `llm_calls`); cost per ticket from the last run against NFR-3; project spend so far against the $5 budget. (Experiment E8 removed by ADR-0018.) | S | Cost per ticket ≤ €0.05 or an explanation; spend so far stated. | `make eval-report` |
| P9-05 | **Operations.** Alerts (operations §3), `docs/runbook.md`, `make export-review-candidates`. No scheduled evaluation workflow (ADR-0018). | M | Runbook covers the five situations. A forced budget alert appears in the Slack mock. | `make test-int` |
| P9-06 | **Milestone run.** One run with `--no-cache` on the 20 tickets and the 16 attacks with the chosen configuration (there is no test set, ADR-0018); results into the README, marked "demo check, not a proof". | S | Table with target / red line / result for every metric, with counts and intervals; failures stated openly. | report file |

**Gate 9:** milestone numbers published as a demo check; auto-reply decision made by Viktoriia and recorded in an ADR or the experiments file.

---

## Phase 10 — Web app and deployment

References: [UI](ui.md); [operations](operations.md) §5–6; ADR-0015, 0016.

| ID | Task | Size | Done when | Verify |
|---|---|---|---|---|
| P10-01 | **Web scaffold.** Vite + React + TypeScript strict, Tailwind, router, TanStack Query, generated API types, auth flow, layouts, error and loading patterns. | M | `pnpm build` and `tsc` clean; login works against the local API. | `make lint && pnpm test` |
| P10-02 | **Customer chat.** Everything in UI §2, including the SSE reader with polling fallback. **From the P2-06 review (deferred):** the rule "one feedback per AI message" is enforced only by a row lock in the API (`POST /tickets/{id}/feedback`); add a unique constraint on `feedback.message_id` (new migration) so the database enforces it too, and an API-level test with two concurrent feedback requests for one message (the existing test proves only the repository lock). | L | All states from UI §2 exist and are reachable in tests. | Playwright |
| P10-03 | **Operator console.** Queue and ticket workspace (UI §3), draft editor with citation chips, actions, conflict banner, shortcuts. | L | An operator can do the full review flow without the API docs. | Playwright |
| P10-04 | **KB search, dashboard, settings, audit pages.** UI §3–5. | M | Dashboard numbers equal the API response; settings change needs a reason. | Playwright |
| P10-05 | **E2E and accessibility.** The Playwright scenarios from UI §6 in CI with fake models; axe checks. | M | All scenarios green in CI. | CI run link |
| P10-06 | **Production images.** Multi-stage `Dockerfile.api` (includes the built web app) and `Dockerfile.mcp`, non-root; the API image contains the local embedding model, so the container needs no network for embeddings (ADR-0018); `infra/compose.prod.yml` + Caddy as the VPS fallback. | S | `docker compose -f infra/compose.prod.yml up` serves the app on one port. | manual check in the PR |
| P10-07 | **Cloud deploy.** Check current free-tier limits and update operations §6. Cloud Run services, Neon, Qdrant Cloud, secrets, `deploy.yml` (build → migrate → deploy → smoke → rollback on failure), scheduled workflows that call the job endpoints (close-stale, recover-runs, retention, ping). **Human gate H8.** **From the P2-02 review:** migration 0001 has only run with a superuser migrator; run it on Neon as the database owner (not a superuser): `CREATE ROLE hub_app` when missing, `REVOKE CREATE ON SCHEMA public FROM PUBLIC`, `ALTER DEFAULT PRIVILEGES`; then create the app login user (operations §2, "Database roles"). **From P2-06 (deferred):** the rate limiter (`hub/api/limits.py`) counts in process memory, so with Cloud Run max 2 instances (operations §6) a customer can get up to twice the limits of API §1; choose max instances 1 or a shared limiter in Postgres. The limits key on the client IP (`request.client.host`): run uvicorn with `--proxy-headers` and `--forwarded-allow-ips` set for the Cloud Run proxy, otherwise every customer shares the proxy's address and the per-IP limit. **From the P2-06 review (deferred):** a retry with the same `Idempotency-Key` (a replay) still counts against the rate limits, so a client that retries after a network drop uses up its budget; and the 32 KB body cap is below a 4,000-character message of characters above U+FFFF written as JSON escapes (12 bytes each, about 48 KB), so 4,000 escaped emoji get `413` instead of reaching the 422 check. | L | Public URL works end to end. `make rollback` returns the previous revision. On Neon, `make migrate` succeeds as the owner and the app user cannot `UPDATE` `audit_log`. | URL + CI run link |
| P10-08 | **Load smoke.** Script with 10 parallel customers and 50 tickets against a deployment of the demo stack with fake models only (`LLM_MODE=fake`; no real-model cost, ADR-0018). | S | p50/p95 latency and error rate in the README, labelled "fake models: model time not included"; no lost runs. | script output |

**Gate 10:** a stranger can open the URL, enter the access code, ask a question as a customer, then log in as an operator and approve a draft.

---

## Phase 11 — removed

Removed by [ADR-0018](adr/0018-budget-mode.md): the fine-tuning experiment (E10) needed the test set and a training set of about 1,500 messages, which the budget does not cover.

---

## Phase 12 — Portfolio package

| ID | Task | Size | Done when |
|---|---|---|---|
| P12-01 | **README final.** One page: what it is, demo link and access, architecture diagram, numbers table (milestone run on the 20 tickets, marked "demo check, not a proof", with the same-family judge note) with targets and red lines, the questions not measured (E1–E8, E10), what does not work, limits of synthetic data, how to run locally in 5 commands. Also write `docs/requirements-trace.md` (each PRD requirement → the test or metric that proves it). | S | Passes the checklist in section 3. |
| P12-02 | **Case study** `docs/case-study.md`: problem → approach → data → architecture → evaluation → routing threshold and business numbers → security → what failed → what next. About 1,500 words. | M | Every number links to a report. |
| P12-03 | **Demo script** `docs/demo-script.md` for a 3–5 minute video: routine ticket, card number masked, fraud handoff, operator approval with Jira, dashboard and kill switch, a trace. **Human gate H9** (recording). | S | Script runs against the demo with the listed demo customers. |
| P12-04 | **Interview notes** `docs/interview-notes.md`: 25 likely questions about this system with short answers that point to the ADRs, experiments and walkthroughs. | S | Covers design, evaluation, RAG, agents, security, cost, working with coding agents. |

---

## 3. Definition of done for the whole project

"Production-ready" for this project means:

- [ ] Clean clone → `make dev` → working system in under 10 minutes with documented steps.
- [ ] All **M** requirements of the PRD have a passing acceptance test or eval metric (trace table in `docs/requirements-trace.md`: requirement → test or metric).
- [ ] Every NFR has a measured value in the README next to its target and red line.
- [ ] Milestone results published as a demo check, including the ones that miss the target.
- [ ] Safety modules ≥ 95% coverage; PAN leakage 0; attack suite within limits.
- [ ] Every model call is traced and costed; the dashboard works on the demo.
- [ ] CI: lint, types, tests, e2e, security scans — all required for merge (no real model calls in CI; eval smoke by hand, ADR-0018).
- [ ] Public demo deployed from CI with rollback; budget guard on.
- [ ] 16+ ADRs current; design documents match the code; one walkthrough per phase.
- [ ] No secret in the repository history; licence present; fictional-company notice in the README.
