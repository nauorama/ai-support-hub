# ADR-0018: Budget mode — one provider, local embeddings, a small evaluation

- Status: accepted
- Date: 2026-11-16
- Supersedes: parts of [ADR-0003](0003-litellm-provider-gateway.md) (several providers, cross-provider judge and fallback), [ADR-0005](0005-qdrant-hybrid-search.md) (experiments E1–E3), [ADR-0009](0009-confidence-and-routing.md) (trained confidence model) and [ADR-0013](0013-prompts-as-files.md) (eval-smoke in CI)

## Context
The owner set a budget of **about $5 of real model spend for the whole rest of the project**, with Anthropic as the only provider: no OpenAI account and no `OPENAI_API_KEY`. The design assumed more. It had two providers (a cross-provider judge and fallback, experiment E8), API embeddings, 300 labelled tickets with a dev/test split, a 60-item judge validation set, ten experiments, a real-model eval-smoke on every AI pull request, a weekly full evaluation, and a fine-tuning phase. Each of these spends money or owner time that the project no longer has.

## Decision
1. **One provider.** Every chat role (`triage`, `research`, `write`, `verify`, `judge`, and the summary prompts) runs on `anthropic/claude-haiku-5-5` with thinking off, with `anthropic/claude-sonnet-5-5` as the fallback (thinking `between_tools`, effort `low`). LiteLLM stays as the gateway library: the gateway is built and tested (P3-01, P3-02), the module rules do not change, and a provider can still be added by configuration later.
2. **Judge from the same model family.** The judge runs on Haiku 5.5, the same model that writes the answers, so self-preference is likely. Judge numbers are reported as **unvalidated** next to every judge metric (eval plan §5); nobody labels a judge validation set.
3. **Local embeddings.** The `embedding` role uses `BAAI/bge-small-en-v1.5` run by **FastEmbed `fastembed==0.9.1`** (released 2026-10-24; checked on PyPI and in the v0.9.1 source and model list on 2026-11-16). The model has 384 dimensions, is 0.067 GB of ONNX, uses the MIT licence and needs no PyTorch. FastEmbed was already planned for the BM25 sparse vectors (ADR-0005), so this adds no new library. Embeddings are produced locally, never sent to an API. Calls still go through `Gateway.embed`, write an `llm_calls` row and cost 0 (`config/pricing.yaml`).
   - **No network at run time.** `make setup` (and from P10-06 the image build) downloads the model into a cache folder. At run time the model loads with `local_files_only=True` (a FastEmbed option), so a missing model is an error, never a download. P5-01 adds the cache-folder setting, records the SHA-256 of the model file, and checks whether FastEmbed can pin the Hugging Face revision.
   - **Until P5-01** there is no local backend: a real-mode embedding call fails with `LocalEmbeddingNotAvailable` (not retried, recorded, then `LlmUnavailableError`). Nothing calls `embed()` before P5-01.
   - The model reads at most 512 tokens and is English only. Section chunks must fit (P5-01 checks this); non-English tickets already go to a human (`language_not_english` in `config/policy.yaml`).
