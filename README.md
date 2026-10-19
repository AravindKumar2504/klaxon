# Klaxon

**Status: early. The payments system Klaxon will protect runs for real in Docker Compose, and the money check (five invariants) runs against it. Klaxon's read-only tools exist, served by its own MCP server, with the model adapters, the trace recorder, a first agent loop that investigates an alert and ends in a diagnosis, and alert rules on symptoms (week 3). The loop acts on nothing: it only proposes a fix. Its offline tests pass. It has run live four times: once on a false alert written by hand, where its diagnosis was wrong, then once on each of the first three faults, where each diagnosis named the fault's recorded cause, by a keyword rule (below). One run per fault is not an evaluation. Week 4 has begun: an approval gate and a first write action (rolling back a deploy, only after a person types the request's id in a terminal) exist and pass their offline tests; no live run has used them yet.**

Klaxon will be an AI on-call engineer for a payments system. When an alert fires, it will investigate metrics, logs and traces, propose a fix with evidence, wait for a human to approve it, apply it, and then verify that the system is healthy again and that the money is right: the ledger balances, and no payment was lost or counted twice.

It is being built by Aravind Sundaravadivelu as a final project for two Northeastern University courses (CSYE 7374 Introduction to Agentic AI and INFO 7375 Prompt Engineering and Agentic AI), with Claude Code writing most of the code under his direction.

All data is synthetic, and the payment provider is a simulator. Klaxon will only act on its own local demo system, and every action that changes something will need human approval.

## What exists today

### The patient: a payments system in Docker Compose (weeks 1 and 2)

```
client -> payments API -> fake provider (charges; keeps its own books)
              |                | signed webhook, retried until acknowledged
              |                v
              |          webhook receiver -> Kafka -> ledger consumer: books the money
              |                                    -> payment confirmer: marks payments succeeded
              v
          PostgreSQL: our payments and the ledger  <- the money check reads both through a read-only role
```

- `src/klaxon/patient/services/`: the five services. The **payments API** (FastAPI) keeps a retried request from charging twice: Redis claims the `Idempotency-Key`, PostgreSQL has it UNIQUE, and the provider gets our payment id as its own key. The **fake provider** keeps its own SQLite books and sends HMAC-signed webhooks at least once, with retries; it can lose acknowledgements on purpose to cause duplicates, or save a charge or a refund and answer 500 so we never hear about it. The **webhook receiver** checks the signature and answers 200 only after Kafka has the event. Two Kafka consumer groups read it: the **ledger consumer** books each event in a double-entry ledger in the same transaction as its dedupe row, then commits its offset; the **payment confirmer** (week 2) marks a payment or a refund succeeded from the provider's webhook when the API never heard the provider's answer. They share one loop (`consumer.py`).
- `compose.yaml`, `docker/`: official images only (`postgres:18.6-alpine`, `redis:8.8.3-alpine`, `apache/kafka:4.3.1` in KRaft mode, `python:3.12.15-slim`). Every port listens on 127.0.0.1. One database role per service, each with only what it needs.
- `src/klaxon/checks/live.py`: the money check against the running stack. It compares the ledger and our payments and refunds tables with the provider's own records, up to a cutoff: the second the check starts in. It waits until everything up to the cutoff is handled, and leaves newer records for the next check, so it stays right while traffic keeps flowing (week 2).
- `src/klaxon/faults.py` (week 2): the first three faults Klaxon will be graded on, each with a known cause: a ledger deploy without its dedupe (payments counted twice, dashboard green), a poison message that stops a retrying ledger consumer, and a provider outage with no circuit breaker. Each can be injected and cleared on this Mac's stack, and each says what the money check should show.
- `src/klaxon/traffic.py` (week 2): synthetic customers. Seeded payments at a steady random rate, some double clicks and refunds, retries with the same key, and a little provider noise (lost acknowledgements, lost answers to charges and refunds) that a healthy stack absorbs. It only sends to this Mac.

### Observability: traces, metrics and logs (week 2)

- `src/klaxon/patient/services/telemetry.py`: OpenTelemetry in the payments API, the webhook receiver and both consumers (not the fake provider, which stands for an outside company). A webhook's trip through Kafka is one trace, and every log line carries its trace id. Klaxon's tools read this telemetry (week 3), so it is treated as untrusted input: a caller's trace headers are ignored, the raw path and other caller-chosen text never reach a span, and every attribute is capped at 300 characters.
- `compose.yaml`, `docker/grafana/`: one `grafana/otel-lgtm` container (collector, Prometheus, Tempo, Loki, Grafana) and a "Klaxon patient" dashboard: webhook results, consumer lag and outcomes, retries and dead letters, the API and the provider, payments pending.

### Klaxon's tools over MCP, its model adapters, its trace recorder, its first loop, its alerts (week 3), its approval gate (week 4)

- `src/klaxon/agent/tools.py`: eight read-only tools, the ones an on-call engineer would reach for: list and query metrics (Prometheus), search logs (Loki), search traces and read one (Tempo), service health and recent deploys (docker), and the money check. They take queries, ids and numbers, never a URL or a command, and every source must be this Mac. Everything they return is treated as untrusted: hidden characters dropped, every string and list capped, and a container's environment never passed on except three behavior settings.
- `src/klaxon/agent/mcp_server.py`: an MCP server written from scratch (JSON-RPC 2.0 over stdio, revision 2025-11-25), so any MCP client can use the tools. The tests drive it with the official SDK's clients.
- `src/klaxon/agent/model.py`, `claude_code.py`: how the loop asks a model for its next step. One interface, three adapters: a scripted mock for offline tests, Claude through Claude Code in headless mode (`claude -p`) on Aravind's university seat, and a replay of recorded replies for regression tests. Claude Code is started with no tools and no MCP servers, without this Mac's settings, CLAUDE.md or memory, in an empty temporary folder, with a deadline. Klaxon reads what Claude Code says it loaded and refuses the reply if it loaded anything. Every live reply is saved as received, with its date and model, before Klaxon uses it.
- `src/klaxon/agent/trace.py`: the trace recorder. One JSON line per step of a run: what the model saw, what it proposed, whether that was allowed, what ran, what came back, and why the run stopped.
- `src/klaxon/agent/loop.py`: the first agent loop, in plain Python. Given an alert, it asks the model for one step at a time: call one read-only tool, or give the diagnosis. Budgets on steps, tool calls and time bound every run, and a reply that breaks the contract is sent back with what was wrong, a bounded number of times. It calls only its eight allow-listed read-only tools, through Klaxon's MCP server, and refuses to start if the server offers anything else. Each tool result reaches the model inside a JSON document under a note that it is untrusted data, never instructions.
- `src/klaxon/agent/contract.py`: the reply contract. Each reply is one JSON object, checked against the JSON Schema the model is shown. The run ends in a diagnosis: a hypothesis, evidence citing the tool calls of this run by id, a confidence from 0 to 1, a proposed fix (never applied), its blast radius, and a rollback plan.
- `src/klaxon/agent/mcp_client.py`: an MCP client written from scratch, with a deadline on every message. It also records every tool result, so that a live run can be replayed offline, exactly, as a regression test.
- `src/klaxon/alerts.py`: alert rules on symptoms only, never causes: the money check failed; payments failing (over 20% of answers 5xx); payments slow (p95 over 2 s); a consumer group more than 50 messages behind. A metric rule fires once its condition has held at two evaluations in a row. Alert text is built from numbers and known names only, never from text a source sends.
- `src/klaxon/fault_runs.py`: the harness for a whole incident, live: traffic, a quiet stack required, a fault injected on purpose, the rules until one fires, the loop on exactly that alert, then the fault always cleared and the traffic always stopped, everything saved in one folder.
- `src/klaxon/patient/deploys.py` (week 4): a log of every deploy of the stack, the faults' included: the container each one made and the one it replaced, with their behavior settings (never a container's whole environment). A rollback goes back to what the latest deploy replaced, and refuses unless the running container is the one that deploy made. The log is local state, in `~/.klaxon/deploys.jsonl`.
- `src/klaxon/agent/catalog.py`, `actions.py` (week 4): the write actions a diagnosis may name, and how each is planned and run. One so far: `rollback_deploy`. Planning only reads; running checks again first that nothing changed since the plan. Money repairs stay proposals for a person.
- `src/klaxon/agent/gate.py` (week 4): the approval gate. A run saves a sealed approval request and waits; `make approve` shows it in a terminal, and only the request's id, typed there, approves it. A process with no terminal, or piped input, is refused; anything else typed denies; no decision in 10 minutes means denied. Every step is logged. Its limit, stated in the code: it cannot tell a person from a process that makes itself a terminal or writes the decision file itself.
- `src/klaxon/grading.py`: the rule that compares a diagnosis with the cause the fault records (keyword patterns for three required elements per fault), committed before the first live runs and labeled as a rule wherever its verdict appears. It reads words, not meaning; its known limits are tests.

