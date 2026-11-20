# Postmortem draft: Payments counted twice in the books

A draft for a person to review, about one gated run of Klaxon on a fault injected on purpose: synthetic data, a simulated provider, no real money. The facts, in the lists and tables, were filled in by code from the run's saved evidence, in its folder 2026-11-18T055057Z\_gated-run\_duplicate-delivery. The paragraphs are Claude's draft: claude-opus-5-5, a recorded response received 2026-11-18T05:59:17.941Z (postmortem-recording.jsonl), drafted from those facts alone: its prompt held nothing else. Code checked every number in them against the facts, but for one, once, twice and the ordinals, which it does not read: all 71 are there.

## Summary

On 2026-11-18 a fault was injected on purpose into a payments system on one machine. The system ran with synthetic data and a simulated provider, so no real money moved. Deploy 133 set LEDGER\_HANDLER=naive on the ledger consumer, so the consumer booked resent provider webhooks a second time. The alert MoneyCheckFailed fired 17.7 s after the fault. Klaxon diagnosed the cause, and the grading rule found that it named it. Klaxon then asked at the approval gate to redeploy the ledger consumer with LEDGER\_HANDLER=idempotent. The request was approved and ran as deploy 134. Code verified the action and it passed. The cause was removed and the system was healthy. The money was still not right. At the last check, 36 events were counted more than once and our books were $1,518.19 ahead of the provider's balance. That damage stays until a person repairs it.

## Timeline

At 05:52:13 UTC the alert rules were quiet at 2 evaluations in a row, and the money check passed. The fault's injection then began, and it took 2.7 s. The deploy log records it as deploy 133 at 05:52:16 UTC. At 05:52:31 UTC MoneyCheckFailed fired. At 05:52:32 UTC Klaxon's loop began to investigate. At 05:54:08 UTC the loop stopped as diagnosed, after 11 steps and 7 tool calls, in 95.8 s. At 05:54:09 UTC Klaxon made request r-7dee at the gate. The request was approved at 05:56:02 UTC, after it had been open 113 s. At 05:56:05 UTC deploy 134 ran. Health checks from 05:56:06 UTC to 05:56:22 UTC found the stack healthy. A later money check at 05:57:05 UTC still failed. Verification passed at 292 s after the fault. The harness's clearing at 292.2 s found the good settings already running and changed nothing. At 05:58:36 UTC the rules read the stack again, and MoneyCheckFailed fired again.

The facts. Times are UTC, on 2026-11-18 unless a row says otherwise; since the fault: by the run's own clock, from when the fault's injection began.

