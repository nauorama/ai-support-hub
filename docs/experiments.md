# Experiments

One row per evaluation run that the owner asked for (eval plan §6). Numbers are on the 20 synthetic dev tickets, which are also the tuning set: **demo check, not a proof**. Judge metrics are unvalidated, same model family (eval plan §5). A difference of one or two tickets is noise.

| Date | Run | Question | Change | Dev metrics | Cost / latency | Decision |
|---|---|---|---|---|---|---|
| 2026-11-17 | [`2026-11-17-b0_full_kb-4bc48fc`](../evals/runs/2026-11-17-b0_full_kb-4bc48fc/report.md) (P4-06) | How good is the baseline B0 (whole knowledge base in the prompt, linear pipeline)? | None, first run: triage v1, write v1, judges v1, Claude Haiku 5.5 | Triage 17 of 20; flag recall 1 of 1 each; key-fact coverage 0.61 (14 tickets) *judged*; unsupported claim 12 of 13 answered *judged*; invalid citation 10 of 13 *judged*; hard-handoff 0 of 8 (no routing before P6-02); escalation recall 10 of 10 | $0.0015 per ticket; $0.048 paid in total with judges (114 calls); all runs p50 3.7 s / p95 5.9 s | Baseline recorded. Auto-reply stays off. Candidates for the owner: write prompt v2 with a grounding rule; give the faithfulness judge the ticket; check labels T-0008, T-0005, T-0015 |
