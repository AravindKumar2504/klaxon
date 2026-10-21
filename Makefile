# Klaxon's commands. `make help` lists them. The stack needs Docker Desktop running.

SHELL := /bin/bash
# Docker Desktop puts its CLI in ~/.docker/bin. Use the full path (make finds commands with the PATH it
# started with), and add the folder to PATH for docker's own helpers, in case this shell predates the install.
DOCKER := $(or $(shell command -v docker 2>/dev/null),$(HOME)/.docker/bin/docker)
export PATH := $(HOME)/.docker/bin:$(PATH)
export DOCKER_CLI_HINTS := false
export LEDGER_HANDLER := $(or $(HANDLER),idempotent)
export CONFIRMER_HANDLER := $(or $(CONFIRMER),by_our_id)
# The known-bad "retry forever" setting only ever comes from a fault (make fault-inject), never from a stray variable.
export LEDGER_BAD_EVENTS := dead_letter
export CONFIRMER_BAD_EVENTS := dead_letter
# The same for the payments API's circuit breaker: off, the known-bad old behavior, only ever comes from a deploy.
export PROVIDER_BREAKER := on

# Only ever this repo's own project on this Mac: ignore variables that could point docker somewhere else.
unexport DOCKER_HOST DOCKER_CONTEXT COMPOSE_FILE COMPOSE_PROJECT_NAME COMPOSE_PROFILES

PY := .venv/bin/python
COMPOSE := $(DOCKER) compose --file compose.yaml --project-name klaxon

.PHONY: help venv test lint up down reset ps logs test-integration e2e mutants check demo-live demo-lost-answer demo-lost-refund \
	traffic check-under-traffic faults fault-inject fault-clear demo-fault local-docker-only tools tool \
	claude-command claude-check loop-prompt investigate replay-run alerts alerts-under-traffic investigate-fault grade-run \
	approve gate-demo

help: ## list the commands
	@grep -E '^[a-z0-9-]+:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  make %-20s %s\n", $$1, $$2}'

venv: ## create .venv with Klaxon, its test tools and the service libraries
	python3.12 -m venv .venv
	.venv/bin/pip install -e '.[dev,services]'

test: ## offline tests, no Docker needed
	$(PY) -m pytest -q

lint: ## ruff: style and common mistakes
	.venv/bin/ruff check src tests scripts
	.venv/bin/ruff format --check src/klaxon/patient/services src/klaxon/patient/stack.py src/klaxon/checks/live.py \
		src/klaxon/demo/live.py src/klaxon/demo/lost_answer.py src/klaxon/demo/under_traffic.py src/klaxon/traffic.py \
		tests/services tests/integration tests/e2e tests/test_live_check.py tests/test_payments_match_provider.py \
		tests/test_observability_config.py tests/test_traffic.py tests/test_money_cutoff.py tests/test_faults.py \
		src/klaxon/faults.py src/klaxon/demo/fault_scenario.py src/klaxon/demo/lost_refund.py tests/test_refunds_match_provider.py \
		src/klaxon/agent tests/agent src/klaxon/alerts.py src/klaxon/fault_runs.py src/klaxon/grading.py \
		tests/test_alerts.py tests/test_fault_runs.py tests/test_grading.py tests/conftest.py \
		src/klaxon/patient/deploys.py src/klaxon/demo/gate_demo.py tests/test_deploys.py tests/fake_docker.py

up: local-docker-only ## build, start, wait until healthy (bad baselines: HANDLER=naive, CONFIRMER=by_provider_id)
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

e2e: ## reset the stack and drive it end to end (about five minutes)
	$(PY) -m pytest -q -m e2e

mutants: ## break the code on purpose, on a copy, and check the tests notice (make up first)
	$(PY) scripts/mutation_check.py "$(CURDIR)" "$${TMPDIR:-/tmp}/klaxon-mutants" --with-stack

check: ## run the money check against the running stack
	$(PY) -m klaxon.checks.live

tools: ## list Klaxon's read-only tools, the ones its MCP server serves (week 3)
	$(PY) -m klaxon.agent.mcp_server --list

NAME ?= service_health
ARGS ?= {}
tool: ## call one read-only tool and print what it returns: make tool NAME=recent_deploys ARGS='{}'
	$(PY) -m klaxon.agent.mcp_server --call '$(NAME)' '$(ARGS)'

claude-command: ## print the exact command of each live model call (claude -p, locked down); calls nothing (week 3)
	$(PY) -m klaxon.agent.claude_code command

claude-check: ## one tiny live call to Claude on Aravind's seat ("Reply with the word ok"), saved to evidence/week3/
	$(PY) -m klaxon.agent.claude_code check

