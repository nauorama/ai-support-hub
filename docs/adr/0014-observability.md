# ADR-0014: Tracing with Langfuse plus an own `llm_calls` table

- Status: accepted
- Date: 2026-11-12

## Context
We must see every step of a run, its cost and its latency (FR-16), build a dashboard (FR-17), and debug bad answers.

## Decision
Send OpenTelemetry traces to Langfuse (free cloud plan in the demo): one trace per run, spans for nodes, retrieval, tools and model calls. Separately, write one row per model call into Postgres (`llm_calls`) with tokens, cost, latency and versions. The dashboard and the budget guard read Postgres only.

## Alternatives considered
- **Only the tracing tool.** Dashboard and budget would depend on a vendor API and its limits.
- **Only own tables.** No step-by-step view of prompts and outputs.
- **Arize Phoenix or LangSmith.** Comparable. Langfuse is open source, has a free cloud plan and a self-host option.

## Consequences
Two places hold run data, with different jobs. Traces contain masked text and are kept 30 days. The system works with tracing switched off.