| UTC | Since the fault | What happened | From |
|---|---|---|---|
| 05:52:13 |  | The alert rules were quiet at 2 evaluations in a row, and the money check passed: the stack was ready for the fault. | run.json: evaluations (before) |
|  | 0 s | The fault's injection began, on purpose (klaxon.faults): a deploy of ledger-consumer with LEDGER\_HANDLER=naive. It took 2.7 s. The deploy log has it as deploy 133, by klaxon.faults, at 2026-11-18T05:52:16Z. | run.json: injected, timings, deploys |
| 05:52:31 | 17.7 s | The alert MoneyCheckFailed fired, raised by klaxon.alerts from what the stack showed: The money check failed 2 of its 5 invariants: posted\_exactly\_once (7 findings), matches\_provider (8 findings). By its count, our ledger is $493.23 ahead of the provider's balance. | run.json: alert |
| 05:52:32 | 18 s | Klaxon's loop began to investigate the alert: claude-opus-5-5 through Claude Code on Aravind's seat, with read-only tools, offered one write action (rollback\_deploy) that runs only if a person approves it. | trace.jsonl: start |
| 05:54:08 | 113.8 s | The loop stopped: diagnosed (confidence 0.85), after 11 steps and 7 tool calls, in 95.8 s. | trace.jsonl: stop; run.json: loop |
| 05:54:08 | 113.9 s | The money check right before the request: 20 events counted more than once, 20 payments whose books differ from the provider's; our books $1,097.74 ahead of the provider's balance. | run.json: damage.at\_request |
| 05:54:09 |  | Klaxon asked to act, at the approval gate: request r-7dee, redeploy ledger-consumer: LEDGER\_HANDLER naive -\> idempotent, LEDGER\_BAD\_EVENTS dead\_letter (unchanged). It was open until 2026-11-18T06:04:09Z. Its undo: going back to LEDGER\_HANDLER=naive would deploy a known-bad setting: Klaxon will not; a person can, by hand. | gate.jsonl: requested |
| 05:56:02 |  | Request r-7dee was approved with make approve: typed at a terminal. | gate.jsonl: approved |
| 05:56:05 |  | The action ran: deploy 134 of ledger-consumer in the deploy log, by Klaxon, approved request r-7dee, with LEDGER\_HANDLER=idempotent, LEDGER\_BAD\_EVENTS=dead\_letter. | gate.jsonl: done |
| 05:56:05 |  | The money check right after the action: 36 events counted more than once, 36 payments whose books differ from the provider's; our books $1,518.19 ahead of the provider's balance. | run.json: verification.at\_action |
| 05:56:22 |  | Health, by code: 2 rounds of the rules and the containers, from 2026-11-18T05:56:06Z to 2026-11-18T05:56:22Z: healthy. | run.json: verification.health |
| 05:57:05 |  | The later money check, at least a minute after the action: 36 events counted more than once, 36 payments whose books differ from the provider's; our books $1,518.19 ahead of the provider's balance. | run.json: verification.after |
|  | 292 s (4 min 52 s) | Verification, by code and not by the model: passed. | run.json: verification |
|  | 292.2 s | The harness's clearing, its safety net, asked for the good settings of ledger-consumer (LEDGER\_HANDLER=idempotent, LEDGER\_BAD\_EVENTS=dead\_letter). The fault's cause was removed by Klaxon: its rollback (Klaxon, approved request r-7dee) removed the cause, and the harness's clearing then found the good settings running, and changed nothing. | run.json: cleared, removed\_by, deploys |
| 05:58:36 | 382.2 s | The rules read the stack again after the clearing: the alert MoneyCheckFailed fired; the money check failed: posted\_exactly\_once 36, matches\_provider 37; our books $1,518.19 ahead of the provider's. | run.json: evaluations (after) |

How long each part took:

- from the fault to the alert: 17.7 s
- Klaxon's investigation: 95.8 s
- from the alert to the diagnosis: 96.1 s
- the request's window: open until it expires: 600 s (10 min)
- the request was open until it was approved: 113 s (1 min 53 s)
- from the alert to the action: 214 s (3 min 34 s)
- from the fault's deploy to Klaxon's rollback: 229 s (3 min 49 s)
- the verification: 61.2 s
- from the fault to the harness's clearing: 292.2 s

## Impact

The synthetic clients saw no failures. 1327 payments arrived and 1327 succeeded. 0 were refused, and 0 were left with their outcome unknown. 127 refunds succeeded. The clients retried 44 times. The books were wrong, and the damage grew. At the alert the money check failed 2 of its 5 invariants, with posted\_exactly\_once at 7 and matches\_provider at 8. Our ledger was then $493.23 ahead of the provider's balance. Right before the request there were 41 findings: 20 events were counted more than once, and our books were $1,097.74 ahead. Right after the action there were 73 findings: 36 events were counted more than once, and our books were $1,518.19 ahead. So the damage grew while the request waited for approval. The later check at 05:57:05 UTC found the same 36 events and the same $1,518.19. After the clearing, posted\_exactly\_once stood at 36 and matches\_provider at 37, with the same $1,518.19. The evidence does not explain why matches\_provider rose to 37.

The facts. The alert, MoneyCheckFailed: The money check failed 2 of its 5 invariants: posted\_exactly\_once (7 findings), matches\_provider (8 findings). By its count, our ledger is $493.23 ahead of the provider's balance.

| When | UTC | The money check |
|---|---|---|
| at the alert | 05:52:31 | the money check failed: posted\_exactly\_once 7, matches\_provider 8; our books $493.23 ahead of the provider's |
| right before the request | 05:54:08 | 20 events counted more than once, 20 payments whose books differ from the provider's; our books $1,097.74 ahead of the provider's balance |
| right after the action | 05:56:05 | 36 events counted more than once, 36 payments whose books differ from the provider's; our books $1,518.19 ahead of the provider's balance |
| later, at least a minute after the action | 05:57:05 | 36 events counted more than once, 36 payments whose books differ from the provider's; our books $1,518.19 ahead of the provider's balance |
| after the harness's clearing | 05:58:36 | the money check failed: posted\_exactly\_once 36, matches\_provider 37; our books $1,518.19 ahead of the provider's |

