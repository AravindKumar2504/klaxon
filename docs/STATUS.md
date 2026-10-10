# Status

Last updated: 2026-10-09, end of the week 1 session (started early: PLAN has week 1 as Oct 13-19). Week 1 is built, on the branch `week-1-compose`, waiting for Aravind's review. Nothing is pushed.

## Done

### Week 1: the patient runs for real in Docker Compose (2026-10-09)

- **Runtime:** Docker Desktop (Aravind's choice), engine 29.8.2, Compose 5.5.1. Rosetta is off: all four pinned images have native arm64 builds (checked with `docker manifest inspect`): `postgres:18.6-alpine`, `redis:8.8.3-alpine`, `apache/kafka:4.3.1`, `python:3.12.15-slim`.
- **The stack:** `compose.yaml` runs 8 containers: PostgreSQL, Redis, Kafka (KRaft, one node), a one-shot topic creator, and our four services: fake provider, webhook receiver, payments API, ledger consumer. Every port listens on 127.0.0.1. `make up` took 13 to 14 s after the first build (measured 2026-10-09).
- **The flow:** API -> provider -> signed webhook -> receiver -> Kafka `provider-events` (3 partitions, keyed by payment id) -> ledger consumer -> PostgreSQL. Idempotency: Redis claims the API's `Idempotency-Key`, PostgreSQL holds it UNIQUE, the provider gets our payment id as its own key; the ledger dedupes with `processed_events` in the same transaction as the posting and commits the Kafka offset only after that.
- **The money check against the live stack:** `make check` (`klaxon.checks.live`) waits until nothing is in flight, then runs the first piece's unchanged check through the read-only role `klaxon_readonly`.
- **Real runs, 2026-10-09 19:13 PDT, commit `408670a` (clean code and stack), seed 7** (`make demo-live HANDLER=naive`, then `HANDLER=idempotent`; files in `evidence/week1/`):
  - naive consumer: 7 webhook requests, 0 errors, dashboard GREEN, ledger balanced, money check FAILED: `evt_000003` (pay_000003, $69.68) booked twice from Kafka offsets `1@0` and `1@2`, books off by +$69.68. The same numbers as the first piece's in-process demo.
  - idempotent consumer: the same 7 deliveries and the same GREEN dashboard; all 6 events booked once; ledger and provider both $252.97; PASSED.
- **Tests** (all measured 2026-10-09):
  - `make test`: 242 passed, offline, 0.7 s.
  - `make test-integration` (stack up): 33 passed in 13 s. Real PostgreSQL, Redis and Kafka: booking once under a race, rollback of the dedupe row, every role's permissions, the money check's SQL, Kafka offsets and lag, the Redis Lua scripts.
  - `make e2e`: 8 passed in 61 s. Resets the stack, then: the first piece's scenario; 20 payments each sent twice with 30% of acknowledgements lost; 8 concurrent copies of one request (one charge); Redis stopped (no second charge); the whole topic replayed from offset 0 (nothing booked twice); Kafka stopped (webhooks wait at the provider, booked once after); PostgreSQL stopped (the consumer retries, books all 3 after); the naive consumer caught (+$69.68, dashboard GREEN).
  - `make mutants` (stack up): 45 of 45 deliberate breaks caught, 132 s. Each one replaces a piece of code with a plausible mistake, on a copy of the repo (`scripts/mutation_check.py`).
- **Independent review:** a separate Claude review found no way to book twice, charge twice, or acknowledge without a durable write in idempotent mode. Its other findings were fixed before the runs above (see `DECISIONS.md`), except the two "known gaps" below.
- The first piece still reproduces: both `python -m klaxon.demo duplicate-webhook` runs print exactly their saved evidence (checked 2026-10-09 after the last `src/klaxon` change).

### Before week 1

- Project chosen and planned (`PLAN.md`, `DECISIONS.md`, `RESEARCH.md`).
- First piece built and committed (`801a0bf`): the in-process money check demo, 91 tests then, evidence in `evidence/first-piece/`. Aravind ran the tests and both demos himself on 2026-10-09.
- Pitch film made, approved and submitted on Canvas by Aravind (2026-10-09); course PR https://github.com/nikbearbrown/info-7375-prompt-engineering-for-generative-ai/pull/13 (commit `8164179`). Do not change PR #13 unless the instructor asks: the GitHub folder must keep matching the Canvas zip.

## Known gaps (found in review, not fixed yet)

1. **Payment status never catches up.** `payments.status` comes only from the provider's synchronous answer. If every attempt times out after the provider has already charged, our row stays `pending` (and refunds are refused), and the money check cannot see it because it compares only the ledger. This is also why two planned incidents ("payments stuck pending", "never confirmed") would show no symptom yet. Needs Aravind's decision below.
2. **Kafka is a trusted zone.** Any process on this Mac or in the Compose network can publish to Kafka without the provider's signature. The money check would flag a forged event as "posted, but the provider never sent it", but nothing stops it. Planned for the week 9 safety pass: forward the signature in Kafka headers and check it again in the consumer.
3. Smaller limits, documented where they live: no circuit breaker on provider calls (deliberate, a week 2 incident); no OpenTelemetry yet (week 2); one Kafka broker with one copy, so a crash of the whole Docker VM could lose the newest writes; the APIs have no authentication (local only, Host-checked); provider event ids are sequential per provider volume, so reset all volumes together (`make reset`).

## Needs Aravind

1. **Review week 1** (30-45 min). With Docker Desktop open: `make up`, `make check`, `make demo-live HANDLER=naive`, `make e2e`. Read, in this order: `compose.yaml`, `docker/postgres/initdb/20-schema.sql`, `src/klaxon/patient/services/ledger_consumer.py`, `pg_ledger.py`, `payments_api.py`, `idempotency.py`. `docs/STUDY-NOTES.md` (week 1 section) explains the concepts. Then decide: merge `week-1-compose` into `main`?
2. **Decide how a payment gets confirmed** (known gap 1). Claude's recommendation: (a) confirm payments from the provider's webhooks (a second consumer group updates `payments.status`), so the "stuck pending" and "never confirmed" incidents have a real symptom, and (b) add a fourth money-check invariant that compares our payments table with the provider's payments. Alternative: a sweeper that retries pending payments with the same provider key.
3. Optional: `docker` is not on this Claude session's PATH (Docker Desktop added it to `~/.zprofile`, which only new login shells read). A new Terminal window works; adding `export PATH="$HOME/.docker/bin:$PATH"` to `~/.zshrc` fixes every shell. The Makefile works either way. Docker Desktop is set not to start at login, so open it before `make up`.
4. Still open from before: keep the name Klaxon? (default yes); make the repo public, and when? (default: ask before pushing); a few dollars of Anthropic API credit later for the HolmesGPT comparison?

## Next: week 2 (PLAN: Oct 20-26), monitoring, traffic and the first 3 faults

1. Aravind's decision on payment confirmation (above), then build it.
2. OpenTelemetry in all four services, into `grafana/otel-lgtm` (one container: collector, metrics, traces, logs, Grafana). Metrics to have: webhook status codes, Kafka consumer lag, booking outcomes, dead letters, provider latency.
3. A traffic generator (a service or a make target) using the seeded `lose_ack_rate`.
4. The first 3 faults with known causes, injected on purpose: duplicate delivery with idempotency broken, a poison message stalling a consumer (the default consumer now dead-letters poison messages, so this will be a known-bad consumer variant, like `LEDGER_HANDLER=naive`), and a provider outage without a circuit breaker.
5. Update STATUS, DECISIONS and STUDY-NOTES at the end (week 2 concepts: OpenTelemetry traces, metrics and logs; what Grafana shows).

Course tie-in: CSYE 7374 on 10/14 covers an agent working in a real project, and this repo is that project.
