# Postmortem draft: The books stop catching up: our webhook receiver refuses every webhook

> Reviewed by Aravind, paragraph by paragraph, at 2026-11-07T06:37:39Z (his marks given in the chat, entered by Claude): 6 right, 0 wrong. His mark is under each paragraph; the draft is otherwise exactly as received.

A draft for a person to review, about one gated run of Klaxon on a fault injected on purpose: synthetic data, a simulated provider, no real money. The facts, in the lists and tables, were filled in by code from the run's saved evidence, in its folder 2026-11-01T024929Z\_gated-run\_secret-rotation. The paragraphs are Claude's draft: claude-opus-5-5, a recorded response received 2026-11-01T19:33:05.698Z (postmortem-recording.jsonl), drafted from those facts alone: its prompt held nothing else. Code checked every number in them against the facts, but for one, once, twice and the ordinals, which it does not read: all 76 are there.

## Summary

On 2026-11-01 a fault was injected on purpose into a payments system running on one machine with synthetic data and no real money. A deploy of the webhook receiver moved it to a secret version the provider does not sign with. The receiver then refused every webhook, and our books could not catch up with the provider. The alert BooksBehindProvider fired 47.7 s after the fault. Klaxon diagnosed the cause in 19.6 s, with confidence 0.85. It asked at the approval gate to roll the receiver back to WEBHOOK\_SECRET\_VERSION v1. The request r-cc97 was approved, and the rollback ran as deploy 26. Code verified the result, and verification passed: the stack was healthy, the cause was removed, and the money check passed. The harness's clearing then found the good settings already running and changed nothing.

> **Aravind's mark:** right: The paragraph accurately summarizes the injected secret-rotation fault, webhook signature refusals, the lag alert, the successful rollback deploy, and clean verification results.

## Timeline

At 02:50:45 UTC the alert rules were quiet at 2 evaluations in a row, and the money check passed. The fault's injection then began. It was deploy 25 of the webhook receiver, by klaxon.faults, at 02:50:48 UTC, with WEBHOOK\_SECRET\_VERSION=v2. It took 2.2 s. The alert BooksBehindProvider fired at 02:51:34 UTC, 47.7 s after the fault. Klaxon's loop began at 02:51:50 UTC. It stopped at 02:52:09 UTC as diagnosed, after 4 steps and 3 tool calls. At 02:52:25 UTC Klaxon asked at the gate for request r-cc97, which was open until 03:02:25 UTC. The request was approved at 02:59:41 UTC, after it had been open 436 s. The rollback ran at 02:59:44 UTC as deploy 26. Code then checked health in 6 rounds, from 03:00:45 UTC to 03:03:06 UTC, and found it healthy. The later money check at 03:03:07 UTC passed. Verification passed at 742.1 s after the fault. The harness's clearing came at 742.2 s and changed nothing. At 03:04:38 UTC the rules read the stack again. No alert fired, and the money check passed.

> **Aravind's mark:** right: The paragraph faithfully details the chronological sequence of the secret-rotation incident from baseline readiness through alert, diagnosis, approval, action, and final clearing.

The facts. Times are UTC, on 2026-11-01 unless a row says otherwise; since the fault: by the run's own clock, from when the fault's injection began.

