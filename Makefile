# Commands from docs/operations.md §1. Targets that are not built yet print
# "not implemented" and exit with an error, so nobody mistakes a stub for a passing step.
# When you implement a target, move it from NOT_IMPLEMENTED to BUILT: the unit tests check
# (in make's database, without running anything) that every NOT_IMPLEMENTED target is the stub.
# Do not use $(shell), $(eval) or include here: make runs them while reading the file.

SHELL := /bin/bash
.DEFAULT_GOAL := help

BUILT := setup lint test data-check up down dev test-int
NOT_IMPLEMENTED := migrate seed kb-load test-e2e \
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
# Add the other safety modules (tools/permissions, auth) here when they are built.
SAFETY_COVERAGE := --cov=hub.guardrails --cov-report=term-missing:skip-covered --cov-fail-under=95

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

# operations §1: dev = up + migrate + seed + kb-load. migrate, seed and kb-load join here when
# they are built (P2-02, P2-05, P2-09); until then dev only starts the stack.
dev: up
	@echo "dev: stack is up. API http://127.0.0.1:8000 (/health; /ready is 503 until the MCP server, P7)"

# Integration tests start their own Postgres and Qdrant with testcontainers (needs Docker).
test-int:
	uv run pytest apps/api/tests/integration

$(NOT_IMPLEMENTED):
	@echo "make $@: not implemented"
	@exit 1
