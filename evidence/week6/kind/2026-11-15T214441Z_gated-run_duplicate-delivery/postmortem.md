# Postmortem draft: Payments counted twice in the books

A draft for a person to review, about one gated run of Klaxon on a fault injected on purpose: synthetic data, a simulated provider, no real money. The facts, in the lists and tables, were filled in by code from the run's saved evidence, in its folder 2026-11-15T214441Z\_gated-run\_duplicate-delivery. The paragraphs are Claude's draft: claude-opus-5-5, a recorded response received 2026-11-15T22:31:25.685Z (postmortem-recording.jsonl), drafted from those facts alone: its prompt held nothing else. Code checked every number in them against the facts, but for one, once, twice and the ordinals, which it does not read: all 56 are there.

## Summary

On 2026-11-15, a fault was injected on purpose into the payments system on one machine, using synthetic data and no real money. A deploy of ledger-consumer switched off its dedupe, so webhooks that the provider sent again were booked a second time. The alert MoneyCheckFailed fired 18.4 s after the fault. Klaxon's loop diagnosed the cause with confidence 0.8, and a rule graded its diagnosis as naming the cause. Klaxon asked at the approval gate to redeploy ledger-consumer with the idempotent handler. The request was approved and the action ran. Code verified the result and it passed: the cause was removed and the stack was healthy. The money was not right. At the last money check, 41 events had been counted more than once, and our books were $2,067.33 ahead of the provider's balance. That damage stays until a person repairs it.

## Timeline

At 21:45:57 UTC, the alert rules were quiet and the money check passed. The fault's injection began at deploy 13 of ledger-consumer, which the deploy log puts at 21:46:01 UTC, with LEDGER\_HANDLER=naive. It took 3.4 s. MoneyCheckFailed fired at 21:46:16 UTC, 18.4 s after the fault. Klaxon's loop began at 21:46:17 UTC. It stopped diagnosed at 21:46:44 UTC, after 6 steps and 4 tool calls, in 27.4 s. Klaxon made request r-65be at 21:46:45 UTC, and it was open until 21:56:45 UTC. It was approved at 21:50:11 UTC, after 206 s. Deploy 14 ran at 21:50:14 UTC. That was 238 s after the alert and 253 s after the fault's deploy. Health checks from 21:50:15 UTC to 21:50:31 UTC found the stack healthy. A later money check ran at 21:51:14 UTC. Verification passed at 317.1 s after the fault. The harness's clearing followed at 317.3 s and changed nothing. At 21:52:45 UTC, MoneyCheckFailed fired again.

The facts. Times are UTC, on 2026-11-15 unless a row says otherwise; since the fault: by the run's own clock, from when the fault's injection began.

