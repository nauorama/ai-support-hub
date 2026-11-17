# API Contract

Base path `/api/v1`. JSON, `snake_case`. FastAPI generates the OpenAPI file; this document is the contract the code must match. The web app generates its TypeScript types from the OpenAPI file (`make web-types`).

## 1. Conventions

- **Errors:** RFC 9457 problem details, media type `application/problem+json`: `{"type": "about:blank", "title": "Conflict", "status": 409, "detail": "...", "code": "stale_draft"}`. `type` is always `about:blank`, so `title` is the standard HTTP status phrase (`HTTP 499` for a status without one) and `code` carries the meaning. Codes:
  - codes named by an endpoint in this document, e.g. `stale_draft`;
  - `validation_error` (422): the request does not match the schema. Extension member `errors`: a list of `{"loc": [...], "type": "...", "msg": "..."}`. Submitted values are never repeated; `loc` can name a key the client sent;
  - `internal_error` (500): unexpected error. `detail` is generic; quote the `X-Request-ID` to find the log line;
  - any other HTTP error: the status phrase in snake case, e.g. `not_found`, `method_not_allowed`, `too_many_requests` (`http_<status>` for a status without a phrase).
- **Request id:** every response has an `X-Request-ID` header. A caller's own `X-Request-ID` is kept when it has 1–64 characters from `A-Z a-z 0-9 . _ -`, no run of 13 or more digits, and nothing the card-data mask (G1, which also masks the logs) would change, so the id reads the same in the logs; otherwise the API generates one: 32 lowercase letters `a`–`p` (a random UUID's hex digits mapped to letters), so it has no digits and the mask can never change it. Every log line of the request carries it as `request_id`, exactly as in the header.
- **Auth:** two cookie sessions, both `HttpOnly`, `Secure`, `SameSite=Lax`, signed.
  - `hub_customer` — demo customer session (stands for the login of the Tallybridge app).
  - `hub_operator` — operator session (8 hours from login). Every request that is not `GET`/`HEAD`/`OPTIONS` on `/console`, `/admin` and `/auth/logout` also needs the `X-CSRF-Token` header with the token from `POST /auth/login` or `GET /auth/me`. Answers: no or invalid session → `401 unauthorized`; missing or wrong token → `403 csrf_failed`; role not allowed → `403 forbidden` ([security](security.md) §4).
- **Scheduled jobs:** `POST /admin/jobs/{name}` accepts a bearer token (`JOBS_TOKEN`) instead of a session, because GitHub Actions calls it. The token works only for these endpoints.
- **Roles:** `operator` < `lead` < `admin`; `compliance` can read tickets and the audit log, and cannot change anything.
- **Idempotency:** `Idempotency-Key` header (UUID) on every POST that creates a message or runs an action; a missing or malformed key is a `validation_error`. The same key returns the first result. Keys are scoped to the caller (a customer's key never returns another customer's result). The same key sent with a different request (other text, other ticket) returns `409 idempotency_key_reused`.
- **Pagination:** `?limit=50&cursor=...`; response `{"items": [...], "next_cursor": "..."}`.
- **Rate limits (demo):** customer endpoints 10 messages per minute per session and 60 per hour per IP; login 5 tries per 15 minutes (for the demo session: 5 wrong access codes per 15 minutes per IP). Over the limit → 429 with `Retry-After`.
- **Input limits:** a request body is at most 32 KB (`413 content_too_large`, checked before the body is parsed). A customer message is at most 4,000 characters (`422 validation_error`, checked before masking, security T9). Text with a NUL character is refused with `422 validation_error` (Postgres cannot store it).
- **Time:** ISO 8601 UTC.

## 2. Customer endpoints

