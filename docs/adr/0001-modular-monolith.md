# ADR-0001: Modular monolith, not microservices

- Status: accepted
- Date: 2026-11-12

## Context
The earlier version of this project was planned as event-driven microservices (ingestion, retrieval, resolution, MCP bridge) with Redis Streams. One person builds and runs the system. The load target is 2,000 tickets per day (NFR-9). No requirement needs independent scaling or separate teams.

## Decision
Build one backend service (`hub`) with clear internal modules and import rules checked in CI. Keep one separate process only where a real boundary exists: the MCP tools server.

## Alternatives considered
- **Microservices with an event bus.** More infrastructure, distributed failure modes and tracing work, with no requirement that pays for it.
- **One process including the tools.** Loses the MCP boundary, which is a requirement (FR-14) and lets the same tools be used from Claude Code.

## Consequences
Simple local setup and deployment; one place to debug. If a module must be split later, the import rules keep the cut clean. Risk: modules grow together — import-linter stops this.
