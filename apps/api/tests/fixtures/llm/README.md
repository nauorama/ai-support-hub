# Recorded model responses (fake mode)

`LLM_MODE=fake` makes the gateway answer from these files (`hub/llm/gateway.py`, `FixtureProvider`).

- `requests/*.json`: the requests to record. One file per request: `{"role": ..., "messages": [...]}`,
  optionally `"tools"`, or `{"role": "embedding", "texts": [...]}`. Text must already be masked.
  There is no embedding request for now: embeddings come from a local model (ADR-0018), whose
  backend lands in P5-01; until then a real-mode embedding call fails, so it cannot be recorded.
- `<key>.json`: one response per model the role uses (model and fallback). The key is a hash of the
  request as the gateway builds it (`ProviderRequest.fixture_key`), so a changed prompt, model or
  parameter needs a new recording. A missing file is an error, never a real call.
- `"recorded": "synthetic"` marks a hand-written response that no model produced. Replace it with
  `make record-llm`: real calls through the gateway (G2 scan, retries, one `llm_calls` row per
  attempt with context `record`); needs `ANTHROPIC_API_KEY` and the database (`make dev`); costs a few cents.