| Method | Path | Purpose |
|---|---|---|
| POST | `/demo/session` | Body `{"customer_ref": "CUS-0007", "access_code": "..."}`. Sets `hub_customer` (8 hours). Returns the customer profile for the header of the chat (`customer_ref`, `display_name`, `tier`). Wrong code → `401 invalid_access_code`; unknown or erased customer → 404; `SESSION_SECRET` or `DEMO_ACCESS_CODE` empty → `503 demo_unavailable`. |
| GET | `/demo/customers` | List of demo customers for the picker (name, tier, short scenario hint). Needs the access code in the header `X-Demo-Access-Code`. |
| POST | `/tickets` | Body `{"message": "..."}`. Creates a ticket and starts a run. **Response is an SSE stream** (section 4). |
| POST | `/tickets/{id}/messages` | Body `{"message": "..."}`. Adds a message. SSE stream. Starts a run only when the AI owns the ticket. A closed ticket returns `409 ticket_closed`; the chat then opens a new ticket. |
| GET | `/tickets` | The customer's own tickets, newest first (pagination). |
| GET | `/tickets/{id}` | Ticket with status and the AI disclosure text. |
| GET | `/tickets/{id}/messages?after={message_id}` | New messages. The chat polls this every 3 s while the ticket is not closed and no stream is open. |
| POST | `/tickets/{id}/request-human` | Immediate handoff with reason `customer_request`. Returns the ticket. A ticket that already waits for a human (`handed_off`, `pending_approval`) is returned unchanged; a closed ticket returns `409 ticket_closed`. |
| POST | `/tickets/{id}/feedback` | Body `{"message_id": "...", "rating": "up" | "down", "comment": "..."}` (comment optional, max 1,000 characters, masked). 201 with `{"id"}`. Only AI messages of this ticket: another message → `422 not_ai_message`. One rating per message: only the first rating is kept. Any later request for the same message (a double click, another rating, a new comment) stores nothing and returns the first rating's `{"id"}` with 200. |
| GET | `/help/{doc_id}` | Public. One `external` knowledge-base article: `{"id", "title", "url", "updated_at", "sections": [{"anchor", "heading", "text"}]}`, sections in file order (anchors: [data model](data-model.md) §2). `internal` and unknown documents return the same `404 not_found`. Target of citation links (`/help/<doc_id>#<anchor>`). |
| GET | `/privacy` | Public. Short privacy text: what the AI sees, which providers process the data, how to reach a human. |

A customer can read only their own tickets (404 for others, not 403).

**Message object (customer view)**
```json
{
  "id": "0193…",
  "role": "assistant",
  "author_label": "AI assistant",
  "is_ai_generated": true,
  "body": "Your withdrawal of €420.00 from 9 November is still in progress… [1]",
  "citations": [{"marker": 1, "title": "How long does a withdrawal take?", "url": "/help/withdrawal-times#sepa-withdrawals"}],
  "masked": false,
  "created_at": "2026-11-12T10:15:04Z"
}
```
`masked` is true when the server hid card data in the message (not for a partly masked IBAN, ADR-0017) (the chat shows a note, [UI](ui.md) §2). Internal fields (chunk ids, scores, confidence, route reasons) are never in customer responses.

**Ticket object (customer view):** `id`, `status`, `ai_disclosure` (the text of [security](security.md) §7), `ai_disclosed_at`, `created_at`.

## 3. Console endpoints

All need `hub_operator`. Minimum role in brackets.

