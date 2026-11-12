# ADR-0008: Human-in-the-loop with LangGraph interrupt and a Postgres checkpointer

- Status: accepted
- Date: 2026-11-12

## Context
Operators may answer minutes or hours after the AI run paused. The service may restart in between (NFR-11). After the decision the run must continue: execute approved actions, send the message, write memory and audit.

## Decision
The `human_review` node calls `interrupt()` with the data the operator needs. LangGraph saves the state with `langgraph-checkpoint-postgres` (thread id = ticket id). The console endpoints resume the run with `Command(resume=decision)`. The pause is also visible in our own tables (`runs.status = interrupted`, `drafts.status = pending`), which the console reads — the UI never reads checkpoint tables.

## Alternatives considered
- **End the run at the draft and start a second 'send' function.** Simple, but the workflow is then split in two places and loses its state between them.
- **In-memory checkpointer.** Lost on restart.

## Consequences
One workflow from message to final answer. Checkpoint tables grow — the retention job deletes checkpoints of closed tickets. Nodes before the interrupt must be safe to re-run (idempotent side effects).
