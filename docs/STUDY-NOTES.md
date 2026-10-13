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

## The project explained simply (2026-10-09)

**In one sentence.** Klaxon will be a robot helper that gets called when a payment website breaks. It will figure out what went wrong, suggest a fix, ask a human for permission, and then double-check that the money adds up, not just that the website looks okay.

**The lemonade stand.** You run an online lemonade stand. A payment company (like Stripe) collects the money. Each time someone pays, it sends your computer a message, "Ana paid $5" (a **webhook**). Your computer writes it into the money notebook (the **ledger**). A screen counts the messages and whether each arrived okay (the **dashboard**). If the "got it" reply gets lost, the payment company sends the same message again. If your computer writes it down twice, the notebook claims $5 you never got, and the dashboard still looks perfect.

**What the full Klaxon will do (later).** When an alarm goes off, it will (1) investigate the clues: metrics (numbers over time), logs (the system's diary), traces (the path each request took); (2) write a diagnosis with proof; (3) suggest a fix and wait for a human's yes; (4) apply it, then check that the system is healthy and the money is right; (5) write the report (postmortem).

**Why it's needed.** Finding the cause of an outage is slow and stressful. The scariest bugs are quiet: money goes wrong while every screen looks fine. Today's AI helpers mostly verify with the same screens; HolmesGPT's deployment check compares "error rates, latency, restarts, and logs", which are exactly the signals that stayed green in the demo. Klaxon's job is the money check.

**Why it was a good idea.** A real, unsolved problem that companies pay for (AI on-call startups have reportedly raised large rounds; on the ITBench research benchmark the best agents fixed 11.4% of problems). Small enough for one person to rebuild at small scale. It uses the skills in the backend and infrastructure jobs I'm applying for, it counts for both courses, and it connects to the webhook and money bugs I caught in code review at my co-op.

**Where it could be used.** Anywhere money moves through software: online stores, payment providers, banks and fintech apps, subscriptions, marketplaces and delivery apps that pay sellers or drivers, ticketing, payroll, insurance. The same idea fits anywhere records must match reality: inventory, airline seats, hotel bookings.

**The six breakdowns Klaxon will practice on.** A payment message arrives twice (counted twice); a message gets stuck in the queue (payments stuck "pending"); the payment company goes down and nothing stops the waiting (the site crawls); a bad update mixes up cents and dollars ($5 becomes $500 or 5 cents); a secret key is rotated wrong (every payment message rejected); the database runs out of connections (errors pile up).

**What the demo shows.** Five payments and one refund (6 events) were sent as 7 messages, because payment #3's "got it" reply was "lost" on purpose, so #7 is its retry.
- *Naive handler:* writes every message, so payment 3 ($69.68) went in twice. Dashboard GREEN (7 of 7 arrived fine). Ledger "balanced", because each entry has two matching halves ("money we're holding" and "sales"), and doubling both keeps them equal. Money check FAILED: the payment company says we hold $252.97, our notebook says $322.65, and the $69.68 difference is exactly payment 3 ("evt_0003 posted 2 times, expected 1").
- *Idempotent handler (the fix):* "idempotent" means doing it twice is the same as doing it once. It stamps each message id DONE. When #7 arrived, it saw the stamp, wrote nothing, and still replied "got it" so the retries stop. Notebook = $252.97; every check passes. The DONE stamp and the entry are saved in one step (one database transaction), so a crash in the middle cannot leave a stamp without an entry, which would lose the payment.
- *The lesson:* the dashboard looked the same in both runs. Only the money check told them apart. That is the check Klaxon will run after every fix.

## Why does Stripe send the same message twice? (2026-10-09)

**It's not a mistake. It's on purpose.** Over a network, a sender cannot know for sure whether the other side got a message. If no "got it" comes back, either the message was lost or the reply was lost, and the two look identical from the sender's side (in computer science this is the Two Generals' Problem). The provider has two choices:
- *Send once and never retry ("at most once"):* some payments would simply never be recorded. A lost payment cannot be recovered by the receiver.
- *Retry until it hears "got it" ("at least once"):* nothing is lost, but sometimes a message arrives twice. The receiver can spot a repeat by its event id and skip it.