# The loop's values reach its command line through the environment (make passes on what it was given), never
# pasted into the shell's text: an apostrophe in an alert cannot break the command or run as one.
LOOP_ARGS = --alert "$$ALERT" --alert-name "$${ALERT_NAME:-manual}" $${STEPS:+--steps "$$STEPS"} \
	$${TOOL_CALLS:+--tool-calls "$$TOOL_CALLS"} $${TIME_LIMIT:+--seconds "$$TIME_LIMIT"}
loop-prompt: ## print what the loop tells the model, for an alert; calls no model: make loop-prompt ALERT='...'
	$(PY) -m klaxon.agent.loop prompt $(LOOP_ARGS)

investigate: ## one live run of the loop: Claude on Aravind's seat, the running stack (STEPS=, TOOL_CALLS=, TIME_LIMIT=seconds)
	$(PY) -m klaxon.agent.loop run $(LOOP_ARGS)

replay-run: ## replay a recorded run offline and check it reproduces: make replay-run RUN=evidence/week3/<run folder>
	$(PY) -m klaxon.agent.loop replay "$$RUN"

alerts: ## the alert rules (symptoms only) read the running stack once, and print what they read; reads only (WATCH=seconds)
	$(PY) -m klaxon.alerts $${WATCH:+--watch "$$WATCH"}

alerts-under-traffic: ## traffic and no fault: do the alert rules stay quiet? (MINUTES=5, RATE=3), saved to evidence/week4/
	$(PY) -m klaxon.fault_runs quiet $${MINUTES:+--minutes "$$MINUTES"} $${RATE:+--rate "$$RATE"}

investigate-fault: ## one gated live run on a fault: Klaxon may ask to act (make approve answers), verifies, then the fault is cleared (his OK first; NO_ACTIONS=1: diagnosis only)
	$(PY) -m klaxon.fault_runs run "$$FAULT" $${STEPS:+--steps "$$STEPS"} $${TOOL_CALLS:+--tool-calls "$$TOOL_CALLS"} \
		$${TIME_LIMIT:+--seconds "$$TIME_LIMIT"} $${NO_ACTIONS:+--no-actions}

grade-run: ## compare a fault run's diagnosis with the fault's recorded cause, by the rule; offline: make grade-run RUN=evidence/week4/<folder>
	$(PY) -m klaxon.fault_runs grade "$$RUN"

approve: ## approve or deny the action a run asks for, in your own terminal, by typing its request id: make approve RUN=<folder>
	@test -n "$$RUN" || { echo "make approve RUN=<the run's folder>: RUN is missing" >&2; exit 2; }
	$(PY) -m klaxon.agent.gate approve "$$RUN"

gate-demo: ## the approval gate, offline: a made-up stack and diagnosis; answer it with make approve in a second terminal
	$(PY) -m klaxon.demo.gate_demo

traffic: ## synthetic customers until Ctrl-C (RATE=2 payments/s, DURATION=seconds, SEED=7)
	$(PY) -m klaxon.traffic --rate $(or $(RATE),2) --seed $(or $(SEED),7) $(if $(DURATION),--seconds $(DURATION))

check-under-traffic: ## the money check again and again while traffic flows (CHECKS=20, RATE=5)
	$(PY) -m klaxon.demo live-check-under-traffic --checks $(or $(CHECKS),20) --rate $(or $(RATE),5)

faults: ## the faults Klaxon will be graded on: their causes, symptoms and remedies
	$(PY) -m klaxon.faults list

fault-inject: ## inject one fault into the running stack: make fault-inject FAULT=poison-message
	$(PY) -m klaxon.faults inject $(FAULT)

fault-clear: ## remove its cause (a rollback, or the outage ending): make fault-clear FAULT=poison-message
	$(PY) -m klaxon.faults clear $(FAULT)

demo-fault: ## a fault under traffic, recorded as evidence (about 3 minutes): make demo-fault FAULT=provider-outage
	$(PY) -m klaxon.demo live-fault --fault $(FAULT)

demo-live: ## the first piece's scenario on the real stack, on fresh data: make demo-live HANDLER=naive|idempotent
	$(MAKE) reset
	$(MAKE) up
	$(PY) -m klaxon.demo live-duplicate-webhook --handler $(LEDGER_HANDLER)

demo-lost-answer: ## a charge whose answer is lost, on fresh data: make demo-lost-answer CONFIRMER=by_our_id|by_provider_id
	$(MAKE) reset
	$(MAKE) up
	$(PY) -m klaxon.demo live-lost-answer --confirmer $(CONFIRMER_HANDLER)

demo-lost-refund: ## a refund whose answer is lost, on fresh data: make demo-lost-refund CONFIRMER=by_our_id|by_provider_id
	$(MAKE) reset
	$(MAKE) up
	$(PY) -m klaxon.demo live-lost-refund --confirmer $(CONFIRMER_HANDLER)
