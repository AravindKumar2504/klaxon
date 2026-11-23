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
# And for the bad deploys of faults 4 to 6 (week 4): the money-unit bug, the connection leak, the rotated secret.
export CHARGE_AMOUNT_UNIT := cents
export REFUND_CONNECTION := returned
export WEBHOOK_SECRET_VERSION := v1

# Only ever this repo's own project on this Mac: ignore variables that could point docker somewhere else.
unexport DOCKER_HOST DOCKER_CONTEXT COMPOSE_FILE COMPOSE_PROJECT_NAME COMPOSE_PROFILES

PY := .venv/bin/python
COMPOSE := $(DOCKER) compose --file compose.yaml --project-name klaxon

.PHONY: help venv test test-go lint ci-linux up down reset ps logs test-integration e2e mutants check demo-live demo-lost-answer demo-lost-refund \
	traffic check-under-traffic faults fault-inject fault-clear demo-fault local-docker-only tools tool \
	claude-command claude-check loop-prompt loop-graph investigate replay-run alerts alerts-under-traffic investigate-fault grade-run \
	approve gate-demo rehearse-fault postmortem-facts postmortem replay-postmortem review-postmortem eval-cases eval eval-report rehearse-case \
	judge-prompt judge replay-judge label label-pick calibrate calibration-report replay-calibration \
	kind-up kind-down kind-ps kind-check kind-reader panel \
	openhands-venv openhands-snapshot openhands-run openhands-task openhands-compare

help: ## list the commands
	@grep -E '^[a-z0-9-]+:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  make %-20s %s\n", $$1, $$2}'

venv: ## create .venv with Klaxon, its test tools, the service libraries and LangGraph, every library pinned (requirements/graph.lock.txt and dev.lock.txt)
	python3.12 -m venv .venv
	.venv/bin/pip install -c requirements/graph.lock.txt -c requirements/dev.lock.txt -e '.[dev,services,graph]'

test: ## offline tests, no Docker needed
	$(PY) -m pytest -q

# The webhook receiver in Go (week 8, item 2). Go is not installed on this Mac: every Go command runs in the official
# golang image, pinned by digest, the one docker/receiver.Dockerfile builds in. go/receiver is mounted read-only, so
# nothing is written to this folder; the modules and Go's build cache stay in a Docker volume, klaxon-go-cache
# (docker volume rm klaxon-go-cache deletes them). The first run downloads the modules go.sum pins.
GO_IMAGE := golang:1.27.3-alpine@sha256:d200d2dd76b03a99b52651fd1325aff7dcd35422c35c01bac56571b0507f3deb
test-go: local-docker-only ## the Go receiver's checks in the official golang image: gofmt, go vet, go test (needs Docker)
	$(DOCKER) run --rm --mount type=bind,source="$(CURDIR)/go/receiver",target=/src,readonly \
		--mount type=volume,source=klaxon-go-cache,target=/cache --env GOCACHE=/cache/build --env GOMODCACHE=/cache/mod \
		--env CGO_ENABLED=0 --env GOFLAGS=-mod=readonly --workdir /src $(GO_IMAGE) sh -euc ' \
		unformatted="$$(gofmt -l .)"; if [ -n "$$unformatted" ]; then echo "gofmt would change: $$unformatted"; exit 1; fi; \
		go vet ./... && go test -count=1 ./...'

lint: ## ruff: style and common mistakes
	.venv/bin/ruff check src tests scripts experiments
	.venv/bin/ruff format --check src/klaxon/patient/services src/klaxon/patient/stack.py src/klaxon/checks/live.py \
		src/klaxon/demo/live.py src/klaxon/demo/lost_answer.py src/klaxon/demo/under_traffic.py src/klaxon/traffic.py \
		tests/services tests/integration tests/e2e tests/test_live_check.py tests/test_payments_match_provider.py \
		tests/test_observability_config.py tests/test_traffic.py tests/test_money_cutoff.py tests/test_faults.py \
		src/klaxon/faults.py src/klaxon/demo/fault_scenario.py src/klaxon/demo/lost_refund.py tests/test_refunds_match_provider.py \
		src/klaxon/agent tests/agent src/klaxon/alerts.py src/klaxon/fault_runs.py src/klaxon/grading.py \
		tests/test_alerts.py tests/test_fault_runs.py tests/test_grading.py tests/conftest.py \
		src/klaxon/patient/deploys.py src/klaxon/demo/gate_demo.py tests/test_deploys.py tests/fake_docker.py \
		tests/test_mutation_check.py src/klaxon/evaluation.py tests/test_evaluation.py src/klaxon/judge.py \
		src/klaxon/labels.py src/klaxon/calibration.py tests/test_judge.py tests/test_labels.py tests/test_calibration.py \
		src/klaxon/patient/cluster.py tests/test_cluster.py tests/test_rollback_on_kind.py tests/test_loop_choice.py \
		src/klaxon/panel tests/test_panel.py tests/test_panel_live.py tests/test_dev_lock.py tests/test_comparison.py \
		tests/test_go_receiver.py src/klaxon/openhands_task.py tests/test_openhands_task.py experiments/openhands

