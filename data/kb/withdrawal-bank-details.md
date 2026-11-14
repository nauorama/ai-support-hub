---
id: withdrawal-bank-details
title: Bank details errors on withdrawals (IBAN, SWIFT/BIC, SEPA)
product: withdrawals
visibility: external
intents: [withdrawal_issue]
updated_at: 2026-11-14
---
## SEPA or SWIFT

- **SEPA** is for EUR to a bank account in the EU. It costs €1.50 and takes 1 business day if you request it before 15:00 CET.
- **SWIFT** is for everything else. It costs €15 and takes 2–5 business days.

## ERR-2010: IBAN invalid

The IBAN you entered is not valid.

1. Ask your bank for your IBAN or copy it from your bank's app.
2. Check every character, including the country code at the start.
3. Enter the IBAN again.

## ERR-2011: SWIFT/BIC details invalid

The SWIFT/BIC code or other SWIFT details are not valid.

1. Ask your bank for the correct SWIFT/BIC code for international payments.
2. Check the code and the other bank details.
3. Create the withdrawal again.

## ERR-2012: SEPA not available

SEPA is only for EUR to a bank account in the EU. You see ERR-2012 if you choose SEPA for another currency or for a country where SEPA is not available. Use a SWIFT withdrawal instead.

## ERR-2003: beneficiary bank rejected

The receiving bank rejected the withdrawal. The money goes back to your balance in 3–7 business days, without a fee. Check the bank details with your bank, then create the withdrawal again.

## Own name only

The bank account must be in your own name. If the name does not match, the app shows ERR-2015.
