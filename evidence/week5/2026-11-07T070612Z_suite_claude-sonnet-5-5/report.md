# Klaxon evaluation report

Worked out offline from saved runs by klaxon.evaluation: no model was called and nothing was run. The rule's verdicts are klaxon.grading's, worked out again with the newest rubric for each run's system; the judge's are Claude's (klaxon.judge), recorded responses saved beside the runs it judged; Aravind's were typed at a terminal (make label). The costs are Claude Code's estimates at API prices; the seat is not billed per call; they leave out the judge's calls.

Made 2026-11-07T07:39:42Z from 8 runs in: evidence/week5/2026-11-07T070612Z_suite_claude-sonnet-5-5.

## Suites

- 2026-11-07T070612Z_suite_claude-sonnet-5-5: claude-sonnet-5-5, 8 runs planned, 8 with an outcome.

## claude-sonnet-5-5

| Case | Mode | Runs | Diagnosed | Named the cause: rule | Named the cause: judge | Graders disagree | Fix fits: rule | Action right | Named where none fits | Refused by code | Median s, fault to alert | Median s to diagnose | Median calls | Cost |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| duplicate-delivery | evaluation | 1 ran, 0 not run | 1 of 1 | 1 of 1 | - | 0 | 1 of 1 | 1 of 1 | 0 | 0 | 17.6 | 18.9 | 4 | $0.06 |
| poison-message | evaluation | 1 ran, 0 not run | 1 of 1 | 1 of 1 | - | 0 | 1 of 1 | 1 of 1 | 0 | 0 | 47.9 | 40.1 | 8 | $0.20 |
| provider-outage | evaluation | 1 ran, 0 not run | 1 of 1 | 0 of 1 | - | 0 | 1 of 1 | 1 of 1 | 0 | 0 | 47 | 31.8 | 8 | $0.24 |
| money-unit | evaluation | 1 ran, 0 not run | 1 of 1 | 1 of 1 | - | 0 | 1 of 1 | 1 of 1 | 0 | 0 | 17.2 | 22.8 | 5 | $0.10 |
| secret-rotation | evaluation | 1 ran, 0 not run | 1 of 1 | 1 of 1 | - | 0 | 1 of 1 | 1 of 1 | 0 | 0 | 47.9 | 32.9 | 8 | $0.18 |
| pool-exhaustion | evaluation | 1 ran, 0 not run | 1 of 1 | 1 of 1 | - | 0 | 1 of 1 | 0 of 1 | 0 | 0 | 65 | 35 | 9 | $0.37 |
| false-alarm | evaluation | 1 ran, 0 not run | 1 of 1 | 0 of 1 | - | 0 | 1 of 1 | 1 of 1 | 0 | 0 | - | 23.3 | 7 | $0.11 |
| missing-deploys | evaluation | 1 ran, 0 not run | 1 of 1 | 1 of 1 | - | 0 | 0 of 1 | 0 of 1 | 0 | 0 | 17.4 | 70.1 | 9 | $0.22 |

In all: 8 runs (0 not run), 58 model calls, $1.47.

Named the cause: of the runs each grader graded. The rule (klaxon.grading) grades every diagnosis, by the newest rubric written for the system its run ran on; the judge (klaxon.judge) only those it has judged, and its "no verdict" counts in neither. Graders disagree: runs where both gave a verdict on the cause, and different ones (listed below). Diagnosed: the runs that ended in a diagnosis; a run out of steps, tool calls or time ran, and gave none. A run that did not run counts only in the Runs column and the totals. Medians are over the runs that ran and have the number.

## Each run

| Run | Case | Seed | Mode | Rule | Judge | Aravind | Action named | Gate | s to diagnose | Calls | Cost |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 2026-11-07T070627Z_duplicate-delivery_seed8 | duplicate-delivery | 8 | evaluation | names the cause | - | - | rollback_deploy ledger-consumer | not asked | 18.9 | 4 | $0.06 |
| 2026-11-07T071010Z_poison-message_seed8 | poison-message | 8 | evaluation | names the cause | - | - | rollback_deploy ledger-consumer | not asked | 40.1 | 8 | $0.20 |
| 2026-11-07T071515Z_provider-outage_seed8 | provider-outage | 8 | evaluation | partly | - | - | - | no action | 31.8 | 8 | $0.24 |
| 2026-11-07T071938Z_money-unit_seed8 | money-unit | 8 | evaluation | names the cause | - | - | rollback_deploy payments-api | not asked | 22.8 | 5 | $0.10 |
| 2026-11-07T072326Z_secret-rotation_seed8 | secret-rotation | 8 | evaluation | names the cause | - | - | rollback_deploy webhook-receiver | not asked | 32.9 | 8 | $0.18 |
| 2026-11-07T072837Z_pool-exhaustion_seed8 | pool-exhaustion | 8 | evaluation | names the cause | - | - | - | no action | 35 | 9 | $0.37 |
| 2026-11-07T073327Z_false-alarm_seed8 | false-alarm | 8 | evaluation | partly | - | - | - | no action | 23.3 | 7 | $0.11 |
| 2026-11-07T073523Z_missing-deploys_seed8 | missing-deploys | 8 | evaluation | names the cause | - | - | - | no action | 70.1 | 9 | $0.22 |