panel: ## the web panel on 127.0.0.1 (PORT=8090; ALSO=<folder>, such as make gate-demo's): it shows runs, changes nothing (week 7)
	$(PY) -m klaxon.panel --port $(or $(PORT),8090) $(if $(ALSO),--also $(ALSO))

# CI's offline job on Linux, here on this Mac (week 7, item 3), before a push: an official python image with CI's
# Python. The repo is mounted read-only and copied inside, without .venv, .kind, .git or anything built, then run by
# a user that is not root, as on GitHub's machines, with live model calls off. Nothing is written to this folder.
CI_IMAGE := python:3.12.16-trixie
ci-linux: local-docker-only ## CI's offline job in an official Linux python container: make venv, lint and test, on a copy (needs Docker)
	$(DOCKER) run --rm --init --mount type=bind,source="$(CURDIR)",target=/repo,readonly \
		--env KLAXON_NO_LIVE_MODEL=1 --env PIP_DISABLE_PIP_VERSION_CHECK=1 $(CI_IMAGE) bash -euo pipefail -c ' \
		useradd --create-home runner; mkdir /home/runner/work; \
		tar -C /repo --exclude=./.venv --exclude=./.kind --exclude=./.git --exclude=./.claude --exclude="*.egg-info" \
			--exclude=__pycache__ --exclude=.pytest_cache --exclude=.ruff_cache -cf - . | tar -C /home/runner/work -xf -; \
		chown -R runner:runner /home/runner/work; \
		runuser -u runner -- bash -euo pipefail -c "cd ~/work && make venv && make lint && make test"'

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

# The same patient on a local kind cluster (Kubernetes in Docker), from our own manifests in deploy/kind (week 5).
# It uses the Compose stack's ports on this Mac, so the two take turns: make down before make kind-up.
kind-up: local-docker-only ## the patient on a local kind cluster, from deploy/kind: build, load, start in order, wait (make down first)
	$(PY) -m klaxon.patient.cluster up

kind-down: ## delete the kind cluster and its data (synthetic): the next make kind-up starts from empty books
	$(PY) -m klaxon.patient.cluster down

kind-ps: ## the kind cluster's pods: ready, restarts, images
	$(PY) -m klaxon.patient.cluster ps

kind-reader: ## a token of klaxon-reader, which may only read, for Klaxon's tools on kind: an hour long, in .kind/ (make kind-up first)
	$(PY) -m klaxon.patient.cluster reader

kind-check: ## on the kind cluster, the money check again and again while traffic flows (CHECKS=20, RATE=5), saved to evidence/week6/kind/
	$(PY) -m klaxon.patient.cluster check --checks $(or $(CHECKS),20) --rate $(or $(RATE),5)

test-integration: ## tests against the running stack's PostgreSQL and Kafka (make up first)
	$(PY) -m pytest -q -m integration

e2e: ## reset the stack and drive it end to end (about ten minutes)
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
loop-prompt: ## print what the loop tells the model, for an alert; calls no model: make loop-prompt ALERT='...' (OFFERED=1: as a gated or evaluation run, with rollback_deploy; LOOP=multi: each role's, week 8)
	$(PY) -m klaxon.agent.$(if $(filter multi,$(LOOP)),team,loop) prompt $(LOOP_ARGS) $${OFFERED:+--offered}

loop-graph: ## the LangGraph loop's graph in Mermaid, as LangGraph draws it (week 6; LOOP=multi: the multi-agent loop's, week 8); calls nothing
	$(PY) -m klaxon.agent.$(if $(filter multi,$(LOOP)),team,graph) draw

investigate: ## one live run of the loop: Claude on Aravind's seat, the running stack (STEPS=, TOOL_CALLS=, TIME_LIMIT=seconds)
	$(PY) -m klaxon.agent.loop run $(LOOP_ARGS)

