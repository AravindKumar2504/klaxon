# Postmortem draft: Customers charged 100 times the amount: a money-unit bug

A draft for a person to review, about one gated run of Klaxon on a fault injected on purpose: synthetic data, a simulated provider, no real money. The facts, in the lists and tables, were filled in by code from the run's saved evidence, in its folder 2026-11-15T222013Z\_gated-run\_money-unit. The paragraphs are Claude's draft: claude-opus-5-5, a recorded response received 2026-11-15T22:31:52.577Z (postmortem-recording.jsonl), drafted from those facts alone: its prompt held nothing else. Code checked every number in them against the facts, but for one, once, twice and the ordinals, which it does not read: all 71 are there.

## Summary

On 2026-11-15, a fault was injected on purpose into a payments system running on one machine, with synthetic data, a simulated provider and no real money. The fault was a deploy of payments-api with CHARGE\_AMOUNT\_UNIT=dollars, so the provider charged customers 100 times the amount while our own records kept the right one. The alert MoneyCheckFailed fired 17.5 s after the fault. Klaxon's loop diagnosed the cause in 22.2 s with confidence 0.88, and a grading rule found that the diagnosis named the cause. Klaxon asked at the approval gate to redeploy payments-api with CHARGE\_AMOUNT\_UNIT=cents. The request was approved and the action ran. Code verified the result and it passed: the cause was removed, but the money was not right. The last money check still found 239 payments that disagreed with the provider's charges, and that damage stays until a person repairs it.

## Timeline

At 22:21:29 UTC the alert rules were quiet at 2 evaluations in a row and the money check passed. The fault's injection then began, and the deploy log records it as deploy 17 of payments-api, by klaxon.faults, at 22:21:32 UTC. The injection took 2.5 s. At 22:21:47 UTC, 17.5 s after the fault, the alert MoneyCheckFailed fired. At 22:21:48 UTC Klaxon's loop began to investigate, using tools that only read. It stopped at 22:22:10 UTC as diagnosed, after 4 steps and 3 tool calls. At 22:22:11 UTC Klaxon made request r-b619 at the approval gate, which was open until 22:32:11 UTC. The request was approved at 22:22:48 UTC, after it had been open 37 s. The action ran at 22:22:50 UTC as deploy 18 of payments-api. That was 63 s after the alert, and 78 s after the fault's deploy. Health checks from 22:22:51 UTC to 22:23:07 UTC found the stack healthy. A later money check ran at 22:23:50 UTC. Verification passed at 141 s after the fault. The harness's clearing at 141.3 s found the good settings already running and changed nothing. At 22:25:21 UTC the rules read the stack again, and MoneyCheckFailed fired, because the damage was still there.

The facts. Times are UTC, on 2026-11-15 unless a row says otherwise; since the fault: by the run's own clock, from when the fault's injection began.

