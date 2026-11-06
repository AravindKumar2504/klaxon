# Klaxon evaluation report

Worked out offline from saved runs by klaxon.evaluation: no model was called and nothing was run. The rule's verdicts are klaxon.grading's, worked out again with the newest rubric for each run's system; the judge's are Claude's (klaxon.judge), recorded responses saved beside the runs it judged; Aravind's were typed at a terminal (make label). The costs are Claude Code's estimates at API prices; the seat is not billed per call; they leave out the judge's calls.

Made 2026-11-06T19:25:19Z from 8 runs in: evidence/week5/2026-11-06T185017Z_suite_claude-opus-5-5.

## Suites

- 2026-11-06T185017Z_suite_claude-opus-5-5: claude-opus-5-5, 8 runs planned, 8 with an outcome.

## claude-opus-5-5

| Case | Mode | Runs | Diagnosed | Named the cause: rule | Named the cause: judge | Graders disagree | Fix fits: rule | Action right | Named where none fits | Refused by code | Median s, fault to alert | Median s to diagnose | Median calls | Cost |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| duplicate-delivery | evaluation | 1 ran, 0 not run | 1 of 1 | 1 of 1 | - | 0 | 1 of 1 | 1 of 1 | 0 | 0 | 17.6 | 42.2 | 7 | $0.60 |
| poison-message | evaluation | 1 ran, 0 not run | 1 of 1 | 1 of 1 | - | 0 | 1 of 1 | 1 of 1 | 0 | 0 | 48 | 31.7 | 6 | $0.24 |
| provider-outage | evaluation | 1 ran, 0 not run | 1 of 1 | 1 of 1 | - | 0 | 1 of 1 | 1 of 1 | 0 | 0 | 47 | 39.7 | 9 | $0.52 |
| money-unit | evaluation | 1 ran, 0 not run | 1 of 1 | 1 of 1 | - | 0 | 1 of 1 | 1 of 1 | 0 | 0 | 17.3 | 31.9 | 6 | $0.31 |
| secret-rotation | evaluation | 1 ran, 0 not run | 1 of 1 | 1 of 1 | - | 0 | 1 of 1 | 1 of 1 | 0 | 0 | 48 | 61.6 | 7 | $0.23 |
| pool-exhaustion | evaluation | 1 ran, 0 not run | 1 of 1 | 1 of 1 | - | 0 | 1 of 1 | 1 of 1 | 0 | 0 | 49 | 48.9 | 9 | $0.62 |
| false-alarm | evaluation | 1 ran, 0 not run | 1 of 1 | 1 of 1 | - | 0 | 1 of 1 | 1 of 1 | 0 | 0 | - | 46.5 | 10 | $0.40 |
| missing-deploys | evaluation | 1 ran, 0 not run | 1 of 1 | 1 of 1 | - | 0 | 0 of 1 | 0 of 1 | 0 | 0 | 17.6 | 79.6 | 9 | $0.45 |

In all: 8 runs (0 not run), 63 model calls, $3.37.

Named the cause: of the runs each grader graded. The rule (klaxon.grading) grades every diagnosis, by the newest rubric written for the system its run ran on; the judge (klaxon.judge) only those it has judged, and its "no verdict" counts in neither. Graders disagree: runs where both gave a verdict on the cause, and different ones (listed below). Diagnosed: the runs that ended in a diagnosis; a run out of steps, tool calls or time ran, and gave none. A run that did not run counts only in the Runs column and the totals. Medians are over the runs that ran and have the number.

## Each run

| Run | Case | Seed | Mode | Rule | Judge | Aravind | Action named | Gate | s to diagnose | Calls | Cost |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 2026-11-06T185036Z_duplicate-delivery_seed7 | duplicate-delivery | 7 | evaluation | names the cause | - | - | rollback_deploy ledger-consumer | not asked | 42.2 | 7 | $0.60 |
| 2026-11-06T185441Z_poison-message_seed7 | poison-message | 7 | evaluation | names the cause | - | - | rollback_deploy ledger-consumer | not asked | 31.7 | 6 | $0.24 |
| 2026-11-06T185936Z_provider-outage_seed7 | provider-outage | 7 | evaluation | names the cause | - | - | - | no action | 39.7 | 9 | $0.52 |
| 2026-11-06T190407Z_money-unit_seed7 | money-unit | 7 | evaluation | names the cause | - | - | rollback_deploy payments-api | not asked | 31.9 | 6 | $0.31 |
| 2026-11-06T190804Z_secret-rotation_seed7 | secret-rotation | 7 | evaluation | names the cause | - | - | rollback_deploy webhook-receiver | not asked | 61.6 | 7 | $0.23 |
| 2026-11-06T191344Z_pool-exhaustion_seed7 | pool-exhaustion | 7 | evaluation | names the cause | - | - | rollback_deploy payments-api | not asked | 48.9 | 9 | $0.62 |
| 2026-11-06T191831Z_false-alarm_seed7 | false-alarm | 7 | evaluation | names the cause | - | - | - | no action | 46.5 | 10 | $0.40 |
| 2026-11-06T192051Z_missing-deploys_seed7 | missing-deploys | 7 | evaluation | names the cause | - | - | - | no action | 79.6 | 9 | $0.45 |
