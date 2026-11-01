# Postmortem draft: Payments counted twice in the books

A draft for a person to review, about one gated run of Klaxon on a fault injected on purpose: synthetic data, a simulated provider, no real money. The facts, in the lists and tables, were filled in by code from the run's saved evidence, in its folder 2026-10-21T090513Z\_gated-run\_duplicate-delivery. The paragraphs are Claude's draft: claude-opus-5-5, a recorded response received 2026-11-01T19:30:28.588Z (postmortem-recording.jsonl), drafted from those facts alone: its prompt held nothing else. Code checked every number in them against the facts, but for one, once, twice and the ordinals, which it does not read: all 77 are there.

## Summary

On 2026-10-21, a fault called duplicate-delivery was injected on purpose into the payments stack on one machine. The data was synthetic and no real money moved. A deploy of ledger-consumer with LEDGER\_HANDLER=naive made the ledger book resent provider webhooks a second time. The alert MoneyCheckFailed fired at 09:06:47 UTC. Klaxon's loop diagnosed the cause in 45.8 s. It asked at the approval gate to redeploy ledger-consumer with the idempotent handler. Request r-e16d was approved, and the action ran at 09:09:13 UTC. Verification by code passed. The stack was healthy and the cause was removed. The money was not right. The last money check found 28 events counted more than once. It found our books $1,531.87 ahead of the provider's balance. That damage stays until a person repairs it.

## Timeline

At 09:06:29 UTC the alert rules were quiet at 2 evaluations in a row, and the money check passed. The fault's injection then began. The deploy log has it as deploy 1 of ledger-consumer, by klaxon.faults, at 09:06:32 UTC. The alert MoneyCheckFailed fired at 09:06:47 UTC, 17.5 s after the fault. Klaxon's loop began at 09:06:48 UTC. It stopped at 09:07:34 UTC, diagnosed with confidence 0.85, after 6 steps and 4 tool calls. Klaxon made request r-e16d at 09:07:35 UTC. The request was open until 09:17:35 UTC. It was approved at 09:09:11 UTC, after 96 s. The action ran at 09:09:13 UTC as deploy 2 of ledger-consumer. That was 161 s after the fault's deploy. Health checks from 09:09:14 UTC to 09:09:30 UTC found the stack healthy. A later money check ran at 09:10:13 UTC. Verification passed 224.1 s after the fault. The harness's clearing came at 224.3 s. It found the good settings running and changed nothing. At 09:11:44 UTC the rules read the stack again. MoneyCheckFailed fired again, because the money check still failed.

The facts. Times are UTC, on 2026-10-21 unless a row says otherwise; since the fault: by the run's own clock, from when the fault's injection began.

