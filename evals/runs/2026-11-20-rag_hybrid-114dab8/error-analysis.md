# Error analysis: run 2026-11-20-rag_hybrid-114dab8 (P7-07 step 2)

Write `v4`, judges faithfulness `v3`, citation `v3`, key facts `v1`; Haiku 5.5 writer and judge.
All 28 failing judged items on the three missed red lines were read: ticket, retrieved chunks, account facts, draft, judge verdicts.
`results.jsonl` keeps the judge verdicts and claim texts but not the judge quotes. Where a judge was wrong, the reason given below comes from reading the draft against the source.

The run used the committed `tickets.dev.jsonl` (sha `be6431…`). In that version, key fact 1 of T-0002, T-0007 and T-0020 names a merchant that appears only in the customer message. This is fixed locally but not yet committed.

Classes: **writer** = the draft really has the problem; **writer (retrieval)** = the draft lacks a key fact that is in the KB but not in the retrieved chunks, so no prompt change can fix it; **judge** = the draft is fine and the judge is wrong; **label** = the key fact cannot be reached from the KB or from the account facts.

## Faithfulness (3 of 9 answered tickets fail)

| Ticket | Item | Class | Evidence |
|---|---|---|---|
| T-0007 | claim | writer | "Since the card cannot be used for crypto exchanges, you will need to pay this merchant another way [2]": no source says that the merchant is a crypto exchange (A3 names no merchant). The writer took the category from "CoinHarbor" in the ticket. |
| T-0007 | claim | writer | "Your balance is not the cause of this error": this is the inference that v4 forbids by name ("no statement that something is or is not the reason"). The writer moved it to a help-centre marker. |
| T-0018 | claim | writer (borderline) | "close the account …, which stops further fees [3][1]": inactivity-fee#3 lists closing as a way to avoid the fee. "Stops further fees" is broader than that, and closure takes 30 days. |
| T-0001 | claim | writer | "You can withdraw once the money is available.": no marker and no source. The sources only say "You cannot use the money yet." |

## Citation validity (4 of 9 answered tickets fail; 8 of 43 markers)

| Ticket | Marker | Class | Evidence |
|---|---|---|---|
| T-0007 | [3] card-declined#1 | writer | "Your balance is not the cause of this error, as insufficient balance gives a different error code": the source lists ERR-3001 and says nothing about this payment. |
| T-0013 | [3] A3 | writer | "Your **Starter tier** monthly receive limit is 1,000.00 EUR, and 960.00 EUR … used": the tier comes from A1, so A3 does not state it. This breaks the v4 one-fact-per-sentence rule. |
| T-0018 | [4] A2 | writer | "The two 5.00 EUR charges are **inactivity fees**": A2 says only "fee, 5.00 EUR". Naming which fee was charged is forbidden in the prompt. |
| T-0018 | [5] A1 | writer | Same sentence: A1 says only "fee, 5.00 EUR". (The faithfulness judge accepted this claim. The citation judge is right.) |
| T-0018 | [1] close-account#0 | writer | "…, which stops further fees [3][1]": close-account#0 says only that closing is free. Same inference as the faithfulness item. |
| T-0001 | [2] payment-request-card-availability#1 | **judge** | "first 3 Payment Requests paid by card become available after 7 calendar days, otherwise after 3 business days": the source states both rules word for word. |
| T-0001 | [3] A1 | **judge** | "TX-000027 (95.00 EUR) … pending [3][4][5]": A1 states exactly that. [5] A3 passed in the same sentence with the same structure. |
| T-0001 | [4] A2 | **judge** | Same as [3]: A2 states "TX-000026, 180.00 EUR, status pending". |

## Key-fact coverage (0.60; red line 0.75)