4. **A small evaluation.** Exactly **20 dev tickets** and **16 attacks** (8 categories × 2), no test set, no dev/test split, no judge validation set. Human gates H4 (check every evaluation ticket) and H5 (label the judge set) are removed; H6 (review the attack set) stays. Targets and red lines are counts on these sets. Every report and the README say that the numbers are **a demo check, not a proof**: 0 errors in 20 still allows a true error rate of up to 16% (95% Wilson interval).
5. **Experiments.** E1–E8 and E10 are removed, and so is phase 11 (fine-tuning, which needed the test set). Only E9 stays, in a reduced form (point 6). The questions the removed experiments asked (B0 against RAG, dense against hybrid, reranker, chunking, `top_k`, tools on/off, retry loop on/off, small against strong model) are listed as not measured. The reranker mode (`hybrid_rerank`) is not built; hybrid search is the default without an experiment. This replaces the sentence of ADR-0005 that the reranker "is tested in experiment E3". The notes "measured in E1" and "measured in E2" in ADR-0005 (and its consequence about the B0 result) and "measured in E4" in ADR-0006 no longer apply: those comparisons are not made.
6. **Confidence (replaces layer 2 of ADR-0009).** Twenty tickets cannot train or calibrate a logistic regression. The **rule score** from AI pipeline §6 stays as the confidence: `confidence = groundedness` when `top_score` is above a fixed value, else 0. Auto-reply is **off by default**. Human gate H7 becomes "decide whether auto-reply goes on in the demo". The threshold and the rule score are set from the 20-ticket run and called uncalibrated. Layer 1 of ADR-0009 (policy rules first) is unchanged.
7. **Fewer paid runs.**
   - `make eval-smoke` runs only by hand, never in CI. A prompt or model change needs a manual eval-smoke only when the owner asks. This replaces "A prompt change needs an eval-smoke run in CI" in ADR-0013.
   - `eval-smoke.yml` (P4-07) and the weekly `eval-full.yml` (P9-05) are not built.
   - CI runs no real model call. The one exception is the post-deploy canary ticket (operations §6), which costs under one cent per deploy.
   - The evaluation runner's disk cache is **on by default**, so a re-run makes only the calls whose input changed. The final milestone run uses `--no-cache`.
   - Guards: `EVAL_MAX_COST_USD` 1.00 per run, `DAILY_BUDGET_USD` 0.50 (start value of the daily budget setting), and a spending limit of $5 set by the owner in the Anthropic console (H2).

## Cost estimate for the rest of the project
Assumption: about $0.004 per ticket with judges and $0.002 without, from about 28K input and 2.7K output tokens on Haiku 5.5 at $0.10 / $0.50 per million tokens. The first real numbers (P3-05) replace this assumption.

| Item | Estimate |
|---|---|
| Fixture recording, P3–P7 (about 200 calls) | $0.15 |
| Manual tries during the build | $0.20 |
| First report (20 tickets, with judges) | $0.10 |
| Manual eval-smoke on request (about 10 runs, partly cached) | $0.80 |
| Attack hardening (about 5 runs × 16 attacks) | $0.35 |
| Milestone run (20 tickets + 16 attacks, no cache) | $0.15 |
| Demo: deploy canaries, recording the video, public traffic | $0.55 |
| Reserve: retries, Sonnet fallbacks, mistakes | $1.00 |
| **Total** | **about $3.30** (could be about 50% higher; the $5 console limit stops it) |

## Alternatives considered
- **Keep a second provider for the judge only.** Removes the same-family bias, but needs a second account and key, which the owner ruled out.
- **Call Anthropic directly without LiteLLM.** One dependency less, but the gateway, its tests and the module rules are already built around LiteLLM; with one provider either works. Can be decided later in its own ADR.
- **An embedding API** (for example Voyage AI). Small cost, but another account and key, network at run time, and data leaving the host.
- **`sentence-transformers`.** Needs PyTorch (gigabytes) for the same models; FastEmbed runs the ONNX export on CPU.
- **Larger local models** (`snowflake-arctic-embed-m`, `nomic-embed-text-v1.5`, 768 dimensions). Better on some benchmarks, 6–8 times the size and slower on a small Cloud Run instance. Without retrieval experiments there would be no measurement to justify them.
- **The Message Batches API at half price for evaluation runs.** The graph calls nodes one after another, so a run cannot be sent as one batch.
- **Keep the 300-ticket dataset and run it rarely.** Building and checking it (H4) costs the owner several evenings; one full run with judges would take a large part of the budget.

## Consequences
- Every quality number is a demo check on 20 tickets, with a wide interval. The README and the case study must say so next to the numbers.
- Judge numbers may be too kind (same-family self-preference) and are not checked against human labels.
- The fallback does not help when Anthropic itself is down. Then the gateway raises `LlmUnavailableError` and the ticket goes to a human with reason `system_error`. NFR-10 still holds, because support never stops.
- Retrieval quality depends on a small English model plus BM25. With no experiments, a weak result is reported rather than tuned.
- Auto-reply in the demo depends on an uncalibrated rule score and the owner's decision (H7). The settings page has no risk–coverage curve.
- The FastEmbed model must be present on every machine that runs the app: a setup step locally, the image build for the demo.
- "Never do" rule 7 (`data/eval/tickets.test.jsonl`) stays word for word. With no test file it has nothing left to protect, but it does no harm.