For money, "at least once" is the right choice, so the job of ignoring repeats belongs to the receiver. Stripe's docs say it directly: endpoints "might occasionally receive the same event more than once", and they recommend logging processed event ids, which is exactly what the idempotent handler does.

**How a duplicate actually happens:** our server saves the payment but answers too slowly, so the provider gives up waiting and retries; the network drops our "200 OK" on the way back; our server crashes or restarts during a deploy after saving but before answering; a proxy in front of our server times out; someone resends an event by hand from the provider's dashboard.

**It's everywhere, not just Stripe.** AWS SQS standard queues deliver "at least once". Kafka (week 1) does it too: a consumer that processes a message and crashes before saving its place (committing its offset) will read that message again after it restarts.

**How often?** Providers don't publish a rate; Stripe says "occasionally". But a payment system handles huge numbers of messages, so even something rare per message happens regularly in total. That's why every serious payment system is built to be idempotent.

**So what does Klaxon add, honestly?** The duplicate fix itself is well known (idempotency keys and unique constraints). Comparing your books with the provider's records (reconciliation) is standard practice at payment companies, often as a daily batch job. What Klaxon adds is joining the two worlds: the AI on-call agent proposes a fix, and a reconciliation-style money check right after the fix decides whether it's really fixed, across many kinds of breakdown, with a published score of how often the agent is right. Limits to admit: real reconciliation also handles fees, currencies, settlement timing and disputes; the demo's version is simplified.

## Where else it applies, and do these bugs really happen? (2026-10-09)

