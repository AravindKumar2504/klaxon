# Status

Last updated: 2026-10-09, end of pitch-weekend session 1. The pitch is submitted; next is week 1.

## Done

- Project chosen and planned (see `PLAN.md`, `DECISIONS.md`, `RESEARCH.md`).
- **First piece built and committed** (`801a0bf`, local only, Aravind approved the commit). Synthetic, seeded, in-process demo of the money check: fake provider with HMAC-signed webhooks, double-entry ledger with naive and idempotent handlers, the money check (three invariants with evidence), a dashboard view, and `python -m klaxon.demo duplicate-webhook --handler naive|idempotent`.
  - Tests: 91 passed (`.venv/bin/python -m pytest -q`, 2026-10-09). 21 deliberate code breaks, all caught by the tests.
  - An independent Claude review found no wrong behavior; its gaps were fixed (see `DECISIONS.md`, 2026-10-09 evening).
  - Real runs, 2026-10-09 15:54 PDT, seed 7, code SHA-256 `5cc69830a8d7...`: naive = 7 requests, 0 errors, dashboard GREEN, ledger balanced, money check FAILED (pay_0003 posted twice, off by +$69.68); idempotent = PASSED. Files: `evidence/first-piece/2026-10-09T225438Z_*.json`.
  - **Do not edit `src/klaxon` without re-running both demo commands**: the evidence records the code's SHA-256.
- **Pitch film made and posted.** Aravind watched it and approved the narration on 2026-10-09. Master: 3:48, 1080p, SHA-256 `b551f076...`; all gates pass except the toolkit's bookend check (its outro card hard-codes the instructor's handle, so the film ends on its own drawn title, documented in FRICTIONAL.md).
  - Course repo: branch `final-project-film`, commit `8164179`, folder `fall-2026/aravind-s/final-project-film/` (film, brief, beat sheet, rebuild sources, fact check, gate reports, FRICTIONAL with his own entries, and the two evidence files). No Claude trailer or PR footer (his choice).
  - PR: https://github.com/nikbearbrown/info-7375-prompt-engineering-for-generative-ai/pull/13 (open; no checks reported yet).
  - **Submitted on Canvas by Aravind on 2026-10-09** (he reported it). Do not change PR #13 now unless the instructor asks: the GitHub folder must keep matching the Canvas zip.
  - Canvas zip, built from that commit: `~/Documents/03-School/info7375/submissions/final-project-film/Sundaravadivelu_Aravind_INFO7375_FinalProjectFilm.zip`; paste text in `CANVAS-PASTE.txt` beside it.
  - Build folder (gitignored): `~/Documents/03-School/info7375/info-7375-prompt-engineering-for-generative-ai/learning-artifacts/final-project-film/`.

## Needs Aravind

1. Read the first piece (`src/klaxon/checks/money.py`, `src/klaxon/patient/ledger.py`); `docs/STUDY-NOTES.md` explains the concepts. On 2026-10-09 you ran the tests yourself (91 passed) and both demos (16:46 and 16:47 PDT: naive off by +$69.68, idempotent passed; evidence `2026-10-09T234653Z_..._naive_seed7.json` and `2026-10-09T234752Z_..._idempotent_seed7.json`).
2. Week 1 decision: Docker Desktop (installer, GUI) or Colima (Homebrew, command line) as the container runtime.

## Next: week 1 (Oct 13-19), the patient system in Docker Compose

Goal: the payments system runs for real on this Mac, and the money check from the first piece passes against it.

1. Install the container runtime Aravind picks; confirm `docker compose version` and that the images run on arm64 (`docker manifest inspect <image>`).
2. `compose.yaml` with official images only: PostgreSQL, Redis, Kafka (`apache/kafka`, KRaft mode, no ZooKeeper), plus our Python services: the fake provider, the webhook receiver, the payments API and the ledger consumer.
3. The flow: create a payment through the API; the provider sends a signed webhook; the receiver verifies it and publishes to Kafka; the ledger consumer books it in PostgreSQL. Idempotency moves from the SQLite `processed_events` table to a database constraint plus Redis, as planned.
4. The money check runs against PostgreSQL through a read-only database role.
5. Tests: the unit tests stay offline and fast; one end-to-end test brings the stack up, sends traffic and asserts the money check passes. One command each (for example `make up`, `make test`, `make e2e`).
6. Update STATUS, DECISIONS and STUDY-NOTES at the end (week 1 concepts: Kafka topics, partitions, consumer groups and offsets; Compose networking; Redis as an idempotency store).

Course tie-in: CSYE 7374 on 10/14 covers an agent working in a real project, and this repo is that project.

## Open questions for Aravind (longer term)

- Keep the name Klaxon? (default yes)
- Make this repo public on GitHub, and when? (default: ask before pushing)
- Later: a few dollars of Anthropic API credit for the HolmesGPT comparison?
