# Klaxon

**Status: early. The payments system Klaxon will protect runs for real in Docker Compose, and the money check (five invariants) runs against it. Klaxon's read-only tools exist, served by its own MCP server (week 3); the agent loop that will use them does not exist yet.**

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

- `src/klaxon/patient/services/telemetry.py`: OpenTelemetry in the payments API, the webhook receiver and both consumers (not the fake provider, which stands for an outside company). A webhook's trip through Kafka is one trace, and every log line carries its trace id. Klaxon will read this telemetry, so it is treated as untrusted input: a caller's trace headers are ignored, the raw path and other caller-chosen text never reach a span, and every attribute is capped at 300 characters.
- `compose.yaml`, `docker/grafana/`: one `grafana/otel-lgtm` container (collector, Prometheus, Tempo, Loki, Grafana) and a "Klaxon patient" dashboard: webhook results, consumer lag and outcomes, retries and dead letters, the API and the provider, payments pending.

### Klaxon's tools, over MCP (week 3)

- `src/klaxon/agent/tools.py`: eight read-only tools, the ones an on-call engineer would reach for: list and query metrics (Prometheus), search logs (Loki), search traces and read one (Tempo), service health and recent deploys (docker), and the money check. They take queries, ids and numbers, never a URL or a command, and every source must be this Mac. Everything they return is treated as untrusted: hidden characters dropped, every string and list capped, and a container's environment never passed on except three behavior settings.
- `src/klaxon/agent/mcp_server.py`: an MCP server written from scratch (JSON-RPC 2.0 over stdio, revision 2025-11-25), so any MCP client can use the tools. The tests drive it with the official SDK's clients.

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
| `make mutants` | breaks the code on purpose 254 ways, on a copy, and checks a test notices each one |
| `make check` | the money check against the running stack (exit 0 pass, 1 fail, 2 could not run) |
| `make tools` | list Klaxon's read-only tools, the ones its MCP server serves |
| `make tool NAME=recent_deploys ARGS='{}'` | call one tool against the running stack and print what it returns |
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

- Plan: [docs/PLAN.md](docs/PLAN.md)
- Current status: [docs/STATUS.md](docs/STATUS.md)
- Decisions: [docs/DECISIONS.md](docs/DECISIONS.md)
- Why this project: [docs/RESEARCH.md](docs/RESEARCH.md)
