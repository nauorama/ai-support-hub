---
id: internal-restricted-account-handling
title: "Internal: restricted accounts and the tipping-off rule"
product: account
visibility: internal
intents: [account_restricted]
updated_at: 2026-11-14
---
## What a restriction is

Compliance can restrict an account during a review. A restricted account cannot make outgoing payments. The customer sees ERR-5010.

`account_restricted` tickets are never answered by the AI. They always go to a human and are urgent: first human response within 30 minutes. Outside support hours, post the case in `#support-escalations`.

## The tipping-off rule

Staff and AI must **not** tell the customer the reason for the restriction or any details of the review. This is a legal rule (tipping-off). It applies to chat, email and phone.

Never:

1. state, hint at or confirm a reason,
2. say which payment, person or country the review is about,
3. confirm or deny a guess by the customer ("is it because of the payment from my client?"),
4. say that a report was made to any authority,
5. promise when or if the restriction will be lifted,
6. read out or paste the `restriction_reason` field or any Compliance note.

## The only allowed message

Use only this content, in your own friendly words:

> Your account is under review. Our team may contact you for documents. A review typically takes up to 10 business days.

You may add practical help that does not touch the review: how to upload documents when asked, support hours, and that staff never ask for a password, PIN, CVV or 2FA code.

## When the customer pushes

- Repeat the allowed message calmly. Do not add details.
- If the customer wants to complain, accept a formal complaint. It is handled by a human and gets a written final answer within 15 business days. The complaint answer follows the same tipping-off rule.
- If the customer threatens legal action, treat it as a complaint or legal matter (handled only by humans) and keep to the allowed message.

## Do not confuse with ERR-5011

ERR-5011 means the identity check is not finished. That is not a restriction. The customer can log in and upload documents, and normal verification information may be given.
