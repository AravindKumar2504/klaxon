# Status

Last updated: 2026-10-12. Week 2, item 1 (payment confirmation) is done: Aravind ran it himself and merged it into `main` on 2026-10-12, on his Mac only. Nothing is pushed: GitHub's `main` still holds week 1 (public: https://github.com/AravindKumar2504/klaxon, first pushed 2026-10-11, no license). Next: week 2, items 2-5.

The week 1 commit IDs quoted below were written before the one-time history rewrite of 2026-10-11; `docs/HISTORY.md` maps each one to its current ID.

## Done

### Week 2, item 1: payments confirmed from webhooks, and a fourth money-check invariant (2026-10-11)

Aravind's decision of 2026-10-11, built as he decided it, on the branch `week-2-payment-confirmation`, which was fast-forwarded into `main` on 2026-10-12. Commits: `500a8d9` (the consumer loop moves to its own module, unchanged in behavior), `0babb86` (the feature), `0b9d5c1` (the demo runs), `7bc0151` (Aravind's runs).

- **The payment confirmer:** a fifth service, `payment-confirmer` (`src/klaxon/patient/services/payment_confirmer.py`), in its own Kafka consumer group on `provider-events`. It finds our payment through the metadata the provider sends back (`klaxon_payment_id`) and moves it from pending to succeeded in one conditional UPDATE: only if still pending, for the same amount, not linked to another charge. A final status is never overwritten; a conflict is logged as an error and changes nothing. Its database role `payment_confirmer` reads four columns of `payments.payments` and may change only a status and the provider's id. The stack now runs 9 containers.
- **One consumer loop:** `src/klaxon/patient/services/consumer.py`, run by both consumers (moved from `ledger_consumer.py`).
- **The fourth invariant** (`payments_match_provider` in `src/klaxon/checks/money.py`): our payments table against the provider's payments (new `GET /v1/payments`). `make check` runs all four, after waiting for both consumer groups. It also catches a true double charge and a 100x amount error, which the three ledger invariants pass (offline tests). The first piece's demos still run three invariants and print exactly their saved evidence (checked 2026-10-11 after the last `src/klaxon` change).
- **A simulator control:** `fail_after_charge_rate` and `fail_after_charge_keys` in `PUT /admin/behavior`: the provider saves a charge, then answers 500 to every retry with that key.
- **A known-bad baseline:** `make up CONFIRMER=by_provider_id`.
- **Real runs, 2026-10-11 21:04 and 21:05 PDT, commit `0babb86` (clean code and stack), seed 7** (`make demo-lost-answer CONFIRMER=by_provider_id`, then `CONFIRMER=by_our_id`; files in `evidence/week2/`). In both, the provider saved `pmt_000003`'s charge ($69.68) and answered 500 to all 3 of the API's attempts, so the client got 503 and gave up:
  - by_provider_id: `pmt_000003` stayed pending, its refund was refused (409), the webhook dashboard was GREEN (5 of 5), the ledger balanced and matched the provider ($281.99), and only `payments_match_provider` failed.
  - by_our_id: the webhook confirmed `pmt_000003` (the confirmer logged "confirmed" for `evt_000003` and "already confirmed" for the other four payments), the refund went through (201), and all four invariants passed.
- **Tests** (all measured 2026-10-11 on the code of `0babb86`):
  - `make test`: 287 passed, offline, 0.7 s.
  - `make test-integration` (stack up): 56 passed in 15 s. New: what the confirmer changes and what it never does, its permissions, a race with the API's own update, and the fourth invariant's SQL.
  - `make e2e`: 10 passed in 85 s. New: a charge whose answer is lost is confirmed by its webhook, and a later client retry gets 201 with no new charge; replaying the whole topic changes nothing for either consumer; both consumers wait out a PostgreSQL outage and catch up; the known-bad confirmer leaves the payment pending, and only the fourth invariant sees it.
  - `make mutants` (stack up): 67 of 67 deliberate breaks caught, 206 s. One mutant first survived and exposed a weak test of mine (its INSERT was refused by the id sequence, not by the missing grant); the test was fixed.
- **Aravind's runs, 2026-10-12:** on the branch at `cc6fad7`, after `make reset`, he ran `make test` (287 passed), `make up` (all 9 containers healthy), `make check` (passed, four invariants), `make demo-lost-answer CONFIRMER=by_provider_id` (failed as designed: only `payments_match_provider`), `make demo-lost-answer CONFIRMER=by_our_id` (passed) and `make e2e` (10 passed, 88 s) himself. His two demo runs are in `evidence/week2/` (`2026-10-12T215323Z_...by_provider_id...` and `2026-10-12T215353Z_...by_our_id...`), with the same numbers as Claude's runs.
- **Independent review:** a separate Claude review of the branch found no way to book twice, charge twice, mark a payment succeeded without a charge, or overwrite a final status. Fixed after it: the fourth invariant reports every problem with a payment and shows the charged amount; it matches a charge through our stored link when its metadata names none of ours; the metadata id the check reads is validated; a second claim on one charge is a logged conflict, not a dead letter; the confirmer reads only four columns; both consumers now go through the live database-outage test; five docstrings. Two of its scenarios rested on false premises (week 1 already sent the metadata). The rest is in the known gaps below and in `DECISIONS.md`.

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
- **Independent review:** a separate Claude review found no way to book twice, charge twice, or acknowledge without a durable write in idempotent mode. Its other findings were fixed before the runs above (see `DECISIONS.md`), except two known gaps: payment status only from the provider's answer (fixed for payments on the week 2 branch) and Kafka as a trusted zone.
- The first piece still reproduces: both `python -m klaxon.demo duplicate-webhook` runs print exactly their saved evidence (checked 2026-10-09 after the last `src/klaxon` change).
- **Aravind's review, 2026-10-11:** he ran `make up`, `make check` (passed), `make demo-live HANDLER=naive` (failed as designed, off by +$69.68), `make demo-live HANDLER=idempotent` (passed) and `make e2e` (8 passed, 63 s) himself, and read the week 1 code and notes. His two demo runs are in `evidence/week1/` (`2026-10-12T021434Z_...naive...` and `2026-10-12T021514Z_...idempotent...`). He accepted week 1 and it was merged into `main`.