| Method | Path | Purpose |
|---|---|---|
| POST | `/auth/login` | `{"email", "password"}` → sets cookie, returns `{"operator": {"id", "email", "name", "role"}, "csrf_token"}`. Every failure (unknown email, inactive, wrong password, locked account) → `401 invalid_credentials`; 5 failures per 15 minutes per IP → 429; `SESSION_SECRET` empty → `503 login_unavailable`. |
| POST | `/auth/logout` | 204. Ends **every** session of this operator (the stored `session_version` moves on), not only this browser's. Needs the CSRF token. |
| GET | `/auth/me` | Current operator, same shape as the login answer (`operator`, `csrf_token`), so the console gets the token again after a page reload. |
| GET | `/console/tickets` [operator] | Filters: `status` (repeatable), `intent`, `urgency`, `assigned=me\|none\|any`, `route_reason`, `q` (summary, customer name or reference). Without `status` every status is listed, `processing` included. Default sort: urgency desc (none last), waiting time desc; a ticket waits since its last customer message. Pagination with an opaque cursor. Compliance can read. |
| GET | `/console/tickets/{id}` [operator] | Full ticket: messages, latest run, draft, evidence, route reasons, confidence, customer profile, memory, tool calls, trace URL, `version`. Fields of later phases are `null` or `[]` until then. Compliance can read. |
| POST | `/console/tickets/{id}/claim` [operator] | Body `{"ticket_version"}`. Assign to me. 409 `conflict` when another operator has it (lead can override with `?force=true`; an operator with `force` → 403). A `processing` ticket is handed off first (reason `operator_takeover`, [ai-pipeline](ai-pipeline.md) §6). Closed → `409 ticket_closed`. |
| POST | `/console/tickets/{id}/release` [operator] | Body `{"ticket_version"}`. Unassign. Only the operator who claimed it, or a lead. |
| POST | `/console/drafts/{id}/approve` [operator] | Body `{"ticket_version": 7, "approved_actions": ["a1"]}`, `Idempotency-Key` required. Sends the draft unchanged as an AI message with the label "AI assistant · reviewed by <name>" (`pending_approval → answered`, resolution `ai_approved`), resumes the run (phase 6). A draft with `can_answer = false` cannot be approved unchanged: `409 draft_cannot_answer` (edit and send it, or reject it). |
| POST | `/console/drafts/{id}/edit` [operator] | Body `{"ticket_version", "body", "approved_actions"}`, `Idempotency-Key` required. Sends the edited text as an operator message ("<name> · Support team", resolution `ai_edited`). The server masks card data (G1); every `[n]` marker in the text must have a citation in the draft (otherwise `422 validation_error`, error type `unknown_citation_marker`), and citations whose marker was deleted are not sent. Text limit as for a customer message. An edit whose masked text equals the draft, apart from leading and trailing whitespace, is an approve (the draft text is sent): the label follows who wrote the text. |
| POST | `/console/drafts/{id}/reject` [operator] | Body `{"ticket_version", "reason", "note", "reply_body": null}`, `Idempotency-Key` required. `reason`: `wrong_facts` \| `wrong_tone` \| `missing_info` \| `policy` \| `other`; `note` at most 1,000 characters, masked. With `reply_body` the operator's own answer is sent (`answered`, resolution `human`); without it the ticket goes to `handed_off` and waits in the queue. A reject without a reply stores no message, so its repeat is recognised by the draft's state (rejected by the same operator with the same reason) and returns the first result. |
| POST | `/console/tickets/{id}/reply` [operator] | Body `{"ticket_version", "body"}`, `Idempotency-Key` required. Free reply on a handed-off or answered ticket (`handed_off → answered`); the text is masked (G1) and limited to 4,000 characters like a customer message. Returns the stored message. Another status → 409 (`processing`: claim first; `pending_approval`: use the draft endpoints); another operator's claim → `409 conflict`. The first reply on a handed-off ticket resumes the paused run ([ai-pipeline](ai-pipeline.md) §1, phase 6); body may include `approved_actions` (phase 7). |
| POST | `/console/tickets/{id}/actions` [operator] | Run a tool action by hand: `{"type": "jira_create_issue", "params": {...}}`. |
| POST | `/console/tickets/{id}/close` [operator] | Body `{"ticket_version"}`. Only `answered` or `handed_off` tickets (state diagram); another status → `409 conflict`. |
| GET | `/console/kb/search?q=&mode=&limit=` [operator] | Search over every chunk, `internal` included (compliance cannot use it). `q` 1–500 characters, masked (G1) before it is embedded; `mode` `hybrid` (default) or `dense`; `limit` 1–20, default 6. Returns `{"items": [{"chunk_id", "doc_id", "title", "section", "url", "visibility", "text", "score", "rank"}]}`. Qdrant or the local model not available → `503 kb_unavailable`. |
| GET | `/console/kb/documents/{id}` [operator] | Article text. |
| GET | `/console/metrics/summary?from=&to=` [lead] | Numbers for the dashboard (section 5). |
| GET | `/console/metrics/timeseries?metric=&bucket=day` [lead] | |
| GET | `/console/evals/latest` [lead] | Summary of the newest committed eval report. |
| GET | `/console/audit` [compliance, lead] | Filters: `entity_type`, `entity_id`, `actor_id`, `action`, `from`, `to`. |
| GET | `/console/audit/verify` [compliance, lead] | Checks the hash chain; returns the first broken id or `ok`. |
| GET / PUT | `/admin/settings` [lead] | `auto_reply_enabled`, `confidence_threshold`, `daily_budget_usd`, `intent_allowlist` (may only remove intents from the list in `config/policy.yaml`; adding another intent returns 422). |
| POST | `/admin/kb/reindex?full=false` [admin] | Reload `data/kb` into Postgres and sync the index (only changed chunks are embedded); `full=true` builds a new collection and moves the alias. Returns `{"full", "collection", "documents_added", "documents_updated", "documents_removed", "chunks_embedded", "chunks_payload_updated", "chunks_deleted"}`; audited as `kb.reindexed`. Invalid files → `422 kb_invalid` (nothing written); Qdrant or the model not available → `503 kb_unavailable`. |
| POST | `/admin/runs/recover` [admin] | Resume runs stuck in `running`. |
| POST | `/admin/jobs/{name}` [job token] | `name` = `retention` \| `recover-runs` \| `close-stale` (close `answered` tickets after 72 h, write memory summaries) \| `ping` (touches Postgres and Qdrant so the free services stay awake). Returns a short report. |
| DELETE | `/admin/customers/{id}/data` [admin] | Erasure (see [security](security.md) §6). |