| UTC | Since the fault | What happened | From |
|---|---|---|---|
| 02:50:45 |  | The alert rules were quiet at 2 evaluations in a row, and the money check passed: the stack was ready for the fault. | run.json: evaluations (before) |
|  | 0 s | The fault's injection began, on purpose (klaxon.faults): a deploy of webhook-receiver with WEBHOOK\_SECRET\_VERSION=v2. It took 2.2 s. The deploy log has it as deploy 25, by klaxon.faults, at 2026-11-01T02:50:48Z. | run.json: injected, timings, deploys |
| 02:51:34 | 47.7 s | The alert BooksBehindProvider fired, raised by klaxon.alerts from what the stack showed: Our books cannot catch up with the provider: at each of the last 2 evaluations, the money check waited 15 s and could not settle up to its cutoff. At the last one, 156 of the provider's webhooks up to it had not been acknowledged yet. | run.json: alert |
| 02:51:50 | 63.7 s | Klaxon's loop began to investigate the alert: claude-opus-5-5 through Claude Code on Aravind's seat, with read-only tools, offered one write action (rollback\_deploy) that runs only if a person approves it. | trace.jsonl: start |
| 02:52:09 | 83.4 s | The loop stopped: diagnosed (confidence 0.85), after 4 steps and 3 tool calls, in 19.6 s. | trace.jsonl: stop; run.json: loop |
| 02:52:09 | 83.5 s | The money check right before the request: not known: the money check could not settle (not settled after 15.03 s: webhooks up to its cutoff not acknowledged by our receiver yet). | run.json: damage.at\_request |
| 02:52:25 |  | Klaxon asked to act, at the approval gate: request r-cc97, redeploy webhook-receiver: WEBHOOK\_SECRET\_VERSION v2 -\> v1. It was open until 2026-11-01T03:02:25Z. Its undo: going back to WEBHOOK\_SECRET\_VERSION=v2 would deploy a known-bad setting: Klaxon will not; a person can, by hand. | gate.jsonl: requested |
| 02:59:41 |  | Request r-cc97 was approved with make approve: typed at a terminal. | gate.jsonl: approved |
| 02:59:44 |  | The action ran: deploy 26 of webhook-receiver in the deploy log, by Klaxon, approved request r-cc97, with WEBHOOK\_SECRET\_VERSION=v1. | gate.jsonl: done |
| 02:59:44 |  | The money check right after the action: not known: the money check could not settle (not settled after 60.01 s: webhooks up to its cutoff not acknowledged by our receiver yet). | run.json: verification.at\_action |
| 03:03:06 |  | Health, by code: 6 rounds of the rules and the containers, from 2026-11-01T03:00:45Z to 2026-11-01T03:03:06Z: healthy. | run.json: verification.health |
| 03:03:07 |  | The later money check, at least a minute after the action: no damage: the money check passed. | run.json: verification.after |
|  | 742.1 s | Verification, by code and not by the model: passed. | run.json: verification |
|  | 742.2 s | The harness's clearing, its safety net, asked for the good settings of webhook-receiver (WEBHOOK\_SECRET\_VERSION=v1). The fault's cause was removed by Klaxon: its rollback (Klaxon, approved request r-cc97) removed the cause, and the harness's clearing then found the good settings running, and changed nothing. | run.json: cleared, removed\_by, deploys |
| 03:04:38 | 832.2 s | The rules read the stack again after the clearing: no alert fired; the money check passed. | run.json: evaluations (after) |

How long each part took:

- from the fault to the alert: 47.7 s
- Klaxon's investigation: 19.6 s
- from the alert to the diagnosis: 35.7 s
- the request's window: open until it expires: 600 s (10 min)
- the request was open until it was approved: 436 s (7 min 16 s)
- from the alert to the action: 490 s (8 min 10 s)
- from the fault's deploy to Klaxon's rollback: 536 s (8 min 56 s)
- the verification: 203.7 s
- from the fault to the harness's clearing: 742.2 s

## Impact

At the alert, at 02:51:34 UTC, the money check could not settle. 156 of the provider's webhooks up to its cutoff had not been acknowledged yet. Ledger lag and payment confirmer lag were both 0. Right before the request, at 02:52:09 UTC, the money check could not settle after 15.03 s. Right after the action, at 02:59:44 UTC, it still could not settle after 60.01 s. So the state of the money at those points is not known. The later check at 03:03:07 UTC passed with no damage, and so did the check at 03:04:38 UTC. The synthetic clients sent 3 new payments per second, on average. 2721 payments arrived, and all 2721 got an answer. 2696 succeeded, 0 were refused, 0 got an unexpected answer, and 25 were left with their outcome unknown. Of those 25, 2 were confirmed later, and 23 were still pending at the client's last status check. Refunds: 243 succeeded, 0 were refused, and 3 were left unknown, all 3 still pending at the last status check. The clients retried 139 times, and 0 requests were shed. The evidence does not say whether the pending payments and refunds settled since.

> **Aravind's mark:** right: The paragraph accurately notes the unsettled money check states during webhook blockage followed by subsequent passing checks, alongside complete synthetic client payment and refund counts.

The facts. The alert, BooksBehindProvider: Our books cannot catch up with the provider: at each of the last 2 evaluations, the money check waited 15 s and could not settle up to its cutoff. At the last one, 156 of the provider's webhooks up to it had not been acknowledged yet.

