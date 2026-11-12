# ADR-0007: Process a ticket inside the HTTP request and stream progress with SSE

- Status: accepted
- Date: 2026-11-12

## Context
A run takes 5–20 s. The demo runs on a scale-to-zero platform that gives CPU only while a request is open. The load is small (NFR-9). The customer must see progress at once (NFR-2).

## Decision
`POST /tickets` and `POST /tickets/{id}/messages` run the workflow inside the request and stream events (Server-Sent Events). State is checkpointed after every node, so an interrupted run can be recovered by an admin endpoint or on the next read of the ticket.

## Alternatives considered
- **Queue + worker (Arq/Celery + Redis).** The standard answer for long jobs and higher load. Needs an always-on worker and Redis — no free tier fit, and no requirement at this load.
- **WebSockets.** Two-way channel is not needed; SSE over a POST response is simpler and passes proxies.
- **Client polling only.** Worse first-reaction time; no progress.

## Consequences
No Redis, no worker. Limits: a run is tied to a connection (recovery path exists); concurrency is limited by the instance. When load or run time grows, move the run into a worker — the graph and the checkpointer do not change, only who calls `graph.invoke`.
