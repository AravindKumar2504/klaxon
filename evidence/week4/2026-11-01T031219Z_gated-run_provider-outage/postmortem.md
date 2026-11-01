# Postmortem draft: Payments fail: the provider hangs, and our circuit breaker fails fast

A draft for a person to review, about one gated run of Klaxon on a fault injected on purpose: synthetic data, a simulated provider, no real money. The facts, in the lists and tables, were filled in by code from the run's saved evidence, in its folder 2026-11-01T031219Z\_gated-run\_provider-outage. The paragraphs are Claude's draft: claude-opus-5-5, a recorded response received 2026-11-01T19:31:54.579Z (postmortem-recording.jsonl), drafted from those facts alone: its prompt held nothing else. Code checked every number in them against the facts, but for one, once, twice and the ordinals, which it does not read: all 58 are there.

## Summary

On 2026-11-01, a fault called provider-outage was injected on purpose into a payments system on one machine. The system ran with synthetic data and a simulated provider, so no real money moved. The provider's charge and refund API hung and then answered 503. The alert PaymentsErrorsHigh fired 47 s after the fault. Klaxon's loop investigated and stopped on diagnosed with confidence 0.65. A grading rule found that its diagnosis named the recorded cause. The diagnosis named no action, so nobody was asked to approve one, and no action ran. The harness's clearing removed the fault's cause. After the clearing, no alert fired and the money check passed. 299 payments and 9 refunds were still pending at the client's last status check.

## Timeline

At 03:13:35 UTC the alert rules were quiet at 2 evaluations in a row, and the money check passed, so the stack was ready. The fault's injection then began at the provider, and it took 0 s. The facts give no clock time for it. At 03:14:23 UTC, 47 s after the fault, the alert PaymentsErrorsHigh fired. At 03:14:24 UTC, 48 s after the fault, Klaxon's loop began to investigate. It ran claude-opus-5-5 through Claude Code on Aravind's seat. It had tools that could only read, and it was offered one write action, rollback\_deploy, gated on a person's approval. At 03:15:14 UTC, 98.5 s after the fault, the loop stopped on diagnosed after 10 steps and 8 tool calls, in 50.5 s. The diagnosis named no action. At 98.5 s after the fault, the harness's clearing asked for the provider to answer again. At 03:16:44 UTC, 188.5 s after the fault, the rules read the stack again. No alert fired, and the money check passed.

The facts. Times are UTC, on 2026-11-01 unless a row says otherwise; since the fault: by the run's own clock, from when the fault's injection began.

| UTC | Since the fault | What happened | From |
|---|---|---|---|
| 03:13:35 |  | The alert rules were quiet at 2 evaluations in a row, and the money check passed: the stack was ready for the fault. | run.json: evaluations (before) |
|  | 0 s | The fault's injection began, on purpose (klaxon.faults): at the provider: charges and refunds held 5 s, then 503. It took 0 s. | run.json: injected, timings, deploys |
| 03:14:23 | 47 s | The alert PaymentsErrorsHigh fired, raised by klaxon.alerts from what the stack showed: Payments are failing: over the last minute, 92% of the answers to POST /payments on payments-api were 5xx (8.78 per second), above the threshold of 20%. Payments are slow: over the last minute, the p95 latency of POST /payments on payments-api was 4.3 s, above the threshold of 2 s. | run.json: alert |
| 03:14:24 | 48 s | Klaxon's loop began to investigate the alert: claude-opus-5-5 through Claude Code on Aravind's seat, with read-only tools, offered one write action (rollback\_deploy) that runs only if a person approves it. | trace.jsonl: start |
| 03:15:14 | 98.5 s | The loop stopped: diagnosed (confidence 0.65), after 10 steps and 8 tool calls, in 50.5 s. | trace.jsonl: stop; run.json: loop |
| 03:15:14 |  | The diagnosis named no action, so nobody was asked to approve one. | gate.jsonl: no action |
|  | 98.5 s | The harness's clearing, its safety net, asked for the provider answering again. The fault's cause was removed by the harness's clearing: this fault's cause is not a deploy. | run.json: cleared, removed\_by, deploys |
| 03:16:44 | 188.5 s | The rules read the stack again after the clearing: no alert fired; the money check passed. | run.json: evaluations (after) |

