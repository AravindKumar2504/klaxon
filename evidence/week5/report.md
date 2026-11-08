# Klaxon evaluation report

Worked out offline from saved runs by klaxon.evaluation: no model was called and nothing was run. The rule's verdicts are klaxon.grading's, worked out again with the newest rubric for each run's system; the judge's are Claude's (klaxon.judge), recorded responses saved beside the runs it judged; Aravind's were typed at a terminal (make label). The costs are Claude Code's estimates at API prices; the seat is not billed per call; they leave out the judge's calls.

Made 2026-11-08T23:04:59Z from 48 runs in: evidence/week5.
Left out: 2026-11-04T074308Z_rehearsal_false-alarm: a rehearsal, with no model; 2026-11-04T074551Z_rehearsal_missing-deploys: a rehearsal, with no model; 2026-11-05T025021Z_calibration: it holds no run.json; kind: it holds no run.json.

## Suites

- 2026-11-06T185017Z_suite_claude-opus-5-5: claude-opus-5-5, 8 runs planned, 8 with an outcome.
- 2026-11-07T055744Z_suite_claude-sonnet-5-5: claude-sonnet-5-5, 8 runs planned, 8 with an outcome.
- 2026-11-07T063051Z_suite_claude-opus-5-5: claude-opus-5-5, 8 runs planned, 8 with an outcome.
- 2026-11-07T070612Z_suite_claude-sonnet-5-5: claude-sonnet-5-5, 8 runs planned, 8 with an outcome.
- 2026-11-07T073942Z_suite_claude-opus-5-5: claude-opus-5-5, 8 runs planned, 8 with an outcome.
- 2026-11-07T081533Z_suite_claude-sonnet-5-5: claude-sonnet-5-5, 8 runs planned, 8 with an outcome.

## claude-opus-5-5

| Case | Mode | Runs | Diagnosed | Named the cause: rule | Named the cause: judge | Graders disagree | Fix fits: rule | Action right | Named where none fits | Refused by code | Median s, fault to alert | Median s to diagnose | Median calls | Cost |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| duplicate-delivery | evaluation | 3 ran, 0 not run | 3 of 3 | 3 of 3 | 3 of 3 | 0 | 3 of 3 | 3 of 3 | 0 | 0 | 17.4 | 42.2 | 7 | $1.15 |
| poison-message | evaluation | 3 ran, 0 not run | 3 of 3 | 3 of 3 | 3 of 3 | 0 | 3 of 3 | 3 of 3 | 0 | 0 | 48 | 41.1 | 7 | $1.06 |
| provider-outage | evaluation | 3 ran, 0 not run | 3 of 3 | 3 of 3 | 3 of 3 | 0 | 3 of 3 | 3 of 3 | 0 | 0 | 47 | 39.7 | 9 | $1.81 |
| money-unit | evaluation | 3 ran, 0 not run | 3 of 3 | 3 of 3 | 3 of 3 | 0 | 3 of 3 | 3 of 3 | 0 | 0 | 17.3 | 31.9 | 6 | $0.80 |
| secret-rotation | evaluation | 3 ran, 0 not run | 3 of 3 | 3 of 3 | 3 of 3 | 0 | 3 of 3 | 3 of 3 | 0 | 0 | 47.9 | 26.4 | 6 | $0.61 |
| pool-exhaustion | evaluation | 3 ran, 0 not run | 3 of 3 | 3 of 3 | 3 of 3 | 0 | 3 of 3 | 3 of 3 | 0 | 0 | 65 | 48.9 | 9 | $1.95 |
| false-alarm | evaluation | 3 ran, 0 not run | 3 of 3 | 1 of 3 | 1 of 3 | 2 | 3 of 3 | 3 of 3 | 0 | 0 | - | 46.5 | 10 | $1.15 |
| missing-deploys | evaluation | 3 ran, 0 not run | 3 of 3 | 3 of 3 | 2 of 3 | 1 | 0 of 3 | 0 of 3 | 0 | 0 | 17.6 | 92.5 | 10 | $1.47 |

In all: 24 runs (0 not run), 191 model calls, $10.00.

## claude-sonnet-5-5