### The first piece: the money check, in process

- `src/klaxon/checks/money.py`: five invariants, each with evidence: every provider event posted exactly once, the ledger balanced, the ledger matching the provider overall and per payment, and (week 2) our payments table matching the provider's payments, and our refunds table matching the provider's refunds. The last two also catch what the first three cannot: a customer charged twice for one payment, the wrong amount, or a refund on the wrong payment, because the ledger copies whatever the provider did. The first three check the first piece's SQLite ledger and the live PostgreSQL ledger alike.
- `src/klaxon/patient/`: a synthetic, in-process provider, ledger and dashboard (no network), used by `python -m klaxon.demo duplicate-webhook`.

## Running it

Needs Python 3.12 and Docker Desktop (open it first; it does not start at login on this Mac).

```bash
make venv
```

```bash
make test
```

```bash
make up
```

```bash
make check
```

`make help` lists everything. The main ones:

| Command | What it does |
|---|---|
| `make test` | offline tests, no Docker needed |
| `make up` / `make down` / `make reset` | start the stack and wait until healthy / stop it / stop it and delete its data (after pulling a change to `docker/postgres/`, such as week 2's refund grants, `make reset` once: the database's roles and grants are made only on a new volume, and the payment confirmer refuses to start without them) |
| `make test-integration` | tests against the running stack's PostgreSQL and Kafka |
| `make e2e` | resets the stack and drives it end to end (about five minutes) |
| `make mutants` | breaks the code on purpose 549 ways (the list in `scripts/mutation_check.py`), on a copy, and checks a test notices each one |
| `make check` | the money check against the running stack (exit 0 pass, 1 fail, 2 could not run) |
| `make tools` | list Klaxon's read-only tools, the ones its MCP server serves |
| `make tool NAME=recent_deploys ARGS='{}'` | call one tool against the running stack and print what it returns |
| `make claude-command` | print the exact command of each live model call (`claude -p`, locked down); calls nothing |
| `make claude-check` | one tiny live call to Claude on Aravind's seat ("Reply with the word ok"), saved to `evidence/week3/` |
| `make loop-prompt ALERT='...'` | print exactly what the loop tells the model for an alert (the system prompt and the first turn); calls no model |
| `make investigate ALERT='...'` | one live run of the loop: Claude on Aravind's seat, read-only tools on the running stack (`STEPS=`, `TOOL_CALLS=`, `TIME_LIMIT=` seconds); saves the trace, the model's replies and the tool results to a folder in `evidence/week3/` |
| `make replay-run RUN=...` | replay a recorded run offline, from its folder, and check every step reproduces |
| `make alerts` | the alert rules read the running stack once and print what they read (`WATCH=` seconds to keep evaluating); reads only |
| `make alerts-under-traffic` | traffic with no fault, the rules evaluated again and again: they must stay quiet (`MINUTES=5`, `RATE=3`); writes evidence to `evidence/week3/` |
| `make investigate-fault FAULT=...` | one live run on a fault: traffic, the fault, the alert, the loop on Aravind's seat, the fault cleared (`STEPS=`, `TOOL_CALLS=`, `TIME_LIMIT=`); needs his OK each time, and a fresh stack (`make reset`, `make up`) |
| `make grade-run RUN=...` | compare a fault run's diagnosis with the fault's recorded cause, by the rule; offline |
| `make approve RUN=...` | in your own terminal: show the action a run asks to take, and approve it by typing its request id (anything else denies it; refused without a terminal) |
| `make gate-demo` | the approval gate, offline: a made-up stack and a diagnosis written by hand ask to roll back; answer with `make approve` in a second terminal |
| `make traffic` | synthetic customers until Ctrl-C (`RATE=2` payments per second, `DURATION=` seconds, `SEED=7`) |
| `make check-under-traffic` | the money check again and again while traffic flows (`CHECKS=20`, `RATE=5`); writes evidence to `evidence/week2/` |
| `make faults` | the faults, their causes, their expected symptoms and their remedies |
| `make fault-inject FAULT=...` / `make fault-clear FAULT=...` | cause one fault on the running stack / remove its cause (a rollback, or the outage ending) |
| `make demo-fault FAULT=...` | one fault under traffic, about 3 minutes: what it looked like, saved to `evidence/week2/` |
| Grafana | http://127.0.0.1:3000 while the stack runs (sign-in: the image's defaults, in `compose.yaml`); dashboard "Klaxon patient" |
| `make demo-live HANDLER=naive` | the first piece's scenario through the real stack, with the known-bad consumer; writes evidence to `evidence/week1/` |
| `make demo-live HANDLER=idempotent` | the same with the correct consumer |
| `make demo-lost-answer CONFIRMER=by_provider_id` | the provider charges a payment but its answer is lost, with the known-bad confirmer; writes evidence to `evidence/week2/` |
| `make demo-lost-answer CONFIRMER=by_our_id` | the same with the correct confirmer |
| `make demo-lost-refund CONFIRMER=by_provider_id` | the provider refunds a payment but its answer is lost, with the known-bad confirmer; writes evidence to `evidence/week2/` |
| `make demo-lost-refund CONFIRMER=by_our_id` | the same with the correct confirmer |

