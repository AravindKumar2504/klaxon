# Postmortem draft: Payments counted twice in the books

A draft for a person to review, about one gated run of Klaxon on a fault injected on purpose: synthetic data, a simulated provider, no real money. The facts, in the lists and tables, were filled in by code from the run's saved evidence, in its folder 2026-11-17T042300Z\_gated-run\_duplicate-delivery. The paragraphs are Claude's draft: claude-opus-5-5, a recorded response received 2026-11-17T04:29:21.692Z (postmortem-recording.jsonl), drafted from those facts alone: its prompt held nothing else. Code checked every number in them against the facts, but for one, once, twice and the ordinals, which it does not read: all 74 are there.

## Summary

On 2026-11-17, a fault was injected on purpose into a payments system on one machine, with synthetic data and no real money. The fault was a deploy of the ledger consumer with LEDGER\_HANDLER=naive, which booked webhooks that the provider resent a second time. The alert MoneyCheckFailed fired 17.3 s after the fault. Klaxon diagnosed the cause, and a rule graded that diagnosis as naming the cause. Klaxon then asked at the approval gate to roll the ledger consumer back. Request r-33b5 was approved, and deploy 128 restored LEDGER\_HANDLER=idempotent. Verification by code passed: the system was healthy and the cause was removed. The money was still not right. The last money check found 20 events counted more than once, and our books $1,178.22 ahead of the provider's balance. That damage stays until a person repairs it.

## Timeline

At 04:24:16 UTC the alert rules were quiet and the money check passed. The fault was then injected as deploy 127 of the ledger consumer at 04:24:19, with LEDGER\_HANDLER=naive. The alert MoneyCheckFailed fired at 04:24:34, 17.3 s after the fault. Klaxon's loop began at 04:24:35 and stopped at 04:25:00, diagnosed with confidence 0.85, after 4 steps and 3 tool calls in 25.4 s. Klaxon made request r-33b5 at 04:25:01, open until 04:35:01. It was approved at 04:26:11, after 70 s. The rollback ran as deploy 128 at 04:26:14, 100 s after the alert and 115 s after the fault's deploy. Health checks ran from 04:26:15 to 04:26:31 and found the system healthy. A later money check ran at 04:27:14. Verification passed 178 s after the fault. The harness's clearing came 178.2 s after the fault, found the good settings already running, and changed nothing. At 04:28:45 the rules read the stack again, and MoneyCheckFailed fired again.

The facts. Times are UTC, on 2026-11-17 unless a row says otherwise; since the fault: by the run's own clock, from when the fault's injection began.

