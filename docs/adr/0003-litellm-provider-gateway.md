# ADR-0003: LiteLLM as the gateway to several model providers

- Status: Superseded in part by ADR-0018 (several providers, cross-provider judge and fallback)
- Date: 2026-11-12

## Context
We want to compare models from different providers (experiment E8), use a judge from another provider than the writer, and have a fallback when a provider fails (NFR-10). Each provider has its own SDK and response format.

## Decision
All model calls go through one module, `hub/llm/gateway.py`, which uses the LiteLLM Python SDK (as a library, not the proxy server). Nodes ask for a **role** (`triage`, `write`, …); `config/models.yaml` maps roles to models and fallbacks. The gateway adds retries, timeouts, the outbound card-data scan, cost calculation, the `llm_calls` record, tracing and the fake mode for tests.

## Alternatives considered
- **One provider SDK only.** Simplest, but no cross-provider comparison or fallback.
- **LiteLLM proxy server.** Another service to run and secure; holds all keys in one network service.
- **LangChain chat models.** Heavier abstraction; we need only completion, tools, structured output and embeddings.

## Consequences
Changing a model is a config change plus an eval run. Risks: the library is a supply-chain point (a compromised PyPI release happened in 2026) — pin and lock the version, check advisories, keep the wrapper thin so the library can be replaced. Provider-specific features (prompt caching, structured output) must be checked per provider.
