# ADR-0002: Fixed workflow with one bounded agentic step (LangGraph)

- Status: accepted
- Date: 2026-11-12

## Context
Support tickets follow a predictable path: understand, find information, answer, check, route. Payments need predictable behaviour, limits on cost and time, and a pause for a human (FR-9, FR-13, NFR-1, NFR-3). A free agent that decides every step is harder to evaluate and to bound.

## Decision
Model the path as a LangGraph state graph with fixed nodes and edges. The model chooses freely in one node only — `research` — where it may call read-only tools, at most 5 times. One retry loop from `verify` back to `research` is allowed.

## Alternatives considered
- **A single ReAct agent with all tools.** Unpredictable cost and latency; a prompt injection could change the whole path; routing rules would live in a prompt.
- **Plain Python functions without a graph library.** Possible, but we would write our own checkpointing, pause/resume and streaming. LangGraph gives these and is asked for in job descriptions.
- **Fully deterministic retrieval, no tool loop.** Cannot adapt to tickets that need a second search or account data. Kept as the `no_tools` variant for comparison.

## Consequences
Each node can be evaluated alone. Latency and cost have upper limits. The graph is easy to draw and to explain. Trade-off: less flexible for unusual tickets — these go to a human.
