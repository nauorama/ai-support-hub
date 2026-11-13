# API Contract

Base path `/api/v1`. JSON, `snake_case`. FastAPI generates the OpenAPI file; this document is the contract the code must match. The web app generates its TypeScript types from the OpenAPI file (`make web-types`).

## 1. Conventions

- **Errors:** RFC 9457 problem details, media type `application/problem+json`: `{"type": "about:blank", "title": "Conflict", "status": 409, "detail": "...", "code": "stale_draft"}`. `type` is always `about:blank`, so `title` is the standard HTTP status phrase (`HTTP 499` for a status without one) and `code` carries the meaning. Codes:
  - codes named by an endpoint in this document, e.g. `stale_draft`;
  - `validation_error` (422): the request does not match the schema. Extension member `errors`: a list of `{"loc": [...], "type": "...", "msg": "..."}`. Submitted values are never repeated; `loc` can name a key the client sent;
  - `internal_error` (500): unexpected error. `detail` is generic; quote the `X-Request-ID` to find the log line;
  - any other HTTP error: the status phrase in snake case, e.g. `not_found`, `method_not_allowed`, `too_many_requests` (`http_<status>` for a status without a phrase).
- **Request id:** every response has an `X-Request-ID` header. A caller's own `X-Request-ID` is kept when it has 1–64 characters from `A-Z a-z 0-9 . _ -`, no run of 13 or more digits, and nothing the card-data mask (G1, which also masks the logs) would change, so the id reads the same in the logs; otherwise the API generates one (32 hex characters, also without such a run). Every log line of the request carries it as `request_id`, exactly as in the header.
- **Auth:** two cookie sessions, both `HttpOnly`, `Secure`, `SameSite=Lax`, signed.
  - `hub_customer` — demo customer session (stands for the login of the Tallybridge app).
  - `hub_operator` — operator session. State-changing console requests also need the `X-CSRF-Token` header.
- **Scheduled jobs:** `POST /admin/jobs/{name}` accepts a bearer token (`JOBS_TOKEN`) instead of a session, because GitHub Actions calls it. The token works only for these endpoints.
- **Roles:** `operator` < `lead` < `admin`; `compliance` can read tickets and the audit log, and cannot change anything.
- **Idempotency:** `Idempotency-Key` header (UUID) on every POST that creates a message or runs an action. The same key returns the first result.
- **Pagination:** `?limit=50&cursor=...`; response `{"items": [...], "next_cursor": "..."}`.
- **Rate limits (demo):** customer endpoints 10 messages per minute per session and 60 per hour per IP; login 5 tries per 15 minutes. Over the limit → 429 with `Retry-After`.
- **Time:** ISO 8601 UTC.

## 2. Customer endpoints

