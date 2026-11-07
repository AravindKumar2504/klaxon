# Postmortem draft: Customers charged 100 times the amount: a money-unit bug

> Reviewed by Aravind, paragraph by paragraph, at 2026-11-07T06:37:38Z (his marks given in the chat, entered by Claude): 6 right, 0 wrong. His mark is under each paragraph; the draft is otherwise exactly as received.

A draft for a person to review, about one gated run of Klaxon on a fault injected on purpose: synthetic data, a simulated provider, no real money. The facts, in the lists and tables, were filled in by code from the run's saved evidence, in its folder 2026-10-24T082544Z\_gated-run\_money-unit. The paragraphs are Claude's draft: claude-opus-5-5, a recorded response received 2026-11-01T19:32:22.428Z (postmortem-recording.jsonl), drafted from those facts alone: its prompt held nothing else. Code checked every number in them against the facts, but for one, once, twice and the ordinals, which it does not read: all 62 are there.

## Summary

On 2026-10-24 a fault was injected on purpose into a payments system running on one machine, with synthetic data and a simulated provider, so no real money moved. The fault was a deploy of payments-api with CHARGE\_AMOUNT\_UNIT=dollars. It made the provider charge customers 100 times the amount our records kept. The alert MoneyCheckFailed fired 17.4 s after the fault. Klaxon investigated and named the cause, and the grading rule agreed. Klaxon asked at the approval gate to redeploy payments-api with CHARGE\_AMOUNT\_UNIT=cents. The request r-742c was approved, and the action ran as deploy 22. Verification by code passed. The cause was removed and the stack was healthy, but the money was not right. The last money check still found 1080 payments that disagreed with the provider's charges. That damage stays until a person repairs it.

> **Aravind's mark:** right: The paragraph accurately summarizes the injected money-unit bug, the 100x overcharge impact, Klaxon's diagnosis, the successful rollback deploy, and the lingering uncorrected ledger discrepancies.

## Timeline

At 08:27:00 UTC the alert rules were quiet at 2 evaluations in a row, and the money check passed. The fault's injection then began. The deploy log has it as deploy 21 of payments-api, by klaxon.faults, at 08:27:03 UTC, and it took 2.4 s. At 08:27:18 UTC the alert MoneyCheckFailed fired. At 08:27:19 UTC Klaxon's loop began to investigate. At 08:27:51 UTC the loop stopped on diagnosed, after 6 steps and 5 tool calls, in 32.5 s. At 08:27:52 UTC Klaxon made request r-742c at the gate, open until 08:37:52 UTC. It was approved at 08:33:15 UTC, after 323 s. The action ran at 08:33:18 UTC as deploy 22. Health checks ran from 08:33:19 UTC to 08:33:35 UTC and found the stack healthy. A later money check ran at 08:34:18 UTC. Verification passed at 438.1 s after the fault. The harness's clearing followed at 438.3 s, found the good settings already running, and changed nothing. At 08:35:49 UTC the rules read the stack again, and MoneyCheckFailed fired.

> **Aravind's mark:** right: The paragraph precisely details the chronological timeline of the money-unit incident, including precise timestamps and durations for fault injection, alert, investigation, gate approval, and verification.

The facts. Times are UTC, on 2026-10-24 unless a row says otherwise; since the fault: by the run's own clock, from when the fault's injection began.

