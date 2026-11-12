# Commands from docs/operations.md §1. Targets that are not built yet print
# "not implemented" and exit 1, so nobody mistakes a stub for a passing step.

SHELL := /bin/bash
.DEFAULT_GOAL := help

BUILT := setup lint test
NOT_IMPLEMENTED := up down migrate seed kb-load dev test-int test-e2e data-check \
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
	@echo "import-linter: contracts are added in P0-02"
	@if [ -f apps/web/package.json ]; then pnpm -C apps/web lint && pnpm -C apps/web typecheck; else echo "web: apps/web has no package.json yet, skipping eslint and tsc"; fi

test:
	uv run pytest

$(NOT_IMPLEMENTED):
	@echo "make $@: not implemented"
	@exit 1
