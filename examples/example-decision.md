# Example — a project decision journal

One file per project under `wiki/projects/`. Append-only. A replaced decision is
**dated, never deleted**: six months from now, the rationale of a reversal is
worth more than the decision itself.

Format — one dated entry, four elements:

    - YYYY-MM-DD — Decision: <what was decided>.
      Why: <the reason, measured if possible>.
      Replaced if: <the condition that would change this decision>.
      Rejected: <the obvious alternative, and why not>.

---

- 2026-09-24 — Decision: transcripts are never synced across hosts.
  Why: SQLite in WAL mode through a file-sync = corrupted database (measured).
  Replaced if: an application-level replication mechanism appears.
  Rejected: syncing state.db directly — guaranteed corruption.

- 2026-10-01 — Decision: facts are written to one append-only file per host.
  Why: two writers never touch the same file, so a git conflict is structurally
  impossible instead of being resolved.
  Replaced if: fact volume outgrows one file per host (years away).
  Rejected: a single shared facts file — merge conflicts on every sync.

Write the entry **while the reason is still in context**. Reconstructing a
"why" after the fact does not work, and an entry without a why is just noise.
