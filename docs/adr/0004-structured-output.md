# ADR-0004: Structured output with provider JSON schema and Pydantic validation

- Status: accepted
- Date: 2026-11-12

## Context
Triage, draft and verification results must be machine-readable and valid (FR-4, FR-7, FR-8). Free text parsing is fragile.

## Decision
Define every model output as a Pydantic model. The gateway passes the JSON schema to the provider's structured-output feature through LiteLLM, validates the result with Pydantic and, on failure, makes one repair call that includes the validation error. A second failure fails the node (handoff).

## Alternatives considered
- **Instructor library.** Good, but one more dependency on top of LiteLLM for a small amount of code we can own.
- **Tool-call trick for structured output.** Works everywhere but mixes with real tools in the research node; kept as a fallback inside the gateway for models without native structured output.
- **Parse JSON from free text.** Unreliable.

## Consequences
Schemas are the contract between nodes and are reused by the eval code. Keep schemas flat and small; provider limits on schema features must be checked.