| Ticket | Fact | Class | Evidence |
|---|---|---|---|
| T-0013 | 1 ERR-1020 = limit reached; Starter €1,000 | **judge** | The draft says both, in two sentences: "means the payment is over the monthly receive limit of your tier" / "Your Starter tier monthly receive limit is 1,000.00 EUR". The judge allows only one quote, so it marks the fact absent. |
| T-0013 | 2 600 + 360 received, so 250 went over | writer | The draft gives "960.00 EUR … already used" (A3) but not TX-000044/045 (A7/A8 were available), and it does not say that the payment went over the limit. |
| T-0013 | 4 Starter cannot ask for an increase | writer (retrieval) | limit-increase was not retrieved. The draft paraphrases ERR-1020 correctly ("on Verified and Business tiers, ask for a limit increase"). |
| T-0013 | 5 Verified €30k; proof of address + source of income | writer (retrieval) | No retrieved chunk names the Verified documents. |
| T-0013 | 6 ERR-4010 = proof of address older than 3 months | writer (retrieval) | The draft gives "rejected with error code ERR-4010" and no meaning. error-codes#ERR-4010 and verification-rejected were not retrieved. |
| T-0018 | 1 balance zero, else ERR-5012 | writer | ERR-5012 is in the cited close-account#0 but left out of the draft. |
| T-0018 | 2 withdrawal €1.50 SEPA / €15 SWIFT | writer | withdrawal-fees#0 was retrieved but not used. |
| T-0018 | 4 €5/month after 12 months, never below zero | writer (retrieval) | The first half is present. The "Your balance never goes below zero" section was not retrieved. |
| T-0018 | 5 using the account once in 12 months avoids the fee | writer | This is in the cited inactivity-fee#3 but left out of the draft. |
| T-0006 | 4 failed withdrawal costs nothing | writer | A1 states "fee 0.00 EUR", but the draft leaves it out. (v4's list of what an account sentence may say does not include the fee.) |
| T-0009 | 3 KYC takes 1–2 business days | writer (retrieval) | The customer asked "how long". verification-times#0 was not among the retrieved chunks. |
| T-0007 | 1 payment to CoinHarbor Exchange … ERR-3005 | label | The merchant name appears only in the ticket. The draft states "TX-000281 of 500.00 USD failed with error code ERR-3005". |
| T-0002 | 1 275.00 from Saltmarsh Games Ltd … | label | The merchant name appears only in the ticket. The draft states everything else in the fact. |
| T-0020 | 1 payment to Aerolume Travel … ERR-3009 | label | The merchant name appears only in the ticket. The draft states "TX-000084 of 1140.00 USD failed with error code ERR-3009". |
| T-0008 | 2 pending card payments count toward the limit | label (new) | This rule is in `docs/tallybridge-world.md` line 37 but in no KB article, so it is a KB gap. |
| T-0008 | 3 pending 8,950 + 4,880 + 1,450 > €15,000 | label (new) | This fact depends on fact 2, which no source states. Adding it up would also be a cause, which v4 forbids. |

## Counts

| Class | Faithfulness | Citation | Key facts | Total |
|---|---|---|---|---|
| writer | 4 | 5 | 5 | 14 |
| writer (retrieval) | 0 | 0 | 5 | 5 |
| judge | 0 | 3 | 1 | 4 |
| label | 0 | 0 | 5 | 5 |
| **all** | 4 | 8 | 16 | 28 |

**The writer class is the larger one: 19 of 28 items** (14 that a prompt change can fix and 5 that come from retrieval).

What the metrics would be if the judge and label errors were corrected, without new runs:

| Metric | Now | Corrected | Red line |
|---|---|---|---|
| Faithfulness | 3 of 9 tickets | 3 of 9 tickets (all writer) | 2 |
| Citation validity | 4 of 9 tickets | 3 of 9 tickets (T-0001 is only judge errors) | 1 |
| Key-fact coverage | 0.60 | about 0.77 | 0.75 |

The key-fact estimate assumes T-0008's facts 2 and 3 are dropped or a KB section is added. Three of those red lines are close to the line, and with 9 tickets one or two tickets of difference is noise.

## Patterns

1. **The writer adds its own conclusions to sentences that carry a marker.** This explains all 4 faithfulness items and all 5 writer citation items: "since… crypto", "balance is not the cause", "are inactivity fees", "stops further fees", "can withdraw once available", and the Starter tier put into the A3 sentence. v4 forbids inference only in sentences with an account-fact marker, so the writer moves the inference into a help-centre sentence (T-0007 [3]) or into a sentence without a marker (T-0001).
2. **The writer leaves out facts that are in the sources** on two-intent and multi-fact tickets: T-0018 (686 of 1,000 characters), T-0013 and T-0006. The 1,000-character cap is not the reason.
3. **Retrieval misses supporting articles** that are not the top article: limit-increase, verification-rejected, verification-times#0 and inactivity-fee "below zero" (5 key facts). A prompt change cannot fix these.

Judge errors (4) are a minority. Two of them show patterns to check after the writer fix: the citation judge is inconsistent on sentences with several account-fact markers (T-0001), and the key-facts judge allows only one quote, which fails a fact stated across two sentences (T-0013).

## Proposed fix (writer class): `prompts/write/v5.md`

In "Stay with the sources", replace the bullet "A sentence with a marker holds only facts from the source it cites. …" with:

> - Every sentence, with or without a marker, says only what the sources it cites state. Add no conclusion of your own: no "since", "so" or "which stops" that applies a rule to this customer, no statement that something is or is not the cause, and no merchant category or fee type that no source names (an account fact that says "fee" does not say "inactivity fee"). When the customer asks why, give the account fact and the rule in separate sentences, each with its own marker, and do not join them. A detail from another source (for example the tier from `A1`) needs that source's marker. Leave out any sentence you cannot mark.

**Expected effect:**

- Removes the 4 unsupported claims, so faithfulness goes from 3 of 9 to 0 of 9 tickets.
- Removes the 5 writer marker errors, so citation validity goes from 4 of 9 to 1 of 9 tickets. The remaining one is T-0001, which is only judge errors.
- Risk: shorter answers for "why" questions, such as T-0018, and possibly one fewer key fact.

**How to measure** (the owner runs it): `make eval` with write v5, compared with this run as base. Check:

- faithfulness tickets and citation tickets;
- that key-fact coverage does not drop by more than one ticket's worth;
- by hand, the drafts of T-0007, T-0018, T-0001 and T-0013.