The synthetic clients' own counts:

- The synthetic clients sent 3 new payments per second, on average.
- 1327 payments arrived, and 1327 got an answer: 1327 succeeded, 0 were refused, 0 got an answer the clients did not expect, and 0 were left with their outcome unknown.
- Refunds: 127 succeeded, 0 were refused, and 0 were left with their outcome unknown.
- The clients retried 44 times.

## Cause

The recorded cause was a deploy of the ledger consumer without its dedupe, with LEDGER\_HANDLER=naive. When the provider sent a webhook again after an acknowledgement it never got, the consumer booked it a second time. These double bookings broke the posted\_exactly\_once and matches\_provider invariants, and that failure raised MoneyCheckFailed. Klaxon's diagnosis had a confidence of 0.85. It said the ledger consumer had been redeployed with the naive handler, that it no longer deduplicated provider events, and that the provider resends webhooks. A keyword rule, rubric version 4, graded the diagnosis as naming the cause. It found all 3 required elements and the deploy that brought the fault. It also found that the proposed fix fits the remedy. The rule reads words, not meaning. The diagnosis's own numbers and times were not checked by code. One of its times differs from the deploy log's.

- The recorded cause (the fault was injected on purpose, so this is its cause): a deploy of the ledger consumer without its dedupe (LEDGER\_HANDLER=naive): every webhook the provider sends again, after an acknowledgement it never got, is booked a second time
- Klaxon's diagnosis, by its loop, live: claude-opus-5-5 through Claude Code on Aravind's seat. The loop stopped: diagnosed (confidence 0.85), after 11 steps and 7 tool calls, in 95.8 s. In the model's own words: At 05:52:14Z ledger-consumer was recreated with LEDGER\_HANDLER=naive. It is the only service changed after the 05:50:43 stack start. Since then it has booked every Kafka copy of a provider event, with no deduplication on event\_id. The provider resends webhooks, and webhook-receiver publishes each copy as a separate message, so each copy became a separate ledger transaction. Seven events were posted more than once: six payment.succeeded events, all twice except evt\_000308 three times, and one refund.succeeded, evt\_000288. The extra payment postings add $517.78 to provider\_clearing and the extra refund posting takes away $24.55, which nets to the alert's +$493.23. The ledger still balances, and our payments and refunds rows match the provider. So the fault is in ledger posting only. payment-confirmer, which runs by\_our\_id, still deduplicates.
- Its proposed fix, in the model's words: rollback on ledger-consumer: Roll ledger-consumer back to the settings it ran before the 05:52:14Z deploy, using rollback\_deploy. That should restore the event\_id-deduplicating handler and stop new double postings. The wrong bookings need a separate data repair afterwards, which the rollback does not do. Reverse the extra transactions for the 7 events in c1. The ids seen so far are 255, 266, 272, 273 and 297; the rest, including evt\_000298 and both extras for evt\_000308, are not shown. Then rerun the money check.
- The rule's verdict: names the cause (where: the ledger consumer: named; what: its dedupe is gone: named; how: the provider sends some webhooks again: named; the deploy that brought it: named). The fix fits the remedy: yes. Graded by a rule (klaxon.grading, rubric version 4, committed before any week 5 live run): keyword patterns per element of the right answer (a fault's recorded cause, or an evaluation case's). Not a person and not a model: it reads words, not meaning.

## Response

Klaxon had read-only tools and one write action, rollback\_deploy, which runs only if a person approves it. It asked at the gate in request r-7dee to redeploy the ledger consumer from LEDGER\_HANDLER naive to idempotent, with LEDGER\_BAD\_EVENTS left at dead\_letter. The request said Klaxon would not undo the change by going back to the naive handler, though a person could do that by hand. The diagnosis's own rollback plan did suggest redeploying the naive settings if the consumer misbehaved, and that conflicts with the request. The request was approved with make approve, typed at a terminal. The gate's log records how the decision was made, not who made it. Deploy 134 ran at 05:56:05 UTC, 214 s after the alert. Code then verified the action over 61.2 s. All containers were healthy and the rules were quiet at 2 evaluations in a row. Nothing the provider made after the action was booked wrong. The money was still not right.