replay-run: ## replay a recorded run offline and check it reproduces: make replay-run RUN=evidence/week3/<run folder> (LOOP=plain|langgraph|multi; by default the run's own)
	$(PY) -m klaxon.agent.loop replay "$$RUN" $${LOOP:+--loop "$$LOOP"}

alerts: ## the alert rules (symptoms only) read the running stack once, and print what they read; reads only (WATCH=seconds)
	$(PY) -m klaxon.alerts $${WATCH:+--watch "$$WATCH"}

alerts-under-traffic: ## traffic and no fault: do the alert rules stay quiet? (MINUTES=5, RATE=3), saved to evidence/week4/
	$(PY) -m klaxon.fault_runs quiet $${MINUTES:+--minutes "$$MINUTES"} $${RATE:+--rate "$$RATE"}

investigate-fault: ## one gated live run on a fault: Klaxon may ask to act (make approve answers), verifies, then the fault is cleared (his OK first; NO_ACTIONS=1: diagnosis only; LOOP=langgraph|multi; PLATFORM=kind: on the kind cluster, saved to evidence/week6/kind/)
	$(PY) -m klaxon.fault_runs run "$$FAULT" $${STEPS:+--steps "$$STEPS"} $${TOOL_CALLS:+--tool-calls "$$TOOL_CALLS"} \
		$${TIME_LIMIT:+--seconds "$$TIME_LIMIT"} $${NO_ACTIONS:+--no-actions} $${LOOP:+--loop "$$LOOP"} $${PLATFORM:+--platform "$$PLATFORM"}

rehearse-fault: ## a fault under traffic and the alert rules, no model: what they see, saved to evidence/week4/ (his OK first; PLATFORM=kind: on the kind cluster, saved to evidence/week6/kind/)
	$(PY) -m klaxon.fault_runs rehearse "$$FAULT" $${RATE:+--rate "$$RATE"} $${PLATFORM:+--platform "$$PLATFORM"}

grade-run: ## compare a fault run's diagnosis with the fault's recorded cause, by the rule; offline: make grade-run RUN=evidence/week4/<folder>
	$(PY) -m klaxon.fault_runs grade "$$RUN"

eval-cases: ## the evaluation's eight cases, each with its right answer and the action that fits; calls nothing (week 5)
	$(PY) -m klaxon.evaluation cases

eval: ## live evaluation runs, each on a fresh stack: nobody is asked, nothing acts (his OK first): make eval MODEL=claude-opus-5-5 CASES=all SEEDS=7,8,9 (LOOP=langgraph|multi; LOOPS=plain,langgraph,multi: for each case and seed, the loops in turn; NOT_RUN_OF=<suite folder>: only what it left not run), saved to evidence/week8/
	$(PY) -m klaxon.evaluation run $${MODEL:+--model "$$MODEL"} $${CASES:+--cases "$$CASES"} $${SEEDS:+--seeds "$$SEEDS"} \
		$${LOOP:+--loop "$$LOOP"} $${LOOPS:+--loops "$$LOOPS"} $${NOT_RUN_OF:+--not-run-of "$$NOT_RUN_OF"}

eval-report: ## the evaluation's report, worked out offline from saved runs: make eval-report RUNS='evidence/week5/<suite folder> evidence/week4' (SHOW_JUDGE=1: the judge's verdicts on runs not labeled yet too)
	$(PY) -m klaxon.evaluation report $$RUNS $${SHOW_JUDGE:+--show-judge}

rehearse-case: ## one evaluation case under traffic, with the rules and no model, saved to evidence/week8/ (his OK first): make rehearse-case CASE=false-alarm
	$(PY) -m klaxon.evaluation rehearse "$$CASE" $${SEED:+--seed "$$SEED"}

# OpenHands on fault 1 (week 8, item 4; for CSYE 7374 only): its own agent, on qwen3:8b in Ollama on this Mac, with no
# money and no key. Its SDK lives in a venv of its own, so Klaxon's dependencies do not change. Each step but the last
# two needs his OK first. OpenHands never touches the stack: it reads what the read-only tools answered at the alert.
openhands-venv: ## OpenHands' SDK in a venv of its own, .venv-openhands: four packages, each pinned at 1.60.1 (his OK first)
	python3.12 -m venv .venv-openhands
	.venv-openhands/bin/pip install --disable-pip-version-check -r experiments/openhands/requirements.txt