| Case | Mode | Runs | Diagnosed | Named the cause: rule | Named the cause: judge | Graders disagree | Fix fits: rule | Action right | Named where none fits | Refused by code | Median s, fault to alert | Median s to diagnose | Median calls | Cost |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| duplicate-delivery | evaluation | 3 ran, 0 not run | 2 of 3 | 2 of 2 | 2 of 2 | 0 | 2 of 2 | 2 of 2 | 0 | 0 | 17.6 | 24 | 5 | $0.31 |
| poison-message | evaluation | 3 ran, 0 not run | 3 of 3 | 2 of 3 | 3 of 3 | 1 | 3 of 3 | 3 of 3 | 0 | 0 | 47.9 | 40.1 | 8 | $0.49 |
| provider-outage | evaluation | 3 ran, 0 not run | 3 of 3 | 2 of 3 | 1 of 3 | 3 | 3 of 3 | 3 of 3 | 0 | 0 | 47 | 31.8 | 8 | $0.77 |
| money-unit | evaluation | 3 ran, 0 not run | 3 of 3 | 3 of 3 | 2 of 3 | 1 | 3 of 3 | 3 of 3 | 0 | 0 | 17.3 | 22.8 | 4 | $0.22 |
| secret-rotation | evaluation | 3 ran, 0 not run | 3 of 3 | 3 of 3 | 3 of 3 | 0 | 3 of 3 | 3 of 3 | 0 | 0 | 47.9 | 19.3 | 6 | $0.36 |
| pool-exhaustion | evaluation | 3 ran, 0 not run | 3 of 3 | 3 of 3 | 2 of 3 | 1 | 3 of 3 | 2 of 3 | 0 | 0 | 65 | 40.9 | 9 | $1.05 |
| false-alarm | evaluation | 3 ran, 0 not run | 3 of 3 | 0 of 3 | 0 of 3 | 0 | 3 of 3 | 3 of 3 | 0 | 0 | - | 21.9 | 6 | $0.28 |
| missing-deploys | evaluation | 3 ran, 0 not run | 3 of 3 | 2 of 3 | 1 of 3 | 1 | 1 of 3 | 1 of 3 | 0 | 0 | 17.4 | 70.1 | 9 | $0.67 |

In all: 24 runs (0 not run), 166 model calls, $4.14.

Named the cause: of the runs each grader graded. The rule (klaxon.grading) grades every diagnosis, by the newest rubric written for the system its run ran on; the judge (klaxon.judge) only those it has judged, and its "no verdict" counts in neither. Graders disagree: runs where both gave a verdict on the cause, and different ones (listed below). Diagnosed: the runs that ended in a diagnosis; a run out of steps, tool calls or time ran, and gave none. A run that did not run counts only in the Runs column and the totals. Medians are over the runs that ran and have the number.

## Each run

