# Project brief draft: Klaxon

DRAFT written by Claude on 2026-10-09 from the planning session. Items in [brackets] need Aravind's answer or confirmation. Nothing here is submitted. The final version goes to `fall-2026/aravind-s/final-project-film/PROJECT-BRIEF.md` in the course repo and must fit on one page.

## 1. What is it?

Klaxon will be an AI on-call engineer for a payments system: when an alert fires, it investigates, proposes a fix with evidence, waits for a person to approve it, and then proves the fix worked by checking both the system's health and the money.

When it is done, it will be a public GitHub repository with:
- a small payments system (Python services, a PostgreSQL ledger, Kafka, Redis, and a fake Stripe-like provider), running in Docker Compose and Kubernetes;
- a fault injector with incidents whose causes are known in advance;
- the Klaxon agent (Claude, an MCP server built from scratch, an approval gate, a verifier, drafted postmortems);
- an evaluation report with measured accuracy, time and cost;
- a two-minute demo video.

## 2. Why would anyone care?

The person it helps is an on-call engineer on a payments team. The worst incidents are the quiet ones: a webhook delivered twice charges a customer twice while every dashboard stays green.

Alternatives looked at [Aravind: open these two pages yourself before the pitch says "I looked at"]:
- **HolmesGPT** (open source, a CNCF sandbox project) and **K8sGPT** (also CNCF): AI troubleshooting for Kubernetes from logs, metrics and cluster state.
- **The manual process:** an engineer reading Grafana, logs and a runbook.

What Klaxon will do that they do not: after a fix, it checks the money, not only the dashboards (every payment posted exactly once, the ledger balanced). It will also publish how often it is right, on incidents whose causes are known in advance. Phrase the difference as "in their documentation I did not find a money check", not "no tool does this".

One-line version: Klaxon saves a payments on-call engineer from approving a fix that turns the dashboard green while the money is still wrong.

## 3. Who opens it, and what do they do with it?

- **Target role:** new-grad software engineer, backend or infrastructure, at a big tech or fintech company.
- **Reader:** a hiring manager or senior engineer looking at his GitHub.
- **First sixty seconds:** the top of the README shows one incident from alert to diagnosis (with evidence), to an approved fix, to the money check passing. One command (`make demo`) replays a recorded, dated Claude run with no API key and re-runs the checks live on their machine. Then they see the evaluation table.

## 4. What does it prove about you?

Course skills:
- **Prompt contracts:** the diagnosis has a fixed format that is validated before anything runs.
- **Verification:** health checks and money checks after every fix.
- **Agent loops:** bounded, with budgets, stop conditions and an approval gate.
- **Evaluation:** accuracy, time and cost on known-cause incidents, compared across versions.
- **Source grounding:** every claim in a diagnosis cites a metric, a log line or a trace.

Also backend and distributed-systems skills: idempotency, at-least-once delivery, Kafka, PostgreSQL, Redis, Kubernetes, OpenTelemetry.

A reviewer could fairly conclude: he can build an AI agent whose actions are gated and whose claims are checked, and he can measure where it fails. They could not conclude that Klaxon could run a real company's on-call, or that it beats commercial tools. Evidence: the repository, tests, traces, the evaluation report and its failure analysis.

## 5. What is in, what is out, what comes first?

- **In:** one demo payments system, six incident types, read-only investigation, approved fixes with rollback, health and money verification, an evaluation.
- **Out, deliberately:** Klaxon will never act without a human's approval, and never on a system Aravind does not own (no real companies, no real money, no real Stripe account).
- **First:** the money check. A fake provider delivers the same webhook twice, the dashboard stays green, and the money check flags the double charge. You know it ran because the tests pass and the run writes a dated evidence file. [Fill in the real result and date after it runs.]

## 6. What could sink it?

**Risk: Klaxon grades its own homework.** The same person, with the same AI, builds the incidents and the agent, so the incidents could be too easy or accidentally shaped to fit the agent.

What I will do about it:
- write the incident catalog and its answers before the agent;
- keep a held-out set of incidents that Klaxon's prompts never see during development;
- include incidents with misleading symptoms;
- report the failures, not just the successes.

Second risk: scope. There is a lot of infrastructure for one person, so there is a fixed cut order (Terraform, then Go, then the web panel).

## 7. Why this project?

A mix of showing off and learning. [Aravind: confirm or edit both lists, and say in your own words why you picked it.]

- **Skills I already have:** backend Python and Django; webhooks and idempotency; tests; code review. Evidence:
  - From my co-op: a requested code reviewer who caught a data-loss path in an AWS webhook ingress, an HMAC signature-check crash and a 100x money-unit error. I built idempotent webhook subscribers. That code is private, so the evidence is my record, not a link.
  - Public: [which to cite? e.g. the innovateus-registration repo's tests; the Stripe webhook work in EventEase].
- **Skills that are new to me:** Kafka, Kubernetes, OpenTelemetry, building an agent loop and an MCP server from scratch, LangGraph, and evaluation design.

## Exists today (2026-10-09)

- The plan, the research and this brief.
- An empty repository with docs.
- [After Saturday: the money check, its tests and one dated run.]

## Will exist

Everything else: the payments system, the fault injector, the Klaxon agent, the approval gate, the evaluation, Kubernetes, the demo video.