| UTC | Since the fault | What happened | From |
|---|---|---|---|
| 08:27:00 |  | The alert rules were quiet at 2 evaluations in a row, and the money check passed: the stack was ready for the fault. | run.json: evaluations (before) |
|  | 0 s | The fault's injection began, on purpose (klaxon.faults): a deploy of payments-api with CHARGE\_AMOUNT\_UNIT=dollars. It took 2.4 s. The deploy log has it as deploy 21, by klaxon.faults, at 2026-10-24T08:27:03Z. | run.json: injected, timings, deploys |
| 08:27:18 | 17.4 s | The alert MoneyCheckFailed fired, raised by klaxon.alerts from what the stack showed: The money check failed 1 of its 5 invariants: payments\_match\_provider (56 findings). | run.json: alert |
| 08:27:19 | 18 s | Klaxon's loop began to investigate the alert: claude-opus-5-5 through Claude Code on Aravind's seat, with read-only tools, offered one write action (rollback\_deploy) that runs only if a person approves it. | trace.jsonl: start |
| 08:27:51 | 50.6 s | The loop stopped: diagnosed (confidence 0.85), after 6 steps and 5 tool calls, in 32.5 s. | trace.jsonl: stop; run.json: loop |
| 08:27:51 | 50.7 s | The money check right before the request: 149 payments that disagree with the provider's charges. | run.json: damage.at\_request |
| 08:27:52 |  | Klaxon asked to act, at the approval gate: request r-742c, redeploy payments-api: PROVIDER\_BREAKER on (unchanged), CHARGE\_AMOUNT\_UNIT dollars -\> cents, REFUND\_CONNECTION returned (unchanged). It was open until 2026-10-24T08:37:52Z. Its undo: going back to CHARGE\_AMOUNT\_UNIT=dollars would deploy a known-bad setting: Klaxon will not; a person can, by hand. | gate.jsonl: requested |
| 08:33:15 |  | Request r-742c was approved with make approve: typed at a terminal. | gate.jsonl: approved |
| 08:33:18 |  | The action ran: deploy 22 of payments-api in the deploy log, by Klaxon, approved request r-742c, with PROVIDER\_BREAKER=on, CHARGE\_AMOUNT\_UNIT=cents, REFUND\_CONNECTION=returned. | gate.jsonl: done |
| 08:33:18 |  | The money check right after the action: 1080 payments that disagree with the provider's charges. | run.json: verification.at\_action |
| 08:33:35 |  | Health, by code: 2 rounds of the rules and the containers, from 2026-10-24T08:33:19Z to 2026-10-24T08:33:35Z: healthy. | run.json: verification.health |
| 08:34:18 |  | The later money check, at least a minute after the action: 1080 payments that disagree with the provider's charges. | run.json: verification.after |
|  | 438.1 s | Verification, by code and not by the model: passed. | run.json: verification |
|  | 438.3 s | The harness's clearing, its safety net, asked for the good settings of payments-api (PROVIDER\_BREAKER=on, CHARGE\_AMOUNT\_UNIT=cents, REFUND\_CONNECTION=returned). The fault's cause was removed by Klaxon: its rollback (Klaxon, approved request r-742c) removed the cause, and the harness's clearing then found the good settings running, and changed nothing. | run.json: cleared, removed\_by, deploys |
| 08:35:49 | 528.3 s | The rules read the stack again after the clearing: the alert MoneyCheckFailed fired; the money check failed: payments\_match\_provider 1080. | run.json: evaluations (after) |

How long each part took:

- from the fault to the alert: 17.4 s
- Klaxon's investigation: 32.5 s
- from the alert to the diagnosis: 33.2 s
- the request's window: open until it expires: 600 s (10 min)
- the request was open until it was approved: 323 s (5 min 23 s)
- from the alert to the action: 360 s (6 min)
- from the fault's deploy to Klaxon's rollback: 375 s (6 min 15 s)
- the verification: 60.6 s
- from the fault to the harness's clearing: 438.3 s

## Impact

At the alert the money check failed 1 of its 5 invariants, payments\_match\_provider, with 56 findings. Ledger and payment-confirmer lag were 0. Right before the request, 149 payments disagreed with the provider's charges. Of those, 145 had a differing amount, and 4 were pending but charged with a differing amount. Right after the action, 1080 payments disagreed. Of those, 1060 had a differing amount, and 20 were pending but charged with a differing amount. The damage grew while the request waited for approval. The later check at 08:34:18 UTC and the reading after the clearing both still showed 1080. The first examples began at pmt\_000230. The synthetic clients sent 3 new payments per second on average. 1791 payments arrived and all 1791 got an answer. 1771 succeeded, 0 were refused, and 20 were left with their outcome unknown and still pending at the client's last status check. Refunds: 167 succeeded and 0 were refused. The clients retried 98 times. The evidence does not say whether the 20 pending payments are the same ones the money check listed as pending but charged.

> **Aravind's mark:** right: The paragraph correctly tracks the evolution of invariant failures and growing disagreement counts before and after the action, alongside comprehensive synthetic client traffic statistics.

The facts. The alert, MoneyCheckFailed: The money check failed 1 of its 5 invariants: payments\_match\_provider (56 findings).

| When | UTC | The money check |
|---|---|---|
| at the alert | 08:27:18 | the money check failed: payments\_match\_provider 56 |
| right before the request | 08:27:51 | 149 payments that disagree with the provider's charges (by problem: amount differs 145; pending but charged, amount differs 4) |
| right after the action | 08:33:18 | 1080 payments that disagree with the provider's charges (by problem: amount differs 1060; pending but charged, amount differs 20) |
| later, at least a minute after the action | 08:34:18 | 1080 payments that disagree with the provider's charges (by problem: amount differs 1060; pending but charged, amount differs 20) |
| after the harness's clearing | 08:35:49 | the money check failed: payments\_match\_provider 1080 |