**Two halves.** The on-call part (alarm, investigate, diagnose, fix with a human's yes, report) works for almost any software. The checker is what you swap per business. Payments came first because the truth is easy to check against the provider's records.
- Money moves: online stores (every payment booked once), delivery and ride-sharing (one payout per trip), marketplaces (payouts = sales minus fees), subscriptions (one charge per billing period), ticketing (tickets match payments; seats sold never exceed seats), money-transfer apps (money out = money in), payroll (one paycheck per pay period).
- Same pattern, no money: inventory (website stock = warehouse count), airlines and hotels (one booking per seat or room), notifications (each sent once).
- Interview line: "The agent loop is general and the checker plugs in; I built the payments checker first because there the truth is easy to verify against the provider's records."

**Real incidents (checked 2026-10-09):**
- Santander UK, 25 Dec 2021: a scheduling glitch sent about 75,000 payments a second time (about £130m); workers at roughly 2,000 businesses were paid twice. https://fortune.com/2021/12/31/santander-bank-accidentally-sent-175-million-payments-christmas-day-wants-money-back
- NatWest, May 2022: about 112,000 debit card customers saw purchases taken twice; merchants were paid once; overdraft fees were waived. https://www.moneyexpert.com/news/more-than-100-000-natwest-accounts-mistakenly-showing-payments-twice/
- KeyBank, Nov 2025: a local TV station reported some debit purchases from 3 Nov posted twice, pushing some accounts below zero. https://digital-release.rochesterfirst.com/?p=2440193
- Citibank, Aug 2020: meant to send about $8m in interest, sent about $900m; lost the first ruling on about $500m, won on appeal in 2022. https://www.bankingdive.com/news/citi-wins-appeal-500-million-revlon-loan-blunder/631536/
- Citigroup, Apr 2024: a $280 payment entered as $81 trillion, caught about 90 minutes later and reversed; Citi credited its "detective controls". https://www.nbcsandiego.com/news/business/money-report/another-near-miss-citigroup-mistakenly-credited-a-customer-account-with-81-trillion/3766861/
- Canada's Phoenix pay system, 2016 to now: about 223,000 employees overpaid at least once in its first three years; roughly $5bn spent; 216,000 requests still waiting in Feb 2026. https://www.ctvnews.ca/politics/article/phoenix-pay-errors-continue-to-haunt-public-service-a-decade-after-launch/
- UK Post Office Horizon: accounting-software bugs made branch accounts look short and hundreds of branch managers were blamed; the 2019 High Court ruling called the system "not remotely robust". https://caselaw.nationalarchives.gov.uk/ewhc/qb/2019/3408

**Why customers rarely see them:** rare per payment, usually caught and reversed fast, and many errors hit the company's own books instead of the customer (Santander covered the extra payments itself). They stay rare because engineers catch them in review, the way I caught a webhook data-loss path in code review at my co-op.

## Week 1 (built 2026-10-09): the patient runs for real

**The flow, in one breath.** A client asks our payments API for a payment. The API charges the fake provider. The provider sends a signed webhook. Our webhook receiver checks the signature and writes the event to Kafka. The ledger consumer reads it from Kafka and books it in PostgreSQL. The money check then compares our ledger with the provider's own records. Eight containers: PostgreSQL, Redis, Kafka, a one-shot topic creator, and our four Python services.

**Kafka, the words to own.**
- *Topic:* a named, append-only log of messages. Ours is `provider-events`. A message is never edited; readers just move forward.
- *Partition:* a topic is split into partitions (ours has 3) so several consumers can share the work. Order is guaranteed only inside one partition. I saw this happen: the ledger booked `evt_000003` (partition 1) before `evt_000001` (partition 0).
- *Key:* decides the partition. We key by payment id, so one payment's events stay in order relative to each other.
- *Offset:* a message's position in its partition, like a line number. The ledger keeps it on every posting (`source = provider-events/1@0`), so a posting can be traced to the exact message.
- *Consumer group:* consumers that share a topic. Kafka gives each partition to one member and remembers, per partition, the offset the group has committed: its bookmark. Our group is `ledger`.
- *Lag:* how far the bookmark is behind the end of the log. Lag 0 means everything has been booked. Lag that keeps growing is what an alert will watch in week 2.
- *KRaft:* Kafka runs without ZooKeeper now. One container is both broker and controller.

**Commit after the database, never before.** If the consumer commits its offset first and then crashes, the event is skipped forever: a lost payment. If it books first and crashes before committing, it reads the event again after a restart, and the idempotent booking turns the second read into a no-op. At-least-once delivery plus idempotent processing gives "effectively once". The end-to-end test resets the group's bookmark to the start of the topic, the same as a crash before every single commit, and the ledger books nothing twice.

**Where the dedupe lives.** In the same database transaction as the posting (`processed_events` primary key). Deduping earlier, for example in the receiver, opens a gap: an event can be marked "seen" and then never booked if the next step fails. That is a data-loss path, the bug class I caught in code review at my co-op.

**The receiver only says 200 after Kafka says yes.** It waits for the broker's acknowledgement. If Kafka does not confirm within 3 seconds it answers 503, and the provider retries later. A 200 that is not backed by a durable write is how webhooks get lost.

**Dead letters.** A message that can never be booked as it is goes to `provider-events-dlq` with the reason, so a human can look: no body at all, bad JSON, a malformed id, a wrong currency, an absurd amount, or a value the database refuses. One bad message must not block the partition behind it (that is the "poison message" incident). Anything else, like the database being down or a permission problem, is retried forever without committing. A money event is never skipped because something on our side is broken. The independent review caught that my first version crashed in a loop on a message with no value; the tests now feed it that on purpose.

**Redis as an idempotency store, and why it is not the guarantee.** A client that times out retries with the same `Idempotency-Key`. The API claims the key in Redis with `SET NX` (set only if not there). A repeat gets the saved response (header `Idempotent-Replayed: true`); a repeat that arrives while the first is still running gets 409; the same key with a different request gets 422. But Redis keys expire, our Redis keeps nothing on disk, and Redis can be down. So PostgreSQL has `payments.idempotency_key UNIQUE` behind it, and the provider gets our payment id as its own idempotency key. Three layers. I tested it by stopping Redis: the retried request still found the existing payment and the provider charged once. One more detail: each claim carries a random token, and releasing or finishing a claim runs as a small Lua script that first checks the token. Otherwise a slow request whose lock had expired could delete the lock of a newer request with the same key.

**Transactional outbox (in the provider).** Creating a payment writes the payment, its event and a "to deliver" row in one transaction. The provider can never charge without sending the webhook. The webhook sender then works through the outbox and retries with backoff (0.5 s, 1 s, 2 s, ... up to a minute) until it gets a 2xx.

**Least privilege in the database.** One login role per service:
- `ledger_writer` can SELECT and INSERT ledger rows but has no UPDATE or DELETE. "Ledger rows are never edited" is enforced by the database.
- `payments_api` can change a payment's status, never its amount (a column-level grant).
- `klaxon_readonly` can read everything and write nothing, its transactions are read-only, and its queries stop after 5 seconds. The money check uses it, and Klaxon's tools will too. Even with read-only switched off, the grants refuse a write: the grants are the real guard.

**Docker Compose networking.** Every service is reachable by its name inside the Compose network (`postgres:5432`, `kafka:9092`). From the Mac, the same services are on 127.0.0.1 ports (API 8080, provider 8081, receiver 8082, PostgreSQL 5432, Redis 6379, Kafka 9094). Kafka needs two listeners for that, because it tells clients which address to use next: `kafka:9092` for containers and `127.0.0.1:9094` for the Mac. Every port listens on 127.0.0.1 only, so nothing is reachable from the network. Health checks plus `depends_on` start things in the right order, and named volumes keep data until `make reset`.

**How the live money check knows the system has settled.** It waits until the provider has no webhook left to deliver and the ledger consumer's lag is 0, twice in a row. Then it reads the ledger in one snapshot (REPEATABLE READ). Checking earlier would make an event still in flight look lost.

**What I can say ran (2026-10-09; STATUS has the commands).** The first piece's scenario, replayed through the real stack, gave the same numbers: the naive consumer booked `evt_000003` twice, the dashboard stayed GREEN, the ledger balanced, and the money check failed, off by +$69.68. The idempotent consumer passed. 242 offline tests, 33 integration tests against the real PostgreSQL, Redis and Kafka, and 8 end-to-end tests that stop Redis, Kafka and PostgreSQL in the middle of traffic. And 45 deliberate breaks of the code, every one caught by a test.

**How do you know your tests mean something?** Mutation testing: break the code on purpose in a plausible way (commit before booking, answer 200 when Kafka did not confirm, trust Redis alone) and check that a test fails. `make mutants` does 45 of these on a copy of the repo. Two survived at first, and each exposed a weak test of mine, which I fixed. That is the point of the exercise.

**Likely questions.**
- *Why Kafka between the receiver and the ledger?* The receiver can answer the provider fast, the ledger can be down for a while without losing anything, and later more consumers (fraud checks, analytics) can read the same events without touching the receiver.
- *Is your system exactly-once?* No. Delivery is at least once everywhere. Booking is idempotent, so the effect is once. That is the honest wording.
- *What if Redis is down?* The API logs it, reports itself "degraded", and keeps working with the database's unique key alone. It fails open to the database, never open to nothing.
- *What is not handled yet?* No circuit breaker on provider calls (planned as a week 2 incident), no OpenTelemetry yet, single currency, and the provider is a simulator.
- *Why will webhooks confirm payments (my decision, 2026-10-11)?* Today a payment's status comes only from the provider's immediate answer. If that answer times out after the provider has already charged, our record says "pending" forever, and no check notices, because the money check only compares the ledger. With webhooks confirming payments and a fourth check comparing our payment records with the provider's, a stuck payment becomes visible. Klaxon needs that visible symptom to find the "stuck pending" and "never confirmed" incidents in week 2.

## Week 2, item 1 (built 2026-10-11): webhooks confirm payments

**The problem, in one breath.** Our payments API learns a payment's outcome from the provider's answer. If that answer never arrives (a timeout, or a 500 after the provider already saved the charge), the API cannot know whether the customer was charged. It answers "unknown, retry with the same key" and leaves the payment pending. If the client never retries, the payment stays pending forever: the customer paid, our records say they did not, and a refund is refused. Before this change no check could see it, because the money check only compared the ledger with the provider.

**Why a 500 does not mean "not charged".** Stripe's docs say to treat a 500's outcome as indeterminate. Stripe also replays the same 500 to every retry with the same idempotency key, so retrying cannot settle it. The webhook can: the provider sends `payment.succeeded` whether or not its answer reached us. (Sources in `RESEARCH.md`, section 10.)

**How the webhook finds our payment.** The payments API sends our own payment id with every charge, as metadata (`klaxon_payment_id: pmt_000003`). The provider puts that metadata in the payment's events, as Stripe does. Stripe's docs recommend this: put a local identifier in the metadata, so a webhook can be matched to your own records. Looking the payment up by the provider's id instead would miss exactly the payments that need it, because a lost answer is the only way we never learn the provider's id.

**Two consumer groups on one topic.** The ledger consumer (group `ledger`) and the payment confirmer (group `payment-confirmer`) both read `provider-events`. Kafka keeps a separate bookmark (committed offset) for each group, so each one gets every message, and one can stall while the other keeps going. They share one loop (`consumer.py`): apply the event in one database transaction, then commit the offset; dead-letter only messages that can never be used; retry everything else. A poison message is dead-lettered once per group, and a header says which group refused it. The money check now waits until both groups have caught up.

**One statement: compare and set.** The confirmer runs one UPDATE: set succeeded and the provider's id, but only if the payment is still pending, the amount matches, and it is not linked to another charge. If that changes nothing, it reads the row to say why. Usually the API's own answer got there first ("already confirmed"). Otherwise it is a conflict: we recorded it as failed, the amounts differ, or it is linked to another charge. A conflict changes nothing and is logged as an error for a human. A final status is never overwritten. The confirmer's database role can change only a status and the provider's id: no amounts, no new payments, no refunds, no ledger.

**The fourth invariant.** `payments_match_provider` compares our payments table with the provider's payments. Every payment of ours that the provider charged must be succeeded, for the same amount, linked to that charge, and charged exactly once. Every payment we call succeeded must have a charge. And the provider must have charged nothing that is not ours. A pending or failed payment with no charge is fine: no money moved.

**Why the first three invariants miss some money bugs.** They compare the ledger with the provider. The ledger is built from the provider's webhooks, so when the provider itself charged wrongly, the ledger copies it faithfully and all three pass. Two examples, both in the tests: a true double charge (a retry sent with a new idempotency key makes a second charge for one order) and a 100x money-unit error (we asked for $5.00, a bad deploy charged $500.00). Only the fourth invariant compares the provider with what we asked for.

**The known-bad baseline.** `CONFIRMER=by_provider_id` looks the payment up by the provider's id, the way many integrations do. It passes every normal test, because a normal payment already has the provider's id. It fails exactly on a lost answer, and silently: "not found" looks like "not ours". Like the naive ledger handler, it exists so the money check has a real bug to catch.

**What I can say ran (2026-10-11; STATUS has the commands).** The lost-answer scenario through the live stack, twice, with evidence files in `evidence/week2/`. The fake provider saved payment 3's charge ($69.68) and answered 500 to all three of the API's attempts, so the client got 503 and gave up. With the known-bad confirmer, the payment stayed pending and its refund was refused (409); the webhook dashboard was GREEN, and the ledger balanced and matched the provider; only the fourth invariant failed. With the payment confirmer, the webhook confirmed it, the refund went through, and all four invariants passed. Also measured: 287 offline tests, 56 integration tests, 10 end-to-end tests (including both consumers riding out a PostgreSQL outage), and 67 of 67 deliberate breaks caught.

**What the review taught.** An independent review found no money bug, but it found a misleading report. A stuck payment that had also been charged the wrong amount was reported only as "pending". An agent reading that would confirm it, which is the wrong fix. The check now reports every problem with a payment and always shows the charged amount. Lesson: evidence an agent acts on must point to the right fix, not only give the right verdict.

**Likely questions.**
- *Why not make the webhook the only way a payment becomes succeeded?* Stripe does something like that for slow payment methods such as bank debits: the payment stays "processing", sometimes for days, until it settles. But here it would change our API's answer (201 "succeeded" would become "processing"), and a refund right after a payment would have to wait for the webhook. With both paths, whichever arrives first sets succeeded, and the other changes nothing.
- *What if the API and the confirmer write at the same moment?* They write the same values. The confirmer's UPDATE matches only a pending row, and the API sets succeeded only after the provider said so. An integration test races the two.
- *Why not dead-letter a conflict?* The message is fine; our record disagrees with it. Retrying cannot fix that, and dead letters are for broken messages. It is logged as an error, and the fourth invariant reports it with evidence.
- *What is still not covered?* Refunds have the same gap: a refund whose answer is lost stays pending, and nothing confirms it yet.

## Week 2, item 2 (built 2026-10-12): OpenTelemetry, and what Grafana shows

**Why this matters for Klaxon.** Klaxon investigates an incident through tools that read metrics, logs and traces (week 3). Until now the patient wrote JSON logs and nothing else. Now four services send all three signals to one container, `grafana/otel-lgtm`, and Grafana shows them.

**The three signals, in one line each.**
- *Metrics* are numbers over time: how many webhooks per second, by result; how far behind each consumer is. Cheap to keep, good for alerts and dashboards. Prometheus stores them.
- *Traces* follow one request through every service. A trace is a tree of *spans*; each span is one piece of work (an HTTP request, a database query, a Kafka message) with a start, an end and attributes. Tempo stores them.
- *Logs* are the lines the services print, now with the trace id, so a line leads to its trace. Loki stores them.

**OpenTelemetry** is the vendor-neutral standard for producing all three. The services use its Python SDK and send everything over OTLP (its protocol) to the collector inside `otel-lgtm`, which hands each signal to its store.

**How one trace crosses Kafka.** HTTP carries the trace context in a `traceparent` header automatically. Kafka does not, so the receiver does it by hand: it writes the context into the Kafka message's headers, inside a "send" span. Each consumer reads it back and starts its "process" span as a child. So the webhook, the ledger's booking and the confirmer's confirmation are one trace. I saw it: the trace of payment 3's webhook had the receiver's spans, then the ledger consumer's span ("posted") and the confirmer's ("confirmed"), each with its database statements underneath.

**What a trace does not cross: the provider.** The fake provider sends no telemetry, because it stands for an outside company; Stripe does not send us its internal traces either. So the API's request ends at the provider call, and the provider's webhook starts a new trace at our receiver. Our ids join them: search for the payment id and both traces come up.

**The numbers to know (from the dashboard).**
- *Webhook results*: queued, bad_signature, bad_payload, too_large, queue_unavailable. A rotated secret shows as bad_signature; a broken payload as bad_payload. Both are HTTP 400, which is why the result has its own counter.
- *Consumer lag* per group: measured from the committed offsets, outside the consumers, so a consumer stuck on one message, or stopped, still shows a growing lag.
- *Consumer outcomes*, *retries* and *dead letters*: a steady climb in retries means a consumer is stuck.
- *Provider calls*: every attempt, with its status or error. A provider outage shows here first.
- *Payments pending*, under or over a minute old. Over a minute is the "stuck pending" symptom.

**Untrusted input, again.** Klaxon will read this telemetry, and an attacker controls some of what a request contains. So a caller's trace headers (traceparent, tracestate, baggage) are ignored: otherwise a caller could switch our tracing off, choose our trace ids, or write its own text onto every span. Only our own hop through Kafka carries trace context, and only the traceparent. Server spans never keep the raw path, query, original method or Host header (overwritten when the span starts; the route template stays), database and Redis spans record the statement without its values, and every attribute is cut at 300 characters. The same rule as the week 1 logs.

**Queries to try** (Grafana, Explore; or the APIs on 127.0.0.1):
- Tempo (TraceQL): `{ span.klaxon.payment_id = "pmt_000003" }` finds the payment's traces.
- Prometheus (PromQL): `sum by (consumer) (klaxon_kafka_consumer_lag)` is each group's lag.
- Loki (LogQL): `{service_name="ledger-consumer"} | event_id = "evt_000003"` finds the log line, with its trace_id.

**What I can say ran (2026-10-12; STATUS has the commands).** With the lost-answer demo's traffic: payment 3's webhook was one trace, from the receiver through the Kafka write to both consumers' work ("posted", "confirmed"); the API's trace for that payment showed the three failed provider attempts; the metrics matched the demo's numbers exactly; the log lines in Loki carried the same trace id; and the Grafana dashboard drew it all. A request that sent its own traceparent got a new trace of ours instead. Measured: 309 offline tests, 57 integration tests, 11 end-to-end tests (one reads a payment's trace, metric and log back from the three stores), and 86 of 86 deliberate breaks caught. After the review's fixes (2026-10-13): 312, 57 and 12 tests, and 89 of 89 breaks caught.