| Method | Path | Purpose |
|---|---|---|
| POST | `/demo/session` | Body `{"customer_ref": "CUS-0007", "access_code": "..."}`. Sets `hub_customer`. Returns the customer profile for the header of the chat. |
| GET | `/demo/customers` | List of demo customers for the picker (name, tier, short scenario hint). Needs the access code in the header `X-Demo-Access-Code`. |
| POST | `/tickets` | Body `{"message": "..."}`. Creates a ticket and starts a run. **Response is an SSE stream** (section 4). |
| POST | `/tickets/{id}/messages` | Body `{"message": "..."}`. Adds a message. SSE stream. Starts a run only when the AI owns the ticket. A closed ticket returns `409 ticket_closed`; the chat then opens a new ticket. |
| GET | `/tickets` | The customer's own tickets. |
| GET | `/tickets/{id}` | Ticket with status and the AI disclosure text. |
| GET | `/tickets/{id}/messages?after={message_id}` | New messages. The chat polls this every 3 s while the ticket is not closed and no stream is open. |
| POST | `/tickets/{id}/request-human` | Immediate handoff with reason `customer_request`. |
| POST | `/tickets/{id}/feedback` | Body `{"message_id": "...", "rating": "up" | "down", "comment": "..."}`. |
| GET | `/help/{doc_id}` | Public. One `external` knowledge-base article (title, sections with anchors). `internal` documents return 404. Target of citation links. |
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
  "created_at": "2026-11-12T10:15:04Z"
}
```
Internal fields (chunk ids, scores, confidence, route reasons) are never in customer responses.

## 3. Console endpoints

All need `hub_operator`. Minimum role in brackets.

| Method | Path | Purpose |
|---|---|---|
| POST | `/auth/login` | `{"email", "password"}` → sets cookie, returns operator + CSRF token. |
| POST | `/auth/logout` | |
| GET | `/auth/me` | Current operator. |
| GET | `/console/tickets` [operator] | Filters: `status`, `intent`, `urgency`, `assigned=me\|none\|any`, `route_reason`, `q`. Default sort: urgency desc, waiting time desc. |
| GET | `/console/tickets/{id}` [operator] | Full ticket: messages, latest run, draft, evidence, route reasons, confidence, customer profile, memory, tool calls, trace URL, `version`. |
| POST | `/console/tickets/{id}/claim` [operator] | Assign to me. 409 when another operator has it (lead can override with `?force=true`). |
| POST | `/console/tickets/{id}/release` [operator] | Unassign. |
| POST | `/console/drafts/{id}/approve` [operator] | Body `{"ticket_version": 7, "approved_actions": ["a1"]}`. Sends the draft unchanged, resumes the run. |
| POST | `/console/drafts/{id}/edit` [operator] | Body `{"ticket_version", "body", "approved_actions"}`. Sends the edited text. The server masks card data and checks that citation markers still exist. |
| POST | `/console/drafts/{id}/reject` [operator] | Body `{"ticket_version", "reason", "note", "reply_body": null}`. With `reply_body` the operator's own answer is sent; without it the ticket stays `handed_off`. |
| POST | `/console/tickets/{id}/reply` [operator] | Free reply on a handed-off or answered ticket. The first reply on a handed-off ticket resumes the paused run ([ai-pipeline](ai-pipeline.md) §1); body may include `approved_actions`. |
| POST | `/console/tickets/{id}/actions` [operator] | Run a tool action by hand: `{"type": "jira_create_issue", "params": {...}}`. |
| POST | `/console/tickets/{id}/close` [operator] | |
| GET | `/console/kb/search?q=` [operator] | Hybrid search, includes `internal` articles. |
| GET | `/console/kb/documents/{id}` [operator] | Article text. |
| GET | `/console/metrics/summary?from=&to=` [lead] | Numbers for the dashboard (section 5). |
| GET | `/console/metrics/timeseries?metric=&bucket=day` [lead] | |
| GET | `/console/evals/latest` [lead] | Summary of the newest committed eval report. |
| GET | `/console/audit` [compliance, lead] | Filters: `entity_type`, `entity_id`, `actor_id`, `action`, `from`, `to`. |
| GET | `/console/audit/verify` [compliance, lead] | Checks the hash chain; returns the first broken id or `ok`. |
| GET / PUT | `/admin/settings` [lead] | `auto_reply_enabled`, `confidence_threshold`, `daily_budget_usd`, `intent_allowlist` (may only remove intents from the list in `config/policy.yaml`; adding another intent returns 422). |
| POST | `/admin/kb/reindex` [admin] | Reload `data/kb` and rebuild changed chunks. |
| POST | `/admin/runs/recover` [admin] | Resume runs stuck in `running`. |
| POST | `/admin/jobs/{name}` [job token] | `name` = `retention` \| `recover-runs` \| `close-stale` (close `answered` tickets after 72 h, write memory summaries) \| `ping` (touches Postgres and Qdrant so the free services stay awake). Returns a short report. |
| DELETE | `/admin/customers/{id}/data` [admin] | Erasure (see [security](security.md) §6). |

Conflict rule: every state-changing draft or ticket request sends `ticket_version`. If the ticket changed (for example the customer wrote again), the server returns `409 stale_draft`.

## 4. SSE stream for a customer message

`POST /tickets` and `POST /tickets/{id}/messages` answer with `Content-Type: text/event-stream`. The browser reads it with `fetch` and a stream reader (not `EventSource`, because it is a POST).

| Event | Data | When |
|---|---|---|
| `ticket.created` | `{"ticket_id", "message": {...}}` | after the message is masked and saved |
| `run.status` | `{"step": "understanding" \| "searching" \| "writing" \| "checking"}` | when a workflow step starts; friendly labels only, no internals |
| `message` | message object | the AI answer or the handoff notice |
| `ticket.status` | `{"status": "answered" \| "pending_approval" \| "handed_off"}` | |
| `error` | problem details | the run failed; the ticket was handed off |
| `done` | `{}` | always the last event |

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
| `GET /ready` | Postgres, Qdrant and the MCP server answer. Returns the state of each; 503 when one does not. |

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

Adapters: `core` reads `data/seed` (always a mock). `jira` and `slack` have two implementations chosen by `JIRA_MODE` / `SLACK_MODE` = `mock` (default; writes to a local table and log) or `real` (Jira Cloud REST API, Slack Web API with a bot token).

How the Hub exposes tools to the model during research (see [ai-pipeline](ai-pipeline.md) §4): only the four read tools plus `search_kb`, **without** the `customer_ref` argument. The Hub adds `customer_ref` from the session. Tools with side effects are never given to the model as callable tools.