The synthetic clients' own counts:

- The synthetic clients sent 3 new payments per second, on average.
- 1791 payments arrived, and 1791 got an answer: 1771 succeeded, 0 were refused, 0 got an answer the clients did not expect, and 20 were left with their outcome unknown.
- Of the 20 payments whose outcome was left unknown, 0 were confirmed later, 20 were still pending at the client's last status check.
- Refunds: 167 succeeded, 0 were refused, and 0 were left with their outcome unknown.
- The clients retried 98 times.

## Cause

The recorded cause was a deploy of the payments API with CHARGE\_AMOUNT\_UNIT=dollars. It read each amount, already in cents, as dollars and converted it to cents again. So the provider charged every customer 100 times the amount, while our own records kept the right one. The payments then disagreed with the provider's charges, and that failed the payments\_match\_provider invariant, which raised MoneyCheckFailed. Klaxon's diagnosis, with confidence 0.85, said payments-api had been redeployed with CHARGE\_AMOUNT\_UNIT=dollars and the provider was charging 100 times what we recorded. A rule in klaxon.grading, not a person or a model, judged that it names the cause. It found every required element named, and found that the fix fits the remedy. The rule reads words, not meaning. The diagnosis gave a deploy time that differs from the deploy log's time. It also claimed the ledger still matches the provider, which code did not check.

> **Aravind's mark:** right: The paragraph accurately details how the dollar unit configuration caused the 100x multiplier effect on charges, the grading tool's positive verdict, and the limitations of the model's diagnosis text.

- The recorded cause (the fault was injected on purpose, so this is its cause): a deploy of the payments API with a money-unit bug (CHARGE\_AMOUNT\_UNIT=dollars): it reads each payment's amount, already in cents, as dollars and converts it to cents again, so the provider charges every customer 100 times the amount, while our own records keep the right one
- Klaxon's diagnosis, by its loop, live: claude-opus-5-5 through Claude Code on Aravind's seat. The loop stopped: diagnosed (confidence 0.85), after 6 steps and 5 tool calls, in 32.5 s. In the model's own words: payments-api was redeployed at 08:27:01Z with CHARGE\_AMOUNT\_UNIT=dollars. Since then it sends our amount in cents to fake-provider as if it were dollars, so the provider charges 100x what we recorded. The first affected payment is pmt\_000230: we recorded 1291 cents and the provider charged 129100. Every payment after the deploy is overcharged this way, which produces the payments\_match\_provider findings. The ledger books what the provider reports, so the ledger still matches the provider and stays balanced, but customers are overcharged against our records.
- Its proposed fix, in the model's words: rollback on payments-api: Roll payments-api back to the settings it ran before the 08:27:01Z deploy, presumably CHARGE\_AMOUNT\_UNIT=cents. That stops new 100x charges. Afterwards, refund the overcharged difference for pmt\_000230 onward, or the full charge if refunding and re-charging is simpler, and correct the ledger. Check pmt\_000233, which is pending on our side but charged at the provider, and settle its status.
- The rule's verdict: names the cause (where: the payments API: named; what: our amount in the wrong unit, 100 times over: named; how: the provider charges customers more than our records say: named; the deploy that brought it: named). The fix fits the remedy: yes. Graded by a rule (klaxon.grading, rubric version 3, committed in 75fedf1 before any live run on faults 4 to 6): keyword patterns per element of the fault's recorded cause. Not a person and not a model: it reads words, not meaning.

## Response

Klaxon had tools that only read, plus one write action, rollback\_deploy, which runs only if a person approves it. It asked at the gate, as request r-742c, to redeploy payments-api with CHARGE\_AMOUNT\_UNIT changed from dollars to cents and the other settings unchanged. The request's undo said going back to dollars would deploy a known-bad setting, which Klaxon will not do, though a person can by hand. The request was approved with make approve, typed at a terminal. The gate's log records how a decision was made, not who made it. The rule is that only Aravind approves. The action ran as deploy 22, with PROVIDER\_BREAKER=on, CHARGE\_AMOUNT\_UNIT=cents and REFUND\_CONNECTION=returned. Verification by code passed. Every container was healthy and the rules were quiet at 2 evaluations in a row. Nothing the provider made after the action was booked wrong, so the cause was removed. The money was not right. The facts do not explain why the alert fired again after the clearing, while verification had found the rules quiet.

> **Aravind's mark:** right: The paragraph correctly summarizes the rollback request parameters, terminal approval process, deploy 22 execution, and verification findings showing cause removal despite lingering past monetary damage.

