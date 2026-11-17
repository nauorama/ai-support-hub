# Error analysis — run 2026-11-17-rag_hybrid-4fc85cb

Written by hand after the run (Gate 5 check of phase 5, `eval-analyst` subagent). The runner does not generate or overwrite this file; the generated numbers are in [report.md](report.md). Synthetic data, **demo check, not a proof**. The 20 dev tickets are also the tuning set. Judge findings are **unvalidated, same model family**; the verify step is the same model family too, so its agreement with the judge is not an independent check.

Run: variant `rag_hybrid`, graph `linear-v3`, Claude Haiku 5.5 for every role (fallback Sonnet 5.5). Prompts: triage v1, write v2, verify v1. Judges: key facts v1, faithfulness v2, citation v2. Anchor date not set. Every route is approval (`auto_reply_off`, 20 of 20).

**Gate 5** (build plan): hit@5 ≥ 0.80 and at most 2 answered tickets with an unsupported claim.

| Gate metric | Value | Result |
|---|---|---|
| hit@5 | 14 of 18 (95% CI 55%–91%); 15 of 18 needed | missed by **1 ticket** — noise level; 2 of the 4 misses cannot be reached by a text search (see below) |
| faithfulness: answered tickets with an unsupported claim *(judged)* | 6 of 9 (95% CI 35%–88%); at most 2 allowed | missed by **4 tickets** — a real miss; no judge error changes a ticket's result |

Other red lines missed: key-fact coverage 0.41 (14 tickets) *judged*; citation validity 5 of 9 answered tickets with an invalid marker (95% CI 27%–81%) *judged*; hard-handoff recall 0 of 8 (95% CI 0%–32%) — expected, no routing before P6-02.

**Comparison with B0** (`2026-11-17-b0_full_kb-4bc48fc`): faithfulness and citation validity are **not comparable** (B0 judged with `judge_*/v1`, this run with v2). Key-fact coverage used `judge_key_facts/v1` in both runs, so it can be put side by side, with the caveat that the evidence differs (whole KB against 6 chunks): 0.61 → 0.41 over 14 tickets. The drop comes mainly from 3 more declined drafts scoring 0.00 (T-0005, T-0007, T-0016). On the answered tickets alone it is 0.71 (12) against 0.64 (9), a difference of about one ticket's worth, so noise. Triage is unchanged: 17 of 20, with the same 3 misses (triage v1 in both). B0 has no retrieval metric.

## Categories

Each of the 19 tickets with a problem is in exactly one category of eval plan §7; T-0012 had none. A ticket with more than one problem is filed under the one that explains most of it; the reason column names the others.