Conflict rule: every state-changing draft or ticket request sends `ticket_version`. If the ticket changed (for example the customer wrote again), the server returns `409 stale_draft`. Claim, release and close answer with the ticket's queue row (with the new `version`); reply answers with the stored message; approve, edit and reject answer with `{"draft", "message", "ticket"}` (the reviewed draft, the sent message or `null`, the queue row). A draft that no longer waits (already reviewed or superseded, or the ticket is no longer `pending_approval`) → `409 conflict`; an already reviewed draft answers `conflict` even with an old `ticket_version` (checked before the version); another operator's claim → `409 conflict` (a lead may act on it); an `approved_actions` id that the draft does not propose → `422 validation_error`. Approved actions are recorded in the audit log; phase 7 runs them.

## 4. SSE stream for a customer message

`POST /tickets` and `POST /tickets/{id}/messages` answer with `Content-Type: text/event-stream`. The browser reads it with `fetch` and a stream reader (not `EventSource`, because it is a POST).

| Event | Data | When |
|---|---|---|
| `ticket.created` | `{"ticket_id", "message": {...}}` | after the message is masked and saved |
| `run.status` | `{"step": "understanding" \| "searching" \| "writing" \| "checking"}` | when a workflow step starts; friendly labels only, no internals |
| `message` | message object | the AI answer or the handoff notice |
| `ticket.status` | `{"status": "answered" \| "pending_approval" \| "handed_off" \| "processing" \| "closed"}` | the ticket's current status when the stream ends. `processing` only when no run worked on the ticket, or the run failed and even the handoff could not be saved: the ticket waits in the operator queue. `closed` when an operator closed the ticket while the run worked |
| `error` | problem details | the run failed; the ticket goes to a human |
| `done` | `{}` | always the last event |

`POST /tickets/{id}/messages` also starts with `ticket.created`, carrying the new message.

