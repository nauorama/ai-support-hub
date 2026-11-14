---
id: internal-escalation-procedures
title: "Internal: escalation procedures and SLAs"
product: general
visibility: internal
intents: [dispute_chargeback, login_security, card_lost_stolen, limits, account_restricted, other]
updated_at: 2026-11-14
---
## Which tool for which case

- **Jira Service Management:** for cases that need work by another team. Use it for disputes, fraud, bugs and limit requests. Choose the matching issue type: dispute, fraud, bug or limit_request.
- **Slack channel `#support-escalations`:** for urgent cases only. Post there when a case needs a human at once.

The AI never creates a Jira issue or a Slack post by itself. It can only propose a Jira issue; an operator must approve it.

## What counts as urgent

These cases are urgent:

1. fraud and unauthorised transactions,
2. lost or stolen card,
3. restricted account (ERR-5010).

All other cases are standard.

## Human SLAs

| Case | First human response |
|---|---|
| Standard | within 4 business hours |
| Urgent (fraud, lost card, restricted account) | within 30 minutes |

## Support hours and out-of-hours cases

Human support hours are Mon–Fri 08:00–20:00 CET and Sat 10:00–16:00 CET.

Outside these hours, an on-call fraud analyst reads `#support-escalations`. Target: first human response within 60 minutes.

## What to write in an escalation

1. The ticket id and the customer reference (never a card number, CVV, PIN, password or 2FA code).
2. The error code and the transaction reference, if there is one.
3. What the customer asked for and what has been done so far (for example: card frozen by the customer).
4. Why the case needs another team.

## Cases that always go to a human

Restricted accounts, fraud cases, formal complaints, fee refunds, limit increase decisions, verification decisions and opening a dispute. The AI may give general information on these topics, but a human makes every decision.