| Category | Tickets | Count |
|---|---|---|
| Wrong route (known gap: no policy gate or handoff before P6-02) | T-0003, T-0004, T-0010, T-0011, T-0014, T-0017, T-0019 | 7 |
| Document found but answer wrong | T-0001, T-0005, T-0006, T-0008, T-0009, T-0013 | 6 |
| Document not found (3 of them: the fact is in account data, P7) | T-0002, T-0007, T-0016, T-0018, T-0020 | 5 |
| Wrong intent | T-0015 | 1 |
| Answer right but judge wrong | — (one wrong claim verdict in T-0018, no effect on the ticket's result) | 0 |
| Data label wrong | — (suspected label problems listed below, none is the main cause) | 0 |

| Ticket | Category | Reason |
|---|---|---|
| T-0001 | answer wrong | One sentence applies the "first 3 Payment Requests of a new account" rule to the customer's own story as fact. That counts as a claim under the owner's rule; it is plausible, and account facts (P7) would support it. Omitted "normally 3 business days", which is in the cited chunk. Key fact 4 needs account data. The intro "nothing is wrong with a pending card payment" is now generic and supported, unlike B0. |
| T-0002 | not found (account data) | 4-word ticket, no code. The gold documents (`payment-held-for-review`, `ERR-1012`) follow from the account's error code only. Decline is right before P7. hit@5 miss. |
| T-0003 | wrong route | Complaint flag found, writer declined; `complaints#0` at rank 4. |
| T-0004 | wrong route | Spanish ticket must hand off (`language`). hit@5 miss: Spanish query against an English KB with the English-only embedding model `bge-small-en-v1.5`. The writer declined ("sources do not say how long a withdrawal takes"), which is true for its chunks. |
| T-0005 | answer wrong (writer declined) | Follow-up. `withdrawal-fees#3` (rank 1: a failed withdrawal costs nothing) and `error-codes#ERR-2003` (rank 4) answer key facts 2 and 3. The writer's own `missing_information` says so, but it declined because the money's status needs account data. Also wrong intent (`fees_fx`): triage and the first search get only the newest message, not the history; the triage summary says the earlier context is missing. |
| T-0006 | answer wrong (citation) | Faithful. Marker [3] ("goes back in 3–7 business days without a fee") cites `withdrawal-bank-details#4`, the ERR-2003 section; the general section (`withdrawal-failed-or-returned#0`/`#1`) was not in the top 6. The citation judge is right. Key fact 4 names a transaction id (account data). |
| T-0007 | not found (account data) | Which decline code applies is account data (ERR-3005). The `card-declined#4` section on blocked merchants was not in the top 6. hit@5 counts a hit through `card-declined#1`, a different section. Declining avoids guessing the merchant type, which `write/v2` forbids. |
| T-0008 | answer wrong | "Your balance does not affect this limit" is in no chunk and no KB article (real unsupported claim; same sentence fails its citation). Key fact 2 (pending payments count toward the limit) is still in no external article (label question from B0). Key fact 3 needs account data. |
| T-0009 | answer wrong (owner rule) | "So the passport you uploaded is no longer valid" links the customer's quoted ERR-4002 to their passport. Under the implied-application rule this is a claim without evidence; in substance it is a paraphrase of "document expired" (B0 counted it a judge error under v1). Key fact 3 (KYC 1–2 business days) is in a `verification-times` section that was not retrieved. |
| T-0010 | wrong route | `wants_human` found, writer declined correctly. |
| T-0011 | wrong route | Fee refund request; writer declined and promised nothing. |
| T-0013 | answer wrong | "Includes all payments received this month, not only the balance" is in no chunk (real; the same sentence fails its citation). Also wrong intent (`payment_pending` against `limits`, two-intent ticket). Key facts 2 and 6 need account data; the second intent's documents (`verification-rejected`, ERR-4010) were not retrieved, and the code is not in the ticket. |
| T-0014 | wrong route | `account_restricted`, must hand off. hit@5 miss: the customer's "blocked" matched `card-declined`/`card-pin` (PIN blocked), not `account-under-review`. Correct decline. |
| T-0015 | wrong intent | `card_lost_stolen` against `login_security` (borderline, as in B0). Answer covers all facts, faithful, citations valid. **Also a missed hard handoff** (`fraud_report`), one of the 8. |
| T-0016 | not found (retrieval) | Neither gold article (`dispute-card-transaction`, `merchant-refund`) is in the top 6. The ticket says "charged twice … get one back"; the dispute article never says twice, duplicate or charged. The writer correctly said the sources do not cover disputes. Should escalate (`policy_review`) but is not a must-handoff, so a draft with the dispute steps was expected. hit@5 miss. |
| T-0017 | wrong route | Must hand off (`policy_intent`). Two claims fail under the owner rule: "a card ordered more than a month ago should normally have arrived by now" (the 7–14 day rule applied to the customer's statement; B0 had the same sentence) and "your delivery address country, which is France" (a customer statement written as fact). The first also fails its citation. The virtual-card key fact is in `card-lost-or-stolen#2` (rank 2, replacement context) but was not used. |
| T-0018 | not found (section) | `close-account` at rank 1, but `inactivity-fee` only with section #3 ("how to avoid", rank 6). The sections on when the fee applies (#0, #1) were missing, so the writer said "our help centre does not explain why a fee was taken" and "the team will need to check". Both are unsupported (the KB does explain it), but they follow from the missing chunk. The third claim, "no use for 12 months can be charged an inactivity fee", is a **judge error** (it follows from section #3), the same "follows from the article" pattern as the open B0 case; its citation verdict fails the same way. Unlike B0, the draft does not claim the €5 charges are the inactivity fee. |
| T-0019 | wrong route | No KB answer (partnership contact); correct decline. |
| T-0020 | not found (account data) | Faithful, citations valid. Key fact 1 (declined with ERR-3009) needs the account's code; the customer gave none, and the draft hedges ("if that happened"). The 3-D Secure section `card-declined#5` came only at rank 6; hit@5 counts `card-declined#1` at rank 5. |

## Retrieval: the 4 hit@5 misses

hit@5 is computed over 18 tickets. T-0012 and T-0019 are left out because they have no gold documents (both ran a search; the report wording was corrected in the same pull request to "no KB search, or no gold documents").

| Ticket | Gold | Ranked chunk ids (1–6) | Reading |
|---|---|---|---|
| T-0002 | `payment-held-for-review`, `error-codes#ERR-1012` | `incoming-payment-times#2`, `receiving-fees#4`, `incoming-payment-not-arrived#1`, `incoming-payment-not-arrived#2`, `multi-currency-account#0`, `error-codes#ERR-1001` | **Questionable gold for a first-search metric.** The gold follows from the account's error code (P7); a 4-word text query cannot point to it. The article a text query should find, `incoming-payment-not-arrived`, is at ranks 3–4, and its #2 summarises ERR-1012. |
| T-0004 | `withdrawal-times` | `account-limits#1`, `incoming-payment-not-arrived#2`, `incoming-payment-times#0`, `withdrawal-failed-or-returned#2`, `withdrawal-fees#0`, `receiving-fees#4` | **Retrieval failure from the query's language.** Spanish text against English chunks: BM25 shares no tokens, and the embedding model is English-only. The ticket must hand off (`language`). Whether such a ticket should count in hit@5 is an open question. |
| T-0014 | `account-under-review`, `error-codes#ERR-5010` | `card-declined#4`, `card-pin#0`, `card-declined#3`, `card-declined#1`, `card-pin#1`, `error-codes#ERR-3003` | **Retrieval failure from query wording.** "Blocked", "declined" and "withdrawal failed" match the card decline and PIN-blocked sections; the article says "under review"/ERR-5010, a code the customer does not give. Must hand off. |
| T-0016 | `dispute-card-transaction`, `merchant-refund` | `card-lost-or-stolen#2`, `payment-requests#1`, `freeze-card#4`, `withdrawal-fees#3`, `error-codes#ERR-2020`, `unauthorised-transactions#1` | **Retrieval failure from vocabulary mismatch.** The customer's words (charged twice, get one back) are not in either article. The only miss that cost an answer the ticket should have had. |

Notes on retrieval:
- **Article-level gold is lenient.** T-0007, T-0018 and T-0020 count as hits through a section of the right article that does not hold the answer; the needed section was missing (T-0007, T-0018) or at rank 6 (T-0020). hit@5 overstates how often the writer had the answer.
- **One chunk recurs near the top.** `incoming-payment-not-arrived#2`, a list of five incoming error codes, appears in 5 of 20 searches, at rank 2 in T-0004, T-0008, T-0013 and T-0020 (rank 4 in T-0002). Observation only.
- **Error-code pinning worked** wherever the ticket text had a code (T-0006, T-0008, T-0009, T-0013 at rank 1). In T-0005 the code is only in the history, which the search does not see, so it was not pinned (rank 4).

## The 11 drafts with `can_answer = false`

| Ticket | Answer in the retrieved chunks? | Reading |
|---|---|---|
| T-0003 | partly (ACH timing) | Right outcome: must hand off (complaint). |
| T-0004 | no | Right outcome: must hand off (language); also a retrieval miss. |
| T-0010 | partly | Right outcome: must hand off (asks for a human). |
| T-0011 | partly | Right outcome: must hand off (refund request). |
| T-0012 | no (no KB answer) | Right outcome: `no_kb_answer`. |
| T-0014 | no | Right outcome: must hand off (account restricted); also a retrieval miss. |
| T-0019 | no (no KB answer) | Right outcome: must hand off. |
| T-0002 | general part only | Right before P7: which payment and why it is held is account data. |
| T-0007 | no (ERR-3005 section missing) | Right before P7: the code is account data, and guessing the merchant type is forbidden. |
| T-0016 | no | **Retrieval miss**: the dispute steps (key facts 2–5) are in the KB but were not retrieved. |
| T-0005 | **yes** for the fee question and next steps | **Writer too strict**: declined the whole draft because the money's status needs account data. |

Summary: 7 are the right outcome because the ticket should escalate (6 must-handoff, 1 no KB answer), 2 are right before account data (P7), 1 is a retrieval miss and 1 is a writer that was too strict.

## The 6 unfaithful answers, claim by claim

Classes: **R** = real unsupported claim (the content is in no chunk); **A** = a KB rule applied to the customer's own statements, or a customer statement written as fact. A is a claim under the owner's rule (eval plan §4); it is plausible in substance and supportable with account facts (P7). **J** = judge error.

| Ticket | Claim (paraphrased) | Class | Verify flagged it |
|---|---|---|---|
| T-0001 | the customer's three payments fall under the first-three rule (the judge split it into 2 claims) | A | yes (1 claim) |
| T-0008 | the balance does not affect the daily limit | R | yes |
| T-0009 | the uploaded passport is no longer valid | A | yes |
| T-0013 | the limit includes all payments received this month, not only the balance | R | yes |
| T-0017 | a card ordered over a month ago should have arrived by now | A | yes |
| T-0017 | the delivery country is France | A | yes |
| T-0018 | the help centre does not explain the fee | R (caused by the missing section) | yes |
| T-0018 | the team will need to check the charges | R (a process statement in no chunk) | yes |
| T-0018 | 12 months without use can bring an inactivity fee | **J** (follows from `inactivity-fee#3`) | no |

Tickets with a real (R) claim: T-0008, T-0013, T-0018 — 3 of 9. Tickets with only class A claims: T-0001, T-0009, T-0017 — 3 of 9. Even if the owner rule did not count class A, faithfulness would be 3 of 9, still above the red line of 2 (by one ticket). The one judge error does not change T-0018's result.

Citation validity, 5 of 9 tickets (5 of 32 judged citations): T-0006 (wrong section, real), T-0008 (R sentence), T-0013 (R sentence), T-0017 (A sentence), T-0018 (the J sentence; the citation judge made the same error). No marker problems found by code.

Two of the R claims (T-0008, T-0013) are the same shape: an extra sentence that answers the customer's objection ("but I have money") with a rule that is plausible but not written in the KB.

## Verify step against the faithfulness judge

- **Ticket level: they agree on 9 of 9.** `groundedness` < 1.0 on exactly the 6 tickets the judge found unfaithful (T-0001 0.80, T-0008 0.875, T-0009 0.75, T-0013 0.875, T-0017 0.60, T-0018 0.833), and 1.0 on T-0006, T-0015 and T-0020.
- **Claim level:** the verify step listed 8 unsupported claims and the judge 10. All 8 verify claims are among the judge's. The 2 extra judge claims are T-0001's sentence split in two, and the T-0018 judge error, which the verify step did not flag. Same model family, same evidence, similar rubric: the agreement is not an independent validation.
- **Code checks:** none failed on an answered draft. All 9 drafts with `can_answer = true` have a non-null `groundedness`, so the code checks passed and the LLM check ran. For the 11 declined drafts `check_draft` returns `cannot_answer` by design (so `groundedness` is null and no verify model call is made). The unmarked-fact check works per paragraph (as AI pipeline §5 specifies), so an unmarked sentence in a paragraph that has markers passes (T-0001 sentence 3, T-0006 sentence 1, T-0008 last sentence). Observation only.

## Phase 5 or later

- **Routing, P6-02:** the 7 wrong-route tickets plus T-0015 = hard-handoff recall 0 of 8. Escalation recall 10 of 10 and wrong auto-reply 0 of 0 are trivial passes (everything goes to approval).
- **Account data, P7:** T-0002, T-0007 and T-0020 (main cause), plus account-data key facts in T-0001, T-0005, T-0006, T-0008, T-0013 and T-0016, and the class A claims in T-0001, T-0009 and T-0017, which account facts could support.
- **Phase 5 (retrieval, writer, verify):** retrieval misses T-0016, T-0014 and T-0004 (the last two must hand off anyway); section-level misses in T-0007, T-0009, T-0018 and T-0020; the writer declining T-0005 although the chunks answered part of it; the real unsupported claims in T-0008, T-0013 and T-0018; the wrong-section citation in T-0006; no history in triage or the first search (T-0005).
- **Judge:** the T-0018 "follows from the how-to-avoid section" case is still open.

## Suspected label problems (not changed)

- **T-0008, key fact 2** (open since B0): pending payments counting toward the daily limit is in the world file but in no external article.
- **T-0002, gold for hit@5:** the gold documents depend on the account's error code, which the first search (ticket text) cannot know. As an answer label it is right.
- **T-0004, hit@5 coverage:** a must-handoff Spanish ticket in a metric of English retrieval. The label is right; whether it belongs in hit@5 is a definition question.
- **T-0013, key fact 4:** "a Starter account cannot ask for a limit increase" is only implied by the draft ("on Verified and Business, ask for a limit increase"); the judge counted it absent. A strict verdict, not a label error.

Changing a hit@5 label now would close the 1-ticket gap exactly; that would look like changing a test to pass the gate (AGENTS.md never-do 6), so any such change is the owner's decision, recorded in `data/eval/CHANGELOG.md` and separate from Gate 5.

## Clock

The anchor date was not set again (`core_anchor_date` null). No failure here depends on the date: there is no account data, and the only date reasoning (T-0017, "more than a month") uses the customer's words.
