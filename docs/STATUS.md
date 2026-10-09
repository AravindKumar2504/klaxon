# Status

Last updated: 2026-10-09, 16:25 PDT (pitch-weekend session 1).

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
  - Canvas zip, built from that commit: `~/Documents/03-School/info7375/submissions/final-project-film/Sundaravadivelu_Aravind_INFO7375_FinalProjectFilm.zip`; paste text in `CANVAS-PASTE.txt` beside it.
  - Build folder (gitignored): `~/Documents/03-School/info7375/info-7375-prompt-engineering-for-generative-ai/learning-artifacts/final-project-film/`.

## Needs Aravind

1. Upload the Canvas zip yourself before Mon 2026-10-12, 11:59 pm (treat as Eastern = 8:59 pm Pacific), and paste `CANVAS-PASTE.txt` into the submission comment.
2. Read the first piece (`src/klaxon/checks/money.py`, `src/klaxon/patient/ledger.py`) and run the tests and both demo commands yourself; `docs/STUDY-NOTES.md` has the concepts.

## Next

1. If the PR gets review comments or a failing check, fix them on the same branch (the Canvas zip must then be rebuilt from the new commit).
2. Week 1 (Oct 13-19): install Docker (Docker Desktop or Colima: Aravind's call); the four services, fake provider, Postgres, Redis and Kafka in Compose; tests for the normal flow.

## Open questions for Aravind (longer term)

- Keep the name Klaxon? (default yes)
- Make this repo public on GitHub, and when? (default: ask before pushing)
- Later: a few dollars of Anthropic API credit for the HolmesGPT comparison? Docker Desktop or Colima for week 1?