### Before week 1

- Project chosen and planned (`PLAN.md`, `DECISIONS.md`, `RESEARCH.md`).
- First piece built and committed (`801a0bf`): the in-process money check demo, 91 tests then, evidence in `evidence/first-piece/`. Aravind ran the tests and both demos himself on 2026-10-09.
- Pitch film made, approved and submitted on Canvas by Aravind (2026-10-09); course PR https://github.com/nikbearbrown/info-7375-prompt-engineering-for-generative-ai/pull/13 (commit `8164179`). Do not change PR #13 unless the instructor asks: the GitHub folder must keep matching the Canvas zip.

## Known gaps (found in review, not fixed yet)

1. **Refund status never catches up.** Payments are fixed (week 2, above), refunds are not: a refund whose provider answer is lost stays pending, nothing confirms it, and the fourth invariant does not compare refunds. The same fix applies: our refund id as metadata, a `refund.succeeded` confirmation, refunds in the invariant. **Decided 2026-10-12 (Aravind): later.** Claude's suggested time: after the first 3 faults.
2. **Kafka is a trusted zone.** Any process on this Mac or in the Compose network can publish to Kafka without the provider's signature. The money check would flag a forged event as "posted, but the provider never sent it", but nothing stops it. Planned for the week 9 safety pass: forward the signature in Kafka headers and check it again in the consumer.
3. **The money check assumes no client traffic while it runs.** A request in flight between the provider snapshot and the database read can look like a mismatch ("succeeded but never charged", "charged but not ours"). The traffic generator (week 2, item 3) must pause before a check, or the check needs a cutoff. Decide then.
4. **A stalled consumer makes `make check` exit 2** ("not settled") instead of showing the money symptom. `python -m klaxon.checks.live --no-wait` runs the check anyway; in-flight events then look lost or stuck. This matters for the poison-message fault.
5. Smaller limits, documented where they live: no circuit breaker on provider calls (deliberate, a week 2 incident); no OpenTelemetry yet (week 2); one Kafka broker with one copy, so a crash of the whole Docker VM could lose the newest writes; the APIs have no authentication (local only, Host-checked); provider event ids are sequential per provider volume, so reset all volumes together (`make reset`). New in week 2: the API's own status update still trusts the provider's answer, amount included (see `DECISIONS.md`); a poison message is dead-lettered once per consumer group, so a dead-letter metric must count per consumer.

## Needs Aravind

1. **Push?** `main` now holds week 2, item 1 on this Mac only. Pushing would make it public on GitHub. Claude asks before every push.
2. Before the course demos, if not done yet: read the week 2 section of `docs/STUDY-NOTES.md`, then `payment_confirmer.py`, `consumer.py`, and `_payments_match_provider` in `src/klaxon/checks/money.py`.
3. Optional, from before: `docker` is missing from the PATH of shells that started before Docker was installed (a new Terminal window works, or add `export PATH="$HOME/.docker/bin:$PATH"` to `~/.zshrc`; the Makefile works either way; open Docker Desktop before `make up`). Keep the name Klaxon? (default yes.) A few dollars of Anthropic API credit later for the HolmesGPT comparison?

## Next: the rest of week 2 (PLAN: Oct 20-26), monitoring, traffic and the first 3 faults

1. ~~Payment confirmation~~ Done and merged (above).
2. OpenTelemetry in all five services, into `grafana/otel-lgtm` (one container: collector, metrics, traces, logs, Grafana). Metrics to have: webhook status codes, Kafka consumer lag per group, booking and confirmation outcomes, dead letters per consumer, provider latency, pending payments.
3. A traffic generator (a service or a make target) using the seeded `lose_ack_rate` and `fail_after_charge_rate`. It must pause before a money check, or the check needs a cutoff (known gap 3).
4. The first 3 faults with known causes, injected on purpose: duplicate delivery with idempotency broken, a poison message stalling a consumer (the default consumers dead-letter poison messages, so this will be a known-bad consumer variant, like `LEDGER_HANDLER=naive`), and a provider outage without a circuit breaker. The lost-answer scenario (`klaxon.demo.lost_answer`) already runs on any clean stack, so with `CONFIRMER=by_provider_id` it is a ready-made "payments stuck pending" fault.
5. Update STATUS, DECISIONS and STUDY-NOTES at the end (week 2 concepts: OpenTelemetry traces, metrics and logs; what Grafana shows).
6. Later (Aravind, 2026-10-12): confirm refunds the same way (known gap 1); suggested after the first 3 faults.

Course tie-in: CSYE 7374 on 10/14 covers an agent working in a real project, and this repo is that project.
