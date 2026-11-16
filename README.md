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

Python 3.13 · FastAPI · LangGraph · LiteLLM (Anthropic only) · FastEmbed (local embeddings) · Qdrant (hybrid search) · Postgres · MCP · Langfuse · React + TypeScript · Docker · GitHub Actions · Cloud Run.

**Budget mode** ([ADR-0018](docs/adr/0018-budget-mode.md)): the whole project spends about $5 on model calls. Every model role runs on Claude Haiku 5.5 (fallback Claude Sonnet 5.5); embeddings come from a local model. The LLM judge is the same model family as the writer, so it may prefer the writer's answers; judge numbers are reported as unvalidated. Evaluation uses about 20 tickets and 16 attacks: a demo check that the system works, not a proof of quality ([eval plan](docs/eval-plan.md)).

## How this project is built

The design package was written first. A coding agent builds the system phase by phase from [the build plan](docs/build-plan.md); the owner reviews each phase, checks the attack set and makes the risk decisions (whether auto-reply goes on in the demo). Each phase ends with a walkthrough for the owner, written locally to `docs/walkthrough/`. Walkthroughs are personal notes: the folder is git-ignored and they are not in this repository.

## CI and branch protection

CI (`.github/workflows/ci.yml`, see [operations](docs/operations.md) §5) runs on every pull request and every push to `main`. It has four required jobs: `lint` (ruff, mypy strict, import-linter), `test` (unit tests, with no provider API key), `data-check`, and `security` (gitleaks over the whole history, pip-audit). Each runs the same `make` target you run locally. A fifth job, `hook-bootstrap`, is **informational and not required**: it shows that the gitleaks pre-commit hook installs on a machine with no Go and no pre-commit cache. It downloads Go from go.dev, so an outage there must not block a merge.

Branch protection is **not turned on yet**. GitHub does not offer branch protection or rulesets for a private repository on the free plan, and this repository stays private until phase 12. Until then the rules below hold **by process**: the coding agent opens pull requests and never merges, and only the owner merges, after CI is green.

The owner turns branch protection on in phase 12, when the repository becomes public (Settings → Rules → Rulesets, or Settings → Branches). The rule for `main`:

- Changes reach `main` only through a pull request. Nobody pushes to `main` directly, admins included.
- Required status checks: `lint`, `test`, `data-check`, `security` (not `hook-bootstrap`). The branch must be up to date with `main` before merging.
- Every review conversation must be resolved before merging.
- No force-pushes, and `main` cannot be deleted.
- One task = one pull request (see [build process](docs/build-process.md)). The coding agent opens it; only the owner merges.

## Honest limits

- Synthetic company, synthetic customers, synthetic tickets. Numbers show that the method works, not how a real support team would perform.
- Designed with PCI-DSS- and GDPR-inspired constraints (card-number masking, data minimisation, audit log). Not certified.
- English only. One channel (chat).