How long each part took:

- from the fault to the alert: 47 s
- Klaxon's investigation: 50.5 s
- from the alert to the diagnosis: 51.5 s
- from the fault to the harness's clearing: 98.5 s

## Impact

The alert said that over the last minute, 92% of the answers to POST /payments on payments-api were 5xx, at 8.78 per second, above the threshold of 20%. It also said the p95 latency of POST /payments was 4.3 s, above the threshold of 2 s. The money check passed at the alert, with a lag of 0 for the ledger and 0 for payment-confirmer. It passed again at 03:16:44 UTC, after the clearing. The synthetic clients sent 3 new payments per second, on average. 810 payments arrived, and 810 got an answer. 511 succeeded, 0 were refused, and 0 got an answer the clients did not expect. 299 were left with their outcome unknown, and all 299 were still pending at the client's last status check. For refunds, 50 succeeded, 0 were refused, and 9 were left with their outcome unknown. All 9 were still pending at the last status check. The clients retried 974 times, and 0 requests were shed. The evidence does not say whether the pending payments and refunds settled since. These were synthetic clients, not real customers.

The facts. The alert, PaymentsErrorsHigh: Payments are failing: over the last minute, 92% of the answers to POST /payments on payments-api were 5xx (8.78 per second), above the threshold of 20%. Payments are slow: over the last minute, the p95 latency of POST /payments on payments-api was 4.3 s, above the threshold of 2 s.

| When | UTC | The money check |
|---|---|---|
| at the alert | 03:14:23 | the money check passed |
| after the harness's clearing | 03:16:44 | the money check passed |

The synthetic clients' own counts:

- The synthetic clients sent 3 new payments per second, on average.
- 810 payments arrived, and 810 got an answer: 511 succeeded, 0 were refused, 0 got an answer the clients did not expect, and 299 were left with their outcome unknown.
- Of the 299 payments whose outcome was left unknown, 0 were confirmed later, 299 were still pending at the client's last status check.
- Refunds: 50 succeeded, 0 were refused, and 9 were left with their outcome unknown.
- Of the 9 refunds whose outcome was left unknown, 0 were confirmed later, 9 were still pending at the client's last status check.
- The clients retried 974 times.

## Cause

The recorded cause was that the provider's charge and refund API hung for 5 s and then answered 503. Our payments API's calls to it timed out, each after 3 attempts. Its circuit breaker opened after 5 calls in a row got no answer. From then on the API failed fast. It answered 503 with outcome unknown at once, without calling the provider. It let one trial call through 10 s after it opened, and again after each trial that failed. The errors and slow answers raised the alert. Klaxon's diagnosis said the provider degraded, calls hung and timed out, retries pushed latency up, and the breaker opened and made most requests fail fast. It said the breaker was shedding load as designed and was not the cause. A rule in klaxon.grading, rubric version 3, found that it named every required element: the provider, the hang, and the open breaker. The rule reads words, not meaning. Some details in the diagnosis did not match the recorded cause. It described a different timeout length, and some error answers other than 503. It also said the money check was not run, but the facts show the money check passed at the alert.

