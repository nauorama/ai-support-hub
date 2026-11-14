---
id: internal-limit-increase-handling
title: "Internal: handling limit increase requests"
product: account
visibility: internal
intents: [limits]
updated_at: 2026-11-14
---
## Who decides

A limit increase is decided by a human in the Compliance team. The AI and support operators must not promise an increase or say that one is likely.

## Who can ask

- **Starter:** no limit increase (ERR-5020). Explain the Verified tier: proof of address and proof of source of income, KYC review 1–2 business days.
- **Verified and Business:** can ask for an increase with proof of income.

## Steps

1. Check the customer's tier. If Starter, explain the upgrade and stop.
2. Ask which limit the customer wants to raise (receive per month, withdraw per day or month, card spend, ATM) and why.
3. Make sure the request includes proof of income. If it is missing, ask for it.
4. Create a Jira issue of type limit_request with the tier, the limit, the requested amount and the attached proof.
5. Tell the customer: the review takes 3 business days and Compliance decides.

## Current limits for reference

| | Starter | Verified | Business |
|---|---|---|---|
| Receive per month | €1,000 | €30,000 | €250,000 |
| Withdraw per day | €500 | €10,000 | €50,000 |
| Withdraw per month | €1,000 | €30,000 | €250,000 |
| Card spend per day | — | €5,000 | €15,000 |
| ATM withdrawal per day | — | €1,000 | €2,000 |

Limits are in EUR equivalent. Daily limits reset at 00:00 CET; monthly limits on the 1st day of the month.

## Urgent requests

A limit request is not an urgent case by itself. Do not post it in `#support-escalations` unless there is also fraud, a lost card or a restriction.

## Restricted accounts

If the account is restricted (ERR-5010), do not process a limit request. Hand the ticket to the restricted-account procedure.