| UTC | Since the fault | What happened | From |
|---|---|---|---|
| 09:06:29 |  | The alert rules were quiet at 2 evaluations in a row, and the money check passed: the stack was ready for the fault. | run.json: evaluations (before) |
|  | 0 s | The fault's injection began, on purpose (klaxon.faults): a deploy of ledger-consumer with LEDGER\_HANDLER=naive. It took 2.5 s. The deploy log has it as deploy 1, by klaxon.faults, at 2026-10-21T09:06:32Z. | run.json: injected, timings, deploys |
| 09:06:47 | 17.5 s | The alert MoneyCheckFailed fired, raised by klaxon.alerts from what the stack showed: The money check failed 2 of its 5 invariants: posted\_exactly\_once (7 findings), matches\_provider (8 findings). By its count, our ledger is $493.23 ahead of the provider's balance. | run.json: alert |
| 09:06:48 | 18 s | Klaxon's loop began to investigate the alert: claude-opus-5-5 through Claude Code on Aravind's seat, with read-only tools, offered one write action (rollback\_deploy) that runs only if a person approves it. | trace.jsonl: start |
| 09:07:34 | 63.9 s | The loop stopped: diagnosed (confidence 0.85), after 6 steps and 4 tool calls, in 45.8 s. | trace.jsonl: stop; run.json: loop |
| 09:07:34 | 64 s (1 min 4 s) | The money check right before the request: 12 events counted more than once, 12 payments whose books differ from the provider's; our books $891.82 ahead of the provider's balance. | run.json: damage.at\_request |
| 09:07:35 |  | Klaxon asked to act, at the approval gate: request r-e16d, redeploy ledger-consumer: LEDGER\_HANDLER naive -\> idempotent, LEDGER\_BAD\_EVENTS dead\_letter (unchanged). It was open until 2026-10-21T09:17:35Z. Its undo: going back to LEDGER\_HANDLER=naive would deploy a known-bad setting: Klaxon will not; a person can, by hand. | gate.jsonl: requested |
| 09:09:11 |  | Request r-e16d was approved with make approve: typed at a terminal. | gate.jsonl: approved |
| 09:09:13 |  | The action ran: deploy 2 of ledger-consumer in the deploy log, by Klaxon, approved request r-e16d, with LEDGER\_HANDLER=idempotent, LEDGER\_BAD\_EVENTS=dead\_letter. | gate.jsonl: done |
| 09:09:13 |  | The money check right after the action: 28 events counted more than once, 28 payments whose books differ from the provider's; our books $1,531.87 ahead of the provider's balance. | run.json: verification.at\_action |
| 09:09:30 |  | Health, by code: 2 rounds of the rules and the containers, from 2026-10-21T09:09:14Z to 2026-10-21T09:09:30Z: healthy. | run.json: verification.health |
| 09:10:13 |  | The later money check, at least a minute after the action: 28 events counted more than once, 28 payments whose books differ from the provider's; our books $1,531.87 ahead of the provider's balance. | run.json: verification.after |
|  | 224.1 s | Verification, by code and not by the model: passed. | run.json: verification |
|  | 224.3 s | The harness's clearing, its safety net, asked for the good settings of ledger-consumer (LEDGER\_HANDLER=idempotent, LEDGER\_BAD\_EVENTS=dead\_letter). The fault's cause was removed by Klaxon: its rollback (Klaxon, approved request r-e16d) removed the cause, and the harness's clearing then found the good settings running, and changed nothing. | run.json: cleared, removed\_by, deploys |
| 09:11:44 | 314.3 s | The rules read the stack again after the clearing: the alert MoneyCheckFailed fired; the money check failed: posted\_exactly\_once 28, matches\_provider 29; our books $1,531.87 ahead of the provider's. | run.json: evaluations (after) |

How long each part took:

- from the fault to the alert: 17.5 s
- Klaxon's investigation: 45.8 s
- from the alert to the diagnosis: 46.4 s
- the request's window: open until it expires: 600 s (10 min)
- the request was open until it was approved: 96 s (1 min 36 s)
- from the alert to the action: 146 s (2 min 26 s)
- from the fault's deploy to Klaxon's rollback: 161 s (2 min 41 s)
- the verification: 60.6 s
- from the fault to the harness's clearing: 224.3 s

## Impact

At the alert, the money check failed 2 of its 5 invariants. It found posted\_exactly\_once with 7 findings and matches\_provider with 8. Our books were $493.23 ahead of the provider's. Lag was 0 for the ledger and 0 for payment-confirmer. Right before the request, at 09:07:34 UTC, 12 events were counted more than once. 12 payments had books that differed from the provider's. Our books were $891.82 ahead. Right after the action, at 09:09:13 UTC, 28 events were counted more than once. 28 payments differed, with 57 findings in all. Our books were $1,531.87 ahead. The check at 09:10:13 UTC found the same. So the damage grew while the request waited for approval. It did not grow after the action. After the clearing, at 09:11:44 UTC, posted\_exactly\_once had 28 findings and matches\_provider had 29. Our books were still $1,531.87 ahead. The synthetic clients sent 3 new payments per second, on average. 1118 payments arrived, and all 1118 succeeded. 0 were refused, and 0 were left with their outcome unknown. 112 refunds succeeded, and 0 were refused. The clients retried 41 times, and 0 requests were shed. By these counts, the clients saw no errors. The damage was in our books.

The facts. The alert, MoneyCheckFailed: The money check failed 2 of its 5 invariants: posted\_exactly\_once (7 findings), matches\_provider (8 findings). By its count, our ledger is $493.23 ahead of the provider's balance.

| When | UTC | The money check |
|---|---|---|
| at the alert | 09:06:47 | the money check failed: posted\_exactly\_once 7, matches\_provider 8; our books $493.23 ahead of the provider's |
| right before the request | 09:07:34 | 12 events counted more than once, 12 payments whose books differ from the provider's; our books $891.82 ahead of the provider's balance |
| right after the action | 09:09:13 | 28 events counted more than once, 28 payments whose books differ from the provider's; our books $1,531.87 ahead of the provider's balance |
| later, at least a minute after the action | 09:10:13 | 28 events counted more than once, 28 payments whose books differ from the provider's; our books $1,531.87 ahead of the provider's balance |
| after the harness's clearing | 09:11:44 | the money check failed: posted\_exactly\_once 28, matches\_provider 29; our books $1,531.87 ahead of the provider's |