| UTC | Since the fault | What happened | From |
|---|---|---|---|
| 22:21:29 |  | The alert rules were quiet at 2 evaluations in a row, and the money check passed: the stack was ready for the fault. | run.json: evaluations (before) |
|  | 0 s | The fault's injection began, on purpose (klaxon.faults): a deploy of payments-api with CHARGE\_AMOUNT\_UNIT=dollars. It took 2.5 s. The deploy log has it as deploy 17, by klaxon.faults, at 2026-11-15T22:21:32Z. | run.json: injected, timings, deploys |
| 22:21:47 | 17.5 s | The alert MoneyCheckFailed fired, raised by klaxon.alerts from what the stack showed: The money check failed 1 of its 5 invariants: payments\_match\_provider (48 findings). | run.json: alert |
| 22:21:48 | 18 s | Klaxon's loop began to investigate the alert: claude-opus-5-5 through Claude Code on Aravind's seat, with read-only tools, offered one write action (rollback\_deploy) that runs only if a person approves it. | trace.jsonl: start |
| 22:22:10 | 40.3 s | The loop stopped: diagnosed (confidence 0.88), after 4 steps and 3 tool calls, in 22.2 s. | trace.jsonl: stop; run.json: loop |
| 22:22:10 | 40.5 s | The money check right before the request: 112 payments that disagree with the provider's charges. | run.json: damage.at\_request |
| 22:22:11 |  | Klaxon asked to act, at the approval gate: request r-b619, redeploy payments-api: PROVIDER\_BREAKER on (unchanged), CHARGE\_AMOUNT\_UNIT dollars -\> cents, REFUND\_CONNECTION returned (unchanged). It was open until 2026-11-15T22:32:11Z. Its undo: going back to CHARGE\_AMOUNT\_UNIT=dollars would deploy a known-bad setting: Klaxon will not; a person can, by hand. | gate.jsonl: requested |
| 22:22:48 |  | Request r-b619 was approved with make approve: typed at a terminal. | gate.jsonl: approved |
| 22:22:50 |  | The action ran: deploy 18 of payments-api in the deploy log, by Klaxon, approved request r-b619, with PROVIDER\_BREAKER=on, CHARGE\_AMOUNT\_UNIT=cents, REFUND\_CONNECTION=returned. | gate.jsonl: done |
| 22:22:50 |  | The money check right after the action: 239 payments that disagree with the provider's charges. | run.json: verification.at\_action |
| 22:23:07 |  | Health, by code: 2 rounds of the rules and the cluster's rollouts and pods, from 2026-11-15T22:22:51Z to 2026-11-15T22:23:07Z: healthy. | run.json: verification.health |
| 22:23:50 |  | The later money check, at least a minute after the action: 239 payments that disagree with the provider's charges. | run.json: verification.after |
|  | 141 s (2 min 21 s) | Verification, by code and not by the model: passed. | run.json: verification |
|  | 141.3 s | The harness's clearing, its safety net, asked for the good settings of payments-api (PROVIDER\_BREAKER=on, CHARGE\_AMOUNT\_UNIT=cents, REFUND\_CONNECTION=returned). The fault's cause was removed by Klaxon: its rollback (Klaxon, approved request r-b619) removed the cause, and the harness's clearing then found the good settings running, and changed nothing. | run.json: cleared, removed\_by, deploys |
| 22:25:21 | 231.3 s | The rules read the stack again after the clearing: the alert MoneyCheckFailed fired; the money check failed: payments\_match\_provider 239. | run.json: evaluations (after) |

How long each part took:

- from the fault to the alert: 17.5 s
- Klaxon's investigation: 22.2 s
- from the alert to the diagnosis: 22.8 s
- the request's window: open until it expires: 600 s (10 min)
- the request was open until it was approved: 37 s
- from the alert to the action: 63 s (1 min 3 s)
- from the fault's deploy to Klaxon's rollback: 78 s (1 min 18 s)
- the verification: 60.3 s
- from the fault to the harness's clearing: 141.3 s

## Impact

At the alert, at 22:21:47 UTC, the money check failed 1 of its 5 invariants, payments\_match\_provider, with 48 findings. Ledger and payment confirmer lag were both 0. Right before the request, at 22:22:10 UTC, 112 payments disagreed with the provider's charges. Of these, 109 had an amount that differed, and 3 were pending but charged with an amount that differed. Right after the action, at 22:22:50 UTC, the count was 239: 235 differed in amount, and 4 were pending but charged with an amount that differed. The later check at 22:23:50 UTC found the same 239, so the count stopped growing after the action. After the clearing, at 22:25:21 UTC, payments\_match\_provider still showed 239. The synthetic clients sent 3 new payments per second on average. 930 payments arrived and all 930 got an answer. 926 succeeded and 0 were refused. 4 were left with their outcome unknown, and all 4 were still pending at the client's last status check. Refunds saw 93 succeed, 0 refused and 0 unknown. The clients retried 47 times, and none were shed. These were synthetic customers, so no real person was charged.

The facts. The alert, MoneyCheckFailed: The money check failed 1 of its 5 invariants: payments\_match\_provider (48 findings).

| When | UTC | The money check |
|---|---|---|
| at the alert | 22:21:47 | the money check failed: payments\_match\_provider 48 |
| right before the request | 22:22:10 | 112 payments that disagree with the provider's charges (by problem: amount differs 109; pending but charged, amount differs 3) |
| right after the action | 22:22:50 | 239 payments that disagree with the provider's charges (by problem: amount differs 235; pending but charged, amount differs 4) |
| later, at least a minute after the action | 22:23:50 | 239 payments that disagree with the provider's charges (by problem: amount differs 235; pending but charged, amount differs 4) |
| after the harness's clearing | 22:25:21 | the money check failed: payments\_match\_provider 239 |

The synthetic clients' own counts:

- The synthetic clients sent 3 new payments per second, on average.
- 930 payments arrived, and 930 got an answer: 926 succeeded, 0 were refused, 0 got an answer the clients did not expect, and 4 were left with their outcome unknown.
- Of the 4 payments whose outcome was left unknown, 0 were confirmed later, 4 were still pending at the client's last status check.
- Refunds: 93 succeeded, 0 were refused, and 0 were left with their outcome unknown.
- The clients retried 47 times.

