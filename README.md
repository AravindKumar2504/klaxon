# Klaxon

**Status: early. The first piece exists: a synthetic, seeded demo of the money check, with tests. The Klaxon agent does not exist yet.**

Klaxon will be an AI on-call engineer for a payments system. When an alert fires, it will investigate metrics, logs and traces, propose a fix with evidence, wait for a human to approve it, apply it, and then verify that the system is healthy again and that the money is right: the ledger balances, and no payment was lost or counted twice.

It is being built by Aravind Sundaravadivelu as a final project for two Northeastern University courses (CSYE 7374 Introduction to Agentic AI and INFO 7375 Prompt Engineering and Agentic AI), with Claude Code writing most of the code under his direction.

## What exists today: the first piece

A synthetic, in-process demo of the check Klaxon will use to verify fixes. No network, no real money, no real provider. It is not the agent.

- `src/klaxon/patient/provider.py`: a fake Stripe-like provider. It creates payments and refunds and sends HMAC-signed webhooks on three schedules: normal, duplicate (one delivery retried after a lost acknowledgement) and out of order.
- `src/klaxon/patient/ledger.py`: a double-entry ledger (SQLite in memory, STRICT tables, integer cents) and the webhook endpoint, with a `naive` handler (posts every delivery) and an `idempotent` one (records the event id in the same database transaction as the posting).
- `src/klaxon/checks/money.py`: the money check. Three invariants, each with evidence: every provider event posted exactly once, the ledger balanced, and the ledger matching the provider overall and per payment.
- `src/klaxon/patient/dashboard.py`: what HTTP monitoring sees (request counts, status codes, error rate). Nothing about money.

```bash
python3.12 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/python -m pytest -q
.venv/bin/python -m klaxon.demo duplicate-webhook --handler naive
.venv/bin/python -m klaxon.demo duplicate-webhook --handler idempotent
```

The demo exits 0 when the money check passes and 1 when it fails, and writes a dated JSON evidence file under `evidence/first-piece/`.

**What the runs on 2026-10-09 showed** (seed 7, files in `evidence/first-piece/`): with the naive handler, 7 webhook requests, 0 errors, dashboard GREEN, ledger balanced, and the money check FAILED: one payment (pay_0003, $69.68) posted twice, books off by +$69.68. With the idempotent handler, the same deliveries passed every check.

- Plan: [docs/PLAN.md](docs/PLAN.md)
- Current status: [docs/STATUS.md](docs/STATUS.md)
- Why this project: [docs/RESEARCH.md](docs/RESEARCH.md)

All data is synthetic. Klaxon will only act on its own local demo system, and every action that changes something will need human approval.