The synthetic clients' own counts:

- The synthetic clients sent 3 new payments per second, on average.
- 1118 payments arrived, and 1118 got an answer: 1118 succeeded, 0 were refused, 0 got an answer the clients did not expect, and 0 were left with their outcome unknown.
- Refunds: 112 succeeded, 0 were refused, and 0 were left with their outcome unknown.
- The clients retried 41 times.

## Cause

The recorded cause was a deploy of the ledger consumer without its dedupe, with LEDGER\_HANDLER=naive. Every webhook the provider sends again, after an acknowledgement it never got, was booked a second time. Those double bookings failed the posted\_exactly\_once invariant. They also put our ledger ahead of the provider, which failed matches\_provider. That raised MoneyCheckFailed. Klaxon's diagnosis named this cause. It said the naive handler booked records without deduplicating by event\_id. It said resent webhooks were each booked as new ledger transactions. The grading rule found that it named the cause. It named all 3 required elements and the deploy that brought it. Its fix fit the recorded remedy. The rule reads words, not meaning. The diagnosis itself left open which handler ran before the deploy. Its own deploy time was not one the deploy log holds. Its other numbers are the model's words and were not checked by code.

- The recorded cause (the fault was injected on purpose, so this is its cause): a deploy of the ledger consumer without its dedupe (LEDGER\_HANDLER=naive): every webhook the provider sends again, after an acknowledgement it never got, is booked a second time
- Klaxon's diagnosis, by its loop, live: claude-opus-5-5 through Claude Code on Aravind's seat. The loop stopped: diagnosed (confidence 0.85), after 6 steps and 4 tool calls, in 45.8 s. In the model's own words: At 09:06:30 ledger-consumer was redeployed with LEDGER\_HANDLER=naive. This handler appears to book every Kafka record it reads without deduplicating by event\_id. The provider resends webhooks until they are acknowledged, so one event can land on provider-events more than once, at different offsets. Each copy was booked as a new ledger transaction. That gives 7 events posted 2 or 3 times (posted\_exactly\_once failed). It also puts provider\_clearing $493.23 ahead of the provider (matches\_provider failed). Duplicate payment.succeeded events inflate the balance. The duplicate refund.succeeded for pay\_000217 leaves that payment at 1 cent against the provider's 2456. The ledger stays balanced because each duplicate is a full double-entry transaction. Our payments and refunds still match the provider, so the damage is confined to the ledger. Still open: the handler that ran before this deploy, and whether it was idempotent, was not confirmed.
- Its proposed fix, in the model's words: rollback on ledger-consumer: Roll ledger-consumer back to the settings it ran before the 09:06:30 deploy, presumably an idempotent handler that dedupes by event\_id. Then repair the ledger separately: reverse the extra transactions the money check lists (e.g. 255, 266, 272, 273, 297, plus the extras for evt\_000298 and evt\_000308). Then rerun money\_check.
- The rule's verdict: names the cause (where: the ledger consumer: named; what: its dedupe is gone: named; how: the provider sends some webhooks again: named; the deploy that brought it: named). The fix fits the remedy: yes. Graded by a rule (klaxon.grading, rubric version 1, committed in 95a64ab before the first live runs on the faults): keyword patterns per element of the fault's recorded cause. Not a person and not a model: it reads words, not meaning.

## Response

Klaxon was offered one write action, rollback\_deploy, which runs only if a person approves it. It named rollback\_deploy for ledger-consumer. Its request r-e16d asked to redeploy ledger-consumer with LEDGER\_HANDLER changed from naive to idempotent, and LEDGER\_BAD\_EVENTS left at dead\_letter. The request said that going back to naive would deploy a known bad setting. Klaxon will not do that, and a person can, by hand. The request was approved with make approve, typed at a terminal. The gate's log records how a decision was made, not who made it. The rule says only Aravind approves Klaxon's actions, and Claude never does. The evidence does not show who typed the approval. The action ran as deploy 2 of ledger-consumer. Code, not the model, then verified it over 60.6 s. Every container was healthy, and the rules were quiet at 2 evaluations in a row. The cause was removed: nothing the provider made between the action and the last money check was booked wrong. The money was not right. The harness's clearing later found the good settings running and changed nothing.

