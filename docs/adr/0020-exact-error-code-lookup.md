# ADR-0020: Exact error-code lookup in hybrid search

- Status: accepted
- Date: 2026-11-17
- Adds to: [ADR-0005](0005-qdrant-hybrid-search.md) (hybrid search). Owner decision during P5-02.

## Context
ADR-0005 expects BM25 to catch exact tokens such as `ERR-2003`, and P5-02 requires a query with `ERR-2003` to return the error-code chunk `error-codes#ERR-2003` first in hybrid mode. On the real index it does not:
- FastEmbed's BM25 tokenizer splits `ERR-2003` into `err` and `2003`, so `err` matches every error code;
- three articles mention ERR-2003 too (`withdrawal-failed-or-returned`, `withdrawal-bank-details`, `withdrawal-times`);
- the small local dense model (ADR-0018) barely tells codes apart.

Measured hybrid rank of `error-codes#ERR-2003`: 4th for the query "ERR-2003", 6th for "i got err-2003 from the app". Indexing codes as one BM25 token only improved this to 2nd and 4th.

## Decision
In hybrid mode, the retriever (`hub/kb/retriever.py`) looks up every error code in the query (`ERR-` + 4 digits, any case, in order, without repeats). Each one that exists in the index goes first as the chunk `error-codes#<CODE>`. Then the fused hits follow without those chunks, up to `top_k`.
- The pinned hit obeys the visibility rule like every other hit.
- **The pinned hit gets the top fused score** of the same query (0 when the fusion found nothing). Scores therefore never rise above what the fusion gave and stay in rank order. Phase 6 reads `top_score` for the confidence rule (ai-pipeline §6), so a code in the query raises confidence only as far as the best fused hit already did.
- Dense mode (`rag_dense`) has no lookup; it stays the pure dense baseline.

## Alternatives considered
- **Codes as one BM25 token** (`ERR2003` in documents and queries): better, but still not first (see above).
- **A third prefetch on the code chunk inside RRF:** RRF only adds rank fractions, so the chunk still loses to a hit that is high in both other lists.
- **Change the chunk text** (repeat the code): changes ADR-0006's chunk format for a ranking effect.
- **Leave the requirement unmet:** the error code is the strongest signal a customer can give.

## Consequences
- A question naming a code always cites the code's own explanation first, which is what an operator would do.
- A ticket that names a code but asks about something else still gets the code chunk first; the other hits follow.
- The rule is code, not ranking, so it is deterministic and tested with fake embedders (unit) and on the real index (integration).
