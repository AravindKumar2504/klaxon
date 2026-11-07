# Postmortem draft: Payments fail: the payments API's database pool runs dry

> Reviewed by Aravind, paragraph by paragraph, at 2026-11-07T06:37:39Z (his marks given in the chat, entered by Claude): 6 right, 0 wrong. His mark is under each paragraph; the draft is otherwise exactly as received.

A draft for a person to review, about one gated run of Klaxon on a fault injected on purpose: synthetic data, a simulated provider, no real money. The facts, in the lists and tables, were filled in by code from the run's saved evidence, in its folder 2026-11-01T030525Z\_gated-run\_pool-exhaustion. The paragraphs are Claude's draft: claude-opus-5-5, a recorded response received 2026-11-01T19:33:32.666Z (postmortem-recording.jsonl), drafted from those facts alone: its prompt held nothing else. Code checked every number in them against the facts, but for one, once, twice and the ordinals, which it does not read: all 64 are there.

## Summary

On 2026-11-01, a fault was injected on purpose into the payments system on one machine, with synthetic data and no real money. The fault was a deploy of payments-api with REFUND\_CONNECTION=kept, which leaked database connections. The alert PaymentsLatencyHigh fired 49.1 s later. Klaxon's loop diagnosed the cause with confidence 0.6. It asked at the approval gate to roll payments-api back. The request r-7b9d was approved, and deploy 28 restored REFUND\_CONNECTION=returned. Verification by code passed. The money check passed at every point it was run. The synthetic clients left 234 payments and 2 refunds with their outcome unknown, and those still need a look.

> **Aravind's mark:** right: The paragraph accurately summarizes the database connection leak fault, the latency alert, Klaxon's diagnosis, the successful rollback deploy restoring connection returns, and passing verification checks.

## Timeline

At 03:06:41 UTC the alert rules were quiet at 2 evaluations in a row, and the money check passed. The fault was then injected as deploy 27 of payments-api at 03:06:44 UTC, by klaxon.faults. The injection took 2.2 s. PaymentsLatencyHigh fired at 03:07:31 UTC. The loop began at 03:07:32 UTC and stopped at 03:08:16 UTC, diagnosed after 9 steps and 8 tool calls, in 43.9 s. Klaxon made request r-7b9d at 03:08:17 UTC, open until 03:18:17 UTC. It was approved at 03:08:51 UTC, after 34 s. Deploy 28 ran at 03:08:57 UTC, 86 s (1 min 26 s) after the alert and 133 s (2 min 13 s) after the fault's deploy. Health checks ran from 03:08:58 UTC to 03:10:02 UTC. A later money check passed at 03:10:03 UTC. Verification took 66.8 s and passed. The harness's clearing came 202.2 s after the fault and changed nothing. At 03:11:34 UTC no alert fired and the money check passed.

> **Aravind's mark:** right: The paragraph precisely outlines the chronological timeline of the pool exhaustion incident, matching all saved timestamps, timings, and durations from fault injection to clearing.

The facts. Times are UTC, on 2026-11-01 unless a row says otherwise; since the fault: by the run's own clock, from when the fault's injection began.