- The gate: done: redeploy payments-api: PROVIDER\_BREAKER on (unchanged), CHARGE\_AMOUNT\_UNIT dollars -\> cents, REFUND\_CONNECTION returned (unchanged)
- The request: r-742c, made at 2026-10-24T08:27:52Z, open until 2026-10-24T08:37:52Z: redeploy payments-api: PROVIDER\_BREAKER on (unchanged), CHARGE\_AMOUNT\_UNIT dollars -\> cents, REFUND\_CONNECTION returned (unchanged). It undoes deploy 21 of payments-api, at 2026-10-24T08:27:03Z, by klaxon.faults. Its undo: going back to CHARGE\_AMOUNT\_UNIT=dollars would deploy a known-bad setting: Klaxon will not; a person can, by hand
- The decision: approved at 2026-10-24T08:33:15Z, by make approve: typed at a terminal. Only Aravind approves Klaxon's actions, and Claude never does (DECISIONS.md, the week's plan). The gate's log records how a decision was made, not who made it.
- What ran: deploy 22 of payments-api at 2026-10-24T08:33:18Z, by Klaxon, approved request r-742c: PROVIDER\_BREAKER=on, CHARGE\_AMOUNT\_UNIT=cents, REFUND\_CONNECTION=returned (it replaced PROVIDER\_BREAKER=on, CHARGE\_AMOUNT\_UNIT=dollars, REFUND\_CONNECTION=returned).
- The fault's cause was removed by Klaxon: its rollback (Klaxon, approved request r-742c) removed the cause, and the harness's clearing then found the good settings running, and changed nothing.

Verification, by code and not by the model: passed.

- Healthy: every container is healthy, and the metric rules and the books rule were quiet at 2 evaluations in a row.
- The cause is removed: nothing the provider made between the action and the last money check was booked wrong.
- The money is not right: 1080 payments that disagree with the provider's charges. A rollback repairs nothing: this damage stays until a person repairs it.

## Repairs still owed

The last money check, at 08:34:18 UTC, still found 1080 payments that disagree with the provider's charges. A rollback repairs nothing. Repairs that move money are not among Klaxon's actions, so a person must make them. The recorded remedy is to refund each customer charged since the deploy what was charged above the amount, at the provider. Our API refunds no more than our recorded amount, so this needs a write with approval. The 20 payments still pending at the client's last status check also need a person's attention. The evidence does not say whether each has settled since. The damaged payments the check lists need the repair the fault records, not a retry or being marked failed.

> **Aravind's mark:** right: The paragraph accurately specifies that a rollback cannot fix past overcharges, establishing that manual human-led refunds are required to reconcile the 1080 mismatched payments.

- The last money check (later, at least a minute after the action) still found damage: 1080 payments that disagree with the provider's charges. A rollback repairs nothing: the damage stays until a person repairs it. Repairs that move money are not among Klaxon's actions: a person makes them (DECISIONS.md).
- Of the outcomes the synthetic clients left unknown, 20 payments and 0 refunds were still pending at the client's last status check. The evidence does not say whether each settled since. The last money check still found damage (above): one it lists needs the repair the fault records, not a retry or marking it failed.
- The last money check's findings, the first 10 of 1080 (run.json keeps them all): payment pmt\_000230 (pay\_000230): amount differs; payment pmt\_000231 (pay\_000231): amount differs; payment pmt\_000232 (pay\_000232): amount differs; payment pmt\_000233 (pay\_000233): pending but charged, amount differs; payment pmt\_000234 (pay\_000234): amount differs; payment pmt\_000235 (pay\_000235): amount differs; payment pmt\_000236 (pay\_000236): amount differs; payment pmt\_000237 (pay\_000237): amount differs; payment pmt\_000238 (pay\_000238): amount differs; payment pmt\_000239 (pay\_000239): amount differs
- The remedy this fault records (klaxon.faults): roll the payments API back to CHARGE\_AMOUNT\_UNIT=cents, then refund each customer charged since the deploy what was charged above the amount, at the provider: our API refunds no more than our recorded amount (a write, so it needs approval)

## How this draft was made

- The facts: by code (klaxon.agent.postmortem), from the run's run.json, the gate's request, decision and log, and the loop's trace. postmortem.json keeps them with the narrative.
- The narrative: passed, after 1 call (call 1: passed).
- Each call's prompt and reply are in postmortem-recording.jsonl, recorded before Klaxon used them; make replay-postmortem replays them offline and checks this page comes out the same.
- The check's limit: it finds a number the facts do not hold, not one put to another use. The facts sit beside each paragraph, so a reader can see what each number counts.
