# Study notes (for Aravind)

What you need to be able to explain in an interview or a TA question, added milestone by milestone. Keep answers short and in your own words.

## The one-minute pitch

Klaxon is an AI on-call engineer for a payments system. When an alert fires, it investigates the metrics, logs and traces, proposes a fix with the evidence, waits for a human to approve, applies the fix, and then proves it worked: the system is healthy again and the money is right (no payment lost or charged twice). I built the payments system it protects, a fault injector with incidents whose causes are known in advance, and an evaluation that measures how often Klaxon gets the diagnosis right, how long it takes and what it costs.

## Concepts to own (by milestone)

- **First piece:** at-least-once delivery (why providers send duplicates), idempotency keys, double-entry ledgers, why a dashboard can be green while money is wrong, HMAC signature verification.
- **Week 1-2:** Kafka topics, partitions, consumer groups and offsets; Redis as an idempotency store; Docker Compose networking; OpenTelemetry traces, metrics and logs; what Grafana shows.
- **Week 3-4:** the agent loop (goal, state, action, observation, stop); the MCP protocol (JSON-RPC, tools, schemas); prompt contracts (validated output formats); approval gates; rollback; verification.
- **Week 5-6:** evaluation design (known-cause faults, seeds, held-out cases, graders, cost); Kubernetes basics (pods, deployments, rollouts, `kubectl rollout undo`); LangGraph state, nodes, interrupts.
- **Week 7-9:** multi-agent handoffs and their failure modes; prompt injection through logs; budgets and stop conditions; CI.

## Questions an interviewer will likely ask

- Why did you pivot from code review to on-call?
- How does Klaxon know its fix worked? What if the dashboard is green but something is still wrong?
- How did you make sure your own benchmark wasn't too easy?
- What stops Klaxon from doing something dangerous?
- What failed, and what did you change?
- How is this different from HolmesGPT or K8sGPT?

## First piece (built 2026-10-09): what to be able to explain

- **Why providers send duplicates.** Webhooks are delivered at least once. If your 200 does not reach the provider in time (a timeout, a dropped connection), it retries the same event. Stripe's docs say endpoints "might occasionally receive the same event more than once" and suggest logging processed event ids. The demo simulates exactly this: delivery #3's acknowledgement is treated as lost, and #7 is the retry.
- **Why the dashboard stays green.** The dashboard only sees HTTP status codes. Both deliveries of the duplicate returned 200, so 7 of 7 requests succeeded. Nothing at the HTTP level is wrong.
- **Why "the ledger balances" is not enough.** In double-entry, every posting has equal debits and credits. A payment posted twice is two balanced postings, so total debits still equal total credits. You only catch it by comparing against an outside source of truth: the provider's own records (reconciliation).
- **The three invariants.** (1) Every provider event posted exactly once: 0 means lost, 2 means counted twice, and anything the provider never sent is flagged too. (2) The ledger balances, and every transaction has at least two lines. (3) The ledger's `provider_clearing` balance equals what the provider says it holds, in total and per payment. The per-payment check catches money moved between payments even when the totals agree.
- **How the idempotent handler works.** It inserts the event id into `processed_events` (primary key) and writes the posting in one database transaction. If the id is already there, nothing is written and it still answers 200, so the provider stops retrying. If the posting fails halfway, the whole transaction rolls back, including the dedupe row, so the retry can still post. That last part is the subtle one, and there is a test for it.
- **Why the duplicate still answers 200.** A non-2xx answer makes the provider retry (Stripe keeps trying for up to three days). "Already processed" is a success.
- **HMAC signatures.** The provider signs `"<timestamp>.<raw body>"` with a shared secret (HMAC-SHA256, Stripe's v1 shape). The endpoint recomputes it and compares in constant time (`hmac.compare_digest`). A changed body, the wrong secret or a timestamp more than 5 minutes off means 400. A malformed header must be a 400, never a crash; the tests feed it garbage on purpose (this is the HMAC-crash class from the co-op record).
- **Repairs are reversals.** Ledger rows are never deleted. To undo a double posting you add its mirror image (every line flipped) pointing at the original; the check then counts that posting as cancelled.
- **Money is integer cents, in STRICT tables.** No floats anywhere near money; even raw SQL cannot store 100.5 cents.
- **How you know it ran.** `pytest` passes (91 tests on 2026-10-09), and each demo run writes a dated JSON evidence file with the exact deliveries, the dashboard, the money report, the ledger and a SHA-256 of the code that produced it.
- **How you know the tests mean something.** Claude broke the code on purpose 21 ways (for example: stop deduping, accept a truncated signature, commit the dedupe row separately, count reversed postings) and the tests caught every one.