| UTC | Since the fault | What happened | From |
|---|---|---|---|
| 21:45:57 |  | The alert rules were quiet at 2 evaluations in a row, and the money check passed: the stack was ready for the fault. | run.json: evaluations (before) |
|  | 0 s | The fault's injection began, on purpose (klaxon.faults): a deploy of ledger-consumer with LEDGER\_HANDLER=naive. It took 3.4 s. The deploy log has it as deploy 13, by klaxon.faults, at 2026-11-15T21:46:01Z. | run.json: injected, timings, deploys |
| 21:46:16 | 18.4 s | The alert MoneyCheckFailed fired, raised by klaxon.alerts from what the stack showed: The money check failed 2 of its 5 invariants: posted\_exactly\_once (7 findings), matches\_provider (8 findings). By its count, our ledger is $461.00 ahead of the provider's balance. | run.json: alert |
| 21:46:17 | 19 s | Klaxon's loop began to investigate the alert: claude-opus-5-5 through Claude Code on Aravind's seat, with read-only tools, offered one write action (rollback\_deploy) that runs only if a person approves it. | trace.jsonl: start |
| 21:46:44 | 46.4 s | The loop stopped: diagnosed (confidence 0.8), after 6 steps and 4 tool calls, in 27.4 s. | trace.jsonl: stop; run.json: loop |
| 21:46:44 | 46.6 s | The money check right before the request: 9 events counted more than once, 9 payments whose books differ from the provider's; our books $559.21 ahead of the provider's balance. | run.json: damage.at\_request |
| 21:46:45 |  | Klaxon asked to act, at the approval gate: request r-65be, redeploy ledger-consumer: LEDGER\_HANDLER naive -\> idempotent, LEDGER\_BAD\_EVENTS dead\_letter (unchanged). It was open until 2026-11-15T21:56:45Z. Its undo: going back to LEDGER\_HANDLER=naive would deploy a known-bad setting: Klaxon will not; a person can, by hand. | gate.jsonl: requested |
| 21:50:11 |  | Request r-65be was approved with make approve: typed at a terminal. | gate.jsonl: approved |
| 21:50:14 |  | The action ran: deploy 14 of ledger-consumer in the deploy log, by Klaxon, approved request r-65be, with LEDGER\_HANDLER=idempotent, LEDGER\_BAD\_EVENTS=dead\_letter. | gate.jsonl: done |
| 21:50:14 |  | The money check right after the action: 41 events counted more than once, 41 payments whose books differ from the provider's; our books $2,067.33 ahead of the provider's balance. | run.json: verification.at\_action |
| 21:50:31 |  | Health, by code: 2 rounds of the rules and the cluster's rollouts and pods, from 2026-11-15T21:50:15Z to 2026-11-15T21:50:31Z: healthy. | run.json: verification.health |
| 21:51:14 |  | The later money check, at least a minute after the action: 41 events counted more than once, 41 payments whose books differ from the provider's; our books $2,067.33 ahead of the provider's balance. | run.json: verification.after |
|  | 317.1 s | Verification, by code and not by the model: passed. | run.json: verification |
|  | 317.3 s | The harness's clearing, its safety net, asked for the good settings of ledger-consumer (LEDGER\_HANDLER=idempotent, LEDGER\_BAD\_EVENTS=dead\_letter). The fault's cause was removed by Klaxon: its rollback (Klaxon, approved request r-65be) removed the cause, and the harness's clearing then found the good settings running, and changed nothing. | run.json: cleared, removed\_by, deploys |
| 21:52:45 | 407.3 s | The rules read the stack again after the clearing: the alert MoneyCheckFailed fired; the money check failed: posted\_exactly\_once 41, matches\_provider 42; our books $2,067.33 ahead of the provider's. | run.json: evaluations (after) |

How long each part took:

- from the fault to the alert: 18.4 s
- Klaxon's investigation: 27.4 s
- from the alert to the diagnosis: 28 s
- the request's window: open until it expires: 600 s (10 min)
- the request was open until it was approved: 206 s (3 min 26 s)
- from the alert to the action: 238 s (3 min 58 s)
- from the fault's deploy to Klaxon's rollback: 253 s (4 min 13 s)
- the verification: 60.8 s
- from the fault to the harness's clearing: 317.3 s

## Impact

At the alert, the money check failed 2 of its 5 invariants. It found 7 findings for posted\_exactly\_once and 8 for matches\_provider, and our ledger was $461.00 ahead of the provider's balance. Right before the request, 9 events had been counted more than once and the books were $559.21 ahead. Right after the action, 41 events had been counted more than once, 41 payments differed from the provider, and the books were $2,067.33 ahead. The damage grew while the request waited at the gate. The later check at 21:51:14 UTC found the same figures, so it did not grow after the action. After the clearing, matches\_provider showed 42 against 41 for posted\_exactly\_once. The evidence does not explain that difference beyond one finding that a total differs. The synthetic clients saw no failures. 1398 payments arrived and 1398 succeeded. 132 refunds succeeded, none were refused, and the clients retried 46 times.

The facts. The alert, MoneyCheckFailed: The money check failed 2 of its 5 invariants: posted\_exactly\_once (7 findings), matches\_provider (8 findings). By its count, our ledger is $461.00 ahead of the provider's balance.