- The gate: done: redeploy ledger-consumer: LEDGER\_HANDLER naive -\> idempotent, LEDGER\_BAD\_EVENTS dead\_letter (unchanged)
- The request: r-e16d, made at 2026-10-21T09:07:35Z, open until 2026-10-21T09:17:35Z: redeploy ledger-consumer: LEDGER\_HANDLER naive -\> idempotent, LEDGER\_BAD\_EVENTS dead\_letter (unchanged). It undoes deploy 1 of ledger-consumer, at 2026-10-21T09:06:32Z, by klaxon.faults. Its undo: going back to LEDGER\_HANDLER=naive would deploy a known-bad setting: Klaxon will not; a person can, by hand
- The decision: approved at 2026-10-21T09:09:11Z, by make approve: typed at a terminal. Only Aravind approves Klaxon's actions, and Claude never does (DECISIONS.md, the week's plan). The gate's log records how a decision was made, not who made it.
- What ran: deploy 2 of ledger-consumer at 2026-10-21T09:09:13Z, by Klaxon, approved request r-e16d: LEDGER\_HANDLER=idempotent, LEDGER\_BAD\_EVENTS=dead\_letter (it replaced LEDGER\_HANDLER=naive, LEDGER\_BAD\_EVENTS=dead\_letter).
- The fault's cause was removed by Klaxon: its rollback (Klaxon, approved request r-e16d) removed the cause, and the harness's clearing then found the good settings running, and changed nothing.

Verification, by code and not by the model: passed.

- Healthy: every container is healthy, and the metric rules were quiet at 2 evaluations in a row.
- The cause is removed: nothing the provider made between the action and the last money check was booked wrong.
- The money is not right: 28 events counted more than once, 28 payments whose books differ from the provider's; our books $1,531.87 ahead of the provider's balance. A rollback repairs nothing: this damage stays until a person repairs it.

## Repairs still owed

The books are still wrong after the run. The last money check, at 09:10:13 UTC, found 28 events counted more than once. It found 28 payments whose books differ from the provider's, with 57 findings in all. Our books were $1,531.87 ahead of the provider's balance. Its first examples included event evt\_000251 posted 2 times and event evt\_000308 posted 3 times. The rollback repaired none of this. The recorded remedy is to repair each double posting with a reversal. That is a write, so it needs approval. Repairs that move money are not among Klaxon's actions, so a person must make them. The diagnosis suggested transactions to reverse, but those are the model's words and were not checked by code. The person doing the repair should work from the money check's own findings. MoneyCheckFailed was still firing at 09:11:44 UTC and will stay failing until the repair is done. The evidence does not name who will do the repair, or when.

- The last money check (later, at least a minute after the action) still found damage: 28 events counted more than once, 28 payments whose books differ from the provider's; our books $1,531.87 ahead of the provider's balance. A rollback repairs nothing: the damage stays until a person repairs it. Repairs that move money are not among Klaxon's actions: a person makes them (DECISIONS.md).
- The last money check's findings, the first 10 of 57 (run.json keeps them all): event evt\_000251 posted 2 times; event evt\_000261 posted 2 times; event evt\_000267 posted 2 times; event evt\_000268 posted 2 times; event evt\_000288 posted 2 times; event evt\_000298 posted 2 times; event evt\_000308 posted 3 times; event evt\_000352 posted 2 times; event evt\_000373 posted 2 times; event evt\_000437 posted 2 times
- The remedy this fault records (klaxon.faults): roll the ledger consumer back to the idempotent handler, then repair each double posting with a reversal (a write, so it needs approval)

## How this draft was made

- The facts: by code (klaxon.agent.postmortem), from the run's run.json, the gate's request, decision and log, and the loop's trace. postmortem.json keeps them with the narrative.
- The narrative: passed, after 1 call (call 1: passed).
- Each call's prompt and reply are in postmortem-recording.jsonl, recorded before Klaxon used them; make replay-postmortem replays them offline and checks this page comes out the same.
- The check's limit: it finds a number the facts do not hold, not one put to another use. The facts sit beside each paragraph, so a reader can see what each number counts.