**What the review taught.** I had cleaned what the telemetry records about a request, but not what it takes from it. The trace context arrives in headers, and the tracer believed them: a caller could turn tracing off or put its own text on every span, as far as Kafka and the consumers. Lesson: with untrusted input, check both what you record and what you obey.

**What my Grafana review found (2026-10-12 and 13).** I looked at everything myself first, and five things were wrong. All are fixed now (DECISIONS has the details).
- *The rate panels were flat after the demo.* `rate()` needs two samples. A counter's first sample already held the whole burst, because the burst came before the first export. Fix: Prometheus now records a 0 where each counter started (the SDK sends that time). Lesson: a short burst right after startup is invisible to `rate()` unless the series starts at 0.
- *The database p95 said 4.75 s for 0.3 ms queries.* The histogram had buckets made for milliseconds (0, 5, 10, ...) while the values were in seconds, so every query fell in the first bucket. `histogram_quantile` interpolates inside a bucket: 95% of the way from 0 to 5 is 4.75. Lesson: a p95 that is exactly 0.95 times a bucket edge means the buckets are wrong.
- *"Related logs" found nothing for the confirmer's span.* Grafana searched only the span's own milliseconds, and the "confirmed" line came 0.4 ms after the cut. Fix: search a minute either side, still filtered by trace id.
- *`payment_id` meant two things.* The API logged our id, the consumers the provider's. One search missed half of a payment's story. Now `payment_id` is always ours, and the provider's is `provider_payment_id`.
- *Grafana let anyone on this Mac in as Admin.* That was the image's default, and our docs said the opposite. Lesson: check what a default really does; do not trust what you wrote about it.