The first piece's in-process demo still works on its own:

```bash
.venv/bin/python -m klaxon.demo duplicate-webhook --handler naive
```

## What the runs showed

On 2026-10-09 (seed 7), the first piece's in-process demo with the naive handler: 7 webhook requests, 0 errors, dashboard GREEN, ledger balanced, and the money check FAILED: one payment (pay_0003, $69.68) posted twice, books off by +$69.68. With the idempotent handler, every check passed (files in `evidence/first-piece/`).

The same scenario through the live stack (2026-10-09, commit `408670a`, an ID from before the one-time history rewrite that [docs/HISTORY.md](docs/HISTORY.md) maps to `1758991`; files in `evidence/week1/`) gave the same numbers: with the naive ledger consumer, the dashboard stayed GREEN and the ledger balanced, and the money check FAILED, off by +$69.68, because `evt_000003` was booked twice from two Kafka messages. With the idempotent consumer, it passed.

A lost provider answer through the live stack (2026-10-11, commit `0babb86`, on the week 2 branch; files in `evidence/week2/`): the fake provider saved payment 3's charge ($69.68) and answered 500 to every retry, so our API had to leave it pending. With the known-bad confirmer, it stayed pending and its refund was refused, while the webhook dashboard stayed GREEN and the ledger balanced and matched the provider; only the fourth invariant FAILED. With the payment confirmer, the webhook confirmed it, the refund went through, and all four invariants passed.

