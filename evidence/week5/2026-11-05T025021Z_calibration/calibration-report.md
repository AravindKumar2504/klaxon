# Klaxon's graders, checked

Worked out offline by klaxon.calibration from saved files: no model was called. The rule's verdicts are klaxon.grading's, worked out again; the judge's are Claude's, recorded; Aravind's were typed at a terminal (make label). The sentences' expected verdicts were written by Claude, for the tests.

## The 66 sentences of the grading tests (judged in evidence/week5/2026-11-05T025021Z_calibration)

- The rule agrees with the expected verdict on 64 of 66.
- The judge agrees on 51 of 66; no verdict on 0; judged 66 of 66.

The rule's verdicts (rows: the expected verdict):

| Expected | names the cause | partly | misses the cause | no verdict |
|---|---|---|---|---|
| names the cause | 29 | 0 | 0 | 0 |
| partly | 0 | 19 | 0 | 0 |
| misses the cause | 1 | 1 | 16 | 0 |

The judge's verdicts (rows: the expected verdict):

| Expected | names the cause | partly | misses the cause | no verdict |
|---|---|---|---|---|
| names the cause | 22 | 7 | 0 | 0 |
| partly | 1 | 13 | 5 | 0 |
| misses the cause | 0 | 2 | 16 | 0 |

Where a grader differs from the expected verdict:

- 2. duplicate-delivery (system of version 1): "The ledger consumer no longer checks idempotency keys, so some events are posted twice." expected partly; rule partly (version 4); judge names the cause; no fix given: It names the ledger consumer's missing dedupe as what books events twice and implies a recent change, though it does not mention the deploy or the provider's retries, and it proposes no fix.
- 7. provider-outage (system of version 1): "The payment provider is timing out, and with no circuit breaker each payment waits through three attempts." expected names the cause; rule names the cause (version 1); judge partly; no fix given: It names the provider timing out and the missing circuit breaker forcing three waited attempts per payment, but leaves out how that fills the API's worker threads and slows even its health checks.
- 11. provider-outage (system of version 2): "The provider is unavailable, and our breaker is open." expected names the cause; rule names the cause (version 4); judge partly; no fix given: It rightly blames the provider and notes the open circuit breaker, but never says the provider hangs, calls time out, or that the breaker answers 503 at once, and it proposes no fix.
- 13. provider-outage (system of version 2): "payments-api short-circuits every payment with a 503." expected partly; rule partly (version 4); judge misses the cause; no fix given: It only says payments-api short-circuits payments with a 503, which restates the symptom, and never names the hanging provider API whose timeouts opened the circuit breaker.
- 17. money-unit (system of version 3): "Since its deploy, payments-api sends 100x amounts: the provider's charges are 100x ours." expected names the cause; rule names the cause (version 4); judge partly; no fix given: It ties the 100x provider charges to the payments-api deploy and notes our records are correct, but never names the money-unit bug (CHARGE_AMOUNT_UNIT=dollars, cents converted to cents again) that causes them.
- 19. money-unit (system of version 3): "After the payments-api deploy (CHARGE_AMOUNT_UNIT=dollars), customers are charged 100 times the amount." expected names the cause; rule names the cause (version 4); judge partly; no fix given: It names the payments-api deploy with CHARGE_AMOUNT_UNIT=dollars and the 100x charges, but does not explain that cent amounts are read as dollars and converted again, or that our records keep the right amount.
- 26. money-unit (system of version 3): "payments-api overcharges customers." expected partly; rule partly (version 4); judge misses the cause; no fix given: It only restates the symptom that payments-api overcharges, without naming the deploy's CHARGE_AMOUNT_UNIT=dollars bug that converts cents to cents again, and it proposes no fix.
- 34. secret-rotation (system of version 3): "webhook-receiver was redeployed with WEBHOOK_SECRET_VERSION=v2; every webhook gets a 400 bad_signature." expected names the cause; rule names the cause (version 4); judge partly; no fix given: It names the webhook-receiver redeploy with WEBHOOK_SECRET_VERSION=v2 and the 400 bad_signature refusals, but never says the provider signs with v1 or that refused webhooks keep any event from reaching Kafka, and it proposes no fix.
- 40. secret-rotation (system of version 3): "No recent deploy touched the receiver; the provider must have rotated its signing secret, so every signature check fails." expected partly; rule partly (version 4); judge misses the cause; no fix given: It rules out the actual cause, a receiver deploy with a rotated secret (v2) the provider does not sign with, and blames a provider-side rotation instead, getting only the failing signature check right, with no fix proposed.
- 41. secret-rotation (system of version 3): "The provider moved to v2 of its webhook format, and our receiver rejects the new payloads." expected partly; rule partly (version 4); judge misses the cause; no fix given: It blames the provider for moving to a v2 payload format, but the cause is our own receiver deploy with a rotated v2 secret while the provider still signs with v1, so signature checks fail; no fix is proposed.
- 42. secret-rotation (system of version 3): "The provider rotated its secret; our receiver's config still has the old key, so it rejects every webhook." expected partly; rule partly (version 4); judge misses the cause; no fix given: It blames a provider-side secret rotation with our receiver still on the old key, but the right answer is the reverse: our deploy rotated the receiver to v2 while the provider still signs with v1.
- 52. pool-exhaustion (system of version 3): "Slow database queries in payments-api are holding connections, so requests hit PoolTimeout." expected misses the cause; rule misses the cause (version 4); judge partly; no fix given: It correctly places the failure in payments-api's exhausted connection pool causing timeouts, but blames slow queries instead of the deployed refund connection leak that never returns connections, and it proposes no fix.
- 59. duplicate-delivery (system of version 1): "Redis lost its idempotency keys, so the ledger books webhooks the provider re-sends a second time." expected misses the cause; rule misses the cause (version 4); judge partly; no fix given: It correctly says the ledger books provider re-sends a second time, but blames Redis losing idempotency keys instead of a deploy of the ledger consumer without its dedupe (LEDGER_HANDLER=naive).
- 60. provider-outage (system of version 1): "PostgreSQL is slow and payments-api requests wait on it; fake-provider looks healthy." expected misses the cause; rule names the cause (version 1); judge misses the cause; no fix given: It blames a slow PostgreSQL and calls the provider healthy, while the cause is the provider's hanging charge/refund API with no circuit breaker filling the API's worker threads; it proposes no fix.
- 61. provider-outage (system of version 1): "payments-api is slow because a traffic spike exhausted its worker threads." expected misses the cause; rule partly (version 1); judge misses the cause; no fix given: It blames a traffic spike for the exhausted worker threads, not the hanging provider API and the missing circuit breaker, and it proposes no fix.
- 62. poison-message (system of version 1): "The ledger consumer retries forever one payment.updated event whose amount is not positive integer cents." expected names the cause; rule names the cause (version 4); judge partly; no fix given: It correctly names the ledger consumer retrying one unreadable payment.updated event forever, but it never ties that to the deploy that set BAD_EVENTS=retry or says that nothing behind the event gets booked, and it proposes no fix.
- 63. poison-message (system of version 1): "The ledger consumer retries forever one event whose amount is a string." expected names the cause; rule names the cause (version 4); judge partly; no fix given: It names the ledger consumer retrying forever on an event with a string amount, but not the deploy that set BAD_EVENTS=retry or that the stuck event blocks all booking behind it.

