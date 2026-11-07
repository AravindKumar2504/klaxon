# Postmortem draft: The ledger stops booking: a poison message

> Reviewed by Aravind, paragraph by paragraph, at 2026-11-07T06:37:38Z (his marks given in the chat, entered by Claude): 6 right, 0 wrong. His mark is under each paragraph; the draft is otherwise exactly as received.

A draft for a person to review, about one gated run of Klaxon on a fault injected on purpose: synthetic data, a simulated provider, no real money. The facts, in the lists and tables, were filled in by code from the run's saved evidence, in its folder 2026-10-21T094537Z\_gated-run\_poison-message. The paragraphs are Claude's draft: claude-opus-5-5, a recorded response received 2026-11-01T19:31:12.794Z (postmortem-recording.jsonl), drafted from those facts alone: its prompt held nothing else. Code checked every number in them against the facts, but for one, once, twice and the ordinals, which it does not read: all 55 are there.

## Summary

On 2026-10-21, a fault was injected on purpose into a payments system on one machine, with synthetic data and no real money. A deploy set the ledger consumer to retry unreadable events, and then one unreadable event arrived from the simulated provider. The ledger consumer stuck on that event and booked nothing behind it. The alert ConsumerLagHigh fired at 09:48:13 UTC. Klaxon investigated and diagnosed the cause with confidence 0.75. It asked at the approval gate to redeploy the ledger consumer so that it would dead letter bad events. The request was approved at 09:53:16 UTC, and the action ran at 09:53:19 UTC. Code verified the result and it passed: the stack was healthy, the cause was removed, and the money check passed. No synthetic client lost a payment or a refund.

> **Aravind's mark:** right: The paragraph accurately summarizes the injected poison-message incident, the resulting lag alert, Klaxon's diagnosis, the successful rollback deploy to dead-letter, and clean verification results.

## Timeline

At 09:46:53 UTC the alert rules were quiet and the money check passed. The fault was then injected. Deploy 5, by klaxon.faults at 09:46:56 UTC, set LEDGER\_BAD\_EVENTS=retry on the ledger consumer. Then the provider sent evt\_000261, a payment.updated for pay\_000239, with its amount\_cents written as the text 104.31. ConsumerLagHigh fired at 09:48:13 UTC, 78.9 s after the fault. Klaxon's loop began at 09:48:29 UTC. It stopped at 09:49:16 UTC as diagnosed, after 6 steps and 4 tool calls, in 47.2 s. Klaxon made request r-af6d at 09:49:32 UTC. It was approved at 09:53:16 UTC, after it had been open for 224 s. The action ran at 09:53:19 UTC as deploy 6. Health checks ran from 09:53:20 UTC to 09:53:52 UTC. A later money check passed at 09:54:19 UTC. Verification passed 446 s after the fault. The harness's clearing found the good settings already running and changed nothing. At 09:55:50 UTC no alert fired and the money check passed.

> **Aravind's mark:** right: The paragraph faithfully details the chronological timeline of the poison-message run, matching all saved timestamps and durations from stack readiness through final evaluation.

The facts. Times are UTC, on 2026-10-21 unless a row says otherwise; since the fault: by the run's own clock, from when the fault's injection began.

