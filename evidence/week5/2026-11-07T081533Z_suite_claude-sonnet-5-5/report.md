# Klaxon evaluation report

Worked out offline from saved runs by klaxon.evaluation: no model was called and nothing was run. The rule's verdicts are klaxon.grading's, worked out again with the newest rubric for each run's system; the judge's are Claude's (klaxon.judge), recorded responses saved beside the runs it judged; Aravind's were typed at a terminal (make label). The costs are Claude Code's estimates at API prices; the seat is not billed per call; they leave out the judge's calls.

Made 2026-11-07T08:49:17Z from 8 runs in: evidence/week5/2026-11-07T081533Z_suite_claude-sonnet-5-5.

## Suites

- 2026-11-07T081533Z_suite_claude-sonnet-5-5: claude-sonnet-5-5, 8 runs planned, 8 with an outcome.

## claude-sonnet-5-5

| Case | Mode | Runs | Diagnosed | Named the cause: rule | Named the cause: judge | Graders disagree | Fix fits: rule | Action right | Named where none fits | Refused by code | Median s, fault to alert | Median s to diagnose | Median calls | Cost |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| duplicate-delivery | evaluation | 1 ran, 0 not run | 1 of 1 | 1 of 1 | - | 0 | 1 of 1 | 1 of 1 | 0 | 0 | 17.7 | 24 | 5 | $0.08 |
| poison-message | evaluation | 1 ran, 0 not run | 1 of 1 | 1 of 1 | - | 0 | 1 of 1 | 1 of 1 | 0 | 0 | 47.9 | 40.2 | 8 | $0.21 |
| provider-outage | evaluation | 1 ran, 0 not run | 1 of 1 | 1 of 1 | - | 0 | 1 of 1 | 1 of 1 | 0 | 0 | 47 | 31.2 | 8 | $0.24 |
| money-unit | evaluation | 1 ran, 0 not run | 1 of 1 | 1 of 1 | - | 0 | 1 of 1 | 1 of 1 | 0 | 0 | 17.3 | 25.5 | 4 | $0.08 |
| secret-rotation | evaluation | 1 ran, 0 not run | 1 of 1 | 1 of 1 | - | 0 | 1 of 1 | 1 of 1 | 0 | 0 | 47.9 | 18.5 | 5 | $0.09 |
| pool-exhaustion | evaluation | 1 ran, 0 not run | 1 of 1 | 1 of 1 | - | 0 | 1 of 1 | 1 of 1 | 0 | 0 | 81 | 44.8 | 10 | $0.42 |
| false-alarm | evaluation | 1 ran, 0 not run | 1 of 1 | 0 of 1 | - | 0 | 1 of 1 | 1 of 1 | 0 | 0 | - | 21.9 | 6 | $0.10 |
| missing-deploys | evaluation | 1 ran, 0 not run | 1 of 1 | 0 of 1 | - | 0 | 0 of 1 | 0 of 1 | 0 | 0 | 17.9 | 66.1 | 9 | $0.17 |

In all: 8 runs (0 not run), 55 model calls, $1.39.

Named the cause: of the runs each grader graded. The rule (klaxon.grading) grades every diagnosis, by the newest rubric written for the system its run ran on; the judge (klaxon.judge) only those it has judged, and its "no verdict" counts in neither. Graders disagree: runs where both gave a verdict on the cause, and different ones (listed below). Diagnosed: the runs that ended in a diagnosis; a run out of steps, tool calls or time ran, and gave none. A run that did not run counts only in the Runs column and the totals. Medians are over the runs that ran and have the number.

## Each run

| Run | Case | Seed | Mode | Rule | Judge | Aravind | Action named | Gate | s to diagnose | Calls | Cost |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 2026-11-07T081548Z_duplicate-delivery_seed9 | duplicate-delivery | 9 | evaluation | names the cause | - | - | rollback_deploy ledger-consumer | not asked | 24 | 5 | $0.08 |
| 2026-11-07T081937Z_poison-message_seed9 | poison-message | 9 | evaluation | names the cause | - | - | rollback_deploy ledger-consumer | not asked | 40.2 | 8 | $0.21 |
| 2026-11-07T082442Z_provider-outage_seed9 | provider-outage | 9 | evaluation | names the cause | - | - | - | no action | 31.2 | 8 | $0.24 |
| 2026-11-07T082905Z_money-unit_seed9 | money-unit | 9 | evaluation | names the cause | - | - | rollback_deploy payments-api | not asked | 25.5 | 4 | $0.08 |
| 2026-11-07T083253Z_secret-rotation_seed9 | secret-rotation | 9 | evaluation | names the cause | - | - | rollback_deploy webhook-receiver | not asked | 18.5 | 5 | $0.09 |
| 2026-11-07T083750Z_pool-exhaustion_seed9 | pool-exhaustion | 9 | evaluation | names the cause | - | - | rollback_deploy payments-api | not asked | 44.8 | 10 | $0.42 |
| 2026-11-07T084305Z_false-alarm_seed9 | false-alarm | 9 | evaluation | partly | - | - | - | no action | 21.9 | 6 | $0.10 |
| 2026-11-07T084501Z_missing-deploys_seed9 | missing-deploys | 9 | evaluation | partly | - | - | - | no action | 66.1 | 9 | $0.17 |
