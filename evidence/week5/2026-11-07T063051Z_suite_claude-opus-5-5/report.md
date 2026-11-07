# Klaxon evaluation report

Worked out offline from saved runs by klaxon.evaluation: no model was called and nothing was run. The rule's verdicts are klaxon.grading's, worked out again with the newest rubric for each run's system; the judge's are Claude's (klaxon.judge), recorded responses saved beside the runs it judged; Aravind's were typed at a terminal (make label). The costs are Claude Code's estimates at API prices; the seat is not billed per call; they leave out the judge's calls.

Made 2026-11-07T07:06:12Z from 8 runs in: evidence/week5/2026-11-07T063051Z_suite_claude-opus-5-5.

## Suites

- 2026-11-07T063051Z_suite_claude-opus-5-5: claude-opus-5-5, 8 runs planned, 8 with an outcome.

## claude-opus-5-5

| Case | Mode | Runs | Diagnosed | Named the cause: rule | Named the cause: judge | Graders disagree | Fix fits: rule | Action right | Named where none fits | Refused by code | Median s, fault to alert | Median s to diagnose | Median calls | Cost |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| duplicate-delivery | evaluation | 1 ran, 0 not run | 1 of 1 | 1 of 1 | - | 0 | 1 of 1 | 1 of 1 | 0 | 0 | 17.3 | 42.2 | 7 | $0.31 |
| poison-message | evaluation | 1 ran, 0 not run | 1 of 1 | 1 of 1 | - | 0 | 1 of 1 | 1 of 1 | 0 | 0 | 48 | 58.3 | 10 | $0.53 |
| provider-outage | evaluation | 1 ran, 0 not run | 1 of 1 | 1 of 1 | - | 0 | 1 of 1 | 1 of 1 | 0 | 0 | 47 | 39.6 | 9 | $0.58 |
| money-unit | evaluation | 1 ran, 0 not run | 1 of 1 | 1 of 1 | - | 0 | 1 of 1 | 1 of 1 | 0 | 0 | 17.3 | 24.8 | 5 | $0.17 |
| secret-rotation | evaluation | 1 ran, 0 not run | 1 of 1 | 1 of 1 | - | 0 | 1 of 1 | 1 of 1 | 0 | 0 | 47.8 | 23.5 | 5 | $0.19 |
| pool-exhaustion | evaluation | 1 ran, 0 not run | 1 of 1 | 1 of 1 | - | 0 | 1 of 1 | 1 of 1 | 0 | 0 | 65 | 52.2 | 9 | $0.75 |
| false-alarm | evaluation | 1 ran, 0 not run | 1 of 1 | 0 of 1 | - | 0 | 1 of 1 | 1 of 1 | 0 | 0 | - | 41.2 | 9 | $0.33 |
| missing-deploys | evaluation | 1 ran, 0 not run | 1 of 1 | 1 of 1 | - | 0 | 0 of 1 | 0 of 1 | 0 | 0 | 17.3 | 95.6 | 10 | $0.52 |

In all: 8 runs (0 not run), 64 model calls, $3.37.

Named the cause: of the runs each grader graded. The rule (klaxon.grading) grades every diagnosis, by the newest rubric written for the system its run ran on; the judge (klaxon.judge) only those it has judged, and its "no verdict" counts in neither. Graders disagree: runs where both gave a verdict on the cause, and different ones (listed below). Diagnosed: the runs that ended in a diagnosis; a run out of steps, tool calls or time ran, and gave none. A run that did not run counts only in the Runs column and the totals. Medians are over the runs that ran and have the number.

## Each run

| Run | Case | Seed | Mode | Rule | Judge | Aravind | Action named | Gate | s to diagnose | Calls | Cost |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 2026-11-07T063106Z_duplicate-delivery_seed8 | duplicate-delivery | 8 | evaluation | names the cause | - | - | rollback_deploy ledger-consumer | not asked | 42.2 | 7 | $0.31 |
| 2026-11-07T063513Z_poison-message_seed8 | poison-message | 8 | evaluation | names the cause | - | - | rollback_deploy ledger-consumer | not asked | 58.3 | 10 | $0.53 |
| 2026-11-07T064035Z_provider-outage_seed8 | provider-outage | 8 | evaluation | names the cause | - | - | - | no action | 39.6 | 9 | $0.58 |
| 2026-11-07T064505Z_money-unit_seed8 | money-unit | 8 | evaluation | names the cause | - | - | rollback_deploy payments-api | not asked | 24.8 | 5 | $0.17 |
| 2026-11-07T064906Z_secret-rotation_seed8 | secret-rotation | 8 | evaluation | names the cause | - | - | rollback_deploy webhook-receiver | not asked | 23.5 | 5 | $0.19 |
| 2026-11-07T065408Z_pool-exhaustion_seed8 | pool-exhaustion | 8 | evaluation | names the cause | - | - | rollback_deploy payments-api | not asked | 52.2 | 9 | $0.75 |
| 2026-11-07T065914Z_false-alarm_seed8 | false-alarm | 8 | evaluation | partly | - | - | - | no action | 41.2 | 9 | $0.33 |
| 2026-11-07T070128Z_missing-deploys_seed8 | missing-deploys | 8 | evaluation | names the cause | - | - | - | no action | 95.6 | 10 | $0.52 |
