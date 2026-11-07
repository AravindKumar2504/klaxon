# Klaxon evaluation report

Worked out offline from saved runs by klaxon.evaluation: no model was called and nothing was run. The rule's verdicts are klaxon.grading's, worked out again with the newest rubric for each run's system; the judge's are Claude's (klaxon.judge), recorded responses saved beside the runs it judged; Aravind's were typed at a terminal (make label). The costs are Claude Code's estimates at API prices; the seat is not billed per call; they leave out the judge's calls.

Made 2026-11-07T06:30:51Z from 8 runs in: evidence/week5/2026-11-07T055744Z_suite_claude-sonnet-5-5.

## Suites

- 2026-11-07T055744Z_suite_claude-sonnet-5-5: claude-sonnet-5-5, 8 runs planned, 8 with an outcome.

## claude-sonnet-5-5

| Case | Mode | Runs | Diagnosed | Named the cause: rule | Named the cause: judge | Graders disagree | Fix fits: rule | Action right | Named where none fits | Refused by code | Median s, fault to alert | Median s to diagnose | Median calls | Cost |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| duplicate-delivery | evaluation | 1 ran, 0 not run | 0 of 1 | - | - | 0 | - | - | 0 | 0 | 17.2 | 36.4 | 6 | $0.17 |
| poison-message | evaluation | 1 ran, 0 not run | 1 of 1 | 0 of 1 | - | 0 | 1 of 1 | 1 of 1 | 0 | 0 | 47.9 | 21.7 | 5 | $0.08 |
| provider-outage | evaluation | 1 ran, 0 not run | 1 of 1 | 1 of 1 | - | 0 | 1 of 1 | 1 of 1 | 0 | 0 | 47 | 33.1 | 9 | $0.28 |
| money-unit | evaluation | 1 ran, 0 not run | 1 of 1 | 1 of 1 | - | 0 | 1 of 1 | 1 of 1 | 0 | 0 | 17.4 | 16.8 | 3 | $0.05 |
| secret-rotation | evaluation | 1 ran, 0 not run | 1 of 1 | 1 of 1 | - | 0 | 1 of 1 | 1 of 1 | 0 | 0 | 47.7 | 19.3 | 6 | $0.09 |
| pool-exhaustion | evaluation | 1 ran, 0 not run | 1 of 1 | 1 of 1 | - | 0 | 1 of 1 | 1 of 1 | 0 | 0 | 49 | 40.9 | 9 | $0.25 |
| false-alarm | evaluation | 1 ran, 0 not run | 1 of 1 | 0 of 1 | - | 0 | 1 of 1 | 1 of 1 | 0 | 0 | - | 17.8 | 5 | $0.08 |
| missing-deploys | evaluation | 1 ran, 0 not run | 1 of 1 | 1 of 1 | - | 0 | 1 of 1 | 1 of 1 | 0 | 0 | 17.3 | 79.8 | 10 | $0.28 |

In all: 8 runs (0 not run), 53 model calls, $1.28.

Named the cause: of the runs each grader graded. The rule (klaxon.grading) grades every diagnosis, by the newest rubric written for the system its run ran on; the judge (klaxon.judge) only those it has judged, and its "no verdict" counts in neither. Graders disagree: runs where both gave a verdict on the cause, and different ones (listed below). Diagnosed: the runs that ended in a diagnosis; a run out of steps, tool calls or time ran, and gave none. A run that did not run counts only in the Runs column and the totals. Medians are over the runs that ran and have the number.

## Each run

| Run | Case | Seed | Mode | Rule | Judge | Aravind | Action named | Gate | s to diagnose | Calls | Cost |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 2026-11-07T055759Z_duplicate-delivery_seed7 | duplicate-delivery | 7 | evaluation | no diagnosis | - | - | - | - | 36.4 | 6 | $0.17 |
| 2026-11-07T060158Z_poison-message_seed7 | poison-message | 7 | evaluation | partly | - | - | rollback_deploy ledger-consumer | not asked | 21.7 | 5 | $0.08 |
| 2026-11-07T060644Z_provider-outage_seed7 | provider-outage | 7 | evaluation | names the cause | - | - | - | no action | 33.1 | 9 | $0.28 |
| 2026-11-07T061109Z_money-unit_seed7 | money-unit | 7 | evaluation | names the cause | - | - | rollback_deploy payments-api | not asked | 16.8 | 3 | $0.05 |
| 2026-11-07T061449Z_secret-rotation_seed7 | secret-rotation | 7 | evaluation | names the cause | - | - | rollback_deploy webhook-receiver | not asked | 19.3 | 6 | $0.09 |
| 2026-11-07T061951Z_pool-exhaustion_seed7 | pool-exhaustion | 7 | evaluation | names the cause | - | - | rollback_deploy payments-api | not asked | 40.9 | 9 | $0.25 |
| 2026-11-07T062430Z_false-alarm_seed7 | false-alarm | 7 | evaluation | partly | - | - | - | no action | 17.8 | 5 | $0.08 |
| 2026-11-07T062622Z_missing-deploys_seed7 | missing-deploys | 7 | evaluation | names the cause | - | - | rollback_deploy ledger-consumer | not asked | 79.8 | 10 | $0.28 |