**Phase 3 (P3-05; search P5-03).** `POST /tickets` starts a run of the linear workflow (intake → triage → search → write). The stream is `ticket.created` → `run.status` (`understanding`, `searching`, `writing`; B0 has no `searching`) → `message` (a placeholder system message, not stored) → `ticket.status` (`pending_approval`) → `done`. Every run ends in `pending_approval` (no auto-reply yet). When the run fails, the ticket is handed to a human (reason `system_error`): `error` (problem details, `code` `internal_error`, a fixed `detail`) comes before the placeholder and `ticket.status` (`handed_off`). Every run stream ends with the placeholder, `ticket.status` and `done`: when no run starts (a human already has the ticket) `ticket.status` carries its current status, and when even the handoff cannot be saved it carries `processing`. The run is a task of its own: a closed stream does not stop it. `POST /tickets/{id}/messages`, a replayed `Idempotency-Key` and a new ticket whose masking failed start no run: their stream is `ticket.created` → placeholder → (`ticket.status` `handed_off` after a failed mask) → `done`. The LangGraph workflow (phase 6) replaces the linear run.

The server sends a comment line (`: ping`) every 10 s to keep the connection open. If the stream breaks, the client falls back to polling `GET /tickets/{id}/messages`.

## 5. Metrics summary response

```json
{
  "period": {"from": "2026-11-01", "to": "2026-11-12"},
  "tickets": {"total": 412, "auto_reply": 151, "approval": 139, "handoff": 122},
  "automation_share": 0.367,
  "ai_resolved_share": 0.311,
  "route_reasons": [{"reason": "policy_intent", "count": 61}, {"reason": "low_confidence", "count": 88}],
  "drafts": {"approved": 80, "edited": 41, "rejected": 18, "approved_unchanged_share": 0.576, "median_edit_ratio": 0.12},
  "latency_ms": {"p50": 5400, "p95": 13100},
  "cost_usd": {"total": 14.2, "per_ticket": 0.034, "by_node": {"triage": 0.9, "research": 5.1, "write": 6.8, "verify": 1.4}},
  "feedback": {"up": 96, "down": 14},
  "budget": {"daily_limit_usd": 3.0, "spent_today_usd": 0.42}
}
```

## 6. Health

| Path | Purpose |
|---|---|
| `GET /health` | Process is up. No dependencies. |
| `GET /ready` | Postgres, Qdrant and the MCP server answer, and the knowledge-base index holds one point per chunk in Postgres (`kb_index`, P5-01: `{"status": "ok", "postgres_chunks": n, "qdrant_points": n}`, or an error naming both counts). Returns the state of each; 503 when one fails. |

Both paths are at the root, outside `/api/v1`. They are not `/healthz` and `/readyz`: Cloud Run reserves some paths that end in `z` and recommends avoiding them (https://cloud.google.com/run/docs/issues).

## 7. MCP server: `support-tools`

Transport: streamable HTTP at `/mcp` (Hub → server, bearer token from `MCP_SERVER_TOKEN`) and stdio (for Claude Code). Every tool returns structured content that matches an output schema. Errors are returned as tool errors with a short safe message.

| Tool | Arguments | Result | Side effect |
|---|---|---|---|
| `core_get_customer` | `customer_ref` | tier, country, verification status, restriction flag (**no reason**), limits and how much is used | no |
| `core_list_transactions` | `customer_ref`, `type?`, `status?`, `limit≤20` | list: reference, type, amount, currency, status, dates, error code | no |
| `core_get_transaction` | `customer_ref`, `reference` | one transaction with status history and expected date | no |
| `core_get_cards` | `customer_ref` | cards: last 4 digits, type, status, delivery status | no |
| `jira_create_issue` | `issue_type` (`dispute` \| `fraud` \| `bug` \| `limit_request`), `summary`, `description`, `customer_ref`, `ticket_id`, `idempotency_key` | issue key and URL | yes |
| `jira_get_issue` | `key` | status, assignee | no |
| `slack_post_escalation` | `ticket_id`, `urgency`, `text`, `idempotency_key` | message timestamp | yes |

Adapters: `core` reads `data/seed` (always a mock). `jira` and `slack` are mocks too: they write to a local table and log. There is no real mode ([ADR-0019](adr/0019-scope-cuts.md)).

How the Hub exposes tools to the model during research (see [ai-pipeline](ai-pipeline.md) §4): only the four read tools plus `search_kb`, **without** the `customer_ref` argument. The Hub adds `customer_ref` from the session. Tools with side effects are never given to the model as callable tools.
