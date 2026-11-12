# ADR-0011: All external tools behind one MCP server; customer identity injected by the Hub

- Status: accepted
- Date: 2026-11-12

## Context
The workflow needs account data (FR-6) and actions in Jira and Slack (FR-14). Tools should be reusable from other clients (Claude Code) and have one place for validation. The main risk is the model reading data of the wrong customer or triggering actions (threats T4, T8).

## Decision
One MCP server, `support-tools`, with adapters for the core system (mock), Jira and Slack (mock or real). The Hub is the MCP client. The model sees read tools **without** a customer argument; the Hub adds the customer reference from the session. Tools with side effects are not callable by the model: the writer proposes actions from a closed list, and the `act` node runs them after operator approval, with idempotency keys.

## Alternatives considered
- **Call the adapters as plain Python functions.** Simpler, and enough inside one service. Rejected because the MCP boundary is a requirement and gives a reusable server.
- **Use public Jira/Slack MCP servers.** Too broad permissions; we need three narrow operations.
- **Let the model call write tools with a confirmation prompt.** A prompt is not a permission system.

## Consequences
A network hop and one more process. Clear permission model that can be unit-tested. The mock core adapter makes evaluation repeatable.