| UTC | Since the fault | What happened | From |
|---|---|---|---|
| 04:24:16 |  | The alert rules were quiet at 2 evaluations in a row, and the money check passed: the stack was ready for the fault. | run.json: evaluations (before) |
|  | 0 s | The fault's injection began, on purpose (klaxon.faults): a deploy of ledger-consumer with LEDGER\_HANDLER=naive. It took 2.3 s. The deploy log has it as deploy 127, by klaxon.faults, at 2026-11-17T04:24:19Z. | run.json: injected, timings, deploys |
| 04:24:34 | 17.3 s | The alert MoneyCheckFailed fired, raised by klaxon.alerts from what the stack showed: The money check failed 2 of its 5 invariants: posted\_exactly\_once (7 findings), matches\_provider (8 findings). By its count, our ledger is $461.00 ahead of the provider's balance. | run.json: alert |
| 04:24:35 | 18 s | Klaxon's loop began to investigate the alert: claude-opus-5-5 through Claude Code on Aravind's seat, with read-only tools, offered one write action (rollback\_deploy) that runs only if a person approves it. | trace.jsonl: start |
| 04:25:00 | 43.4 s | The loop stopped: diagnosed (confidence 0.85), after 4 steps and 3 tool calls, in 25.4 s. | trace.jsonl: stop; run.json: loop |
| 04:25:00 | 43.5 s | The money check right before the request: 9 events counted more than once, 9 payments whose books differ from the provider's; our books $559.21 ahead of the provider's balance. | run.json: damage.at\_request |
| 04:25:01 |  | Klaxon asked to act, at the approval gate: request r-33b5, redeploy ledger-consumer: LEDGER\_HANDLER naive -\> idempotent, LEDGER\_BAD\_EVENTS dead\_letter (unchanged). It was open until 2026-11-17T04:35:01Z. Its undo: going back to LEDGER\_HANDLER=naive would deploy a known-bad setting: Klaxon will not; a person can, by hand. | gate.jsonl: requested |
| 04:26:11 |  | Request r-33b5 was approved with make approve: typed at a terminal. | gate.jsonl: approved |
| 04:26:14 |  | The action ran: deploy 128 of ledger-consumer in the deploy log, by Klaxon, approved request r-33b5, with LEDGER\_HANDLER=idempotent, LEDGER\_BAD\_EVENTS=dead\_letter. | gate.jsonl: done |
| 04:26:14 |  | The money check right after the action: 20 events counted more than once, 20 payments whose books differ from the provider's; our books $1,178.22 ahead of the provider's balance. | run.json: verification.at\_action |
| 04:26:31 |  | Health, by code: 2 rounds of the rules and the containers, from 2026-11-17T04:26:15Z to 2026-11-17T04:26:31Z: healthy. | run.json: verification.health |
| 04:27:14 |  | The later money check, at least a minute after the action: 20 events counted more than once, 20 payments whose books differ from the provider's; our books $1,178.22 ahead of the provider's balance. | run.json: verification.after |
|  | 178 s (2 min 58 s) | Verification, by code and not by the model: passed. | run.json: verification |
|  | 178.2 s | The harness's clearing, its safety net, asked for the good settings of ledger-consumer (LEDGER\_HANDLER=idempotent, LEDGER\_BAD\_EVENTS=dead\_letter). The fault's cause was removed by Klaxon: its rollback (Klaxon, approved request r-33b5) removed the cause, and the harness's clearing then found the good settings running, and changed nothing. | run.json: cleared, removed\_by, deploys |
| 04:28:45 | 268.2 s | The rules read the stack again after the clearing: the alert MoneyCheckFailed fired; the money check failed: posted\_exactly\_once 20, matches\_provider 21; our books $1,178.22 ahead of the provider's. | run.json: evaluations (after) |

How long each part took:

- from the fault to the alert: 17.3 s
- Klaxon's investigation: 25.4 s
- from the alert to the diagnosis: 26.1 s
- the request's window: open until it expires: 600 s (10 min)
- the request was open until it was approved: 70 s (1 min 10 s)
- from the alert to the action: 100 s (1 min 40 s)
- from the fault's deploy to Klaxon's rollback: 115 s (1 min 55 s)
- the verification: 61.1 s
- from the fault to the harness's clearing: 178.2 s

## Impact

No real money and no real customers were involved. At the alert, at 04:24:34, the money check failed 2 of its 5 invariants: posted\_exactly\_once had 7 findings and matches\_provider had 8. Our ledger was $461.00 ahead of the provider's balance. Right before the request, at 04:25:00, 9 events were counted more than once, and our books were $559.21 ahead. Right after the action, at 04:26:14, 20 events were counted more than once, 20 payments' books differed from the provider's, and our books were $1,178.22 ahead. The later check at 04:27:14 found the same. After the clearing, at 04:28:45, the rules found posted\_exactly\_once 20 and matches\_provider 21, still $1,178.22 ahead. The evidence does not explain why matches\_provider showed 21 there but 20 payments in the later check. The synthetic clients saw no failures. 1021 payments arrived and 1021 succeeded, with 0 refused and 0 left unknown. Refunds: 103 succeeded and 0 were refused. The clients retried 35 times.

The facts. The alert, MoneyCheckFailed: The money check failed 2 of its 5 invariants: posted\_exactly\_once (7 findings), matches\_provider (8 findings). By its count, our ledger is $461.00 ahead of the provider's balance.

