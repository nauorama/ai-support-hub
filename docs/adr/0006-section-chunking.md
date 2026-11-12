# ADR-0006: Chunk knowledge-base articles by section

- Status: accepted
- Date: 2026-11-12

## Context
Articles are short (150–600 words) and written by us with `##` sections that make sense alone. Error codes are rows in a CSV.

## Decision
One chunk = one `##` section, with the article title and section heading added at the top of the chunk text. Sections over 400 tokens are split at paragraph borders. One error code = one chunk. No overlap.

## Alternatives considered
- **Fixed-size chunks with overlap.** Cuts tables and steps in the middle; measured in E4.
- **Whole article as one chunk.** Less precise citations; more tokens.

## Consequences
Citations point to a section the customer can open. The quality depends on how articles are written — the writing rules are in the data model document.