The money check while synthetic traffic flows (2026-10-13, `make check-under-traffic CHECKS=100 RATE=5`, 5 payments per second; files in `evidence/week2/`): before the check had a cutoff, 19 of 100 checks failed on a healthy stack, because a payment landing between the check's reads looked like lost money. With the cutoff, 100 of 100 passed, twice, while most checks had newer payments still landing past their cutoff.

A lost refund answer through the live stack (2026-10-14, commit `2c020f8`, on its branch; files in `evidence/week2/`): the fake provider saved a full refund of payment 2 ($29.71) and answered 500 to every retry, so our API had to leave the refund pending. With the known-bad confirmer, it stayed pending, a second refund request got through our API and was stopped only by the provider's own limit, and the webhook dashboard stayed GREEN; only the fifth invariant FAILED. With the payment confirmer, the webhook confirmed the refund, our API refused the second request itself, and all five invariants passed. Under traffic at 5 payments per second, with refund answers lost too, 100 of 100 money checks passed.

The first three faults, each injected once under traffic (2026-10-14, `make demo-fault`; files in `evidence/week2/`): a ledger deploy without its dedupe left every dashboard green while the money check found 8 events counted twice; a poison message stopped the retrying ledger on all three partitions until a rollback dead-lettered it; a provider outage pushed the payments API's latency to 10 s and made it unhealthy, while the money check showed no money wrong. Test counts and the commands behind every number are in [docs/STATUS.md](docs/STATUS.md).