| Run | Case | Seed | Mode | Rule | Judge | Aravind | Action named | Gate | s to diagnose | Calls | Cost |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 2026-11-06T185036Z_duplicate-delivery_seed7 | duplicate-delivery | 7 | evaluation | names the cause | names the cause | - | rollback_deploy ledger-consumer | not asked | 42.2 | 7 | $0.60 |
| 2026-11-06T185441Z_poison-message_seed7 | poison-message | 7 | evaluation | names the cause | names the cause | - | rollback_deploy ledger-consumer | not asked | 31.7 | 6 | $0.24 |
| 2026-11-06T185936Z_provider-outage_seed7 | provider-outage | 7 | evaluation | names the cause | names the cause | - | - | no action | 39.7 | 9 | $0.52 |
| 2026-11-06T190407Z_money-unit_seed7 | money-unit | 7 | evaluation | names the cause | names the cause | - | rollback_deploy payments-api | not asked | 31.9 | 6 | $0.31 |
| 2026-11-06T190804Z_secret-rotation_seed7 | secret-rotation | 7 | evaluation | names the cause | names the cause | - | rollback_deploy webhook-receiver | not asked | 61.6 | 7 | $0.23 |
| 2026-11-06T191344Z_pool-exhaustion_seed7 | pool-exhaustion | 7 | evaluation | names the cause | names the cause | - | rollback_deploy payments-api | not asked | 48.9 | 9 | $0.62 |
| 2026-11-06T191831Z_false-alarm_seed7 | false-alarm | 7 | evaluation | names the cause | partly | names the cause | - | no action | 46.5 | 10 | $0.40 |
| 2026-11-06T192051Z_missing-deploys_seed7 | missing-deploys | 7 | evaluation | names the cause | names the cause | names the cause | - | no action | 79.6 | 9 | $0.45 |
| 2026-11-07T055759Z_duplicate-delivery_seed7 | duplicate-delivery | 7 | evaluation | no diagnosis | - | - | - | - | 36.4 | 6 | $0.17 |
| 2026-11-07T060158Z_poison-message_seed7 | poison-message | 7 | evaluation | partly | names the cause | names the cause | rollback_deploy ledger-consumer | not asked | 21.7 | 5 | $0.08 |
| 2026-11-07T060644Z_provider-outage_seed7 | provider-outage | 7 | evaluation | names the cause | partly | names the cause | - | no action | 33.1 | 9 | $0.28 |
| 2026-11-07T061109Z_money-unit_seed7 | money-unit | 7 | evaluation | names the cause | names the cause | names the cause | rollback_deploy payments-api | not asked | 16.8 | 3 | $0.05 |
| 2026-11-07T061449Z_secret-rotation_seed7 | secret-rotation | 7 | evaluation | names the cause | names the cause | names the cause | rollback_deploy webhook-receiver | not asked | 19.3 | 6 | $0.09 |
| 2026-11-07T061951Z_pool-exhaustion_seed7 | pool-exhaustion | 7 | evaluation | names the cause | names the cause | - | rollback_deploy payments-api | not asked | 40.9 | 9 | $0.25 |
| 2026-11-07T062430Z_false-alarm_seed7 | false-alarm | 7 | evaluation | partly | partly | - | - | no action | 17.8 | 5 | $0.08 |
| 2026-11-07T062622Z_missing-deploys_seed7 | missing-deploys | 7 | evaluation | names the cause | names the cause | - | rollback_deploy ledger-consumer | not asked | 79.8 | 10 | $0.28 |
| 2026-11-07T063106Z_duplicate-delivery_seed8 | duplicate-delivery | 8 | evaluation | names the cause | names the cause | - | rollback_deploy ledger-consumer | not asked | 42.2 | 7 | $0.31 |
| 2026-11-07T063513Z_poison-message_seed8 | poison-message | 8 | evaluation | names the cause | names the cause | names the cause | rollback_deploy ledger-consumer | not asked | 58.3 | 10 | $0.53 |
| 2026-11-07T064035Z_provider-outage_seed8 | provider-outage | 8 | evaluation | names the cause | names the cause | - | - | no action | 39.6 | 9 | $0.58 |
| 2026-11-07T064505Z_money-unit_seed8 | money-unit | 8 | evaluation | names the cause | names the cause | - | rollback_deploy payments-api | not asked | 24.8 | 5 | $0.17 |
| 2026-11-07T064906Z_secret-rotation_seed8 | secret-rotation | 8 | evaluation | names the cause | names the cause | - | rollback_deploy webhook-receiver | not asked | 23.5 | 5 | $0.19 |
| 2026-11-07T065408Z_pool-exhaustion_seed8 | pool-exhaustion | 8 | evaluation | names the cause | names the cause | - | rollback_deploy payments-api | not asked | 52.2 | 9 | $0.75 |
| 2026-11-07T065914Z_false-alarm_seed8 | false-alarm | 8 | evaluation | partly | partly | - | - | no action | 41.2 | 9 | $0.33 |
| 2026-11-07T070128Z_missing-deploys_seed8 | missing-deploys | 8 | evaluation | names the cause | partly | partly | - | no action | 95.6 | 10 | $0.52 |
| 2026-11-07T070627Z_duplicate-delivery_seed8 | duplicate-delivery | 8 | evaluation | names the cause | names the cause | - | rollback_deploy ledger-consumer | not asked | 18.9 | 4 | $0.06 |
| 2026-11-07T071010Z_poison-message_seed8 | poison-message | 8 | evaluation | names the cause | names the cause | - | rollback_deploy ledger-consumer | not asked | 40.1 | 8 | $0.20 |
| 2026-11-07T071515Z_provider-outage_seed8 | provider-outage | 8 | evaluation | partly | names the cause | names the cause | - | no action | 31.8 | 8 | $0.24 |
| 2026-11-07T071938Z_money-unit_seed8 | money-unit | 8 | evaluation | names the cause | names the cause | - | rollback_deploy payments-api | not asked | 22.8 | 5 | $0.10 |
| 2026-11-07T072326Z_secret-rotation_seed8 | secret-rotation | 8 | evaluation | names the cause | names the cause | - | rollback_deploy webhook-receiver | not asked | 32.9 | 8 | $0.18 |
| 2026-11-07T072837Z_pool-exhaustion_seed8 | pool-exhaustion | 8 | evaluation | names the cause | partly | names the cause | - | no action | 35 | 9 | $0.37 |
| 2026-11-07T073327Z_false-alarm_seed8 | false-alarm | 8 | evaluation | partly | partly | names the cause | - | no action | 23.3 | 7 | $0.11 |
| 2026-11-07T073523Z_missing-deploys_seed8 | missing-deploys | 8 | evaluation | names the cause | partly | partly | - | no action | 70.1 | 9 | $0.22 |
| 2026-11-07T073957Z_duplicate-delivery_seed9 | duplicate-delivery | 9 | evaluation | names the cause | names the cause | names the cause | rollback_deploy ledger-consumer | not asked | 35.6 | 6 | $0.24 |
| 2026-11-07T074356Z_poison-message_seed9 | poison-message | 9 | evaluation | names the cause | names the cause | - | rollback_deploy ledger-consumer | not asked | 41.1 | 7 | $0.30 |
| 2026-11-07T074901Z_provider-outage_seed9 | provider-outage | 9 | evaluation | names the cause | names the cause | names the cause | - | no action | 51.9 | 10 | $0.71 |
| 2026-11-07T075345Z_money-unit_seed9 | money-unit | 9 | evaluation | names the cause | names the cause | - | rollback_deploy payments-api | not asked | 39.1 | 7 | $0.33 |
| 2026-11-07T075747Z_secret-rotation_seed9 | secret-rotation | 9 | evaluation | names the cause | names the cause | - | rollback_deploy webhook-receiver | not asked | 26.4 | 6 | $0.19 |
| 2026-11-07T080253Z_pool-exhaustion_seed9 | pool-exhaustion | 9 | evaluation | names the cause | names the cause | - | rollback_deploy payments-api | not asked | 45.8 | 8 | $0.59 |
| 2026-11-07T080826Z_false-alarm_seed9 | false-alarm | 9 | evaluation | partly | names the cause | names the cause | - | no action | 49.7 | 10 | $0.42 |
| 2026-11-07T081050Z_missing-deploys_seed9 | missing-deploys | 9 | evaluation | names the cause | names the cause | - | - | no action | 92.5 | 10 | $0.49 |
| 2026-11-07T081548Z_duplicate-delivery_seed9 | duplicate-delivery | 9 | evaluation | names the cause | names the cause | - | rollback_deploy ledger-consumer | not asked | 24 | 5 | $0.08 |
| 2026-11-07T081937Z_poison-message_seed9 | poison-message | 9 | evaluation | names the cause | names the cause | - | rollback_deploy ledger-consumer | not asked | 40.2 | 8 | $0.21 |
| 2026-11-07T082442Z_provider-outage_seed9 | provider-outage | 9 | evaluation | names the cause | partly | names the cause | - | no action | 31.2 | 8 | $0.24 |
| 2026-11-07T082905Z_money-unit_seed9 | money-unit | 9 | evaluation | names the cause | partly | names the cause | rollback_deploy payments-api | not asked | 25.5 | 4 | $0.08 |
| 2026-11-07T083253Z_secret-rotation_seed9 | secret-rotation | 9 | evaluation | names the cause | names the cause | - | rollback_deploy webhook-receiver | not asked | 18.5 | 5 | $0.09 |
| 2026-11-07T083750Z_pool-exhaustion_seed9 | pool-exhaustion | 9 | evaluation | names the cause | names the cause | - | rollback_deploy payments-api | not asked | 44.8 | 10 | $0.42 |
| 2026-11-07T084305Z_false-alarm_seed9 | false-alarm | 9 | evaluation | partly | partly | - | - | no action | 21.9 | 6 | $0.10 |
| 2026-11-07T084501Z_missing-deploys_seed9 | missing-deploys | 9 | evaluation | partly | partly | - | - | no action | 66.1 | 9 | $0.17 |

