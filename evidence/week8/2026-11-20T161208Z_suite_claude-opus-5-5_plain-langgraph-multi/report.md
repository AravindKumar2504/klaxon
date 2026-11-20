# Klaxon evaluation report

Worked out offline from saved runs by klaxon.evaluation: no model was called and nothing was run. The rule's verdicts are klaxon.grading's, worked out again with the newest rubric for each run's system; the judge's are Claude's (klaxon.judge), recorded responses saved beside the runs it judged; Aravind's were typed at a terminal (make label). The costs are Claude Code's estimates at API prices; the seat is not billed per call; they leave out the judge's calls.

Made 2026-11-20T22:21:49Z from 72 runs in: evidence/week8/2026-11-20T161208Z_suite_claude-opus-5-5_plain-langgraph-multi.

## Suites

- 2026-11-20T161208Z_suite_claude-opus-5-5_plain-langgraph-multi: claude-opus-5-5, 72 runs planned, 72 with an outcome.

## claude-opus-5-5

| Case | Mode | Runs | Diagnosed | Named the cause: rule | Named the cause: judge | Graders disagree | Fix fits: rule | Action right | Named where none fits | Refused by code | Median s, fault to alert | Median s to diagnose | Median calls | Cost |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| duplicate-delivery | evaluation, week 6 | 3 ran, 0 not run | 3 of 3 | 3 of 3 | - | 0 | 3 of 3 | 3 of 3 | 0 | 0 | 17.7 | 26.3 | 5 | $0.87 |
| duplicate-delivery | evaluation, week 6, LangGraph loop | 3 ran, 0 not run | 3 of 3 | 3 of 3 | - | 0 | 3 of 3 | 3 of 3 | 0 | 0 | 17.6 | 38.5 | 6 | $0.72 |
| duplicate-delivery | evaluation, week 6, multi-agent loop | 3 ran, 0 not run | 3 of 3 | 3 of 3 | - | 0 | 3 of 3 | 3 of 3 | 0 | 0 | 17.5 | 80.5 | 11 | $2.39 |
| poison-message | evaluation, week 6 | 3 ran, 0 not run | 3 of 3 | 3 of 3 | - | 0 | 3 of 3 | 3 of 3 | 0 | 0 | 47.9 | 32.2 | 7 | $0.88 |
| poison-message | evaluation, week 6, LangGraph loop | 3 ran, 0 not run | 3 of 3 | 3 of 3 | - | 0 | 3 of 3 | 3 of 3 | 0 | 0 | 48 | 39.7 | 7 | $1.09 |
| poison-message | evaluation, week 6, multi-agent loop | 3 ran, 0 not run | 3 of 3 | 1 of 3 | - | 0 | 3 of 3 | 2 of 3 | 0 | 0 | 47.9 | 89.9 | 12 | $2.52 |
| provider-outage | evaluation, week 6 | 3 ran, 0 not run | 3 of 3 | 3 of 3 | - | 0 | 3 of 3 | 3 of 3 | 0 | 0 | 47 | 42.7 | 9 | $1.68 |
| provider-outage | evaluation, week 6, LangGraph loop | 3 ran, 0 not run | 3 of 3 | 3 of 3 | - | 0 | 3 of 3 | 3 of 3 | 0 | 0 | 47 | 46.2 | 9 | $1.76 |
| provider-outage | evaluation, week 6, multi-agent loop | 3 ran, 0 not run | 3 of 3 | 3 of 3 | - | 0 | 3 of 3 | 3 of 3 | 0 | 0 | 47 | 106.9 | 13 | $2.97 |
| money-unit | evaluation, week 6 | 3 ran, 0 not run | 3 of 3 | 3 of 3 | - | 0 | 3 of 3 | 3 of 3 | 0 | 0 | 17.3 | 21.3 | 4 | $0.66 |
| money-unit | evaluation, week 6, LangGraph loop | 3 ran, 0 not run | 3 of 3 | 3 of 3 | - | 0 | 3 of 3 | 3 of 3 | 0 | 0 | 17.3 | 26.6 | 5 | $0.52 |
| money-unit | evaluation, week 6, multi-agent loop | 3 ran, 0 not run | 3 of 3 | 3 of 3 | - | 0 | 3 of 3 | 3 of 3 | 0 | 0 | 17.3 | 91.1 | 11 | $2.18 |
| secret-rotation | evaluation, week 6 | 3 ran, 0 not run | 3 of 3 | 3 of 3 | - | 0 | 3 of 3 | 3 of 3 | 0 | 0 | 47.9 | 26.7 | 6 | $0.62 |
| secret-rotation | evaluation, week 6, LangGraph loop | 3 ran, 0 not run | 3 of 3 | 3 of 3 | - | 0 | 3 of 3 | 3 of 3 | 0 | 0 | 47.9 | 26 | 6 | $0.69 |
| secret-rotation | evaluation, week 6, multi-agent loop | 3 ran, 0 not run | 3 of 3 | 3 of 3 | - | 0 | 3 of 3 | 3 of 3 | 0 | 0 | 47.9 | 98.8 | 11 | $1.67 |
| pool-exhaustion | evaluation, week 6 | 3 ran, 0 not run | 3 of 3 | 3 of 3 | - | 0 | 3 of 3 | 3 of 3 | 0 | 0 | 65.1 | 40.9 | 8 | $1.74 |
| pool-exhaustion | evaluation, week 6, LangGraph loop | 3 ran, 0 not run | 3 of 3 | 3 of 3 | - | 0 | 3 of 3 | 3 of 3 | 0 | 0 | 65.1 | 44.6 | 8 | $1.63 |
| pool-exhaustion | evaluation, week 6, multi-agent loop | 3 ran, 0 not run | 3 of 3 | 3 of 3 | - | 0 | 3 of 3 | 3 of 3 | 0 | 0 | 65.1 | 84.9 | 12 | $3.20 |
| false-alarm | evaluation, week 6 | 3 ran, 0 not run | 3 of 3 | 1 of 3 | - | 0 | 3 of 3 | 3 of 3 | 0 | 0 | - | 35.9 | 8 | $0.85 |
| false-alarm | evaluation, week 6, LangGraph loop | 3 ran, 0 not run | 2 of 3 | 1 of 2 | - | 0 | 2 of 2 | 2 of 2 | 0 | 0 | - | 40.3 | 9 | $1.04 |
| false-alarm | evaluation, week 6, multi-agent loop | 3 ran, 0 not run | 3 of 3 | 0 of 3 | - | 0 | 3 of 3 | 3 of 3 | 0 | 0 | - | 80.1 | 13 | $1.97 |
| missing-deploys | evaluation, week 6 | 3 ran, 0 not run | 3 of 3 | 2 of 3 | - | 0 | 3 of 3 | 3 of 3 | 0 | 0 | 17.4 | 76.7 | 9 | $1.46 |
| missing-deploys | evaluation, week 6, LangGraph loop | 3 ran, 0 not run | 2 of 3 | 2 of 2 | - | 0 | 0 of 2 | 0 of 2 | 0 | 0 | 17.4 | 87.8 | 10 | $1.62 |
| missing-deploys | evaluation, week 6, multi-agent loop | 3 ran, 0 not run | 3 of 3 | 1 of 3 | - | 0 | 3 of 3 | 3 of 3 | 0 | 0 | 17.6 | 147.9 | 13 | $2.65 |

