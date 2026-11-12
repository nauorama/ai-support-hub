# Enterprise AI Support Hub

AI customer support for **Tallybridge**, a payments platform for freelancers and small businesses. A LangGraph workflow answers routine tickets in seconds, with a source for every fact, and sends everything risky to a human operator with a prepared draft.

> Tallybridge is a fictional company made for this project. It has no relation to any real company. All data is synthetic.

**Status:** design package complete; build not started. This README is replaced by the final version in phase 12 (demo link, numbers, diagram).

## What the system does

1. A customer writes in the chat. Card numbers are masked before anything is stored.
2. The workflow classifies the ticket, searches the knowledge base, reads the customer's own account data through tools, writes an answer with citations and checks it.
3. Fixed policy rules and a calibrated confidence score choose a route: **auto-reply**, **operator approval** or **handoff**.
4. Operators review drafts in a console. A support lead sees quality, cost and automation numbers and can switch auto-replies off.
5. Every model call is traced and costed. Every decision is in an audit log. Quality is measured on labelled tickets before anything is automated.

## Documents

| Document | Question it answers |
|---|---|
| [docs/prd.md](docs/prd.md) | What do we build, for whom, and how do we know it works? |
| [docs/tallybridge-world.md](docs/tallybridge-world.md) | What are the facts of the fictional company? |
| [docs/design.md](docs/design.md) | How is the system built? Start here for architecture. |
| [docs/data-model.md](docs/data-model.md) | Which tables, index and files exist? |
| [docs/api.md](docs/api.md) | Which endpoints, events and MCP tools exist? |
| [docs/ai-pipeline.md](docs/ai-pipeline.md) | How does the workflow think: nodes, prompts, routing, memory? |
| [docs/eval-plan.md](docs/eval-plan.md) | How is quality measured, with which targets? |
| [docs/security.md](docs/security.md) | What can go wrong and what stops it? |
| [docs/operations.md](docs/operations.md) | How to run, observe, test and deploy? |
| [docs/ui.md](docs/ui.md) | What do the chat and the console look like? |
| [docs/adr/](docs/adr/README.md) | Why was each key decision made? |
| [docs/build-plan.md](docs/build-plan.md) | In which order is it built, task by task? |
| [docs/build-process.md](docs/build-process.md) | How is the build organised: sessions, agent roles, reviews, what the owner does? |
| [AGENTS.md](AGENTS.md) | Rules for coding agents. |

## Technology

Python 3.13 · FastAPI · LangGraph · LiteLLM (Anthropic + OpenAI) · Qdrant (hybrid search) · Postgres · MCP · Langfuse · React + TypeScript · Docker · GitHub Actions · Cloud Run.

## How this project is built

The design package was written first. A coding agent builds the system phase by phase from [the build plan](docs/build-plan.md); the owner reviews each phase, checks all evaluation data by hand and makes the risk decisions (confidence threshold, auto-reply on or off). Each phase ends with a walkthrough in `docs/walkthrough/`.

## Honest limits

- Synthetic company, synthetic customers, synthetic tickets. Numbers show that the method works, not how a real support team would perform.
- Designed with PCI-DSS- and GDPR-inspired constraints (card-number masking, data minimisation, audit log). Not certified.
- English only. One channel (chat).
