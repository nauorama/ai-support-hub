---
id: internal-verification-decisions
title: "Internal: KYC and KYB decisions belong to Compliance"
product: verification
visibility: internal
intents: [kyc_verification]
updated_at: 2026-11-14
---
## The rule

Verification decisions (KYC and KYB) are made by the Compliance team, never by the AI and never by a support operator. Support explains requirements, error codes and review times. Support does not approve, reject or predict a result.

## What support may say

- Which documents are needed per tier.
- What a rejection error code means (ERR-4001 to ERR-4015) and how to fix it.
- Review times: KYC (Starter, Verified) 1–2 business days; KYB (Business) 3–5 business days.
- That an account without a finished identity check cannot receive, hold or send money (EU anti-money-laundering rules), and that the customer can still log in and upload documents.

## What support must not say

1. "Your documents look fine, you will be approved."
2. Any hint about internal checks beyond the customer-facing error code.
3. Any reason beyond the error code for a rejection.
4. Anything about sanctions lists or screening for ERR-4020. The customer text is only: Tallybridge cannot open or serve accounts in that country.

## When to involve Compliance

Escalate to Compliance in Jira Service Management (a case that needs work by another team) when:

1. the review time has passed (more than 2 business days for KYC, more than 5 for KYB) and there is no decision,
2. the customer says a rejection is wrong and the documents meet the rules,
3. the app shows an error the customer cannot fix with a new upload.

## Document rules for reference

- ID: passport, national ID card, driving licence (EU/UK only).
- Proof of address: utility bill or bank statement not older than 3 months.
- Verified also needs proof of source of income. Business needs company documents and owners.

## Review vs restriction

A pending upgrade is not a restriction. If the account shows ERR-5010, follow the restricted-account procedure instead.
