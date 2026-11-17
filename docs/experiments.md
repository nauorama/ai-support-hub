# Experiments

One row per evaluation run that the owner asked for (eval plan §6). Numbers are on the 20 synthetic dev tickets, which are also the tuning set: **demo check, not a proof**. Judge metrics are unvalidated, same model family (eval plan §5). A difference of one or two tickets is noise.

| Date | Run | Question | Change | Dev metrics | Cost / latency | Decision |
|---|---|---|---|---|---|---|
| 2026-11-17 | [`2026-11-17-b0_full_kb-4bc48fc`](../evals/runs/2026-11-17-b0_full_kb-4bc48fc/report.md) (P4-06; [error analysis](../evals/runs/2026-11-17-b0_full_kb-4bc48fc/error-analysis.md)) | How good is the baseline B0 (whole knowledge base in the prompt, linear pipeline)? | None, first run: triage v1, write v1, judges v1, Claude Haiku 5.5 | Triage 17 of 20; flag recall 1 of 1 each (n = 1); key-fact coverage 0.61 (14 tickets) *judged*; unsupported claim 12 of 13 answered *judged*; invalid citation 10 of 13 *judged*; hard-handoff 0 of 8 (no routing before P6-02); escalation recall 10 of 10 (trivial: everything goes to approval) | $0.0015 per ticket for answers, $0.0024 with judges; $0.048 paid in total (114 calls); all runs p50 3.7 s / p95 5.9 s | Baseline recorded. Auto-reply stays off. Open questions below, for a later pull request |

## Open questions (for a later pull request)

From the error analysis of the B0 run. Nothing here is changed yet; the owner decides.

**Labels** (`data/eval/tickets.dev.jsonl`; a fix gets a note in `data/eval/CHANGELOG.md`):
- **T-0008, key fact 2.** "Pending card payments count toward the daily card spend limit" is in the world file (`docs/tallybridge-world.md` line 37) but in no external article, so a writer grounded in the knowledge base cannot reach it. Either add it to an article (account-limits or card-declined) or drop the key fact.
- **T-0005, key fact 3.** "Check the bank details and create the withdrawal again": the customer's history already says the bank details were checked, so this key fact is weak.
- **T-0015, intent.** `login_security` against `card_lost_stolen` is borderline ("someone has my card or my account"). The label fits world §9; the routing effect is the same through the fraud flag.

**Judge.** The faithfulness judge does not see the ticket, so a draft that restates the customer's message is always unsupported, and a sentence that mixes customer context with an article fact fails its citation. Options: give the judge the ticket as context. Turn the wrong verdicts of T-0007 ("use another way to pay"), T-0009 and T-0018 into judge unit tests with recorded responses (eval plan §5).

**Write prompt v2.** One grounding rule: say nothing about the customer's own account or situation that no document or account fact says (no reassurance, no guessing a merchant type or which fee was charged), do not restate the customer's message, and put only facts from the cited document in a marked sentence. Expected effect: removes about 5–6 real unsupported claims; it will not reach the faithfulness red line alone. Measure with an owner-approved `make eval` (triage cached) and `make eval-report BASE=2026-11-17-b0_full_kb-4bc48fc`; key-fact coverage must not drop by more than one ticket's worth.
