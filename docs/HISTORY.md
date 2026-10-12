# History note

On 2026-10-11, before this repository was first pushed to GitHub, its history was rewritten to take out three things. Nothing else changed.

- **`docs/RESEARCH.md`, in every commit:** one line named a company Aravind is applying to and the folder where he keeps job postings. It now gives the same skill counts without either.
- **`docs/STATUS.md`, in the first commit only:** an open question that tied another GitHub user name to Aravind now asks only which public commit to cite.
- **`docs/DECISIONS.md`, from the commit that recorded it on:** the decision to keep the pitch film course-only no longer lists the film's drawing problems. The decision itself is unchanged.

**What did not change:** every other file, and every author, date and commit message. This was checked commit by commit against the original history (`git diff --name-only <before> <after>` lists only those files).

**Why the commit IDs differ:** changing a line in the first commit changes the ID of every commit after it. Docs, commit messages and evidence files written before the rewrite quote the old IDs; use the table below. The evidence files also record SHA-256 fingerprints of the code (`code_sha256`) and of the stack's files (`stack_files_sha256`), which do not depend on git and still match the code at the new IDs.

| Before | After | Commit |
|---|---|---|
| `801a0bf` | `2a793e5` | Add the first piece: a money check for a duplicate-webhook demo |
| `178a2d7` | `4918e61` | Update status after the pitch film was posted |
| `7c8e4a4` | `33cc1c4` | Record that the pitch film stays course-only |
| `5409c3d` | `9defba9` | Status: pitch submitted; plan week 1 |
| `23104b5` | `7c15c9e` | Status: Aravind ran the tests himself |
| `1131d39` | `c00b7c2` | Add Aravind's own run of the naive demo (2026-10-09) |
| `dfc4f0b` | `05b9b80` | Add Aravind's own run of the idempotent demo (2026-10-09) |
| `9a3c56b` | `36fe35d` | Study notes: the project explained simply, and why providers send duplicates |
| `009ef8c` | `d2e332b` | Study notes: where the idea applies, and real incidents |
| `938dc2a` | `1583145` | Add the four live patient services (not run yet) |
| `2cac077` | `ea595a1` | Run the patient in Docker Compose |
| `3c03971` | `a4004cc` | Harden the services after an independent review |
| `746d687` | `070fb42` | Run the money check against the live stack |
| `7e57cba` | `f70e30a` | Test the services offline, against PostgreSQL and Kafka, and end to end |
| `408670a` | `1758991` | Evidence: call a run clean when its code and stack files match the commit |
| `ce634fe` | `96b860b` | Add the mutation check: break the code on purpose, check the tests notice |
| `4ebf351` | `32a4679` | Record the live demo runs (2026-10-09, commit 408670a, seed 7) |
| `bba2168` | `01055e2` | Docs: week 1 status, decisions and study notes |
| `6869e21` | `4bb5568` | Add Aravind's own runs of the live demos (2026-10-11) |
| `3c381bc` | `4558cc9` | Docs: Aravind's week 1 review and his payment-confirmation decision |