## Where the graders disagree

- 2026-11-06T191831Z_false-alarm_seed7 (false-alarm): the rule (version 4): names the cause; the judge: partly (It read ledger lag as 0 across the firing, but it calls the alert false only as an alternative, favours an unproven startup backlog, and does not settle that the alert was false.); Aravind: names the cause.
- 2026-11-07T060158Z_poison-message_seed7 (poison-message): the rule (version 4): partly; the judge: names the cause (It names the 06:03:15 ledger-consumer deploy with BAD_EVENTS=retry stuck retrying one unbookable provider event (bad amount) and booking nothing behind it, and its rollback restores dead-lettering so the backlog drains.); Aravind: names the cause.
- 2026-11-07T060644Z_provider-outage_seed7 (provider-outage): the rule (version 4): names the cause; the judge: partly (It correctly blames the open circuit breaker that fails fast on provider calls, but says provider 500s tripped it and rules out provider slowness, whereas the provider hung and calls timed out.); Aravind: names the cause.
- 2026-11-07T070128Z_missing-deploys_seed8 (missing-deploys): the rule (version 4): names the cause; the judge: partly (It correctly finds ledger-consumer double-posting a redelivered webhook and honestly says the deploy history could not be read, but it holds the dedup works partly and leaves a removed idempotent handler as one of three open options without choosing it.); Aravind: partly.
- 2026-11-07T071515Z_provider-outage_seed8 (provider-outage): the rule (version 4): partly; the judge: names the cause (It places the fault in the provider: calls to it hang until the 3 s timeout, each request retries three times, then the circuit breaker opens and payments-api answers 503 at once, leaving payments pending; it escalates to the provider and plans to reconcile pending payments, as the remedy asks.); Aravind: names the cause.
- 2026-11-07T072837Z_pool-exhaustion_seed8 (pool-exhaustion): the rule (version 4): names the cause; the judge: partly (It correctly finds exhaustion of the payments-api DB pool, with 5 s waits ending in a 500, and suspects REFUND_CONNECTION=kept, but it never identifies the leak as refund reads taking connections and not returning them, and leaves that unconfirmed.); Aravind: names the cause.
- 2026-11-07T073523Z_missing-deploys_seed8 (missing-deploys): the rule (version 4): names the cause; the judge: partly (It rightly finds that the ledger consumer booked the redelivered webhook twice and that the deploy list could not be checked, but it says dedupe still works for other events instead of saying it is gone, and it offers two possible causes without choosing.); Aravind: partly.
- 2026-11-07T080826Z_false-alarm_seed9 (false-alarm): the rule (version 4): partly; the judge: names the cause (It reads ledger lag near 0 from query_metrics, says there is no current fault so the alert did not reflect real lag, and proposes kind none; its alert-rule review suggestions and its hedged guess about a 'group' label are only follow-ups.); Aravind: names the cause.
- 2026-11-07T082442Z_provider-outage_seed9 (provider-outage): the rule (version 4): names the cause; the judge: partly (It correctly blames the provider and describes the breaker opening and failing fast with 503, but it says the provider answers quickly with HTTP 500 when the provider actually hangs and calls time out.); Aravind: names the cause.
- 2026-11-07T082905Z_money-unit_seed9 (money-unit): the rule (version 4): names the cause; the judge: partly (It names the payments-api deploy with CHARGE_AMOUNT_UNIT=dollars causing 100x provider charges, but it misstates how (cents read as dollars and converted again) and wrongly suggests the ledger holds the inflated amounts, when our records keep the right ones.); Aravind: names the cause.
