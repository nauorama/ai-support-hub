---
id: incoming-payment-not-arrived
title: My incoming payment has not arrived
product: account
visibility: external
intents: [payment_pending]
updated_at: 2026-11-14
---
## First, check the normal time

Count in business days (Monday to Friday, without the closing days of the euro payment system):

- SEPA: 1 business day. SEPA Instant: under 1 minute.
- ACH (USD): 1–3 business days.
- Faster Payments (GBP): same day.
- Payment Request paid by card: pending at once, available after 3 business days (new accounts: 7 calendar days for your first 3 Payment Requests paid by card).

If the normal time has not passed yet, please wait.

## Check the details your payer used

1. Did the payer use the receiving details for the right currency? EUR goes to your IBAN, USD to your account and routing number, GBP to your sort code and account number.
2. Did the payer send EUR, USD or GBP? Other currencies cannot be received (ERR-1001).
3. Ask the payer for proof of payment with the date and the details they used.

## Check the error code in the app

If the payment is in the app with an error code, it tells you what happened:

- **ERR-1001:** the currency is not supported. Only EUR, USD and GBP can be received.
- **ERR-1004:** the sender name does not match. Contact us with the payment details.
- **ERR-1008:** your account is not verified, so the payment cannot be received. Finish your identity check in the app.
- **ERR-1012:** the payment is held for review. This takes up to 3 business days. We may ask for an invoice or contract.
- **ERR-1020:** you reached the monthly receive limit of your tier.

## Monthly receive limits

| Starter | Verified | Business |
|---|---|---|
| €1,000 | €30,000 | €250,000 |

Monthly limits are in EUR equivalent and reset on the 1st day of the month.

## When to contact us

Contact us if the normal time has passed and the payment is not in the app. Include the amount, the currency, the date the payer sent it and the payer's proof of payment.