| UTC | Since the fault | What happened | From |
|---|---|---|---|
| 09:46:53 |  | The alert rules were quiet at 2 evaluations in a row, and the money check passed: the stack was ready for the fault. | run.json: evaluations (before) |
|  | 0 s | The fault's injection began, on purpose (klaxon.faults): a deploy of ledger-consumer with LEDGER\_BAD\_EVENTS=retry, then one unreadable event from the provider, evt\_000261: a payment.updated for pay\_000239, with its amount\_cents written as the text 104.31. It took 2.7 s. The deploy log has it as deploy 5, by klaxon.faults, at 2026-10-21T09:46:56Z. | run.json: injected, timings, deploys |
| 09:48:13 | 78.9 s | The alert ConsumerLagHigh fired, raised by klaxon.alerts from what the stack showed: The ledger consumer group is 250 messages behind on the topic provider-events, all partitions together; the threshold is 50. | run.json: alert |
| 09:48:29 | 94.9 s | Klaxon's loop began to investigate the alert: claude-opus-5-5 through Claude Code on Aravind's seat, with read-only tools, offered one write action (rollback\_deploy) that runs only if a person approves it. | trace.jsonl: start |
| 09:49:16 | 142.1 s | The loop stopped: diagnosed (confidence 0.75), after 6 steps and 4 tool calls, in 47.2 s. | trace.jsonl: stop; run.json: loop |
| 09:49:16 | 142.2 s | The money check right before the request: not known: the money check could not settle (not settled after 15.11 s: messages up to its cutoff not handled yet: ledger 520, payment-confirmer 0). | run.json: damage.at\_request |
| 09:49:32 |  | Klaxon asked to act, at the approval gate: request r-af6d, redeploy ledger-consumer: LEDGER\_HANDLER idempotent (unchanged), LEDGER\_BAD\_EVENTS retry -\> dead\_letter. It was open until 2026-10-21T09:59:32Z. Its undo: going back to LEDGER\_BAD\_EVENTS=retry would deploy a known-bad setting: Klaxon will not; a person can, by hand. | gate.jsonl: requested |
| 09:53:16 |  | Request r-af6d was approved with make approve: typed at a terminal. | gate.jsonl: approved |
| 09:53:19 |  | The action ran: deploy 6 of ledger-consumer in the deploy log, by Klaxon, approved request r-af6d, with LEDGER\_HANDLER=idempotent, LEDGER\_BAD\_EVENTS=dead\_letter. | gate.jsonl: done |
| 09:53:19 |  | The money check right after the action: no damage: the money check passed. | run.json: verification.at\_action |
| 09:53:52 |  | Health, by code: 3 rounds of the rules and the containers, from 2026-10-21T09:53:20Z to 2026-10-21T09:53:52Z: healthy. | run.json: verification.health |
| 09:54:19 |  | The later money check, at least a minute after the action: no damage: the money check passed. | run.json: verification.after |
|  | 446 s (7 min 26 s) | Verification, by code and not by the model: passed. | run.json: verification |
|  | 446.1 s | The harness's clearing, its safety net, asked for the good settings of ledger-consumer (LEDGER\_HANDLER=idempotent, LEDGER\_BAD\_EVENTS=dead\_letter). The fault's cause was removed by Klaxon: its rollback (Klaxon, approved request r-af6d) removed the cause, and the harness's clearing then found the good settings running, and changed nothing. | run.json: cleared, removed\_by, deploys |
| 09:55:50 | 536.1 s | The rules read the stack again after the clearing: no alert fired; the money check passed. | run.json: evaluations (after) |

How long each part took:

- from the fault to the alert: 78.9 s
- Klaxon's investigation: 47.2 s
- from the alert to the diagnosis: 63.2 s
- the request's window: open until it expires: 600 s (10 min)
- the request was open until it was approved: 224 s (3 min 44 s)
- from the alert to the action: 306 s (5 min 6 s)
- from the fault's deploy to Klaxon's rollback: 383 s (6 min 23 s)
- the verification: 61.3 s
- from the fault to the harness's clearing: 446.1 s

## Impact

At the alert, the ledger consumer group was 250 messages behind on the topic provider-events, and the threshold was 50. The money check could not settle at the alert. Right before the request, it still could not settle after 15.11 s, because the ledger had 520 messages up to its cutoff not handled yet. The evidence therefore does not show the state of the books during the stall. Right after the action, the money check passed. It passed again at 09:54:19 UTC and after the clearing at 09:55:50 UTC. The synthetic clients sent 3 new payments per second on average. 1819 payments arrived and all 1819 succeeded, with 0 refused and 0 left with their outcome unknown. Refunds: 170 succeeded and 0 were refused. No requests were shed. The clients retried 63 times.

> **Aravind's mark:** right: The paragraph accurately records the consumer lag at the alert, the inability of the money check to settle during the stall, subsequent passing checks, and flawless client success counts.

The facts. The alert, ConsumerLagHigh: The ledger consumer group is 250 messages behind on the topic provider-events, all partitions together; the threshold is 50.

