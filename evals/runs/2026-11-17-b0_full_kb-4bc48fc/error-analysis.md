# Error analysis — run 2026-11-17-b0_full_kb-4bc48fc

Written by hand after the run (P4-06, `eval-analyst` subagent, corrected after review). The runner does not generate or overwrite this file; the generated numbers are in [report.md](report.md). Synthetic data, **demo check, not a proof**. Judge findings are **unvalidated, same model family**.

Each of the 19 tickets with a problem is in exactly one category of eval plan §7; T-0012 had none. A ticket with more than one problem is filed under the one that explains most of it; the reason column names the others.

| Category | Tickets | Count |
|---|---|---|
| Wrong route (known gap: no policy gate or handoff before P6-02, every run goes to approval) | T-0003, T-0004, T-0010, T-0011, T-0014, T-0017, T-0019 | 7 |
| Answer right but judge wrong | T-0009, T-0016, T-0018, T-0020 | 4 |
| Wrong intent | T-0005, T-0013, T-0015 | 3 |
| Document found but answer wrong | T-0001, T-0006, T-0007 | 3 |
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
| T-0007 | answer wrong (partly judge wrong) | Two claims are in no article: "the decline happens even when the balance is enough" and "the card works for other merchants"; the judge was right on both. Only "use another way to pay", which is word for word in card-declined, was a judge error. |
| T-0008 | label wrong | Key fact 2 (pending payments count toward the daily limit) is in the world file but in no external article. The judge also rejected 3 error-code citations that ERR-3011 supports. |
| T-0009 | judge wrong | "1–2 business days" is in the draft but judged absent; "no longer valid" paraphrases "expired". |
| T-0010 | wrong route | `wants_human` found, writer declined correctly. |
| T-0011 | wrong route | Refund request; writer declined and promised nothing. |
| T-0013 | wrong intent | `payment_pending` instead of `limits`; two of the lost facts need account data. |
| T-0014 | wrong route | Correct decline; coverage 0.00 because the empty draft is judged. |
| T-0015 | wrong intent | `card_lost_stolen` instead of `login_security` (borderline); answer covers all facts. **Also a missed hard handoff** (one of the 8 in hard-handoff recall), filed here because one category per ticket. |
| T-0016 | judge wrong | A supported "contact us with date, amount, merchant" sentence was rejected. |
| T-0017 | wrong route | Answer covers all facts but has two unsupported additions: "a card ordered more than a month ago should normally have arrived by now" and "check delivery details and make sure the address is correct". |
| T-0018 | judge wrong | "Withdrawing the balance and closing the account is the way to stop these charges" follows from inactivity-fee but was judged unsupported. The draft also states as fact that the customer's €5 charges are the inactivity fee, which B0 cannot check; the judge accepted it. |
| T-0019 | wrong route | No knowledge-base answer; declined correctly. |
| T-0020 | judge wrong | "Contact us" claim is close to the card-declined text; one fact needs account data. |

## Patterns

1. **No account data in B0, so the writer fills gaps from the customer's text.** About 10 key facts in 9 tickets need transaction ids or the account's error code. The writer then guesses or reassures: "nothing is wrong" (T-0001), an invented cause (T-0006), "the card works for other merchants" (T-0007). T-0018 shows the same habit in a claim the judge accepted ("the €5 charges are the inactivity fee"), so the judge does not catch every such guess. These are real faithfulness risks.
2. **The faithfulness judge does not see the ticket.** Restating what the customer said is always unsupported (T-0007, T-0008, T-0013, T-0015), and one marker on a sentence that mixes customer context and an article fact fails the citation check. The judge also gave clearly wrong verdicts on exact matches (T-0001, T-0007, T-0008, T-0009, T-0016, T-0018). By the analyst's reading about 6 of 13 answers have a real unsupported addition (T-0001, T-0006, T-0007, T-0017 among them), so faithfulness breaches the red line of 2 even without the judge errors.
3. **Strict quote check and coverage counting.** 4 of the 21 unsupported citations were "supports" verdicts downgraded because the quote was not found; none changes a ticket's result. Declined tickets T-0002 and T-0014 score 0.00 coverage; leaving them out gives 0.71 over 12, still below the 0.75 red line.

## Reading the routing rows

Every ticket went to approval (`auto_reply_off`, 20 of 20). So **escalation recall 10 of 10 and wrong auto-reply 0 of 0 are trivial passes** — they say nothing about routing quality or safety — and escalation precision is 10 of 20 for the same reason. Routing is first measured after P6-02.

## Clock

The anchor date was not set (`CORE_ANCHOR_DATE` empty; the runner does not enforce it). No failure here depends on the date: B0 reads no account data and no draft calculates a date (tickets that say "yesterday" have no date to resolve against). Once tools exist (phase 7), T-0001, T-0008 and T-0013 will depend on it; P7-06 now requires a fixed anchor date.

Open label questions, the judge-context question and the write v2 idea are listed in [docs/experiments.md](../../../docs/experiments.md).