| When | UTC | The money check |
|---|---|---|
| at the alert | 21:46:16 | the money check failed: posted\_exactly\_once 7, matches\_provider 8; our books $461.00 ahead of the provider's |
| right before the request | 21:46:44 | 9 events counted more than once, 9 payments whose books differ from the provider's; our books $559.21 ahead of the provider's balance |
| right after the action | 21:50:14 | 41 events counted more than once, 41 payments whose books differ from the provider's; our books $2,067.33 ahead of the provider's balance |
| later, at least a minute after the action | 21:51:14 | 41 events counted more than once, 41 payments whose books differ from the provider's; our books $2,067.33 ahead of the provider's balance |
| after the harness's clearing | 21:52:45 | the money check failed: posted\_exactly\_once 41, matches\_provider 42; our books $2,067.33 ahead of the provider's |

The synthetic clients' own counts:

- The synthetic clients sent 3 new payments per second, on average.
- 1398 payments arrived, and 1398 got an answer: 1398 succeeded, 0 were refused, 0 got an answer the clients did not expect, and 0 were left with their outcome unknown.
- Refunds: 132 succeeded, 0 were refused, and 0 were left with their outcome unknown.
- The clients retried 46 times.

## Cause

The recorded cause was a deploy of the ledger consumer without its dedupe, LEDGER\_HANDLER=naive. The provider sends a webhook again when it never got an acknowledgement, and the consumer booked every resent webhook a second time. Those double bookings broke the posted\_exactly\_once and matches\_provider invariants, and that raised MoneyCheckFailed. Klaxon's diagnosis named the ledger consumer, the missing dedupe, the provider's resent webhooks and the deploy that brought it. A keyword rule, not a person, graded it as naming the cause, and graded its fix as fitting the remedy. The diagnosis gave the deploy a different time than the deploy log does. It also left open whether duplicates occurred before the fault, and why the provider showed no amount for one payment. The evidence does not settle either point.

- The recorded cause (the fault was injected on purpose, so this is its cause): a deploy of the ledger consumer without its dedupe (LEDGER\_HANDLER=naive): every webhook the provider sends again, after an acknowledgement it never got, is booked a second time
- Klaxon's diagnosis, by its loop, live: claude-opus-5-5 through Claude Code on Aravind's seat. The loop stopped: diagnosed (confidence 0.8), after 6 steps and 4 tool calls, in 27.4 s. In the model's own words: ledger-consumer was redeployed at 21:45:59 (revision 2) with LEDGER\_HANDLER=naive. That handler does not deduplicate by provider event\_id. The provider resends webhooks until they are acknowledged, so the same event can reach provider-events more than once. Each copy lands at its own Kafka offset, and the naive handler posts a new ledger transaction for every copy. Example: evt\_000308 arrived at offsets 100, 101 and 103 and was posted 3 times. Because each duplicate is a balanced double entry, ledger\_balanced still passes. posted\_exactly\_once fails, though, and the provider\_clearing balance runs $461.00 ahead of the provider. Duplicated payment.succeeded events inflate it. A duplicated refund (evt\_000288, pay\_000217) pushes that payment's balance below the provider's. Still open: I did not check whether duplicates occurred before 21:45:59, or why the provider's figure for pay\_000240 shows $0. That charge may be newer than the provider's cutoff.
- Its proposed fix, in the model's words: rollback on ledger-consumer: Roll ledger-consumer back to its previous revision so it again posts each event\_id at most once. Then repair the data separately: reverse the extra ledger transactions listed by the money check (e.g. 255, 265, 272, 273, 297 and the other duplicates) and re-run the money check.
- The rule's verdict: names the cause (where: the ledger consumer: named; what: its dedupe is gone: named; how: the provider sends some webhooks again: named; the deploy that brought it: named). The fix fits the remedy: yes. Graded by a rule (klaxon.grading, rubric version 4, committed before any week 5 live run): keyword patterns per element of the right answer (a fault's recorded cause, or an evaluation case's). Not a person and not a model: it reads words, not meaning.

## Response

Klaxon ran with read-only tools and one write action, rollback\_deploy. That action could run only if a person approved it. Klaxon asked through request r-65be to redeploy ledger-consumer with LEDGER\_HANDLER changed from naive to idempotent, and LEDGER\_BAD\_EVENTS left at dead\_letter. The request said Klaxon would not undo it, because the undo would deploy a known-bad setting. The request was approved by make approve, typed at a terminal. The gate's log records how the decision was made, not who made it. The plan says only Aravind approves. Deploy 14 then ran. Code, not the model, verified the result over 60.8 s. Every container was healthy, and nothing the provider made after the action was booked wrong. The money was still not right. The harness's clearing found the good settings running.