## Cause

The recorded cause was a deploy of the payments API with CHARGE\_AMOUNT\_UNIT=dollars. That setting read each payment's amount, already in cents, as dollars and converted it to cents again. So the provider charged every customer 100 times the amount, while our own records kept the right one. The money check compares our payments with the provider's charges, so the invariant payments\_match\_provider failed and raised MoneyCheckFailed. Klaxon's diagnosis named the payments API, the wrong unit at 100 times, and the provider charging more than our records said. It also named the deploy that brought it. The grading rule found it named the cause and that its fix fit the remedy. The rule reads keywords, not meaning. The diagnosis cited money\_check, recent\_deploys and search\_logs. In its own words it gave a deploy time that differs from the one in the deploy log. It also said the ledger books the inflated amounts from the provider's webhooks. Code did not check that claim. It also said it had not looked at a trace of the outgoing provider request. The facts held no text that tried to give instructions.

- The recorded cause (the fault was injected on purpose, so this is its cause): a deploy of the payments API with a money-unit bug (CHARGE\_AMOUNT\_UNIT=dollars): it reads each payment's amount, already in cents, as dollars and converts it to cents again, so the provider charges every customer 100 times the amount, while our own records keep the right one
- Klaxon's diagnosis, by its loop, live: claude-opus-5-5 through Claude Code on Aravind's seat. The loop stopped: diagnosed (confidence 0.88), after 4 steps and 3 tool calls, in 22.2 s. In the model's own words: A payments-api deploy at 22:21:30Z (revision 2) set CHARGE\_AMOUNT\_UNIT=dollars. payments-api stores amounts in cents but now sends them to the provider as if they were dollars, so the provider charges 100x the intended amount. For example, pmt\_000237 is for 2067 cents ($20.67), but the provider charged 206700 cents ($2,067.00). The first wrong payment, pmt\_000237, completed at 22:21:33Z, 3 seconds after the deploy. All later payments are overcharged the same way, which fails payments\_match\_provider (51 findings at recheck, up from 48). The ledger still balances and still matches the provider, because it books the provider's webhook amounts. That means the ledger also holds the inflated amounts, while our payment records hold the correct ones. Not checked: I did not look at a trace of the outgoing provider request, and I did not confirm that the payment just before 237 was charged correctly. The money check listing no earlier findings strongly suggests it was.
- Its proposed fix, in the model's words: rollback on payments-api: Roll payments-api back to its previous revision so CHARGE\_AMOUNT\_UNIT is cents again and new charges use the correct amounts. Afterwards, as a separate data repair, refund the 100x overcharge (the difference) for pmt\_000237 onward at the provider, and correct the ledger entries that booked the inflated amounts.
- The rule's verdict: names the cause (where: the payments API: named; what: our amount in the wrong unit, 100 times over: named; how: the provider charges customers more than our records say: named; the deploy that brought it: named). The fix fits the remedy: yes. Graded by a rule (klaxon.grading, rubric version 4, committed before any week 5 live run): keyword patterns per element of the right answer (a fault's recorded cause, or an evaluation case's). Not a person and not a model: it reads words, not meaning.

## Response

Klaxon was offered one write action, rollback\_deploy, which runs only if a person approves it. It asked in request r-b619 to redeploy payments-api with CHARGE\_AMOUNT\_UNIT changed from dollars to cents. PROVIDER\_BREAKER stayed on and REFUND\_CONNECTION stayed returned. The request named its undo: going back to CHARGE\_AMOUNT\_UNIT=dollars would deploy a known bad setting, which Klaxon will not do, though a person can by hand. The request was approved with make approve, typed at a terminal. The rule is that only Aravind approves Klaxon's actions and Claude never does. The gate's log records how the decision was made, not who made it, so the evidence does not show who approved. The action ran as deploy 18. Code then verified it, not the model, and verification took 60.3 s. It found every container healthy and the rules quiet at 2 evaluations in a row. It found the cause removed, since nothing the provider made after the action was booked wrong. It found the money not right, with 239 payments that disagreed with the provider's charges. The harness's clearing found the good settings running and changed nothing.