- The recorded cause (the fault was injected on purpose, so this is its cause): the provider's charge and refund API hangs (5 s, then 503): our payments API's calls to it time out, each after 3 attempts, until its circuit breaker opens, after 5 calls in a row got no answer; from then on it fails fast, answering 503 'outcome unknown' at once without calling the provider, and lets one trial call through 10 s after it opened, and again after each trial that fails
- Klaxon's diagnosis, by its loop, live: claude-opus-5-5 through Claude Code on Aravind's seat. The loop stopped: diagnosed (confidence 0.65), after 10 steps and 8 tool calls, in 50.5 s. In the model's own words: At about 03:13:30 the provider (fake-provider, external) degraded. Some POST /v1/payments calls hung until our 3 s read timeout and a few returned 500. payments-api retries each call, so one request took 9.6 s (3 attempts of 3 s each), which pushed p95 above 2 s. The failures opened payments-api's circuit breaker. Since 03:13:56 most POST /payments requests fail fast with 503 and the payment is left pending, which gives the 92% 5xx rate. No change on our side lines up with the onset: every container was created together at 03:12:03. The breaker is shedding load as designed; it is not the cause. Open: the client metrics (c4, c5) seem not to record timeouts, so I cannot count them. I saw no provider-side data. The money check was not run, so payments the provider charged after we timed out are unverified.
- Its proposed fix, in the model's words: none on fake-provider (external) / payments-api circuit breaker: No rollback is needed. Escalate the timeouts and 500s on /v1/payments to the provider, and keep the breaker on. After the provider recovers, run the money check and reconcile the pending payments, including timed-out calls the provider may have charged. Follow-up: record timeouts in the client-duration and provider-attempts metrics.
- The rule's verdict: names the cause (where: the payment provider: named; what: it hangs or does not answer: named; how: our API fails fast, its circuit breaker open: named). The fix fits the remedy: yes. Graded by a rule (klaxon.grading, rubric version 3, committed in 75fedf1 before any live run on faults 4 to 6): keyword patterns per element of the fault's recorded cause. Not a person and not a model: it reads words, not meaning.

## Response

Klaxon was offered one write action, rollback\_deploy, which would run only if a person approved it at the gate. Its diagnosis named no action and judged that no rollback was needed. Since no action was named, nobody was asked to approve one, and nothing ran. Nothing was verified, because no action ran. The diagnosis proposed to escalate to the provider, keep the breaker on, and reconcile the pending payments after recovery. Those words are the model's own and were not checked by code. The fault's cause was not a deploy. It was removed by the harness's clearing, its safety net, at 98.5 s after the fault. After the clearing, the rules saw no alert and the money check passed.

- The gate: no action: the diagnosis names no action
- The fault's cause was removed by the harness's clearing: this fault's cause is not a deploy.

Verification, by code and not by the model: none.

- Nothing was verified: no action ran.

## Repairs still owed

The last money check passed at 03:16:44 UTC, after the clearing, so nothing in the books is left to repair. Up to its cutoff, the provider held no charge or refund that our records did not show as succeeded. So any payment or refund still pending had no money moved for it. 299 payments and 9 refunds were still pending at the client's last status check. Each one still pending needs a decision, either a retry with its key or marking it failed. A refund left pending blocks new refunds on its payment until it settles. The recorded remedy says nothing needs to change on our side, and that the provider must recover, with an escalation to it. The evidence does not name who must make these decisions, so a person reviewing this incident must assign them. The diagnosis also suggested recording timeouts in the client duration and provider attempts metrics. That suggestion is the model's own and was not checked.

- The last money check passed (after the harness's clearing): nothing in the books is left to repair.
- Of the outcomes the synthetic clients left unknown, 299 payments and 9 refunds were still pending at the client's last status check. The evidence does not say whether each settled since. The last money check passed: up to its cutoff, the provider held no charge or refund that our records did not show as succeeded, so any of them still pending had no money moved for it. One still pending needs a decision: a retry with its key, or marking it failed.
- The remedy this fault records (klaxon.faults): nothing to change on our side: the provider must recover (escalate to it). Meanwhile the circuit breaker keeps the API answering at once, and closes again when a trial call gets an answer. After it recovers, the payments and refunds left pending need a decision (retry them with their keys, or mark them failed): a refund left pending blocks new refunds on its payment until it settles

## How this draft was made

- The facts: by code (klaxon.agent.postmortem), from the run's run.json, the gate's request, decision and log, and the loop's trace. postmortem.json keeps them with the narrative.
- The narrative: passed, after 2 calls (call 1: cause: '500' is not in the facts, as a number; repairs: 'two' is a number in words, and the facts do not write it so: write it in digits; call 2: passed).
- Each call's prompt and reply are in postmortem-recording.jsonl, recorded before Klaxon used them; make replay-postmortem replays them offline and checks this page comes out the same.
- The check's limit: it finds a number the facts do not hold, not one put to another use. The facts sit beside each paragraph, so a reader can see what each number counts.
