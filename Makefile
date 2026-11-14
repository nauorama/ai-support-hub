# Commands from docs/operations.md §1. Targets that are not built yet print
# "not implemented" and exit with an error, so nobody mistakes a stub for a passing step.
# When you implement a target, move it from NOT_IMPLEMENTED to BUILT: the unit tests check
# (in make's database, without running anything) that every NOT_IMPLEMENTED target is the stub.
# Do not use $(shell), $(eval) or include here: make runs them while reading the file.

SHELL := /bin/bash
.DEFAULT_GOAL := help

BUILT := setup lint test data-check up down dev test-int migrate seed
NOT_IMPLEMENTED := kb-load test-e2e \
	eval eval-smoke eval-attacks eval-report eval-judges web-types retention \
	record-llm leak-scan export-review-candidates rollback

.PHONY: help $(BUILT) $(NOT_IMPLEMENTED)

help:
	@echo "Built:           $(BUILT)"
	@echo "Not implemented: $(NOT_IMPLEMENTED)"

setup:
	uv sync --all-packages --locked
	@if [ -f apps/web/package.json ]; then pnpm install; else echo "web: apps/web has no package.json yet, skipping pnpm install"; fi
	@if [ ! -f .env ]; then cp .env.example .env && echo "created .env from .env.example"; fi
	uv run pre-commit install

lint:
	uv run ruff check .
	uv run ruff format --check .
	uv run mypy
	uv run lint-imports --no-logo
	@if [ -f apps/web/package.json ]; then pnpm -C apps/web lint && pnpm -C apps/web typecheck; else echo "web: apps/web has no package.json yet, skipping eslint and tsc"; fi

# Coverage gate for deterministic safety code (AGENTS.md, security §3): >= 95% line coverage.
# hub.domain holds the ticket status transitions and the seed rules; hub.audit.chain the audit
# hash chain and the masking of audit details; hub.auth the password hashing (login joins in
# P2-07). Add tools/permissions here when it is built.
SAFETY_COVERAGE := --cov=hub.guardrails --cov=hub.domain --cov=hub.audit.chain --cov=hub.auth --cov-report=term-missing:skip-covered --cov-fail-under=95

test:
	uv run pytest $(SAFETY_COVERAGE)

data-check:
	uv run python -m hub_evals.data_check data

COMPOSE := docker compose -f infra/docker-compose.yml

# --wait returns when every service is healthy (the api healthcheck uses /health).
up:
	$(COMPOSE) up --detach --build --wait

down:
	$(COMPOSE) down

# Alembic upgrade as the migrator (DATABASE_URL_DIRECT). The LangGraph checkpointer setup
# (operations §1) joins here in phase 6.
migrate:
	uv run alembic -c infra/alembic.ini upgrade head

# Demo customers, operators (password from SEED_OPERATOR_PASSWORD) and default settings, as the
# app role (DATABASE_URL). Idempotent: a second run changes nothing.
seed:
	uv run python -m hub.seed data/seed

# operations §1: dev = up + migrate + seed + kb-load. kb-load joins here when it is built (P2-09).
# The check runs first and alone: with required settings empty (SEED_OPERATOR_PASSWORD,
# SESSION_SECRET, DEMO_ACCESS_CODE) it lists them all and stops before anything starts. The
# steps run through a sub-make, in this order, so the check always comes before them.
dev:
	uv run python -m hub.dev_check
	$(MAKE) --no-print-directory up migrate seed
	@echo "dev: stack is up. API http://127.0.0.1:8000 (/health; /ready is 503 until the MCP server, P7)"

# Integration tests start their own Postgres and Qdrant with testcontainers (needs Docker).
test-int:
	uv run pytest apps/api/tests/integration

$(NOT_IMPLEMENTED):
	@echo "make $@: not implemented"
	@exit 1