## The 13 saved diagnoses

Judged: 13 of 13. Labeled by Aravind: 13 of 13. A run's verdicts are held back until he labels it.

- The rule and the judge agree on 13 of 13 of the runs he labeled.
- The rule agrees with Aravind on 13 of 13; on the fix, 12 of 13.
- The judge agrees with Aravind on 13 of 13; on the fix, 13 of 13.

| Run | Graded as | Rule | Judge | Aravind |
|---|---|---|---|---|
| 2026-10-18T112951Z_loop_ledgerlaghigh | false-alarm | misses the cause (v4), fix False | misses the cause, fix False | misses the cause, fix False |
| 2026-10-19T022933Z_fault-run_duplicate-delivery | duplicate-delivery | names the cause (v4), fix True | names the cause, fix True | names the cause, fix True |
| 2026-10-19T023414Z_fault-run_poison-message | poison-message | names the cause (v4), fix True | names the cause, fix True | names the cause, fix True |
| 2026-10-19T024001Z_fault-run_provider-outage | provider-outage | names the cause (v1), fix False | names the cause, fix True | names the cause, fix True |
| 2026-10-21T090513Z_gated-run_duplicate-delivery | duplicate-delivery | names the cause (v4), fix True | names the cause, fix True | names the cause, fix True |
| 2026-10-21T091242Z_gated-run_poison-message | poison-message | names the cause (v4), fix True | names the cause, fix True | names the cause, fix True |
| 2026-10-21T094537Z_gated-run_poison-message | poison-message | names the cause (v4), fix True | names the cause, fix True | names the cause, fix True |
| 2026-10-24T081041Z_gated-run_money-unit | money-unit | names the cause (v4), fix True | names the cause, fix True | names the cause, fix True |
| 2026-10-24T082544Z_gated-run_money-unit | money-unit | names the cause (v4), fix True | names the cause, fix True | names the cause, fix True |
| 2026-10-24T083634Z_gated-run_secret-rotation | secret-rotation | names the cause (v4), fix True | names the cause, fix True | names the cause, fix True |
| 2026-11-01T024929Z_gated-run_secret-rotation | secret-rotation | names the cause (v4), fix True | names the cause, fix True | names the cause, fix True |
| 2026-11-01T030525Z_gated-run_pool-exhaustion | pool-exhaustion | names the cause (v4), fix True | names the cause, fix True | names the cause, fix True |
| 2026-11-01T031219Z_gated-run_provider-outage | provider-outage | names the cause (v4), fix True | names the cause, fix True | names the cause, fix True |
