# Klaxon evaluation report

Worked out offline from saved runs by klaxon.evaluation: no model was called and nothing was run. The rule's verdicts are klaxon.grading's, worked out again with the newest rubric for each run's system; the judge's are Claude's (klaxon.judge), recorded responses saved beside the runs it judged; Aravind's were typed at a terminal (make label). The costs are Claude Code's estimates at API prices; the seat is not billed per call; they leave out the judge's calls.

Made 2026-11-10T11:18:18Z from 8 runs in: evidence/week6/2026-11-10T103918Z_suite_claude-opus-5-5_langgraph.

## Suites

- 2026-11-10T103918Z_suite_claude-opus-5-5_langgraph: claude-opus-5-5, 8 runs planned, 8 with an outcome.

## claude-opus-5-5

| Case | Mode | Runs | Diagnosed | Named the cause: rule | Named the cause: judge | Graders disagree | Fix fits: rule | Action right | Named where none fits | Refused by code | Median s, fault to alert | Median s to diagnose | Median calls | Cost |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| duplicate-delivery | evaluation, week 6, LangGraph loop | 1 ran, 0 not run | 1 of 1 | 1 of 1 | - | 0 | 1 of 1 | 1 of 1 | 0 | 0 | 17.7 | 53.7 | 9 | $0.72 |
| poison-message | evaluation, week 6, LangGraph loop | 1 ran, 0 not run | 1 of 1 | 1 of 1 | - | 0 | 1 of 1 | 1 of 1 | 0 | 0 | 48.1 | 50 | 9 | $0.41 |
| provider-outage | evaluation, week 6, LangGraph loop | 1 ran, 0 not run | 1 of 1 | 1 of 1 | - | 0 | 1 of 1 | 1 of 1 | 0 | 0 | 47 | 50.5 | 10 | $0.72 |
| money-unit | evaluation, week 6, LangGraph loop | 1 ran, 0 not run | 1 of 1 | 1 of 1 | - | 0 | 1 of 1 | 1 of 1 | 0 | 0 | 17.3 | 22.6 | 4 | $0.13 |
| secret-rotation | evaluation, week 6, LangGraph loop | 1 ran, 0 not run | 1 of 1 | 1 of 1 | - | 0 | 1 of 1 | 1 of 1 | 0 | 0 | 47.8 | 31 | 7 | $0.32 |
| pool-exhaustion | evaluation, week 6, LangGraph loop | 1 ran, 0 not run | 1 of 1 | 1 of 1 | - | 0 | 1 of 1 | 1 of 1 | 0 | 0 | 49 | 39.4 | 8 | $0.45 |
| false-alarm | evaluation, week 6, LangGraph loop | 1 ran, 0 not run | 1 of 1 | 0 of 1 | - | 0 | 1 of 1 | 1 of 1 | 0 | 0 | - | 42.5 | 9 | $0.35 |
| missing-deploys | evaluation, week 6, LangGraph loop | 1 ran, 0 not run | 1 of 1 | 1 of 1 | - | 0 | 1 of 1 | 1 of 1 | 0 | 0 | 17.5 | 83.8 | 9 | $0.41 |

In all: 8 runs (0 not run), 65 model calls, $3.51.

Named the cause: of the runs each grader graded. The rule (klaxon.grading) grades every diagnosis, by the newest rubric written for the system its run ran on; the judge (klaxon.judge) only those it has judged, and its "no verdict" counts in neither. Graders disagree: runs where both gave a verdict on the cause, and different ones (listed below). Diagnosed: the runs that ended in a diagnosis; a run out of steps, tool calls or time ran, and gave none. A run that did not run counts only in the Runs column and the totals. Medians are over the runs that ran and have the number.

## Each run

| Run | Case | Seed | Mode | Rule | Judge | Aravind | Action named | Gate | s to diagnose | Calls | Cost |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 2026-11-10T103931Z_duplicate-delivery_seed7 | duplicate-delivery | 7 | evaluation, week 6, LangGraph loop | names the cause | - | - | rollback_deploy ledger-consumer | not asked | 53.7 | 9 | $0.72 |
| 2026-11-10T104347Z_poison-message_seed7 | poison-message | 7 | evaluation, week 6, LangGraph loop | names the cause | - | - | rollback_deploy ledger-consumer | not asked | 50 | 9 | $0.41 |
| 2026-11-10T104903Z_provider-outage_seed7 | provider-outage | 7 | evaluation, week 6, LangGraph loop | names the cause | - | - | - | no action | 50.5 | 10 | $0.72 |
| 2026-11-10T105346Z_money-unit_seed7 | money-unit | 7 | evaluation, week 6, LangGraph loop | names the cause | - | - | rollback_deploy payments-api | not asked | 22.6 | 4 | $0.13 |
| 2026-11-10T105743Z_secret-rotation_seed7 | secret-rotation | 7 | evaluation, week 6, LangGraph loop | names the cause | - | - | rollback_deploy webhook-receiver | not asked | 31 | 7 | $0.32 |
| 2026-11-10T110253Z_pool-exhaustion_seed7 | pool-exhaustion | 7 | evaluation, week 6, LangGraph loop | names the cause | - | - | rollback_deploy payments-api | not asked | 39.4 | 8 | $0.45 |
| 2026-11-10T110730Z_false-alarm_seed7 | false-alarm | 7 | evaluation, week 6, LangGraph loop | partly | - | - | - | no action | 42.5 | 9 | $0.35 |
| 2026-11-10T111345Z_missing-deploys_seed7 | missing-deploys | 7 | evaluation, week 6, LangGraph loop | names the cause | - | - | rollback_deploy ledger-consumer | not asked | 83.8 | 9 | $0.41 |