| When | UTC | The money check |
|---|---|---|
| at the alert | 09:48:13 | the money check could not settle |
| right before the request | 09:49:16 | not known: the money check could not settle (not settled after 15.11 s: messages up to its cutoff not handled yet: ledger 520, payment-confirmer 0) |
| right after the action | 09:53:19 | no damage: the money check passed |
| later, at least a minute after the action | 09:54:19 | no damage: the money check passed |
| after the harness's clearing | 09:55:50 | the money check passed |

The synthetic clients' own counts:

- The synthetic clients sent 3 new payments per second, on average.
- 1819 payments arrived, and 1819 got an answer: 1819 succeeded, 0 were refused, 0 got an answer the clients did not expect, and 0 were left with their outcome unknown.
- Refunds: 170 succeeded, 0 were refused, and 0 were left with their outcome unknown.
- The clients retried 63 times.

## Cause

The recorded cause was a deploy of the ledger consumer that retries unreadable messages forever, followed by one unreadable event from the provider whose amount was a decimal string. The ledger consumer stuck on that event and booked nothing behind it. Its lag grew past the threshold, and that raised ConsumerLagHigh. Klaxon's diagnosis said the ledger consumer was stuck on a poison message, retried it forever under a retry setting from a recent deploy, and stopped booking. A keyword rule, not a person or a model, graded the diagnosis as naming the cause. It named every required element and also named the deploy that brought the fault. The rule found that the fix fit the recorded remedy. The diagnosis left open why the provider sent invalid amounts. It also said it had not run the money check or broken the lag down by partition. Its rollback plan named the faulty retry setting as the state to return to. Its blast radius text told the approver to check the deploy log before approving. That text was data and not an instruction.

> **Aravind's mark:** right: The paragraph correctly outlines the recorded cause, how the unreadable payload triggered the lag alert, the grading verdict confirming the diagnosis named the cause, and model disclaimers.

- The recorded cause (the fault was injected on purpose, so this is its cause): a deploy of the ledger consumer that retries unreadable messages forever (BAD\_EVENTS=retry), then one unreadable event from the provider (a payment.updated with its amount as a decimal string): the ledger consumer is stuck on it, and books nothing behind it
- Klaxon's diagnosis, by its loop, live: claude-opus-5-5 through Claude Code on Aravind's seat. The loop stopped: diagnosed (confidence 0.75), after 6 steps and 4 tool calls, in 47.2 s. In the model's own words: Ledger-consumer is stuck on a poison message. It was redeployed at 09:46:54 with BAD\_EVENTS=retry. Seconds later it hit provider-events/1\[@\]114, whose amount is not positive integer cents. It cannot book it, so it retries the same message forever (attempt 11+, every 15s) and never commits past offset 114. Partition 1 stalls and ledger lag climbs from 0 to 332, past the threshold of 50. Payment-confirmer runs BAD\_EVENTS=dead\_letter: it dead-lettered the same event and another at 1\[@\]116, and kept going. The stall comes from the ledger's bad-event policy, not from Kafka or the producer. Still open: why the provider sent invalid amounts, and what settings ledger-consumer ran before 09:46:54. I did not run money\_check, and I have not broken lag down by partition.
- Its proposed fix, in the model's words: rollback on ledger-consumer: Roll ledger-consumer back to the settings it ran before the 09:46:54 deploy. If those settings dead-letter bad events, it will skip 1\[@\]114 and 1\[@\]116 and drain the backlog. If they also retry, set BAD\_EVENTS=dead\_letter with a config change instead. Then check the dead-lettered events against the provider's records, repair them by hand, and run money\_check. Separately, find out why the provider sent invalid amounts.
- The rule's verdict: names the cause (where: the ledger consumer: named; what: one message it cannot read: named; how: it retries that message forever, so nothing behind it is booked: named; the deploy that brought it: named). The fix fits the remedy: yes. Graded by a rule (klaxon.grading, rubric version 1, committed in 95a64ab before the first live runs on the faults): keyword patterns per element of the fault's recorded cause. Not a person and not a model: it reads words, not meaning.

## Response