openhands-snapshot: local-docker-only ## fault 1 on a fresh stack; at its alert, the read-only tools' answers saved for OpenHands, then the stack stopped; no model (his OK first): make openhands-snapshot SEED=7
	$(PY) -m klaxon.openhands_task snapshot $${SEED:+--seed "$$SEED"}

openhands-run: local-docker-only ## OpenHands on a saved snapshot, in its sandbox, on qwen3:8b (OLLAMA_CONTEXT_LENGTH=24576 ollama serve first; his OK first): make openhands-run RUN=evidence/week8/openhands/<folder>
	$(PY) -m klaxon.openhands_task run "$$RUN"

openhands-task: ## the exact task OpenHands gets for a saved snapshot; calls nothing: make openhands-task RUN=evidence/week8/openhands/<folder>
	$(PY) -m klaxon.openhands_task task "$$RUN"

openhands-compare: ## OpenHands' runs beside the night's 9 on fault 1: the rule, the judge, his labels; calls nothing
	$(PY) -m klaxon.openhands_task compare

# The graders (week 5, item 2). RUN may name several folders, or a folder of runs: it is split on spaces, never pasted
# into the shell's text.
judge-prompt: ## the exact prompt the judge gets for a saved run's diagnosis; calls nothing: make judge-prompt RUN=evidence/week4/<folder>
	@test -n "$$RUN" || { echo "make judge-prompt RUN=<a saved run's folder>: RUN is missing" >&2; exit 2; }
	$(PY) -m klaxon.judge prompt "$$RUN"

judge: ## Claude as a judge, live: one recorded call per saved run not judged yet, saved beside it (his OK first): make judge RUN=<folders>
	@test -n "$$RUN" || { echo "make judge RUN=<saved runs, or folders of them>: RUN is missing" >&2; exit 2; }
	$(PY) -m klaxon.judge run $$RUN

replay-judge: ## replay recorded judgments offline, and check they reproduce: make replay-judge RUN=<folders>
	@test -n "$$RUN" || { echo "make replay-judge RUN=<judged runs, or folders of them>: RUN is missing" >&2; exit 2; }
	$(PY) -m klaxon.judge replay $$RUN

label: ## your own verdict on a run's diagnosis, typed in your terminal (refused without one), saved beside it once: make label RUN=<folders>
	@test -n "$$RUN" || { echo "make label RUN=<saved runs, or folders of them>: RUN is missing" >&2; exit 2; }
	$(PY) -m klaxon.labels $$RUN

label-pick: ## the runs for your next make label: every run the graders disagree on, every false-alarm and missing-deploys run, and a random draw (TOTAL=17); prints the command, never a verdict
	$(PY) -m klaxon.evaluation pick $${RUNS:-evidence/week5} $${TOTAL:+--total "$$TOTAL"}

review-postmortem: ## your marks on a postmortem draft, paragraph by paragraph, typed in your terminal (refused without one), saved beside it once: make review-postmortem RUN=evidence/week4
	@test -n "$$RUN" || { echo "make review-postmortem RUN=<the drafts' run folders, or folders of them>: RUN is missing" >&2; exit 2; }
	$(PY) -m klaxon.agent.postmortem_review $$RUN

calibrate: ## the judge, live, on the grading tests' 66 sentences and the 13 saved diagnoses: 79 calls (his OK first)
	$(PY) -m klaxon.calibration run

calibration-report: ## each grader against the expected verdicts and yours, offline (FOLDER=, the newest by default; WRITE=1 saves it)
	$(PY) -m klaxon.calibration report $${FOLDER:+"$$FOLDER"} $${WRITE:+--write}

replay-calibration: ## replay the calibration's recorded judgments offline, and check they reproduce (FOLDER=, the newest by default)
	$(PY) -m klaxon.calibration replay $${FOLDER:+"$$FOLDER"}

postmortem-facts: ## the facts of a gated run's postmortem, filled in by code, and the exact prompt; calls nothing: make postmortem-facts RUN=evidence/week4/<folder>
	$(PY) -m klaxon.agent.postmortem facts "$$RUN"

postmortem: ## draft a gated run's postmortem: the facts by code, the narrative by Claude in one recorded call, its numbers checked (his OK first): make postmortem RUN=...
	$(PY) -m klaxon.agent.postmortem draft "$$RUN"

replay-postmortem: ## replay a recorded postmortem draft offline, and check it reproduces: make replay-postmortem RUN=evidence/week4/<folder>
	$(PY) -m klaxon.agent.postmortem replay "$$RUN"

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
