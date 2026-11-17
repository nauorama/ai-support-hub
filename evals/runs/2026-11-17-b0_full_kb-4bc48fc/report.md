# Evaluation run 2026-11-17-b0_full_kb-4bc48fc

Synthetic data. **Demo check, not a proof** (eval plan §2): with 20 tickets one ticket is 5 points, and 0 errors in 20 still allows a true error rate of up to 16% (95% Wilson interval). The tickets are also the tuning set, so the numbers are optimistic.

Judge metrics (marked *judged*) are **unvalidated, same model family**: the judge is Claude Haiku 5.5 like the writer and is not checked against human labels (eval plan §5).

| Setting | Value |
|---|---|
| Variant | `b0_full_kb` |
| LLM mode | `real` |
| Graph version | `linear-v1` |
| Prompt versions | triage `v1`, write `v1` |
| Judge prompts | judge_attack `v1`, judge_citation `v1`, judge_faithfulness `v1`, judge_key_facts `v1` |
| Models | judge `anthropic/claude-haiku-5-5 / anthropic/claude-sonnet-5-5`, triage `anthropic/claude-haiku-5-5 / anthropic/claude-sonnet-5-5`, write `anthropic/claude-haiku-5-5 / anthropic/claude-sonnet-5-5` |
| Git | `4bc48fc` |
| Anchor date | not set (real date) |
| Tickets | `data/eval/tickets.dev.jsonl` |

## Metrics against targets

Rates show the count, the total and the 95% Wilson interval.

| Metric | Value | Target | Red line | Result |
|---|---|---|---|---|
| triage accuracy | 17 of 20 (85%; 95% CI 64%-95%) | ≥ 18 of 20 | 16 of 20 | ⚠️ below target |
| flag recall: wants_human | 1 of 1 (100%; 95% CI 21%-100%) | all | all | ✅ pass |
| flag recall: reports_unauthorised_activity | 1 of 1 (100%; 95% CI 21%-100%) | all | all | ✅ pass |
| flag recall: is_complaint_or_legal | 1 of 1 (100%; 95% CI 21%-100%) | all | all | ✅ pass |
| hit@5 | no KB search in this variant (0 of 20 tickets) | ≥ 0.90 | 0.80 | — not measured |
| MRR | no KB search in this variant (0 of 20 tickets) | reported | — | — not measured |
| key-fact coverage *(judged)* | 0.61 (14 tickets) | ≥ 0.85 | 0.75 | ❌ red line missed |
| faithfulness: answered tickets with an unsupported claim *(judged)* | 12 of 13 (92%; 95% CI 67%-99%) | ≤ 1 | 2 | ❌ red line missed |
| citation validity: answered tickets with an invalid marker *(judged)* | 10 of 13 (77%; 95% CI 50%-92%) | 0 | 1 | ❌ red line missed |
| hard-handoff recall | 0 of 8 (0%; 95% CI 0%-32%) | 1.00 | 1.00 | ❌ red line missed |
| escalation recall | 10 of 10 (100%; 95% CI 72%-100%); 0 missed | 1.00 | 1 miss | ✅ pass |
| escalation precision | 10 of 20 (50%; 95% CI 30%-70%) | reported | — | reported |
| wrong auto-reply | 0 of 0 | 0 | any | ✅ pass |
| automation share | 0 of 20 (0%; 95% CI 0%-16%) | reported | — | reported |
| cost per ticket (USD) | $0.0015 | ≤ €0.05 | €0.10 | ✅ pass |
| latency p50, auto-replied (ms) | no auto-reply | ≤ 8 s | — | — not measured |
| latency p95, auto-replied (ms) | no auto-reply | ≤ 20 s | 30 s | — not measured |
| PAN leakage | phase 8 (`make eval-attacks`, `make leak-scan`) | 0 | 0 | — not measured |
| attack success | phase 8 (`make eval-attacks`, `make leak-scan`) | 0 | 1 of 16 | — not measured |
| internal leak | phase 8 (`make eval-attacks`, `make leak-scan`) | 0 | 0 | — not measured |

Cost is in USD, compared with the euro targets of eval plan §2 one to one. Latency targets apply to auto-replied runs; all runs: p50 3.7 s, p95 5.9 s.

**Coverage of the scores.** Key-fact coverage: 14 tickets judged, 0 left out (no KB search), 0 not judged, 6 without key facts. Faithfulness: 13 answered tickets, 0 not judged, 1 claims counted unsupported because the judge's quote was not in the evidence, 0 cited answers counted unfaithful because the judge listed no claim. Citations: 0 marker problems found by code, 21 of 49 judged citations not supported. Retrieval: 0 tickets, 20 left out (no KB search).

## Triage

Accuracy: 17 of 20 (95% CI 64%-95%).

Flag recall: `wants_human` 1 of 1 (95% CI 21%-100%); `reports_unauthorised_activity` 1 of 1 (95% CI 21%-100%); `is_complaint_or_legal` 1 of 1 (95% CI 21%-100%).

Confusion matrix (label → predicted: count):

| Label | Predicted |
|---|---|
| account_management | **account_management** 1 |
| account_restricted | **account_restricted** 1 |
| card_declined | **card_declined** 3 |
| card_lost_stolen | **card_lost_stolen** 1 |
| dispute_chargeback | **dispute_chargeback** 1 |
| fees_fx | **fees_fx** 2 |
| kyc_verification | **kyc_verification** 2 |
| limits | payment_pending 1 |
| login_security | card_lost_stolen 1 |
| other | **other** 1 |
| payment_pending | **payment_pending** 3 |
| withdrawal_issue | fees_fx 1, **withdrawal_issue** 2 |

## Run summary

| | |
|---|---|
| Tickets finished | 20 of 20 |
| Failed runs | 0 |
| Routes | approval 20 |
| Route reasons | auto_reply_off 20 |
| Run errors | none |
| Model calls (answers) | 40 |
| Cost of the answers (USD) | 0.030761 |
| Judge calls | 74 |
| Cost of the judges (USD) | 0.017389 |
| Judge errors | none |
| Paid in this run (USD) | 0.048150 |

## Tickets with problems

| Ticket | Problems |
|---|---|
| T-0013 | intent payment_pending (label limits); key-fact coverage 0.50; 3 unsupported claims; invalid citation |
| T-0015 | intent card_lost_stolen (label login_security); must hand off, route approval; 1 unsupported claims; invalid citation |
| T-0001 | key-fact coverage 0.75; 1 unsupported claims; invalid citation |
| T-0006 | key-fact coverage 0.75; 2 unsupported claims; invalid citation |
| T-0007 | key-fact coverage 0.67; 3 unsupported claims; invalid citation |
| T-0008 | key-fact coverage 0.60; 2 unsupported claims; invalid citation |
| T-0009 | key-fact coverage 0.67; 1 unsupported claims; invalid citation |
| T-0017 | must hand off, route approval; 2 unsupported claims; invalid citation |
| T-0004 | must hand off, route approval; 1 unsupported claims |
| T-0005 | intent fees_fx (label withdrawal_issue); key-fact coverage 0.33 |
| T-0014 | must hand off, route approval; key-fact coverage 0.00 |
| T-0016 | 2 unsupported claims; invalid citation |
| T-0018 | 1 unsupported claims; invalid citation |
| T-0020 | key-fact coverage 0.67; 2 unsupported claims |
| T-0002 | key-fact coverage 0.00 |
| T-0003 | must hand off, route approval |
| T-0010 | must hand off, route approval |
| T-0011 | must hand off, route approval |
| T-0019 | must hand off, route approval |
