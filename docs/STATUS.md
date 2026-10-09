# Status

Last updated: 2026-10-09, 16:10 PDT (pitch-weekend session 1).

## Done

- Project chosen and planned (see `PLAN.md`, `DECISIONS.md`, `RESEARCH.md`).
- **First piece built** (not committed yet: waiting for Aravind's OK). Synthetic, seeded, in-process demo of the money check: fake provider with HMAC-signed webhooks, double-entry ledger with naive and idempotent handlers, the money check (three invariants with evidence), a dashboard view, and `python -m klaxon.demo duplicate-webhook --handler naive|idempotent`.
  - Tests: 91 passed (`.venv/bin/python -m pytest -q`, 2026-10-09). 21 deliberate code breaks, all caught by the tests.
  - An independent Claude review found no wrong behavior; its gaps were fixed (see `DECISIONS.md`, 2026-10-09 evening).
  - Real runs, 2026-10-09 15:54 PDT, seed 7, code SHA-256 `5cc69830a8d7...`: naive = 7 requests, 0 errors, dashboard GREEN, ledger balanced, money check FAILED (pay_0003 posted twice, off by +$69.68); idempotent = PASSED. Files: `evidence/first-piece/2026-10-09T225438Z_*.json`.
  - **Do not edit `src/klaxon` without re-running both demo commands**: the evidence records the code's SHA-256.
- **Pitch film rendered** (review master, not submitted): `~/Documents/03-School/info7375/info-7375-prompt-engineering-for-generative-ai/learning-artifacts/final-project-film/final/klaxon-pitch.mp4`, 3:45, 1080p, SHA-256 `9c4ce144...`. Gates: F, fact-check, beat lint, A, W, B (strict), V (0 blockers), T (pass), loudness (pass). The toolkit's bookend check reports the outro on purpose (its outro card hard-codes the instructor's handle).
  - Build folder files: `make_sheet.py` (narration), `scene_body.py` (scenes), `FACTCHECK.md`, `SHOTLIST.md`, `PROMPTS.md`, `BUILD-PROMPT.md`, `SOURCES.md`, `PROJECT-BRIEF.md`, `FRICTIONAL.md` (Claude's log; Aravind's entries empty).

## Needs Aravind (before the final render and submission)

1. Open https://github.com/HolmesGPT/holmesgpt and https://holmesgpt.dev/operator/deployment-verification/ yourself; the film says "The real alternatives I looked at" and "In its docs, I didn't find a built-in check that the money is right".
2. Confirm or rewrite "why this project" (beat B15; brief question 7).
3. Optional public evidence: which public EventEase commit to cite for the Stripe webhook work?
4. Approve the first commit in this repo (local only).
5. Watch the film; approve the narration.
6. Write your own FRICTIONAL.md entries.
7. Submission: course-repo commit trailer (Week 1 had a Claude co-author line; the Reallocation Engine repo did not); approve the push and the PR; upload the Canvas zip yourself.

## Next

1. Apply Aravind's answers: edit `make_sheet.py`, re-voice only the changed beats (`generate_audio_kokoro.py --only`), `finish_audio.py`, `align.py`, move the changed clips to `_superseded/`, `art run`, then `art final`. Never run `make_sheet.py` while a render is running.
2. Assemble the submission: branch from `upstream/main` in the course repo, folder `fall-2026/aravind-s/final-project-film/` (README, mp4 force-added, PROJECT-BRIEF, beat_sheet.json, BUILD-PROMPT, SOURCES, FRICTIONAL, gate reports, rebuild scripts), the Canvas zip `Sundaravadivelu_Aravind_INFO7375_FinalProjectFilm.zip`, and a paste note with the folder link and commit hash. Push and open the PR only after Aravind says yes.
3. Week 1 (Oct 13-19): install Docker; the four services, fake provider, Postgres, Redis and Kafka in Compose.

## Open questions for Aravind (longer term)

- Keep the name Klaxon? (default yes)
- Make this repo public on GitHub, and when? (default: ask before pushing)
- Later: a few dollars of Anthropic API credit for the HolmesGPT comparison? Docker Desktop or Colima for week 1?
