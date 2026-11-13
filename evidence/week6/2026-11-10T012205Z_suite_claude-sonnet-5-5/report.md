# Klaxon evaluation report

Worked out offline from saved runs by klaxon.evaluation: no model was called and nothing was run. The rule's verdicts are klaxon.grading's, worked out again with the newest rubric for each run's system; the judge's are Claude's (klaxon.judge), recorded responses saved beside the runs it judged; Aravind's were typed at a terminal (make label). The costs are Claude Code's estimates at API prices; the seat is not billed per call; they leave out the judge's calls.

Made 2026-11-10T01:53:41Z from 6 runs in: evidence/week6/2026-11-10T012205Z_suite_claude-sonnet-5-5.

## Suites

- 2026-11-10T012205Z_suite_claude-sonnet-5-5: claude-sonnet-5-5, 6 runs planned, 6 with an outcome.

## claude-sonnet-5-5

| Case | Mode | Runs | Diagnosed | Named the cause: rule | Named the cause: judge | Graders disagree | Fix fits: rule | Action right | Named where none fits | Refused by code | Median s, fault to alert | Median s to diagnose | Median calls | Cost |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| false-alarm | evaluation, week 6 | 3 ran, 0 not run | 3 of 3 | 0 of 3 | - | 0 | 3 of 3 | 3 of 3 | 0 | 0 | - | 25.4 | 7 | $0.42 |
| missing-deploys | evaluation, week 6 | 3 ran, 0 not run | 1 of 3 | 1 of 1 | - | 0 | 0 of 1 | 0 of 1 | 0 | 0 | 17.5 | 68 | 10 | $0.79 |

In all: 6 runs (0 not run), 52 model calls, $1.22.

Named the cause: of the runs each grader graded. The rule (klaxon.grading) grades every diagnosis, by the newest rubric written for the system its run ran on; the judge (klaxon.judge) only those it has judged, and its "no verdict" counts in neither. Graders disagree: runs where both gave a verdict on the cause, and different ones (listed below). Diagnosed: the runs that ended in a diagnosis; a run out of steps, tool calls or time ran, and gave none. A run that did not run counts only in the Runs column and the totals. Medians are over the runs that ran and have the number.

## Each run

| Run | Case | Seed | Mode | Rule | Judge | Aravind | Action named | Gate | s to diagnose | Calls | Cost |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 2026-11-10T012220Z_false-alarm_seed7 | false-alarm | 7 | evaluation, week 6 | partly | - | - | - | no action | 25.9 | 8 | $0.16 |
| 2026-11-10T012819Z_missing-deploys_seed7 | missing-deploys | 7 | evaluation, week 6 | no diagnosis | - | - | - | - | 86.3 | 10 | $0.27 |
| 2026-11-10T013309Z_false-alarm_seed8 | false-alarm | 8 | evaluation, week 6 | partly | - | - | - | no action | 25.4 | 7 | $0.13 |
| 2026-11-10T013907Z_missing-deploys_seed8 | missing-deploys | 8 | evaluation, week 6 | names the cause | - | - | - | no action | 57.8 | 10 | $0.21 |
| 2026-11-10T014328Z_false-alarm_seed9 | false-alarm | 9 | evaluation, week 6 | partly | - | - | - | no action | 23.4 | 7 | $0.13 |
| 2026-11-10T014925Z_missing-deploys_seed9 | missing-deploys | 9 | evaluation, week 6 | no diagnosis | - | - | - | - | 68 | 10 | $0.32 |
