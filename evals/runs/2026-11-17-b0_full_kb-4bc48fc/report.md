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

## Error analysis

Written by hand after the run (P4-06, `eval-analyst` subagent; not generated by the runner). Each of the 19 tickets with a problem is in exactly one category of eval plan §7; T-0012 had none. Judge findings are **unvalidated, same model family**.

| Category | Tickets | Count |
|---|---|---|
| Wrong route (known gap: no policy gate or handoff before P6-02, every run goes to approval) | T-0003, T-0004, T-0010, T-0011, T-0014, T-0017, T-0019 | 7 |
| Answer right but judge wrong | T-0007, T-0009, T-0016, T-0018, T-0020 | 5 |
| Wrong intent | T-0005, T-0013, T-0015 | 3 |
| Document found but answer wrong | T-0001, T-0006 | 2 |
| Document not found (the facts are in account data, which B0 cannot read) | T-0002 | 1 |
| Data label wrong | T-0008 | 1 |

| Ticket | Category | Reason |
|---|---|---|
| T-0001 | answer wrong | "Nothing is wrong" reassures without account data. The judge also rejected a "contact us" sentence that the article's step 4 supports. |
| T-0002 | document not found | Needs the transaction and its error code from the account. Declining was right for B0. |
| T-0003 | wrong route | Complaint flag found, writer declined; handoff is not wired yet. |
| T-0004 | wrong route | Spanish ticket must hand off (`language`); the Spanish answer is otherwise correct. |
| T-0005 | wrong intent | Follow-up classified `fees_fx` instead of `withdrawal_issue`. Answer faithful; one key fact needs account data. |
| T-0006 | answer wrong | Invented guess ("an old email can have an outdated number"); missed "a failed withdrawal costs nothing" from withdrawal-failed-or-returned. |
| T-0007 | judge wrong | "Use another way to pay" is word for word in the article but judged unsupported; restating the customer's words counts against the draft. |
| T-0008 | label wrong | Key fact 2 (pending payments count toward the daily limit) is in the world file but in no external article. The judge also rejected 3 error-code citations that ERR-3011 supports. |
| T-0009 | judge wrong | "1–2 business days" is in the draft but judged absent; "no longer valid" paraphrases "expired". |
| T-0010 | wrong route | `wants_human` found, writer declined correctly. |
| T-0011 | wrong route | Refund request; writer declined and promised nothing. |
| T-0013 | wrong intent | `payment_pending` instead of `limits`; two of the lost facts need account data. |
| T-0014 | wrong route | Correct decline; coverage 0.00 because the empty draft is judged. |
| T-0015 | wrong intent | `card_lost_stolen` instead of `login_security` (borderline); answer covers all facts; also not handed off. |
| T-0016 | judge wrong | A supported "contact us with date, amount, merchant" sentence was rejected. |
| T-0017 | wrong route | Answer covers all facts; one small unsupported addition. |
| T-0018 | judge wrong | "Withdraw and close to stop the fee" is in inactivity-fee but judged unsupported. |
| T-0019 | wrong route | No knowledge-base answer; declined correctly. |
| T-0020 | judge wrong | "Contact us" claim is close to the card-declined text; one fact needs account data. |

**Patterns.**
1. *No account data in B0, so the writer fills gaps from the customer's text.* About 10 key facts in 9 tickets need transaction ids or the account's error code. The writer then guesses or reassures (T-0001, T-0007, T-0018). These are real faithfulness risks.
2. *The faithfulness judge does not see the ticket.* Restating what the customer said is always unsupported (T-0007, T-0008, T-0013, T-0015), and one marker on a sentence that mixes customer context and an article fact fails the citation check. The judge also gave clearly wrong verdicts on exact matches (T-0001, T-0008, T-0009, T-0016, T-0018). By the analyst's reading about 6 of 13 answers have a real unsupported addition, so faithfulness breaches the red line even without the judge errors.
3. *Strict quote check and coverage counting.* 4 of the 21 unsupported citations were "supports" verdicts downgraded because the quote was not found; none changes a ticket's result. Declined tickets T-0002 and T-0014 score 0.00 coverage; leaving them out gives 0.71 over 12, still below the 0.75 red line.

**Clock.** The anchor date was not set (`CORE_ANCHOR_DATE` empty). No failure here depends on the date: B0 reads no account data and no draft calculates a date. Once tools exist (phase 7), T-0001, T-0008 and T-0013 will depend on it.

**Suspected label errors** (not changed; the owner decides): T-0008 key fact 2 (world fact missing from the external KB); T-0005 key fact 3 (the customer already checked the bank details); T-0015 intent (borderline between `login_security` and `card_lost_stolen`).

**Proposed next fix** (not done in this task): write prompt v2 with one grounding rule — say nothing about the customer's own account that no document or account fact says, do not restate the customer's message, and put only facts from the cited document in a marked sentence. Separately: give the faithfulness judge the ticket as context and turn the wrong verdicts of T-0007, T-0009 and T-0018 into judge unit tests (eval plan §5).
