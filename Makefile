# Klaxon's commands. `make help` lists them. The stack needs Docker Desktop running.

SHELL := /bin/bash
# Docker Desktop puts its CLI in ~/.docker/bin. Use the full path (make finds commands with the PATH it
# started with), and add the folder to PATH for docker's own helpers, in case this shell predates the install.
DOCKER := $(or $(shell command -v docker 2>/dev/null),$(HOME)/.docker/bin/docker)
export PATH := $(HOME)/.docker/bin:$(PATH)
export DOCKER_CLI_HINTS := false
export LEDGER_HANDLER := $(or $(HANDLER),idempotent)

# Only ever this repo's own project on this Mac: ignore variables that could point docker somewhere else.
unexport DOCKER_HOST DOCKER_CONTEXT COMPOSE_FILE COMPOSE_PROJECT_NAME COMPOSE_PROFILES

PY := .venv/bin/python
COMPOSE := $(DOCKER) compose --file compose.yaml --project-name klaxon

.PHONY: help venv test lint up down reset ps logs test-integration e2e mutants check demo-live local-docker-only

help: ## list the commands
	@grep -E '^[a-z0-9-]+:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  make %-17s %s\n", $$1, $$2}'

venv: ## create .venv with Klaxon, its test tools and the service libraries
	python3.12 -m venv .venv
	.venv/bin/pip install -e '.[dev,services]'

test: ## offline tests, no Docker needed
	$(PY) -m pytest -q

lint: ## ruff: style and common mistakes
	.venv/bin/ruff check src tests scripts
	.venv/bin/ruff format --check src/klaxon/patient/services src/klaxon/patient/stack.py src/klaxon/checks/live.py \
		src/klaxon/demo/live.py tests/services tests/integration tests/e2e tests/test_live_check.py

up: local-docker-only ## build and start the stack, wait until every service is healthy (HANDLER=naive: bad baseline)
	$(COMPOSE) up --detach --build --wait

down: ## stop the stack, keep its data
	$(COMPOSE) down

reset: local-docker-only ## stop the stack and delete all of its data
	$(COMPOSE) down --volumes --remove-orphans

local-docker-only:
	@endpoint="$$($(DOCKER) context inspect --format '{{.Endpoints.docker.Host}}')"; \
	case "$$endpoint" in unix://*) ;; *) echo "refusing: docker points at $$endpoint, not this Mac"; exit 1;; esac

ps: ## what is running, and is it healthy
	$(COMPOSE) ps --all

logs: ## follow every service's logs
	$(COMPOSE) logs --follow --tail=50

test-integration: ## tests against the running stack's PostgreSQL and Kafka (make up first)
	$(PY) -m pytest -q -m integration

e2e: ## reset the stack and drive it end to end (about a minute)
	$(PY) -m pytest -q -m e2e

mutants: ## break the code on purpose, on a copy, and check the tests notice (make up first)
	$(PY) scripts/mutation_check.py "$(CURDIR)" "$${TMPDIR:-/tmp}/klaxon-mutants" --with-stack

check: ## run the money check against the running stack
	$(PY) -m klaxon.checks.live

demo-live: ## the first piece's scenario on the real stack, on fresh data: make demo-live HANDLER=naive|idempotent
	$(MAKE) reset
	$(MAKE) up
	$(PY) -m klaxon.demo live-duplicate-webhook --handler $(LEDGER_HANDLER)