Klaxon was offered one write action, rollback\_deploy, which could run only if a person approved it. It made request r-af6d to redeploy the ledger consumer. LEDGER\_HANDLER stayed idempotent, and LEDGER\_BAD\_EVENTS changed from retry to dead\_letter. The request undid deploy 5. Its window was 600 s. Klaxon recorded that it would not go back to the retry setting, because that is known to be bad, though a person could do so by hand. The request was approved with make approve typed at a terminal. The gate's log records how the decision was made, not who made it, so the evidence does not show the approver. The rule says only Aravind approves. The action ran as deploy 6, 306 s after the alert and 383 s after the fault's deploy. Code then verified the result over 61.3 s and it passed. Every container was healthy, and the cause was removed. The money check passed. In 1 of the 3 health rounds the ConsumerLagHigh condition still held for the ledger.

> **Aravind's mark:** right: The paragraph correctly details the approval gate process, deployment execution of deploy 6, automated verification success, and the minor transient lag condition noted during health checks.

- The gate: done: redeploy ledger-consumer: LEDGER\_HANDLER idempotent (unchanged), LEDGER\_BAD\_EVENTS retry -\> dead\_letter
- The request: r-af6d, made at 2026-10-21T09:49:32Z, open until 2026-10-21T09:59:32Z: redeploy ledger-consumer: LEDGER\_HANDLER idempotent (unchanged), LEDGER\_BAD\_EVENTS retry -\> dead\_letter. It undoes deploy 5 of ledger-consumer, at 2026-10-21T09:46:56Z, by klaxon.faults. Its undo: going back to LEDGER\_BAD\_EVENTS=retry would deploy a known-bad setting: Klaxon will not; a person can, by hand
- The decision: approved at 2026-10-21T09:53:16Z, by make approve: typed at a terminal. Only Aravind approves Klaxon's actions, and Claude never does (DECISIONS.md, the week's plan). The gate's log records how a decision was made, not who made it.
- What ran: deploy 6 of ledger-consumer at 2026-10-21T09:53:19Z, by Klaxon, approved request r-af6d: LEDGER\_HANDLER=idempotent, LEDGER\_BAD\_EVENTS=dead\_letter (it replaced LEDGER\_HANDLER=idempotent, LEDGER\_BAD\_EVENTS=retry).
- The fault's cause was removed by Klaxon: its rollback (Klaxon, approved request r-af6d) removed the cause, and the harness's clearing then found the good settings running, and changed nothing.

Verification, by code and not by the model: passed.

- Healthy: every container is healthy, and the metric rules were quiet at 2 evaluations in a row.
- The cause is removed: nothing the provider made between the action and the last money check was booked wrong.
- The money is right: the money check passed.
- Health rounds that were not quiet: a rule's condition holds: ConsumerLagHigh (ledger) (1 of the 3 rounds).

## Repairs still owed

The last money check, at 09:54:19 UTC, passed, and the facts say nothing in the books is left to repair. Nothing is owed to the books. The facts do not say what became of evt\_000261 after it went to dead lettering. The diagnosis had said that dead lettered events would need handling by hand, so a person should confirm this. The fault was injected on purpose, so the invalid amount from the provider needs no further hunt in this run. A person should review this narrative. Aravind should confirm that he was the one who approved request r-af6d, because the gate's log does not record who approved it.

> **Aravind's mark:** right: The paragraph accurately notes that the money check passed with no outstanding ledger repairs required, while highlighting recommendations to handle dead-lettered events and confirm terminal approval.

- The last money check passed (later, at least a minute after the action): nothing in the books is left to repair.
- The remedy this fault records (klaxon.faults): roll the ledger consumer back to dead-lettering: the poison goes to the dead-letter topic, the backlog drains, and the money check passes again

## How this draft was made

- The facts: by code (klaxon.agent.postmortem), from the run's run.json, the gate's request, decision and log, and the loop's trace. postmortem.json keeps them with the narrative.
- The narrative: passed, after 2 calls (call 1: cause: 'three' is a number in words, and the facts do not write it so: write it in digits; call 2: passed).
- Each call's prompt and reply are in postmortem-recording.jsonl, recorded before Klaxon used them; make replay-postmortem replays them offline and checks this page comes out the same.
- The check's limit: it finds a number the facts do not hold, not one put to another use. The facts sit beside each paragraph, so a reader can see what each number counts.
