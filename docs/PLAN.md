# Klaxon plan

Decided 2026-10-09: full build by mid-December 2026, then keep improving it after the courses end.

## What gets built

1. **The system Klaxon protects (the "patient"):** a small payments system.
   - Services: API, payments, webhook receiver, ledger (PostgreSQL, double-entry).
   - Kafka between the services; Redis for idempotency keys.
   - A fake payment provider that sends webhooks the way Stripe does: retries, duplicates, out of order, signed with HMAC.
   - Instrumented with OpenTelemetry, viewed in Grafana (via `grafana/otel-lgtm`).
   - Runs in Docker Compose first, then Kubernetes (kind).
2. **A fault injector:** a library of realistic incidents, each with a known root cause, so Klaxon can be graded. Planned incident types (each is a bug class Aravind has seen in real code review):
   - duplicate webhook delivery with idempotency broken: a ledger deploy without its dedupe -> payments counted twice in the books (built in week 2; first planned as "Redis down -> double charge", which our design prevents; see DECISIONS, 2026-10-14)
   - poison message stalls the ledger consumer (a deploy that retries it forever) -> the books stop, the money check cannot settle (built in week 2)
   - payment provider outage with no circuit breaker -> cascading timeouts (built in week 2; the breaker is its fix, week 4)
   - bad deploy with a 100x money-unit bug (cents vs dollars)
   - HMAC secret rotation mismatch -> webhooks rejected, payments never confirmed
   - database connection pool exhaustion -> latency spike and errors
3. **Klaxon itself:**
   - A plain-Python agent loop first (CSYE teaches this), then a LangGraph version, then a multi-agent version (triage, investigator, fixer, verifier).
   - Tools exposed through an MCP server built from scratch (INFO teaches this): query metrics, search logs, read traces, list recent deploys, check ledger invariants (read-only database role), search runbooks and past incidents.
   - A diagnosis "prompt contract": a fixed, validated format (hypothesis, evidence with citations to metric/log/trace IDs, confidence, proposed fix, blast radius, rollback plan).
   - Human approval gate before any write action; rollback for every action; verification of health and money invariants after the fix; a drafted postmortem.
   - A trace recorder for every step (what the model saw, proposed, what was allowed, what ran, what came back, why it stopped).
4. **Evaluation harness:** inject each fault many times (seeds), grade diagnosis accuracy, time to diagnosis, cost, correct fix, and blocked unsafe actions. Comparisons: plain Python vs LangGraph vs multi-agent; OpenHands on a comparable task; Sonnet vs Opus as Klaxon's model; with and without runbook retrieval. Optional: HolmesGPT running Claude on the same incidents (needs an Anthropic API key, a few dollars).

## Week by week

| Week | Dates | What gets built | Course tie-in |
|---|---|---|---|
| 0 | Oct 9-12 | Pitch film; first piece: a fake provider delivers the same webhook twice, the dashboard stays green, and a money check catches the double charge | INFO pitch due Mon 10/12 |
| 1 | Oct 13-19 | Install Docker. 4 services, fake provider, Postgres, Redis and Kafka in Docker Compose; tests for the normal flow | CSYE 10/14: agent in a real project |
| 2 | Oct 20-26 | Monitoring (OpenTelemetry, Prometheus/Grafana, traces); a traffic generator; the first 3 faults | CSYE 10/21: reflection |
| 3 | Oct 27-Nov 2 | MCP server with read-only ops tools; Klaxon's first loop diagnosing faults 1-3; trace recorder | CSYE 10/28: planning and OpenHands; INFO MCP week |
| 4 | Nov 3-9 | Approval gate, rollback, verification (health plus money checks); faults 4-6; postmortem draft; the circuit breaker and two stuck-refund fixes | CSYE 11/4: retrieval and memory |
| 5 | Nov 10-16 | Evaluation harness and first results; start Kubernetes | |
| 6 | Nov 17-23 | LangGraph version; Klaxon runs on Kubernetes and can undo a bad rollout | CSYE 11/18: LangGraph |
| 7 | Nov 24-30 | Web control panel; CI pipeline (light week, Thanksgiving 11/26) | |
| 8 | Dec 1-7 | Multi-agent version; the Go service; comparison runs | CSYE 12/2: multi-agent |
| 9 | Dec 8-14 | Safety pass (prompt injection planted in logs, budgets); Terraform; INFO ten-artifact packet | CSYE 12/9 eval, 12/16 safety |
| 10 | Dec 15-~19 | Both finals; public write-up; 2-minute demo video; resume bullets with measured numbers | Finals (check Canvas for exact dates) |

**Cut order if a week slips:** Terraform first, then the Go service, then the web panel. Keep Kubernetes (it is part of what Klaxon does: undoing a bad rollout).

**Time budget:** Claude about 2-2.5 hours a day, 6 days a week. Aravind 1-1.5 hours a day: kick off a milestone, do other work while Claude builds, then 30-45 minutes reviewing (run it, read the files Claude points to, make the decisions Claude brings).

## Course requirements this must satisfy

**INFO 7375 capstone (Assignment 10, 100 pts; public text in the course repo `assignments/assignment-10-supervised-capstone.md`):**
- a real need, named owner, data boundary, action surface, success evidence (10)
- plan, dependencies, permissions, budgets, stop conditions, approval gates, premortem (12)
- bounded Python mechanism with meaningful tests (14)
- execute or simulate only as authorized; preserve evidence; label every simulation, fixture and unverified claim (10)
- ten-artifact packet: brief, data boundary, action-surface map, plan, gates, premortem, evidence, artifact, audit note, transfer reflection (10)
- final human decision and handoff (4)
- Claude as the only model; "a failure, a reviewed recovery, and an independently verified result" (Lesson 15)
- plus FRICTIONAL.md (10), GitHub posting (10), Relative Quartile (20)

**INFO 7375 pitch film (due Mon 2026-10-12):** see `~/Documents/03-School/info7375/submissions/final-project-film/ASSIGNMENT-CANVAS.md` (local copy of the Canvas text; not for publishing).

**CSYE 7374 final project (30% of the grade), "Build, Trace, Audit, Explain"** (syllabus: `~/Downloads/CSYE_7374_Online_Fall_2026_Syllabus-1.pdf`):
- show the agent, representative traces, evaluation results, failures, safety controls, limits, and the evidence for any claim of success; defend it from its artifacts, not a polished demo
- learning outcomes it should demonstrate: a plain-Python loop with goal/state/actions/observations/stopping rules; a loop contract; model-call adapters, tool wrappers, validation, retry limits, budget checks; a structured trace recorder; an agent working in a real project directory with files, code, commands and tests, using approval, rollback and verification; comparison with OpenHands; plain Python vs a framework (LangGraph); single vs multi-agent; evaluation with test cases, traces, graders, error analysis, regression checks and safety reviews
- week 13 topics: failure classes, bounded retries, graceful degradation, permissions, irreversible actions, human review, incident response

## After December

Kubernetes hardening, more incident types, a public write-up, and resume numbers from the measured evaluation.
