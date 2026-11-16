# ADR-0005: Qdrant with hybrid search (dense + BM25, RRF)

- Status: Superseded in part by ADR-0018 (experiments E1–E3)
- Date: 2026-11-12

## Context
Tickets contain exact tokens (error codes like ERR-2003, product names, 'SWIFT') and also loose wording ('my money is stuck'). Dense search is weak on exact codes; keyword search is weak on paraphrase (FR-5, retrieval targets in the eval plan).

## Decision
Store chunks in Qdrant with two named vectors: a dense embedding and a BM25 sparse vector. Query both and fuse with reciprocal rank fusion in the Query API. Filter by `visibility`. Reranking is off by default and tested in experiment E3.

## Alternatives considered
- **pgvector in the existing Postgres.** One service less. Hybrid search needs extra work (full-text search + own fusion). A real option; rejected because Qdrant's hybrid query and payload filters are simpler, and vector-database experience is a hiring signal. Revisit if operations cost matters.
- **Dense only.** Fails on error codes; measured in E2.
- **No retrieval: whole knowledge base in the prompt.** Fine at 50 articles; cost and latency grow with the base. Kept as baseline B0 and measured in E1.

## Consequences
One more service to run (free cluster in the demo). The index can always be rebuilt from Postgres. If B0 wins on quality at this size, the README says so openly and argues with cost, latency and growth.