- The gate: done: redeploy ledger-consumer: LEDGER\_HANDLER naive -\> idempotent, LEDGER\_BAD\_EVENTS dead\_letter (unchanged)
- The request: r-7dee, made at 2026-11-18T05:54:09Z, open until 2026-11-18T06:04:09Z: redeploy ledger-consumer: LEDGER\_HANDLER naive -\> idempotent, LEDGER\_BAD\_EVENTS dead\_letter (unchanged). It undoes deploy 133 of ledger-consumer, at 2026-11-18T05:52:16Z, by klaxon.faults. Its undo: going back to LEDGER\_HANDLER=naive would deploy a known-bad setting: Klaxon will not; a person can, by hand
- The decision: approved at 2026-11-18T05:56:02Z, by make approve: typed at a terminal. Only Aravind approves Klaxon's actions, and Claude never does (DECISIONS.md, the week's plan). The gate's log records how a decision was made, not who made it.
- What ran: deploy 134 of ledger-consumer at 2026-11-18T05:56:05Z, by Klaxon, approved request r-7dee: LEDGER\_HANDLER=idempotent, LEDGER\_BAD\_EVENTS=dead\_letter (it replaced LEDGER\_HANDLER=naive, LEDGER\_BAD\_EVENTS=dead\_letter).
- The fault's cause was removed by Klaxon: its rollback (Klaxon, approved request r-7dee) removed the cause, and the harness's clearing then found the good settings running, and changed nothing.

Verification, by code and not by the model: passed.

- Healthy: every container is healthy, and the metric rules and the books rule were quiet at 2 evaluations in a row.
- The cause is removed: nothing the provider made between the action and the last money check was booked wrong.
- The money is not right: 36 events counted more than once, 36 payments whose books differ from the provider's; our books $1,518.19 ahead of the provider's balance. A rollback repairs nothing: this damage stays until a person repairs it.

## Repairs still owed

The books still need repair. At the last check, at 05:57:05 UTC, 36 events were counted more than once, 36 payments had books that differ from the provider's, and our books were $1,518.19 ahead of the provider's balance. The examples included evt\_000308 posted 3 times and evt\_000251 posted 2 times. A rollback repairs nothing already booked. The recorded remedy is to reverse each double posting. That is a write, so it needs approval. Repairs that move money are not among Klaxon's actions, so a person must make them and then rerun the money check. The evidence also leaves open why the check after the clearing showed matches\_provider at 37, and that should be looked at during the repair.

- The last money check (later, at least a minute after the action) still found damage: 36 events counted more than once, 36 payments whose books differ from the provider's; our books $1,518.19 ahead of the provider's balance. A rollback repairs nothing: the damage stays until a person repairs it. Repairs that move money are not among Klaxon's actions: a person makes them (DECISIONS.md).
- The last money check's findings, the first 10 of 73 (run.json keeps them all): event evt\_000251 posted 2 times; event evt\_000261 posted 2 times; event evt\_000267 posted 2 times; event evt\_000268 posted 2 times; event evt\_000288 posted 2 times; event evt\_000298 posted 2 times; event evt\_000308 posted 3 times; event evt\_000352 posted 2 times; event evt\_000373 posted 2 times; event evt\_000437 posted 2 times
- The remedy this fault records (klaxon.faults): roll the ledger consumer back to the idempotent handler, then repair each double posting with a reversal (a write, so it needs approval)

## How this draft was made

- The facts: by code (klaxon.agent.postmortem), from the run's run.json, the gate's request, decision and log, and the loop's trace. postmortem.json keeps them with the narrative.
- The narrative: passed, after 1 call (call 1: passed).
- Each call's prompt and reply are in postmortem-recording.jsonl, recorded before Klaxon used them; make replay-postmortem replays them offline and checks this page comes out the same.
- The check's limit: it finds a number the facts do not hold, not one put to another use. The facts sit beside each paragraph, so a reader can see what each number counts.