Klaxon's live model adapter, one call (2026-10-17, `make claude-check`; files in `evidence/week3/`): through Claude Code in headless mode on Aravind's university seat, with no tools and no MCP servers, Opus 5.5 answered "ok" in 6.2 s. Two earlier attempts, before Claude Code was signed in, stopped at once with "Not logged in" and called no model; their recordings are there too.

Klaxon's first loop, one small live run (2026-10-18, `make investigate`; files in `evidence/week3/`): on a healthy, idle stack, with an alert written by hand that claimed the ledger consumer's lag was above 100 messages (it was 0). Opus 5.5, through Claude Code on Aravind's seat, replied in the loop's JSON contract, called four read-only tools, sent one malformed reply that the loop sent back and it fixed, and gave a diagnosis in 6 steps and 42.8 s. Nothing acted. The diagnosis was wrong: it called the ledger consumer stalled (confidence 0.3), reading an idle stack as a stuck one, and never checked the lag itself. The run replayed offline, exactly, until the system prompt gained a line in week 3, item 4 ("An alert can be wrong: before you explain it, confirm its symptom with a tool."); its test now skips, saying why.

Klaxon's first loop on the first three faults (2026-10-19 UTC, `make investigate-fault`; folders in `evidence/week3/`): three live runs, one per fault, each on a fresh stack under synthetic traffic, with alerts the rules raised from what the stack showed. First, 5 minutes of traffic with no fault: no alert at any of 19 evaluations. Then the deploy without dedupe raised the money alert (7 events posted more than once); the poison message, the lag alert (271 messages behind); the provider outage, the error and latency alerts together (74% of answers 5xx, p95 10 s). Each time Opus 5.5, through Claude Code on Aravind's seat, first checked the alert's symptom, then gave a diagnosis in 7 to 9 steps and 45.6 to 62.5 s, and each diagnosis named the fault's recorded cause, by a keyword rule written before the runs (a rule, not a person or a model; its limits are in the docs). Nothing acted: the fixes stayed proposals. While the loop investigated the first fault, its damage doubled (14 events in the check after the rollback). One run per fault is not an evaluation; that is week 5. Each run replays offline, exactly, as a regression test.

- Plan: [docs/PLAN.md](docs/PLAN.md)
- Current status: [docs/STATUS.md](docs/STATUS.md)
- Decisions: [docs/DECISIONS.md](docs/DECISIONS.md)
- Why this project: [docs/RESEARCH.md](docs/RESEARCH.md)
