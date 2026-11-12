# ADR-0012: Customer memory as ticket summaries in Postgres

- Status: accepted
- Date: 2026-11-12

## Context
Repeat customers should not explain everything again, and operators want the history (FR-20). Memory holds personal data (GDPR) and can carry wrong facts into new answers.

## Decision
When a ticket is closed, a small model writes a one-to-two sentence summary into `customer_memory` (masked, with an expiry date). The next run loads the five newest. Memory is context only: facts in answers still need a citation from the knowledge base or account data.

## Alternatives considered
- **A memory product (Mem0, Zep).** More features than needed; another data store with personal data.
- **Vector search over all past messages.** More recall, more risk of pulling old or wrong details; not needed at five items.
- **LangGraph Store.** A good fit technically; a plain table is easier to query, expire and erase.

## Consequences
Simple to inspect, expire and delete. The effect is measured with the repeat-customer tickets in the eval set.