| UTC | Since the fault | What happened | From |
|---|---|---|---|
| 03:06:41 |  | The alert rules were quiet at 2 evaluations in a row, and the money check passed: the stack was ready for the fault. | run.json: evaluations (before) |
|  | 0 s | The fault's injection began, on purpose (klaxon.faults): a deploy of payments-api with REFUND\_CONNECTION=kept. It took 2.2 s. The deploy log has it as deploy 27, by klaxon.faults, at 2026-11-01T03:06:44Z. | run.json: injected, timings, deploys |
| 03:07:31 | 49.1 s | The alert PaymentsLatencyHigh fired, raised by klaxon.alerts from what the stack showed: Payments are slow: over the last minute, the p95 latency of POST /payments on payments-api was 7.3 s, above the threshold of 2 s. | run.json: alert |
| 03:07:32 | 50.1 s | Klaxon's loop began to investigate the alert: claude-opus-5-5 through Claude Code on Aravind's seat, with read-only tools, offered one write action (rollback\_deploy) that runs only if a person approves it. | trace.jsonl: start |
| 03:08:16 | 94 s (1 min 34 s) | The loop stopped: diagnosed (confidence 0.6), after 9 steps and 8 tool calls, in 43.9 s. | trace.jsonl: stop; run.json: loop |
| 03:08:16 | 94.1 s | The money check right before the request: no damage: the money check passed. | run.json: damage.at\_request |
| 03:08:17 |  | Klaxon asked to act, at the approval gate: request r-7b9d, redeploy payments-api: PROVIDER\_BREAKER on (unchanged), CHARGE\_AMOUNT\_UNIT cents (unchanged), REFUND\_CONNECTION kept -\> returned. It was open until 2026-11-01T03:18:17Z. Its undo: going back to REFUND\_CONNECTION=kept would deploy a known-bad setting: Klaxon will not; a person can, by hand. | gate.jsonl: requested |
| 03:08:51 |  | Request r-7b9d was approved with make approve: typed at a terminal. | gate.jsonl: approved |
| 03:08:57 |  | The action ran: deploy 28 of payments-api in the deploy log, by Klaxon, approved request r-7b9d, with PROVIDER\_BREAKER=on, CHARGE\_AMOUNT\_UNIT=cents, REFUND\_CONNECTION=returned. | gate.jsonl: done |
| 03:08:57 |  | The money check right after the action: no damage: the money check passed. | run.json: verification.at\_action |
| 03:10:02 |  | Health, by code: 5 rounds of the rules and the containers, from 2026-11-01T03:08:58Z to 2026-11-01T03:10:02Z: healthy. | run.json: verification.health |
| 03:10:03 |  | The later money check, at least a minute after the action: no damage: the money check passed. | run.json: verification.after |
|  | 202.1 s | Verification, by code and not by the model: passed. | run.json: verification |
|  | 202.2 s | The harness's clearing, its safety net, asked for the good settings of payments-api (PROVIDER\_BREAKER=on, CHARGE\_AMOUNT\_UNIT=cents, REFUND\_CONNECTION=returned). The fault's cause was removed by Klaxon: its rollback (Klaxon, approved request r-7b9d) removed the cause, and the harness's clearing then found the good settings running, and changed nothing. | run.json: cleared, removed\_by, deploys |
| 03:11:34 | 292.2 s | The rules read the stack again after the clearing: no alert fired; the money check passed. | run.json: evaluations (after) |

How long each part took:

- from the fault to the alert: 49.1 s
- Klaxon's investigation: 43.9 s
- from the alert to the diagnosis: 44.9 s
- the request's window: open until it expires: 600 s (10 min)
- the request was open until it was approved: 34 s
- from the alert to the action: 86 s (1 min 26 s)
- from the fault's deploy to Klaxon's rollback: 133 s (2 min 13 s)
- the verification: 66.8 s
- from the fault to the harness's clearing: 202.2 s

## Impact

Customers saw slow payments. Over the last minute before the alert, the p95 latency of POST /payments was 7.3 s, above the threshold of 2 s. The money check passed at the alert, with lag of ledger 0 and payment-confirmer 0. It passed again right before the request, right after the action, at 03:10:03 UTC, and after the clearing. The synthetic clients sent 3 new payments per second, on average. Of 1076 payments that arrived, 964 got an answer: 730 succeeded, 0 were refused, and 234 were left with their outcome unknown. 112 got no answer the clients counted. Refunds: 73 succeeded, 0 were refused, and 2 were left unknown. The clients shed 117 requests and retried 811 times. The evidence does not split these counts by time, so it does not show how many fell inside the fault.

> **Aravind's mark:** right: The paragraph correctly details the p95 latency spike observed prior to the alert, passing money check statuses across the run, and synthetic client traffic metrics including retries and shed requests.

The facts. The alert, PaymentsLatencyHigh: Payments are slow: over the last minute, the p95 latency of POST /payments on payments-api was 7.3 s, above the threshold of 2 s.

| When | UTC | The money check |
|---|---|---|
| at the alert | 03:07:31 | the money check passed |
| right before the request | 03:08:16 | no damage: the money check passed |
| right after the action | 03:08:57 | no damage: the money check passed |
| later, at least a minute after the action | 03:10:03 | no damage: the money check passed |
| after the harness's clearing | 03:11:34 | the money check passed |

The synthetic clients' own counts:

- The synthetic clients sent 3 new payments per second, on average.
- 1076 payments arrived, and 964 got an answer: 730 succeeded, 0 were refused, 0 got an answer the clients did not expect, and 234 were left with their outcome unknown.
- Of the 234 payments whose outcome was left unknown, 0 were confirmed later, 0 were still pending at the client's last status check, 234 have no status check of the clients on record (their answer named no payment to check, or the check was shed, or not sent before the traffic stopped).
- 112 of the payments that arrived got no answer the clients counted.
- Refunds: 73 succeeded, 0 were refused, and 2 were left with their outcome unknown.
- Of the 2 refunds whose outcome was left unknown, 0 were confirmed later, 0 were still pending at the client's last status check, 2 have no status check of the clients on record (their answer named no refund to check, or the check was shed, or not sent before the traffic stopped).
- The clients shed 117 requests in all, of every kind, when 64 were already in flight.
- The clients retried 811 times.

## Cause

The recorded cause was deploy 27 of payments-api with REFUND\_CONNECTION=kept. Reading a payment's refunds took a connection from the database pool of 10 and never gave it back. Once the pool was empty, every request waited until the pool's 5 s timeout and then failed with a 500. That waiting raised payment latency and fired PaymentsLatencyHigh. Klaxon's diagnosis named this cause. A keyword rule, klaxon.grading at rubric version 3, found that the diagnosis named every required element and the deploy that brought it, and that its fix fit the remedy. That rule reads words, not meaning. No person reviewed the diagnosis before this draft.

> **Aravind's mark:** right: The paragraph accurately explains how the connection leak depleted the pool and triggered timeouts, the rubric v3 grading verdict, and model diagnostic details.

- The recorded cause (the fault was injected on purpose, so this is its cause): a deploy of the payments API with a connection leak (REFUND\_CONNECTION=kept): reading a payment's refunds takes a connection from its database pool of 10 and never gives it back, so after about ten refunds the pool is empty, and every request waits for a connection until the pool's 5 s timeout, then fails with a 500
- Klaxon's diagnosis, by its loop, live: claude-opus-5-5 through Claude Code on Aravind's seat. The loop stopped: diagnosed (confidence 0.6), after 9 steps and 8 tool calls, in 43.9 s. In the model's own words: payments-api was redeployed at 03:06:42 with REFUND\_CONNECTION=kept. Under that setting, each refund request most likely keeps its Postgres connection instead of returning it to the pool. Refunds have arrived at about 0.1-0.4/s since about 03:06, so the held connections build up. Within about 30 s the psycopg pool is empty. From then on every request that needs the database, POST /payments and refunds alike, waits for the pool's 5 s acquire timeout and fails with psycopg\_pool.PoolTimeout as HTTP 500. That raised p95 for POST /payments from about 24 ms to 7.3 s. Provider calls stay fast, so the provider is not the cause. Traffic on POST /payments also grew, from 0.5/s to 5.8/s, which adds pressure on the pool, but the timeouts start after the deploy and after refunds begin.
- Its proposed fix, in the model's words: rollback on payments-api: Roll payments-api back to the settings it ran before the 03:06:42 deploy, presumably a REFUND\_CONNECTION value that releases the connection after each refund. Recreating the container also frees the held connections. Afterwards, confirm that PoolTimeout errors stop and that p95 for POST /payments returns to about 25 ms. Then run money\_check to see whether payments or refunds that failed with 500 left our books out of step with the provider.
- The rule's verdict: names the cause (where: the payments API's database pool: named; what: its connections are never given back: named; how: with none left, requests wait for a connection and fail: named; the deploy that brought it: named). The fix fits the remedy: yes. Graded by a rule (klaxon.grading, rubric version 3, committed in 75fedf1 before any live run on faults 4 to 6): keyword patterns per element of the fault's recorded cause. Not a person and not a model: it reads words, not meaning.

## Response