| When | UTC | The money check |
|---|---|---|
| at the alert | 04:24:34 | the money check failed: posted\_exactly\_once 7, matches\_provider 8; our books $461.00 ahead of the provider's |
| right before the request | 04:25:00 | 9 events counted more than once, 9 payments whose books differ from the provider's; our books $559.21 ahead of the provider's balance |
| right after the action | 04:26:14 | 20 events counted more than once, 20 payments whose books differ from the provider's; our books $1,178.22 ahead of the provider's balance |
| later, at least a minute after the action | 04:27:14 | 20 events counted more than once, 20 payments whose books differ from the provider's; our books $1,178.22 ahead of the provider's balance |
| after the harness's clearing | 04:28:45 | the money check failed: posted\_exactly\_once 20, matches\_provider 21; our books $1,178.22 ahead of the provider's |

The synthetic clients' own counts:

- The synthetic clients sent 3 new payments per second, on average.
- 1021 payments arrived, and 1021 got an answer: 1021 succeeded, 0 were refused, 0 got an answer the clients did not expect, and 0 were left with their outcome unknown.
- Refunds: 103 succeeded, 0 were refused, and 0 were left with their outcome unknown.
- The clients retried 35 times.

## Cause

The recorded cause was a deploy of the ledger consumer without its dedupe, with LEDGER\_HANDLER=naive. Every webhook that the provider sent again, after an acknowledgement it never got, was booked a second time. Those extra postings put our books ahead of the provider's balance, and the money check raised MoneyCheckFailed. Klaxon's diagnosis said the ledger consumer had been redeployed with the naive handler, which did not check whether an event was already posted, and that the provider resends unacknowledged webhooks. A rule, not a person or a model, graded the diagnosis as naming the cause. It named the place, the lost dedupe, the resent webhooks and the deploy, and its fix fit the remedy. The rule reads words, not meaning. The diagnosis gave a time for the deploy that differs from the deploy log's time. It also said it had not confirmed the previous handler setting, or whether any duplicates predated the deploy.

- The recorded cause (the fault was injected on purpose, so this is its cause): a deploy of the ledger consumer without its dedupe (LEDGER\_HANDLER=naive): every webhook the provider sends again, after an acknowledgement it never got, is booked a second time
- Klaxon's diagnosis, by its loop, live: claude-opus-5-5 through Claude Code on Aravind's seat. The loop stopped: diagnosed (confidence 0.85), after 4 steps and 3 tool calls, in 25.4 s. In the model's own words: ledger-consumer was redeployed at 04:24:17Z, 17 seconds before the alert, with LEDGER\_HANDLER=naive. This handler books every Kafka message it receives and never checks whether that event\_id was already posted. The provider resends webhooks until they are acknowledged, so the same event can reach provider-events more than once. Each copy then becomes a new ledger transaction. evt\_000308 was booked three times from three offsets (100, 101 and 103). Six other events, five charges and one refund, were booked twice. The result is $461.00 of extra provider\_clearing balance. The ledger still balances because each duplicate is a full double entry. Our payments and refunds still match the provider, which places the fault in ledger posting only. The duplicate refund also explains why pay\_000217 nets to 1 cent in our ledger against $24.56 at the provider.
- Its proposed fix, in the model's words: rollback on ledger-consumer: Roll ledger-consumer back to its previous settings, which should restore the idempotent handler that dedups on event\_id. Then repair the ledger as a separate step. Reverse the extra transactions for the 7 events in c1 (for example 255, 266, 272, 273, 297 and the extras for evt\_000298 and evt\_000308), keeping one posting per event. Re-run the money check to confirm that posted\_exactly\_once and matches\_provider pass.
- The rule's verdict: names the cause (where: the ledger consumer: named; what: its dedupe is gone: named; how: the provider sends some webhooks again: named; the deploy that brought it: named). The fix fits the remedy: yes. Graded by a rule (klaxon.grading, rubric version 4, committed before any week 5 live run): keyword patterns per element of the right answer (a fault's recorded cause, or an evaluation case's). Not a person and not a model: it reads words, not meaning.

## Response

Klaxon investigated with tools that only read, and was offered one write action, rollback\_deploy. It asked at the gate with request r-33b5: redeploy the ledger consumer with LEDGER\_HANDLER changed from naive to idempotent, and LEDGER\_BAD\_EVENTS left as dead\_letter. The request was approved at 04:26:11 with make approve, typed at a terminal. The gate's log records how the decision was made, not who made it. The plan says only Aravind approves Klaxon's actions. Deploy 128 ran at 04:26:14. Code, not the model, then verified the result in 61.1 s. It found every container healthy, and found that nothing the provider made after the action was booked wrong. It also found the money not right. The evidence leaves open why the health check reported the books rule quiet while the money checks around it failed, and why MoneyCheckFailed fired again at 04:28:45.

