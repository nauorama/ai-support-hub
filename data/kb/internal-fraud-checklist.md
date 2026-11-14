---
id: internal-fraud-checklist
title: "Internal: fraud and unauthorised transaction checklist"
product: security
visibility: internal
intents: [login_security, card_lost_stolen, dispute_chargeback]
updated_at: 2026-11-14
---
## Who handles fraud

Only humans handle fraud cases. The AI may tell the customer to freeze the card at once, then it hands the ticket to a human. Fraud is urgent: first human response within 30 minutes. Outside support hours, the on-call fraud analyst reads `#support-escalations` (target: first human response within 60 minutes).

## Checklist

1. **Never ask for card data.** Do not ask for the full card number, CVV, PIN, password or 2FA code. Identify the card by the last 4 digits shown in the console.
2. **Freeze the card.** Check that the card is frozen. If the customer has not frozen it, freeze it now.
3. **Collect the transactions.** For each transaction the customer did not make: date, amount, currency, merchant or recipient, transaction reference.
4. **Check the reporting window.** The customer must report unauthorised transactions within 13 months. Note the date of the oldest transaction in the case.
5. **Open a fraud case.** Create a Jira issue of type fraud with the facts above. Never paste card data into Jira.
6. **Post in `#support-escalations`** with the ticket id and the Jira key.
7. **Replacement card.** A lost or stolen card must be replaced. The replacement is a new delivery with the delivery fee (€9.90 standard, €24.90 express).
8. **Answer the customer.** Confirm the card is frozen and that a fraud case is open. Do not promise a refund or an outcome.

## Account takeover signs

Treat these as possible fraud too: login locks (ERR-5001) the customer did not cause, 2FA codes the customer did not request, changed details the customer does not recognise, withdrawals the customer did not make. Follow the same checklist.

## Fraud or dispute?

- The customer did **not** make or allow the payment → fraud case (this checklist).
- The customer made the payment but has a problem with the merchant → dispute (120-day window, see the dispute approval article).