Klaxon used tools that only read, and it was offered one write action, rollback\_deploy. It asked to redeploy payments-api with PROVIDER\_BREAKER on and CHARGE\_AMOUNT\_UNIT cents, both unchanged, and REFUND\_CONNECTION changed from kept to returned. The request was approved with make approve, typed at a terminal. The rule says only Aravind approves, but the gate's log records how the decision was made, not who made it, so the evidence leaves the approver open. Deploy 28 ran by Klaxon under r-7b9d. Verification by code found every container healthy, the cause removed and the money right. In 3 of the 5 health rounds, PaymentsLatencyHigh and PaymentsErrorsHigh still had their conditions holding, before the rules went quiet.

> **Aravind's mark:** right: The paragraph faithfully recounts Klaxon's rollback request details, terminal approval, deploy 28 execution, and verification outcomes confirming container health and cause removal.

- The gate: done: redeploy payments-api: PROVIDER\_BREAKER on (unchanged), CHARGE\_AMOUNT\_UNIT cents (unchanged), REFUND\_CONNECTION kept -\> returned
- The request: r-7b9d, made at 2026-11-01T03:08:17Z, open until 2026-11-01T03:18:17Z: redeploy payments-api: PROVIDER\_BREAKER on (unchanged), CHARGE\_AMOUNT\_UNIT cents (unchanged), REFUND\_CONNECTION kept -\> returned. It undoes deploy 27 of payments-api, at 2026-11-01T03:06:44Z, by klaxon.faults. Its undo: going back to REFUND\_CONNECTION=kept would deploy a known-bad setting: Klaxon will not; a person can, by hand
- The decision: approved at 2026-11-01T03:08:51Z, by make approve: typed at a terminal. Only Aravind approves Klaxon's actions, and Claude never does (DECISIONS.md, the week's plan). The gate's log records how a decision was made, not who made it.
- What ran: deploy 28 of payments-api at 2026-11-01T03:08:57Z, by Klaxon, approved request r-7b9d: PROVIDER\_BREAKER=on, CHARGE\_AMOUNT\_UNIT=cents, REFUND\_CONNECTION=returned (it replaced PROVIDER\_BREAKER=on, CHARGE\_AMOUNT\_UNIT=cents, REFUND\_CONNECTION=kept).
- The fault's cause was removed by Klaxon: its rollback (Klaxon, approved request r-7b9d) removed the cause, and the harness's clearing then found the good settings running, and changed nothing.

Verification, by code and not by the model: passed.

- Healthy: every container is healthy, and the metric rules and the books rule were quiet at 2 evaluations in a row.
- The cause is removed: nothing the provider made between the action and the last money check was booked wrong.
- The money is right: the money check passed.
- Health rounds that were not quiet: a rule's condition holds: PaymentsLatencyHigh (payments-api), PaymentsErrorsHigh (payments-api) (3 of the 5 rounds).

## Repairs still owed

Nothing in the books is left to repair, since the last money check passed. Up to its cutoff, the provider held no charge or refund that our records did not show as succeeded. The 234 payments and 2 refunds left unknown have no status check of the clients on record. The evidence does not say whether each settled since. Any still pending needs a person to decide whether to retry it with its key or mark it failed. The evidence does not name who that person is. Going back to REFUND\_CONNECTION=kept would deploy a known bad setting, and Klaxon will not do it.

> **Aravind's mark:** right: The paragraph correctly confirms that books required no repairs given the passing money check, while noting that unknown client outcomes and future setting protections remain important.

- The last money check passed (later, at least a minute after the action): nothing in the books is left to repair.
- Of the outcomes the synthetic clients left unknown, 234 payments and 2 refunds have no status check of the clients on record. The evidence does not say whether each settled since. The last money check passed: up to its cutoff, the provider held no charge or refund that our records did not show as succeeded, so any of them still pending had no money moved for it. One still pending needs a decision: a retry with its key, or marking it failed.
- The remedy this fault records (klaxon.faults): roll the payments API back to REFUND\_CONNECTION=returned: its new container starts with a full pool

## How this draft was made

- The facts: by code (klaxon.agent.postmortem), from the run's run.json, the gate's request, decision and log, and the loop's trace. postmortem.json keeps them with the narrative.
- The narrative: passed, after 1 call (call 1: passed).
- Each call's prompt and reply are in postmortem-recording.jsonl, recorded before Klaxon used them; make replay-postmortem replays them offline and checks this page comes out the same.
- The check's limit: it finds a number the facts do not hold, not one put to another use. The facts sit beside each paragraph, so a reader can see what each number counts.