- The gate: done: redeploy payments-api: PROVIDER\_BREAKER on (unchanged), CHARGE\_AMOUNT\_UNIT dollars -\> cents, REFUND\_CONNECTION returned (unchanged)
- The request: r-b619, made at 2026-11-15T22:22:11Z, open until 2026-11-15T22:32:11Z: redeploy payments-api: PROVIDER\_BREAKER on (unchanged), CHARGE\_AMOUNT\_UNIT dollars -\> cents, REFUND\_CONNECTION returned (unchanged). It undoes deploy 17 of payments-api, at 2026-11-15T22:21:32Z, by klaxon.faults. Its undo: going back to CHARGE\_AMOUNT\_UNIT=dollars would deploy a known-bad setting: Klaxon will not; a person can, by hand
- The decision: approved at 2026-11-15T22:22:48Z, by make approve: typed at a terminal. Only Aravind approves Klaxon's actions, and Claude never does (DECISIONS.md, the week's plan). The gate's log records how a decision was made, not who made it.
- What ran: deploy 18 of payments-api at 2026-11-15T22:22:50Z, by Klaxon, approved request r-b619: PROVIDER\_BREAKER=on, CHARGE\_AMOUNT\_UNIT=cents, REFUND\_CONNECTION=returned (it replaced PROVIDER\_BREAKER=on, CHARGE\_AMOUNT\_UNIT=dollars, REFUND\_CONNECTION=returned).
- The fault's cause was removed by Klaxon: its rollback (Klaxon, approved request r-b619) removed the cause, and the harness's clearing then found the good settings running, and changed nothing.

Verification, by code and not by the model: passed.

- Healthy: every container is healthy, and the metric rules and the books rule were quiet at 2 evaluations in a row.
- The cause is removed: nothing the provider made between the action and the last money check was booked wrong.
- The money is not right: 239 payments that disagree with the provider's charges. A rollback repairs nothing: this damage stays until a person repairs it.

## Repairs still owed

The rollback stopped new overcharges but repaired nothing. The last money check, at 22:23:50 UTC, still found 239 payments that disagreed with the provider's charges, starting with pmt\_000237. A person must repair them, because repairs that move money are not among Klaxon's actions. The recorded remedy is to refund each customer charged since the deploy what was charged above the amount, at the provider. Our API refunds no more than our recorded amount, so this needs a different route, and it is a write that needs approval. The 4 payments still pending at the client's last status check also need checking. The evidence does not say whether each one settled since. Any of them that the money check lists needs the same repair, not a retry or being marked failed. The diagnosis also suggested correcting ledger entries that booked inflated amounts. The evidence does not confirm that the ledger holds them, so a person should check the ledger before changing it.

- The last money check (later, at least a minute after the action) still found damage: 239 payments that disagree with the provider's charges. A rollback repairs nothing: the damage stays until a person repairs it. Repairs that move money are not among Klaxon's actions: a person makes them (DECISIONS.md).
- Of the outcomes the synthetic clients left unknown, 4 payments and 0 refunds were still pending at the client's last status check. The evidence does not say whether each settled since. The last money check still found damage (above): one it lists needs the repair the fault records, not a retry or marking it failed.
- The last money check's findings, the first 10 of 239 (run.json keeps them all): payment pmt\_000237 (pay\_000237): amount differs; payment pmt\_000238 (pay\_000238): amount differs; payment pmt\_000239 (pay\_000239): amount differs; payment pmt\_000240 (pay\_000240): amount differs; payment pmt\_000241 (pay\_000241): amount differs; payment pmt\_000242 (pay\_000242): amount differs; payment pmt\_000243 (pay\_000243): amount differs; payment pmt\_000244 (pay\_000244): amount differs; payment pmt\_000245 (pay\_000245): amount differs; payment pmt\_000246 (pay\_000246): amount differs
- The remedy this fault records (klaxon.faults): roll the payments API back to CHARGE\_AMOUNT\_UNIT=cents, then refund each customer charged since the deploy what was charged above the amount, at the provider: our API refunds no more than our recorded amount (a write, so it needs approval)

## How this draft was made

- The facts: by code (klaxon.agent.postmortem), from the run's run.json, the gate's request, decision and log, and the loop's trace. postmortem.json keeps them with the narrative.
- The narrative: passed, after 1 call (call 1: passed).
- Each call's prompt and reply are in postmortem-recording.jsonl, recorded before Klaxon used them; make replay-postmortem replays them offline and checks this page comes out the same.
- The check's limit: it finds a number the facts do not hold, not one put to another use. The facts sit beside each paragraph, so a reader can see what each number counts.
