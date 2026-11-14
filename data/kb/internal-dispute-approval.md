---
id: internal-dispute-approval
title: "Internal: approving and opening card disputes"
product: card
visibility: internal
intents: [dispute_chargeback]
updated_at: 2026-11-14
---
## Who opens a dispute

The AI may explain how disputes work. Opening a dispute needs a human: the AI can only propose a Jira issue of type dispute, and an operator must approve it.

## Before you approve

1. **Fraud or dispute?** If the customer did not make or allow the payment, it is fraud, not a dispute. Use the fraud checklist.
2. **Window.** The dispute window is 120 days from the transaction date. Check the date in the console.
3. **Merchant refund.** If the merchant has already promised a refund, tell the customer that a merchant refund takes 5–10 business days. Keep the 120-day window in mind.
4. **Details.** Make sure you have the transaction reference, the date, the amount, the merchant and the customer's explanation.

## Opening the dispute

1. Approve the proposed Jira issue of type dispute, or create one.
2. Add the facts above and any proof the customer sent. Never paste card data.
3. Tell the customer the dispute is open and the decision takes up to 45 days, complex cases up to 90 days.

## Outside the window

If the transaction date is more than 120 days ago, do not open a dispute. Explain the window to the customer. If the customer says the payment was not made by them, it may still be a fraud case: unauthorised transactions can be reported within 13 months.

## What not to promise

Do not promise the outcome of a dispute or a refund.

## SLA

A dispute is a standard case: first human response within 4 business hours.