- The gate: done: redeploy ledger-consumer: LEDGER\_HANDLER naive -\> idempotent, LEDGER\_BAD\_EVENTS dead\_letter (unchanged)
- The request: r-65be, made at 2026-11-15T21:46:45Z, open until 2026-11-15T21:56:45Z: redeploy ledger-consumer: LEDGER\_HANDLER naive -\> idempotent, LEDGER\_BAD\_EVENTS dead\_letter (unchanged). It undoes deploy 13 of ledger-consumer, at 2026-11-15T21:46:01Z, by klaxon.faults. Its undo: going back to LEDGER\_HANDLER=naive would deploy a known-bad setting: Klaxon will not; a person can, by hand
- The decision: approved at 2026-11-15T21:50:11Z, by make approve: typed at a terminal. Only Aravind approves Klaxon's actions, and Claude never does (DECISIONS.md, the week's plan). The gate's log records how a decision was made, not who made it.
- What ran: deploy 14 of ledger-consumer at 2026-11-15T21:50:14Z, by Klaxon, approved request r-65be: LEDGER\_HANDLER=idempotent, LEDGER\_BAD\_EVENTS=dead\_letter (it replaced LEDGER\_HANDLER=naive, LEDGER\_BAD\_EVENTS=dead\_letter).
- The fault's cause was removed by Klaxon: its rollback (Klaxon, approved request r-65be) removed the cause, and the harness's clearing then found the good settings running, and changed nothing.

Verification, by code and not by the model: passed.

- Healthy: every container is healthy, and the metric rules and the books rule were quiet at 2 evaluations in a row.
- The cause is removed: nothing the provider made between the action and the last money check was booked wrong.
- The money is not right: 41 events counted more than once, 41 payments whose books differ from the provider's; our books $2,067.33 ahead of the provider's balance. A rollback repairs nothing: this damage stays until a person repairs it.

## Repairs still owed

The books still hold the damage from the last money check. 41 events were counted more than once, 41 payments differ from the provider, and our books are $2,067.33 ahead of the provider's balance. The examples include evt\_000308, which was posted 3 times. The recorded remedy is to repair each double posting with a reversal. That reversal is a write that moves money, so it is not among Klaxon's actions. A person must make it, with approval, and then run the money check again until MoneyCheckFailed is quiet. A person should also check whether any duplicates came before the fault, and why matches\_provider showed one more finding than posted\_exactly\_once after the clearing.

- The last money check (later, at least a minute after the action) still found damage: 41 events counted more than once, 41 payments whose books differ from the provider's; our books $2,067.33 ahead of the provider's balance. A rollback repairs nothing: the damage stays until a person repairs it. Repairs that move money are not among Klaxon's actions: a person makes them (DECISIONS.md).
- The last money check's findings, the first 10 of 83 (run.json keeps them all): event evt\_000251 posted 2 times; event evt\_000261 posted 2 times; event evt\_000267 posted 2 times; event evt\_000268 posted 2 times; event evt\_000288 posted 2 times; event evt\_000298 posted 2 times; event evt\_000308 posted 3 times; event evt\_000352 posted 2 times; event evt\_000373 posted 2 times; event evt\_000437 posted 2 times
- The remedy this fault records (klaxon.faults): roll the ledger consumer back to the idempotent handler, then repair each double posting with a reversal (a write, so it needs approval)

## How this draft was made

- The facts: by code (klaxon.agent.postmortem), from the run's run.json, the gate's request, decision and log, and the loop's trace. postmortem.json keeps them with the narrative.
- The narrative: passed, after 1 call (call 1: passed).
- Each call's prompt and reply are in postmortem-recording.jsonl, recorded before Klaxon used them; make replay-postmortem replays them offline and checks this page comes out the same.
- The check's limit: it finds a number the facts do not hold, not one put to another use. The facts sit beside each paragraph, so a reader can see what each number counts.
