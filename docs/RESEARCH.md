# Research behind Klaxon (2026-10-09)

Claude did this research on 2026-10-09 with web searches and five parallel research agents. Aravind has not yet read these sources himself. Before the pitch says "alternatives I actually looked at", he should open at least the HolmesGPT and Claude Code Code Review pages below. Startup funding and accuracy figures are mostly vendor or press claims; treat them as reported, not verified.

## 1. Why the original Klaxon (code-review agent) was dropped

- Claude Code's own Code Review already runs "a fleet of specialized agents" on pull requests, then "a verification step checks candidates against actual code behavior to filter out false positives". It averages $15-25 per review and is a research preview for Team and Enterprise plans. https://code.claude.com/docs/en/code-review.md
- OpenAI's Codex Security executes a finding's verification steps in a sandbox and attaches the output as evidence (a security product, separate from Codex PR review). https://developers.openai.com/codex/security/faq
- Conclusion: AI code review is crowded, including the "proof" angle.

## 2. Ideas considered and why they were not chosen

| Idea | Verdict | Key evidence |
|---|---|---|
| Fault injection ("chaos") for tool-using agents | Taken | mcp-storm, mcpfault, agentic-chaos, agent-chaos on GitHub; papers AgentCheck (arXiv 2607.11098), AgentChaos (arXiv 2608.06790), ReliabilityBench (arXiv 2601.06112) |
| Flaky-test forensics for pytest | Taken | Trunk AI investigations https://docs.trunk.io/changelog/2026-04-06-flaky-tests-ai-investigations-webhooks.md ; Datadog https://docs.datadoghq.com/tests/flaky_management/ ; Uber FlakyGuard https://arxiv.org/abs/2511.14002 |
| Deterministic simulation testing for asyncio | Core harness exists | simloop (July 2026) https://github.com/dhruvl/simloop ; frontrun https://github.com/lucaswiman/frontrun ; Antithesis agent skills https://github.com/antithesishq/antithesis-skills |
| API-docs fact-checker | Partly taken | RBCTest https://arxiv.org/abs/2504.17287 ; SATORI https://arxiv.org/abs/2508.16318 |
| Money-bug property tests | Method taken, money oracles open; few real targets | Agentic PBT https://arxiv.org/abs/2510.09907 ; Hypothesis Claude Code plugin https://hypothesis.works/articles/claude-code-plugin/ ; currency rules disagree: https://docs.stripe.com/currencies , https://developer.paypal.com/reference/currency-codes/ |
| "Faultline": breaking-change prover for PyPI releases | Most open, but too niche for his goals | cargo-semver-checks study (17.2% of top 1,000 crates) https://predr.ag/blog/semver-violations-are-common-better-tooling-is-the-answer/ ; griffe https://mkdocstrings.github.io/griffe/guide/users/checking/ ; Testora https://arxiv.org/abs/2503.18597 |
| Least-privilege permissions for agents | Partly open, research-hard, weak personal story | Progent https://arxiv.org/abs/2504.11703 ; Agent-Sentry https://arxiv.org/abs/2603.22868 ; AWS AgentCore Policy https://aws.amazon.com/blogs/aws/amazon-bedrock-agentcore-adds-quality-evaluations-and-policy-controls-for-deploying-trusted-ai-agents/ ; AgentDojo runs https://github.com/ethz-spylab/agentdojo/tree/main/runs |
| MCP server behavior conformance | Partly open | official conformance suite https://github.com/modelcontextprotocol/conformance ; ToolFuzz https://arxiv.org/abs/2503.04479 |
| "Jepsen for webhooks" | Partly taken; the open part was folded INTO Klaxon | hookfuzz https://github.com/shaurmittal/hookfuzz , webhookwitness, hookfault, stripe-webhooks-out-of-order all skip concurrent delivery and doc-derived provider profiles |
| AI payment-exceptions agent | Good fintech idea, fewer infra skills, weaker CSYE fit | YC W26 End Close https://www.ycombinator.com/companies/end-close |

Provider delivery rules differ (used for the fake provider): Stripe retries up to 3 days https://docs.stripe.com/webhooks ; GitHub does not redeliver automatically https://docs.github.com/en/webhooks/using-webhooks/handling-failed-webhook-deliveries ; Shopify retries 8 times in 4 hours https://shopify.dev/docs/apps/build/webhooks/troubleshooting-webhooks ; SQS standard queues deliver at least once https://docs.aws.amazon.com/AWSSimpleQueueService/latest/SQSDeveloperGuide/standard-queues-at-least-once-delivery.html

## 3. The pattern big tech uses for AI in engineering

The LLM proposes; builds, tests, mutants or fuzzers decide; a person approves.
- Google: LLM wrote 74.45% of code changes across 39 internal migrations https://arxiv.org/abs/2504.09691
- Meta ACH (mutation-guided test generation) https://engineering.fb.com/2025/02/05/security/revolutionizing-software-testing-llm-powered-bug-catchers-meta-ach/
- Airbnb test migration https://airbnb.tech/infrastructure/accelerating-large-scale-test-migration-with-llms/
- Stripe Minions https://stripe.dev/blog/minions-stripes-one-shot-end-to-end-coding-agents
- Spotify Honk https://engineering.atspotify.com/2025/12/feedback-loops-background-coding-agents-part-3/
- Microsoft AIOpsLab (fault-injection testbed for ops agents) https://github.com/microsoft/AIOpsLab

