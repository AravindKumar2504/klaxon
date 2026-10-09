# Decision log

Newest at the bottom. Each entry: date, decision, who decided, why.

## 2026-10-09

- **Project: Klaxon becomes an AI on-call engineer for a payments system** (Aravind). It replaces the earlier code-review design. Why: AI code review is crowded (Claude Code's Code Review already does multi-agent review with verification); he wanted a known-good idea at small scale that covers in-demand skills and satisfies both course finals. See `RESEARCH.md`.
- **Full build by mid-December, then keep going** (Aravind). He commits 1-1.5 hours a day and works on other things while Claude builds. Cut order if behind: Terraform, then Go, then the web panel; keep Kubernetes.
- **Rejected alternatives** (Aravind, on Claude's research): "Faultline" PyPI breaking-change prover (too niche), payment-exceptions agent (fewer infra skills), least-privilege for agents (research-hard), agent fault injection (already taken).
- **Model choice for building** (Claude's advice, Aravind agreed): `opusplan` by default; full Opus for design, hard debugging, safety code, eval design, milestone reviews. Klaxon's own runtime model for bulk evals: Sonnet or Haiku, with a smaller Opus comparison.
- **Defaults chosen by Claude, open to Aravind's override:** keep the name Klaxon despite a UK incident-notification product with the same name; synthetic (Kokoro) narration for the pitch film, disclosed on screen; code stays on his Mac until he approves a public push.
- **Shared memory across folders** (Aravind approved): `autoMemoryDirectory` in `~/.claude/settings.json` points every session at the memory folder under `~/.claude/projects/-Users-aravinds-Downloads/memory`.
- **Repo location** (Claude's default): `~/Documents/03-School/klaxon`, git identity = his GitHub noreply email, no Claude co-author trailer (his standing preference).

## 2026-10-09 (pitch-weekend session, evening)

- **First piece built to the STATUS spec, plus review fixes** (Claude; Aravind to review before the first commit). An independent Claude review found no wrong behavior but real gaps. Fixed: the dedupe row and the posting were already one transaction but no test proved it (now tested by failing the posting mid-transaction); transactions are explicit (`autocommit=True` plus `BEGIN IMMEDIATE`/`COMMIT`), so the SQLite default changing in a future Python cannot silently break atomicity or foreign keys; dedupe uses `ON CONFLICT (event_id) DO NOTHING`, so only a repeated event id counts as "seen before"; STRICT tables refuse fractional cents.
- **Ledger rows are never edited or deleted; a wrong posting is repaired by a reversal** (Claude, standard accounting practice). The money check counts a reversed posting as cancelled, so the check can pass after a proper repair. This is how Klaxon's future fix-and-verify step will repair a double posting.
- **Wording: "posted twice", not "double charge", for the first piece** (Claude, for accuracy). A duplicate webhook makes our books count a payment twice; the provider charged the customer once. A true double charge (a retried charge request without an idempotency key) is a separate fault class for later.
- **Single currency (USD).** A webhook in another currency is refused with a 400 instead of being booked as dollars.
- **Evidence is tied to the code by a SHA-256 of `src/klaxon`**, so a run is traceable even before the first commit. The demo's exit code mirrors the money check (0 pass, 1 fail).
- **Pitch film alternatives: HolmesGPT and the manual process** (Claude's draft; Aravind must confirm he looked at them). K8sGPT was dropped from the film to keep only claims he can defend. HolmesGPT's own deployment verification compares "error rates, latency, restarts, and logs" (its docs, read 2026-10-09); the film says that, and says "in its docs I didn't find a built-in money check", never "it has none".
- **Film outro is a drawn Manim title, not the toolkit's ClaudeTitleOutro** (Claude). That component hard-codes the instructor's handle, which the assignment forbids. The toolkit's separate bookend check will report this; the reason is recorded in the film's FRICTIONAL.md.
