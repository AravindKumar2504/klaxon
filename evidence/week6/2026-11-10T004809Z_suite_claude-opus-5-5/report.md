# Klaxon evaluation report

Worked out offline from saved runs by klaxon.evaluation: no model was called and nothing was run. The rule's verdicts are klaxon.grading's, worked out again with the newest rubric for each run's system; the judge's are Claude's (klaxon.judge), recorded responses saved beside the runs it judged; Aravind's were typed at a terminal (make label). The costs are Claude Code's estimates at API prices; the seat is not billed per call; they leave out the judge's calls.

Made 2026-11-10T01:22:05Z from 6 runs in: evidence/week6/2026-11-10T004809Z_suite_claude-opus-5-5.

## Suites

- 2026-11-10T004809Z_suite_claude-opus-5-5: claude-opus-5-5, 6 runs planned, 6 with an outcome.

## claude-opus-5-5

| Case | Mode | Runs | Diagnosed | Named the cause: rule | Named the cause: judge | Graders disagree | Fix fits: rule | Action right | Named where none fits | Refused by code | Median s, fault to alert | Median s to diagnose | Median calls | Cost |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| false-alarm | evaluation, week 6 | 3 ran, 0 not run | 3 of 3 | 1 of 3 | - | 0 | 3 of 3 | 3 of 3 | 0 | 0 | - | 46.1 | 9 | $0.98 |
| missing-deploys | evaluation, week 6 | 3 ran, 0 not run | 2 of 3 | 2 of 2 | - | 0 | 2 of 2 | 2 of 2 | 0 | 0 | 17.3 | 100.2 | 10 | $1.47 |

In all: 6 runs (0 not run), 55 model calls, $2.45.

Named the cause: of the runs each grader graded. The rule (klaxon.grading) grades every diagnosis, by the newest rubric written for the system its run ran on; the judge (klaxon.judge) only those it has judged, and its "no verdict" counts in neither. Graders disagree: runs where both gave a verdict on the cause, and different ones (listed below). Diagnosed: the runs that ended in a diagnosis; a run out of steps, tool calls or time ran, and gave none. A run that did not run counts only in the Runs column and the totals. Medians are over the runs that ran and have the number.

## Each run

| Run | Case | Seed | Mode | Rule | Judge | Aravind | Action named | Gate | s to diagnose | Calls | Cost |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 2026-11-10T004824Z_false-alarm_seed7 | false-alarm | 7 | evaluation, week 6 | partly | - | - | - | no action | 46.1 | 9 | $0.38 |
| 2026-11-10T005443Z_missing-deploys_seed7 | missing-deploys | 7 | evaluation, week 6 | no diagnosis | - | - | - | - | 94.1 | 10 | $0.43 |
| 2026-11-10T005941Z_false-alarm_seed8 | false-alarm | 8 | evaluation, week 6 | partly | - | - | - | no action | 52.4 | 10 | $0.45 |
| 2026-11-10T010606Z_missing-deploys_seed8 | missing-deploys | 8 | evaluation, week 6 | names the cause | - | - | rollback_deploy ledger-consumer | not asked | 105 | 10 | $0.49 |
| 2026-11-10T011115Z_false-alarm_seed9 | false-alarm | 9 | evaluation, week 6 | names the cause | - | - | - | no action | 27.6 | 6 | $0.15 |
| 2026-11-10T011716Z_missing-deploys_seed9 | missing-deploys | 9 | evaluation, week 6 | names the cause | - | - | rollback_deploy ledger-consumer | not asked | 100.2 | 10 | $0.54 |