## 4. The category Klaxon replicates: AI on-call engineers ("AI SRE")

- Resolve AI reportedly valued at $1B https://siliconangle.com/2025/12/22/incident-remediation-startup-resolve-ai-reportedly-valued-1b-new-funding-round/
- Traversal raised $48M (Sequoia, Kleiner Perkins) https://traversal.com/blog/launch-announcement
- New Relic SRE agent (Feb 2026) https://newrelic.com/press-release/20260224
- Cleric "self-learning AI SRE" (Dec 2025) https://businesswire.com/news/home/20251209625361/en/Cleric-Launches-the-First-Self-Learning-AI-SRE
- Still unsolved: on ITBench the best agents resolve 11.4% of SRE scenarios https://proceedings.mlr.press/v267/jha25a.html

**Open-source alternatives (the "real alternatives" for the pitch):**
- HolmesGPT, CNCF sandbox, Robusta + Microsoft; supports Anthropic models via LiteLLM https://www.cncf.io/blog/2026/01/07/holmesgpt-agentic-troubleshooting-built-for-the-cloud-native-era/
- K8sGPT, CNCF sandbox https://k8sgpt.ai
- kagent, CNCF sandbox, has an Anthropic provider https://www.cncf.io/projects/kagent/
- Also: OpenSRE (public alpha) https://pypi.org/project/opensre ; OpenDerisk (ICSE 2026) https://conf.researchr.org/details/icse-2026/icse-2026-software-engineering-in-practice/14/OpenDerisk-An-Industrial-Framework-for-AI-Driven-SRE-with-Design-Implementation-a

**How Klaxon differs (claims to keep honest and specific):**
1. After a fix it verifies business invariants (money conservation, no double charge, every provider event accounted for), not only infrastructure health. The open-source tools above describe working from logs, metrics and cluster state; we have not found one that checks domain invariants like this (say "we did not find", not "nobody does").
2. A published, reproducible evaluation with faults whose causes are known in advance.
3. Safety by design: read-only default, approval plus rollback for every write, prompt-injection-in-logs tests.
4. Optional head-to-head against HolmesGPT running Claude on the same incidents.

## 5. Why it is good for interviews and the resume

- *System Design Interview Vol. 2* (Xu and Lam) has chapters on a distributed message queue (ch. 4), metrics monitoring (ch. 5) and a payment system (ch. 11). Klaxon's patient system contains all three. https://newsletter.pragmaticengineer.com/p/designing-a-payment-system
- 17 new-grad backend and infrastructure job postings that Aravind collected in 2026: Go listed in 7 (always as one of several accepted languages), Kubernetes 4, distributed systems 4, microservices 3, Kafka or event-driven 3, Terraform 1; reliability, observability or on-call named as a responsibility in 9. One payments company's posting asks for reconciliation, event-driven monitoring and on-call for systems that move money.
- Market analyses rank cloud plus containers and applied AI as the strongest signals: https://www.hirebase.org/blogs/in-demand-technical-skills-2026 , https://www.seekerscore.com/blog/most-requested-software-engineering-skills-2026

**What hiring managers say (summary):** recruiters spend about 31 seconds per resume and background matters most https://interviewing.io/blog/are-recruiters-better-than-a-coin-flip-at-judging-resumes ; what stands out is something a stranger can run, measured results (evals), a write-up, and outside validation; what is discounted is thin AI wrappers and AI code the candidate cannot explain. Meta's AI-enabled coding interview grades verification of AI output https://hellointerview.com/blog/meta-ai-enabled-coding

## 6. Investor signals

- YC Requests for Startups (all seasons) https://www.ycombinator.com/rfs : Fall 2025 "Infrastructure for Multi-Agent Systems"; Spring 2026 "AI for Government"; Fall 2026 "Self-Maintaining APIs", "AI-Native Compliance Infrastructure".
- a16z Big Ideas 2026 https://a16z.com/newsletter/big-ideas-2026-part-3/
- Sequoia "2026: This is AGI" https://sequoiacap.com/article/2026-this-is-agi/

## 7. Tech notes

- Bitnami free images ended Aug 28, 2025 https://thenewstack.io/broadcom-ends-free-bitnami-images-forcing-users-to-find-alternatives/ (use official images).
- `grafana/otel-lgtm` all-in-one dev observability image https://grafana.com/docs/opentelemetry/docker-lgtm/
- Kafka 4.0 is KRaft-only (no ZooKeeper). Check arm64 support with `docker manifest inspect <image>` on the M5 Mac.

## 8. Name check

A UK company sells "Klaxon", a major-incident and mass-notification platform https://www.capterra.com/p/191975/Klaxon/ . Low risk for a non-commercial portfolio project; kept for continuity with applications already sent (decision can be revisited).

## 9. Claude model facts used in planning (cached 2026-10-06)

API prices per million tokens: Opus 5.5 $4 in / $20 out; Sonnet 5.5 $2 / $10; Haiku 5.5 $0.10 / $0.50. Claude Code aliases: `opusplan` uses Opus in plan mode and Sonnet for execution; Opus 5.5 and Sonnet 5.5 default to `medium` effort. https://code.claude.com/docs/en/model-config.md