In all: 72 runs (0 not run), 642 model calls, $37.37.

Named the cause: of the runs each grader graded. The rule (klaxon.grading) grades every diagnosis, by the newest rubric written for the system its run ran on; the judge (klaxon.judge) only those it has judged, and its "no verdict" counts in neither. Graders disagree: runs where both gave a verdict on the cause, and different ones (listed below). Diagnosed: the runs that ended in a diagnosis; a run out of steps, tool calls or time ran, and gave none. A run that did not run counts only in the Runs column and the totals. Medians are over the runs that ran and have the number.

## Each run

| Run | Case | Seed | Mode | Rule | Judge | Aravind | Action named | Gate | s to diagnose | Calls | Cost |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 2026-11-20T161222Z_duplicate-delivery_seed7_plain | duplicate-delivery | 7 | evaluation, week 6 | names the cause | - | - | rollback_deploy ledger-consumer | not asked | 45.3 | 7 | $0.57 |
| 2026-11-20T161631Z_duplicate-delivery_seed7_langgraph | duplicate-delivery | 7 | evaluation, week 6, LangGraph loop | names the cause | - | - | rollback_deploy ledger-consumer | not asked | 38.6 | 7 | $0.32 |
| 2026-11-20T162033Z_duplicate-delivery_seed7_multi | duplicate-delivery | 7 | evaluation, week 6, multi-agent loop | names the cause | - | - | rollback_deploy ledger-consumer | not asked | 106.5 | 13 | $1.14 |
| 2026-11-20T162544Z_poison-message_seed7_plain | poison-message | 7 | evaluation, week 6 | names the cause | - | - | rollback_deploy ledger-consumer | not asked | 36.3 | 7 | $0.35 |
| 2026-11-20T163045Z_poison-message_seed7_langgraph | poison-message | 7 | evaluation, week 6, LangGraph loop | names the cause | - | - | rollback_deploy ledger-consumer | not asked | 39.3 | 7 | $0.36 |
| 2026-11-20T163549Z_poison-message_seed7_multi | poison-message | 7 | evaluation, week 6, multi-agent loop | names the cause | - | - | rollback_deploy ledger-consumer | not asked | 101.5 | 13 | $0.93 |
| 2026-11-20T164154Z_provider-outage_seed7_plain | provider-outage | 7 | evaluation, week 6 | names the cause | - | - | - | no action | 40.1 | 8 | $0.45 |
| 2026-11-20T164626Z_provider-outage_seed7_langgraph | provider-outage | 7 | evaluation, week 6, LangGraph loop | names the cause | - | - | - | no action | 49 | 10 | $0.69 |
| 2026-11-20T165107Z_provider-outage_seed7_multi | provider-outage | 7 | evaluation, week 6, multi-agent loop | names the cause | - | - | - | no action | 103.1 | 13 | $1.03 |
| 2026-11-20T165642Z_money-unit_seed7_plain | money-unit | 7 | evaluation, week 6 | names the cause | - | - | rollback_deploy payments-api | not asked | 18.4 | 3 | $0.09 |
| 2026-11-20T170024Z_money-unit_seed7_langgraph | money-unit | 7 | evaluation, week 6, LangGraph loop | names the cause | - | - | rollback_deploy payments-api | not asked | 26.6 | 5 | $0.19 |
| 2026-11-20T170414Z_money-unit_seed7_multi | money-unit | 7 | evaluation, week 6, multi-agent loop | names the cause | - | - | rollback_deploy payments-api | not asked | 94 | 13 | $0.86 |
| 2026-11-20T170912Z_secret-rotation_seed7_plain | secret-rotation | 7 | evaluation, week 6 | names the cause | - | - | rollback_deploy webhook-receiver | not asked | 26.7 | 6 | $0.19 |
| 2026-11-20T171417Z_secret-rotation_seed7_langgraph | secret-rotation | 7 | evaluation, week 6, LangGraph loop | names the cause | - | - | rollback_deploy webhook-receiver | not asked | 31.7 | 6 | $0.26 |
| 2026-11-20T171927Z_secret-rotation_seed7_multi | secret-rotation | 7 | evaluation, week 6, multi-agent loop | names the cause | - | - | rollback_deploy webhook-receiver | not asked | 60.3 | 10 | $0.53 |
| 2026-11-20T172506Z_pool-exhaustion_seed7_plain | pool-exhaustion | 7 | evaluation, week 6 | names the cause | - | - | rollback_deploy payments-api | not asked | 49.6 | 9 | $0.59 |
| 2026-11-20T172953Z_pool-exhaustion_seed7_langgraph | pool-exhaustion | 7 | evaluation, week 6, LangGraph loop | names the cause | - | - | rollback_deploy payments-api | not asked | 36.4 | 8 | $0.44 |
| 2026-11-20T173428Z_pool-exhaustion_seed7_multi | pool-exhaustion | 7 | evaluation, week 6, multi-agent loop | names the cause | - | - | rollback_deploy payments-api | not asked | 84.9 | 12 | $1.05 |
| 2026-11-20T173952Z_false-alarm_seed7_plain | false-alarm | 7 | evaluation, week 6 | names the cause | - | - | - | no action | 30.9 | 7 | $0.22 |
| 2026-11-20T174557Z_false-alarm_seed7_langgraph | false-alarm | 7 | evaluation, week 6, LangGraph loop | no diagnosis | - | - | - | - | 40 | 10 | $0.38 |
| 2026-11-20T175210Z_false-alarm_seed7_multi | false-alarm | 7 | evaluation, week 6, multi-agent loop | partly | - | - | - | no action | 98.7 | 14 | $0.76 |
| 2026-11-20T175922Z_missing-deploys_seed7_plain | missing-deploys | 7 | evaluation, week 6 | partly | - | - | rollback_deploy ledger-consumer | not asked | 76.7 | 9 | $0.44 |
| 2026-11-20T180403Z_missing-deploys_seed7_langgraph | missing-deploys | 7 | evaluation, week 6, LangGraph loop | no diagnosis | - | - | - | - | 87.8 | 10 | $0.55 |
| 2026-11-20T180854Z_missing-deploys_seed7_multi | missing-deploys | 7 | evaluation, week 6, multi-agent loop | partly | - | - | rollback_deploy ledger-consumer | not asked | 174.9 | 13 | $1.01 |
| 2026-11-20T181513Z_duplicate-delivery_seed8_plain | duplicate-delivery | 8 | evaluation, week 6 | names the cause | - | - | rollback_deploy ledger-consumer | not asked | 23.4 | 5 | $0.14 |
| 2026-11-20T181900Z_duplicate-delivery_seed8_langgraph | duplicate-delivery | 8 | evaluation, week 6, LangGraph loop | names the cause | - | - | rollback_deploy ledger-consumer | not asked | 27.3 | 5 | $0.17 |
| 2026-11-20T182250Z_duplicate-delivery_seed8_multi | duplicate-delivery | 8 | evaluation, week 6, multi-agent loop | names the cause | - | - | rollback_deploy ledger-consumer | not asked | 78.3 | 10 | $0.56 |
| 2026-11-20T182732Z_poison-message_seed8_plain | poison-message | 8 | evaluation, week 6 | names the cause | - | - | rollback_deploy ledger-consumer | not asked | 31.2 | 6 | $0.23 |
| 2026-11-20T183228Z_poison-message_seed8_langgraph | poison-message | 8 | evaluation, week 6, LangGraph loop | names the cause | - | - | rollback_deploy ledger-consumer | not asked | 52 | 8 | $0.39 |
| 2026-11-20T183745Z_poison-message_seed8_multi | poison-message | 8 | evaluation, week 6, multi-agent loop | partly | - | - | rollback_deploy ledger-consumer | not asked | 77.7 | 12 | $0.77 |
| 2026-11-20T184327Z_provider-outage_seed8_plain | provider-outage | 8 | evaluation, week 6 | names the cause | - | - | - | no action | 42.7 | 9 | $0.54 |
| 2026-11-20T184800Z_provider-outage_seed8_langgraph | provider-outage | 8 | evaluation, week 6, LangGraph loop | names the cause | - | - | - | no action | 46.2 | 9 | $0.60 |
| 2026-11-20T185238Z_provider-outage_seed8_multi | provider-outage | 8 | evaluation, week 6, multi-agent loop | names the cause | - | - | - | no action | 106.9 | 13 | $1.06 |
| 2026-11-20T185817Z_money-unit_seed8_plain | money-unit | 8 | evaluation, week 6 | names the cause | - | - | rollback_deploy payments-api | not asked | 21.3 | 4 | $0.13 |
| 2026-11-20T190202Z_money-unit_seed8_langgraph | money-unit | 8 | evaluation, week 6, LangGraph loop | names the cause | - | - | rollback_deploy payments-api | not asked | 23.2 | 4 | $0.13 |
| 2026-11-20T190549Z_money-unit_seed8_multi | money-unit | 8 | evaluation, week 6, multi-agent loop | names the cause | - | - | rollback_deploy payments-api | not asked | 77.9 | 10 | $0.58 |
| 2026-11-20T191031Z_secret-rotation_seed8_plain | secret-rotation | 8 | evaluation, week 6 | names the cause | - | - | rollback_deploy webhook-receiver | not asked | 43.5 | 7 | $0.24 |
| 2026-11-20T191555Z_secret-rotation_seed8_langgraph | secret-rotation | 8 | evaluation, week 6, LangGraph loop | names the cause | - | - | rollback_deploy webhook-receiver | not asked | 21.2 | 5 | $0.17 |
| 2026-11-20T192055Z_secret-rotation_seed8_multi | secret-rotation | 8 | evaluation, week 6, multi-agent loop | names the cause | - | - | rollback_deploy webhook-receiver | not asked | 98.8 | 11 | $0.46 |
| 2026-11-20T192712Z_pool-exhaustion_seed8_plain | pool-exhaustion | 8 | evaluation, week 6 | names the cause | - | - | rollback_deploy payments-api | not asked | 40.9 | 8 | $0.60 |
| 2026-11-20T193207Z_pool-exhaustion_seed8_langgraph | pool-exhaustion | 8 | evaluation, week 6, LangGraph loop | names the cause | - | - | rollback_deploy payments-api | not asked | 44.6 | 8 | $0.60 |
| 2026-11-20T193706Z_pool-exhaustion_seed8_multi | pool-exhaustion | 8 | evaluation, week 6, multi-agent loop | names the cause | - | - | rollback_deploy payments-api | not asked | 85.1 | 12 | $1.02 |
| 2026-11-20T194246Z_false-alarm_seed8_plain | false-alarm | 8 | evaluation, week 6 | partly | - | - | - | no action | 41.8 | 9 | $0.34 |
| 2026-11-20T194902Z_false-alarm_seed8_langgraph | false-alarm | 8 | evaluation, week 6, LangGraph loop | partly | - | - | - | no action | 43.6 | 9 | $0.36 |
| 2026-11-20T195519Z_false-alarm_seed8_multi | false-alarm | 8 | evaluation, week 6, multi-agent loop | partly | - | - | - | no action | 80.1 | 12 | $0.62 |
| 2026-11-20T200212Z_missing-deploys_seed8_plain | missing-deploys | 8 | evaluation, week 6 | names the cause | - | - | rollback_deploy ledger-consumer | not asked | 62.1 | 8 | $0.35 |
| 2026-11-20T200638Z_missing-deploys_seed8_langgraph | missing-deploys | 8 | evaluation, week 6, LangGraph loop | names the cause | - | - | - | no action | 76.3 | 10 | $0.47 |
| 2026-11-20T201118Z_missing-deploys_seed8_multi | missing-deploys | 8 | evaluation, week 6, multi-agent loop | partly | - | - | rollback_deploy ledger-consumer | not asked | 147.9 | 12 | $0.74 |
| 2026-11-20T201710Z_duplicate-delivery_seed9_plain | duplicate-delivery | 9 | evaluation, week 6 | names the cause | - | - | rollback_deploy ledger-consumer | not asked | 26.3 | 5 | $0.16 |
| 2026-11-20T202118Z_duplicate-delivery_seed9_langgraph | duplicate-delivery | 9 | evaluation, week 6, LangGraph loop | names the cause | - | - | rollback_deploy ledger-consumer | not asked | 38.5 | 6 | $0.24 |
| 2026-11-20T202520Z_duplicate-delivery_seed9_multi | duplicate-delivery | 9 | evaluation, week 6, multi-agent loop | names the cause | - | - | rollback_deploy ledger-consumer | not asked | 80.5 | 11 | $0.68 |
| 2026-11-20T203004Z_poison-message_seed9_plain | poison-message | 9 | evaluation, week 6 | names the cause | - | - | rollback_deploy ledger-consumer | not asked | 32.2 | 7 | $0.29 |
| 2026-11-20T203500Z_poison-message_seed9_langgraph | poison-message | 9 | evaluation, week 6, LangGraph loop | names the cause | - | - | rollback_deploy ledger-consumer | not asked | 39.7 | 7 | $0.34 |
| 2026-11-20T204005Z_poison-message_seed9_multi | poison-message | 9 | evaluation, week 6, multi-agent loop | partly | - | - | - | no action | 89.9 | 12 | $0.81 |
| 2026-11-20T204600Z_provider-outage_seed9_plain | provider-outage | 9 | evaluation, week 6 | names the cause | - | - | - | no action | 50.9 | 10 | $0.70 |
| 2026-11-20T205042Z_provider-outage_seed9_langgraph | provider-outage | 9 | evaluation, week 6, LangGraph loop | names the cause | - | - | - | no action | 37.5 | 8 | $0.47 |
| 2026-11-20T205511Z_provider-outage_seed9_multi | provider-outage | 9 | evaluation, week 6, multi-agent loop | names the cause | - | - | - | no action | 107.6 | 12 | $0.88 |
| 2026-11-20T210050Z_money-unit_seed9_plain | money-unit | 9 | evaluation, week 6 | names the cause | - | - | rollback_deploy payments-api | not asked | 43 | 7 | $0.44 |
| 2026-11-20T210458Z_money-unit_seed9_langgraph | money-unit | 9 | evaluation, week 6, LangGraph loop | names the cause | - | - | rollback_deploy payments-api | not asked | 26.6 | 5 | $0.19 |
| 2026-11-20T210848Z_money-unit_seed9_multi | money-unit | 9 | evaluation, week 6, multi-agent loop | names the cause | - | - | rollback_deploy payments-api | not asked | 91.1 | 11 | $0.73 |
| 2026-11-20T211342Z_secret-rotation_seed9_plain | secret-rotation | 9 | evaluation, week 6 | names the cause | - | - | rollback_deploy webhook-receiver | not asked | 23 | 6 | $0.19 |
| 2026-11-20T211845Z_secret-rotation_seed9_langgraph | secret-rotation | 9 | evaluation, week 6, LangGraph loop | names the cause | - | - | rollback_deploy webhook-receiver | not asked | 26 | 6 | $0.27 |
| 2026-11-20T212351Z_secret-rotation_seed9_multi | secret-rotation | 9 | evaluation, week 6, multi-agent loop | names the cause | - | - | rollback_deploy webhook-receiver | not asked | 100 | 13 | $0.67 |
| 2026-11-20T213011Z_pool-exhaustion_seed9_plain | pool-exhaustion | 9 | evaluation, week 6 | names the cause | - | - | rollback_deploy payments-api | not asked | 37.8 | 8 | $0.56 |
| 2026-11-20T213536Z_pool-exhaustion_seed9_langgraph | pool-exhaustion | 9 | evaluation, week 6, LangGraph loop | names the cause | - | - | rollback_deploy payments-api | not asked | 45.3 | 8 | $0.59 |
| 2026-11-20T214108Z_pool-exhaustion_seed9_multi | pool-exhaustion | 9 | evaluation, week 6, multi-agent loop | names the cause | - | - | rollback_deploy payments-api | not asked | 83.2 | 12 | $1.13 |
| 2026-11-20T214717Z_false-alarm_seed9_plain | false-alarm | 9 | evaluation, week 6 | partly | - | - | - | no action | 35.9 | 8 | $0.30 |
| 2026-11-20T215327Z_false-alarm_seed9_langgraph | false-alarm | 9 | evaluation, week 6, LangGraph loop | names the cause | - | - | - | no action | 40.3 | 8 | $0.30 |
| 2026-11-20T215942Z_false-alarm_seed9_multi | false-alarm | 9 | evaluation, week 6, multi-agent loop | partly | - | - | - | no action | 77.9 | 13 | $0.59 |
| 2026-11-20T220635Z_missing-deploys_seed9_plain | missing-deploys | 9 | evaluation, week 6 | names the cause | - | - | rollback_deploy ledger-consumer | not asked | 88.9 | 10 | $0.67 |
| 2026-11-20T221128Z_missing-deploys_seed9_langgraph | missing-deploys | 9 | evaluation, week 6, LangGraph loop | names the cause | - | - | - | no action | 98.7 | 10 | $0.59 |
| 2026-11-20T221632Z_missing-deploys_seed9_multi | missing-deploys | 9 | evaluation, week 6, multi-agent loop | names the cause | - | - | rollback_deploy ledger-consumer | not asked | 126.9 | 13 | $0.89 |