- The gate: done: redeploy ledger-consumer: LEDGER\_HANDLER naive -\> idempotent, LEDGER\_BAD\_EVENTS dead\_letter (unchanged)
- The request: r-33b5, made at 2026-11-17T04:25:01Z, open until 2026-11-17T04:35:01Z: redeploy ledger-consumer: LEDGER\_HANDLER naive -\> idempotent, LEDGER\_BAD\_EVENTS dead\_letter (unchanged). It undoes deploy 127 of ledger-consumer, at 2026-11-17T04:24:19Z, by klaxon.faults. Its undo: going back to LEDGER\_HANDLER=naive would deploy a known-bad setting: Klaxon will not; a person can, by hand
- The decision: approved at 2026-11-17T04:26:11Z, by make approve: typed at a terminal. Only Aravind approves Klaxon's actions, and Claude never does (DECISIONS.md, the week's plan). The gate's log records how a decision was made, not who made it.
- What ran: deploy 128 of ledger-consumer at 2026-11-17T04:26:14Z, by Klaxon, approved request r-33b5: LEDGER\_HANDLER=idempotent, LEDGER\_BAD\_EVENTS=dead\_letter (it replaced LEDGER\_HANDLER=naive, LEDGER\_BAD\_EVENTS=dead\_letter).
- The fault's cause was removed by Klaxon: its rollback (Klaxon, approved request r-33b5) removed the cause, and the harness's clearing then found the good settings running, and changed nothing.

Verification, by code and not by the model: passed.

- Healthy: every container is healthy, and the metric rules and the books rule were quiet at 2 evaluations in a row.
- The cause is removed: nothing the provider made between the action and the last money check was booked wrong.
- The money is not right: 20 events counted more than once, 20 payments whose books differ from the provider's; our books $1,178.22 ahead of the provider's balance. A rollback repairs nothing: this damage stays until a person repairs it.

## Repairs still owed

The damage already booked is still owed. The last money check, at 04:27:14, found 20 events counted more than once, 20 payments whose books differ from the provider's, and our books $1,178.22 ahead of the provider's balance. Its first examples include event evt\_000308, posted 3 times, and events such as evt\_000251 and evt\_000298, posted 2 times. The rollback stopped new duplicates but repaired nothing. The recorded remedy is to reverse each double posting. That is a write, so it needs approval. Repairs that move money are not among Klaxon's actions, so a person must make them. A person should also confirm why the alert fired again after the clearing, and settle the different counts for matches\_provider.

- The last money check (later, at least a minute after the action) still found damage: 20 events counted more than once, 20 payments whose books differ from the provider's; our books $1,178.22 ahead of the provider's balance. A rollback repairs nothing: the damage stays until a person repairs it. Repairs that move money are not among Klaxon's actions: a person makes them (DECISIONS.md).
- The last money check's findings, the first 10 of 41 (run.json keeps them all): event evt\_000251 posted 2 times; event evt\_000261 posted 2 times; event evt\_000267 posted 2 times; event evt\_000268 posted 2 times; event evt\_000288 posted 2 times; event evt\_000298 posted 2 times; event evt\_000308 posted 3 times; event evt\_000352 posted 2 times; event evt\_000373 posted 2 times; event evt\_000437 posted 2 times
- The remedy this fault records (klaxon.faults): roll the ledger consumer back to the idempotent handler, then repair each double posting with a reversal (a write, so it needs approval)

## How this draft was made

- The facts: by code (klaxon.agent.postmortem), from the run's run.json, the gate's request, decision and log, and the loop's trace. postmortem.json keeps them with the narrative.
- The narrative: passed, after 1 call (call 1: passed).
- Each call's prompt and reply are in postmortem-recording.jsonl, recorded before Klaxon used them; make replay-postmortem replays them offline and checks this page comes out the same.
- The check's limit: it finds a number the facts do not hold, not one put to another use. The facts sit beside each paragraph, so a reader can see what each number counts.
