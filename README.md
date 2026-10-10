# Klaxon

**Status: early. The payments system Klaxon will protect now runs for real in Docker Compose, and the money check runs against it. The Klaxon agent does not exist yet.**

Klaxon will be an AI on-call engineer for a payments system. When an alert fires, it will investigate metrics, logs and traces, propose a fix with evidence, wait for a human to approve it, apply it, and then verify that the system is healthy again and that the money is right: the ledger balances, and no payment was lost or counted twice.

It is being built by Aravind Sundaravadivelu as a final project for two Northeastern University courses (CSYE 7374 Introduction to Agentic AI and INFO 7375 Prompt Engineering and Agentic AI), with Claude Code writing most of the code under his direction.

All data is synthetic, and the payment provider is a simulator. Klaxon will only act on its own local demo system, and every action that changes something will need human approval.

## What exists today

### The patient: a payments system in Docker Compose (week 1)

```
client -> payments API -> fake provider (charges; keeps its own books)
                               | signed webhook, retried until acknowledged
                               v
          ledger consumer <- Kafka <- webhook receiver
                |
                v
          PostgreSQL ledger  <- the money check reads it through a read-only role
```

- `src/klaxon/patient/services/`: the four services. The **payments API** (FastAPI) keeps a retried request from charging twice: Redis claims the `Idempotency-Key`, PostgreSQL has it UNIQUE, and the provider gets our payment id as its own key. The **fake provider** keeps its own SQLite books and sends HMAC-signed webhooks at least once, with retries; it can lose acknowledgements on purpose to cause duplicates. The **webhook receiver** checks the signature and answers 200 only after Kafka has the event. The **ledger consumer** books each event in a double-entry ledger in the same transaction as its dedupe row, then commits its Kafka offset.
- `compose.yaml`, `docker/`: official images only (`postgres:18.6-alpine`, `redis:8.8.3-alpine`, `apache/kafka:4.3.1` in KRaft mode, `python:3.12.15-slim`). Every port listens on 127.0.0.1. One database role per service, each with only what it needs.
- `src/klaxon/checks/live.py`: the money check against the running stack. It waits until nothing is in flight, then compares the ledger with the provider's own records.

### The first piece: the money check, in process

- `src/klaxon/checks/money.py`: three invariants, each with evidence: every provider event posted exactly once, the ledger balanced, and the ledger matching the provider overall and per payment. The same function checks the first piece's SQLite ledger and the live PostgreSQL ledger.
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
| `make up` / `make down` / `make reset` | start the stack and wait until healthy / stop it / stop it and delete its data |
| `make test-integration` | tests against the running stack's PostgreSQL and Kafka |
| `make e2e` | resets the stack and drives it end to end (about a minute) |
| `make mutants` | breaks the code on purpose 45 ways, on a copy, and checks a test notices each one |
| `make check` | the money check against the running stack (exit 0 pass, 1 fail, 2 could not run) |
| `make demo-live HANDLER=naive` | the first piece's scenario through the real stack, with the known-bad consumer; writes evidence to `evidence/week1/` |
| `make demo-live HANDLER=idempotent` | the same with the correct consumer |

The first piece's in-process demo still works on its own:

```bash
.venv/bin/python -m klaxon.demo duplicate-webhook --handler naive
```

## What the runs showed

On 2026-10-09 (seed 7), the first piece's in-process demo with the naive handler: 7 webhook requests, 0 errors, dashboard GREEN, ledger balanced, and the money check FAILED: one payment (pay_0003, $69.68) posted twice, books off by +$69.68. With the idempotent handler, every check passed (files in `evidence/first-piece/`).

The same scenario through the live stack (2026-10-09, commit `408670a`, files in `evidence/week1/`) gave the same numbers: with the naive ledger consumer, the dashboard stayed GREEN and the ledger balanced, and the money check FAILED, off by +$69.68, because `evt_000003` was booked twice from two Kafka messages. With the idempotent consumer, it passed. Test counts and the commands behind every number are in [docs/STATUS.md](docs/STATUS.md).

- Plan: [docs/PLAN.md](docs/PLAN.md)
- Current status: [docs/STATUS.md](docs/STATUS.md)
- Decisions: [docs/DECISIONS.md](docs/DECISIONS.md)
- Why this project: [docs/RESEARCH.md](docs/RESEARCH.md)