| When | UTC | The money check |
|---|---|---|
| at the alert | 02:51:34 | the money check could not settle: 156 of the provider's webhooks up to its cutoff were not acknowledged yet |
| right before the request | 02:52:09 | not known: the money check could not settle (not settled after 15.03 s: webhooks up to its cutoff not acknowledged by our receiver yet) |
| right after the action | 02:59:44 | not known: the money check could not settle (not settled after 60.01 s: webhooks up to its cutoff not acknowledged by our receiver yet) |
| later, at least a minute after the action | 03:03:07 | no damage: the money check passed |
| after the harness's clearing | 03:04:38 | the money check passed |

The synthetic clients' own counts:

- The synthetic clients sent 3 new payments per second, on average.
- 2721 payments arrived, and 2721 got an answer: 2696 succeeded, 0 were refused, 0 got an answer the clients did not expect, and 25 were left with their outcome unknown.
- Of the 25 payments whose outcome was left unknown, 2 were confirmed later, 23 were still pending at the client's last status check.
- Refunds: 243 succeeded, 0 were refused, and 3 were left with their outcome unknown.
- Of the 3 refunds whose outcome was left unknown, 0 were confirmed later, 3 were still pending at the client's last status check.
- The clients retried 139 times.

## Cause

The recorded cause was a deploy of the webhook receiver with a rotated secret, WEBHOOK\_SECRET\_VERSION=v2, while the provider signs with v1. Every webhook failed its signature check and was refused with a 400. No event reached Kafka, and the provider kept sending the webhooks again, with backoff. The books therefore could not settle up to the money check's cutoff, and unacknowledged webhooks piled up. That fired BooksBehindProvider. Klaxon's diagnosis said the receiver had been redeployed with v2 and was rejecting provider webhooks as bad signatures. It said the provider seemed to still sign with the earlier secret. A rule in klaxon.grading, not a person or a model, judged that the diagnosis named the cause. It named every required element and the deploy that brought the fault, and its fix fit the recorded remedy. The rule reads words, not meaning. The diagnosis gave the deploy a time that differs from the deploy log's time. The facts did not explain the difference. The facts held no text that told the reader what to do.

> **Aravind's mark:** right: The paragraph precisely explains how the rotated secret version mismatched the provider's signature, causing 400 errors and backlogs that fired the alert, alongside grading validation.

- The recorded cause (the fault was injected on purpose, so this is its cause): a deploy of the webhook receiver with a rotated secret the provider does not sign with (WEBHOOK\_SECRET\_VERSION=v2, while the provider signs with v1): every webhook fails its signature check and is refused with a 400, so no event reaches Kafka, and the provider keeps sending them again, with backoff
- Klaxon's diagnosis, by its loop, live: claude-opus-5-5 through Claude Code on Aravind's seat. The loop stopped: diagnosed (confidence 0.85), after 4 steps and 3 tool calls, in 19.6 s. In the model's own words: webhook-receiver was redeployed at 02:50:46Z with WEBHOOK\_SECRET\_VERSION=v2. Since then it has rejected every provider webhook as bad\_signature, at about 12/s and rising. The rate rises because the provider keeps resending webhooks that are not acknowledged. The provider seems to still sign with the earlier secret, so the receiver neither acknowledges these webhooks nor puts them on provider-events. Neither consumer books or confirms them. The money check cannot settle to its cutoff, and unacknowledged webhooks pile up (156 at the alert), which fires BooksBehindProvider.
- Its proposed fix, in the model's words: rollback on webhook-receiver: Roll webhook-receiver back to its settings before the 02:50:46Z deploy, which used the earlier webhook secret version, so it accepts the provider's signatures again. The provider keeps resending unacknowledged webhooks, so the backlog should drain without a replay. Afterwards, run money\_check with wait=true to confirm all five invariants hold. Rotate to the v2 secret again only when the provider is known to sign with it, or when the receiver accepts both versions during the switch.
- The rule's verdict: names the cause (where: the webhook receiver: named; what: our side moved it to a secret the provider does not sign with: named; how: it refuses every webhook, so nothing reaches the books: named; the deploy that brought it: named). The fix fits the remedy: yes. Graded by a rule (klaxon.grading, rubric version 3, committed in 75fedf1 before any live run on faults 4 to 6): keyword patterns per element of the fault's recorded cause. Not a person and not a model: it reads words, not meaning.

## Response