**Reading a trace's gaps.** Payment 3's webhook trace lasted 1.16 s, but every span took a few milliseconds. The logs explained it: the demo starts the services fresh, and the ledger consumer was still joining its Kafka consumer group ("partitions assigned" came half a second after the webhook). A gap in a trace is time nobody was working on the request; the logs around it say why.

**Grafana basics I needed.** Explore starts at "Last 1 hour": older data needs a wider time range, or it looks missing. Clicking a trace id, or a log line's "Trace" link, opens the trace in a second pane. A red icon on a span means it ended in an error.

**Likely questions.**
- *Why might a dashboard miss a spike?* `rate()` needs two samples, so a counter whose first sample already holds the spike shows nothing. Start every series at 0 (here Prometheus does it from the SDK's start time), or count the totals instead.
- *Why does p95 depend on the buckets?* A histogram only knows how many values fell in each bucket, and `histogram_quantile` guesses linearly inside one. Buckets that do not fit the values give a meaningless answer.
- *Why not instrument the provider too?* A real provider is another company; we would not see its internals, and Klaxon must work without them.
- *Why put the trace context in Kafka headers by hand?* A contrib instrumentation for confluent-kafka exists (0.66b0 supports our version). I did it by hand instead: our publisher already passes headers, it is about 20 lines, and the tests check the hand-off directly. Either would work.
- *Why measure lag outside the consumers?* A consumer stuck in a retry loop, or stopped, cannot be trusted to report its own lag.
- *What does it cost?* The container uses about 0.8 GB of memory, and `make up` still takes about 13 s.
