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