Klaxon's loop ran with read only tools and was offered a single write action, rollback\_deploy. It cited recent deploys, a metrics query and a log search. It asked at the gate for request r-cc97: redeploy the webhook receiver from WEBHOOK\_SECRET\_VERSION v2 to v1. The request's undo said that going back to v2 would deploy a known bad setting, so Klaxon would not do it, though a person could by hand. The request was approved with make approve, typed at a terminal. The rule is that only Aravind approves Klaxon's actions. The gate's log records how a decision was made, not who made it, so the evidence leaves the approver open. The action ran 490 s after the alert, as deploy 26 with WEBHOOK\_SECRET\_VERSION=v1. Code, not the model, verified it over 203.7 s. The BooksBehindProvider condition still held in 4 of the 6 health rounds. The rules were quiet at 2 evaluations in a row by the end, and every container was healthy. Nothing the provider made after the action was booked wrong, and the money check passed. Verification passed.

> **Aravind's mark:** right: The paragraph accurately outlines Klaxon's investigation tools, the rollback request to v1, the terminal approval process, and verification metrics confirming stack health and cause removal.

- The gate: done: redeploy webhook-receiver: WEBHOOK\_SECRET\_VERSION v2 -\> v1
- The request: r-cc97, made at 2026-11-01T02:52:25Z, open until 2026-11-01T03:02:25Z: redeploy webhook-receiver: WEBHOOK\_SECRET\_VERSION v2 -\> v1. It undoes deploy 25 of webhook-receiver, at 2026-11-01T02:50:48Z, by klaxon.faults. Its undo: going back to WEBHOOK\_SECRET\_VERSION=v2 would deploy a known-bad setting: Klaxon will not; a person can, by hand
- The decision: approved at 2026-11-01T02:59:41Z, by make approve: typed at a terminal. Only Aravind approves Klaxon's actions, and Claude never does (DECISIONS.md, the week's plan). The gate's log records how a decision was made, not who made it.
- What ran: deploy 26 of webhook-receiver at 2026-11-01T02:59:44Z, by Klaxon, approved request r-cc97: WEBHOOK\_SECRET\_VERSION=v1 (it replaced WEBHOOK\_SECRET\_VERSION=v2).
- The fault's cause was removed by Klaxon: its rollback (Klaxon, approved request r-cc97) removed the cause, and the harness's clearing then found the good settings running, and changed nothing.

Verification, by code and not by the model: passed.

- Healthy: every container is healthy, and the metric rules and the books rule were quiet at 2 evaluations in a row.
- The cause is removed: nothing the provider made between the action and the last money check was booked wrong.
- The money is right: the money check passed.
- Health rounds that were not quiet: a rule's condition holds: BooksBehindProvider (the books) (4 of the 6 rounds).

## Repairs still owed

The last money check passed, so nothing in the books is left to repair. 23 payments and 3 refunds were still pending at the client's last status check. The evidence does not say whether each of them settled since. Up to its cutoff, the provider held no charge or refund that our records did not show as succeeded. So no money moved for any that are still pending. Each payment or refund still pending needs a decision by a person: a retry with its key, or marking it failed. The evidence does not name who must decide. The receiver now runs the earlier secret version. A later rotation to v2 should wait until the provider signs with it, or until the receiver accepts both versions during the switch. That work is owed by a person, and the evidence does not name who.

> **Aravind's mark:** right: The paragraph correctly highlights that the passing money check leaves no ledger damage behind, while noting that pending client statuses and future secret rotation coordination require manual human decisions.

- The last money check passed (later, at least a minute after the action): nothing in the books is left to repair.
- Of the outcomes the synthetic clients left unknown, 23 payments and 3 refunds were still pending at the client's last status check. The evidence does not say whether each settled since. The last money check passed: up to its cutoff, the provider held no charge or refund that our records did not show as succeeded, so any of them still pending had no money moved for it. One still pending needs a decision: a retry with its key, or marking it failed.
- The remedy this fault records (klaxon.faults): roll the webhook receiver back to v1 (or accept both versions until the provider signs with v2): the provider sends every refused webhook again, and the books catch up by themselves

## How this draft was made

- The facts: by code (klaxon.agent.postmortem), from the run's run.json, the gate's request, decision and log, and the loop's trace. postmortem.json keeps them with the narrative.
- The narrative: passed, after 2 calls (call 1: cause: 'three' is a number in words, and the facts do not write it so: write it in digits; call 2: passed).
- Each call's prompt and reply are in postmortem-recording.jsonl, recorded before Klaxon used them; make replay-postmortem replays them offline and checks this page comes out the same.
- The check's limit: it finds a number the facts do not hold, not one put to another use. The facts sit beside each paragraph, so a reader can see what each number counts.
