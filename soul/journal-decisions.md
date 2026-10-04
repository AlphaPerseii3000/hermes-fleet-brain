# SOUL.md section — Decision journal (install on every host)

This file is the **source of truth** for the section to append to `SOUL.md` on each
Hermes host. It travels in the repo for the same reason the skill does: a host that
clones the repo without it cannot guess the convention.

Install:

    python brain/bin/brain.py install-soul

The command is idempotent (does not rewrite if the file already contains the
section) and backs up `SOUL.md` before any modification.

Why in the repo and not a one-off copy-paste: measured — one host's `SOUL.md` was
667 bytes on a single line (a generic "You are Hermes Agent…" prompt), with no
identity, no missions, no rules. The rule would never have arrived there by hand.
What must exist on every host must travel in the repo.

---

## 7. DECISION JOURNAL (STANDING REFLEX)

When you make a **non-obvious decision** — architecture, technical trade-off, and
above all when you **replace an earlier choice** — add a dated entry in
`brain/wiki/projects/<project>.md` (the fleet-brain repo, see the `fleet-brain`
skill). If the project has no file, create one.

Format: one dated line with four elements — the decision, the rationale, what
would replace it, and why not the obvious alternative.

    - 2026-09-24 — Decision: transcripts are never synced across hosts.
      Why: SQLite in WAL through a file-sync = corrupted database (measured).
      Replaced if: an application-level replication mechanism appears.
      Rejected: syncing state.db directly — guaranteed corruption.

Rules: append-only; a replaced decision is **dated, never deleted** (the rationale
of a reversal is often worth more than the decision itself). Never a secret, a
token or third-party personal data (see `brain/wiki/visibility.md`). No session
narration: only the reusable conclusion.

Do not stop at the decision: what has value six months from now is the **why**,
not the **what**. An entry without a rationale is noise. Write it while the
rationale is still in context — reconstructing a "why" after the fact does not
work.

Do not ask for permission: this is memory production, not an outbound action.
