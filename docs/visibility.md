# Visibility — what may be written to this repo

The repo is **private**, but "private" does not mean "without consequence": it
is cloned on every host, and an authorized A2A peer can read it. Every fact
written here must pass this filter.

## Tiers

**Tier 1 — Safe to share (no limit)**
Technical facts, environment gotchas, conventions, paths, ports, project
names, architecture notes. This is ~90% of the repo.

**Tier 2 — Sensitive but necessary (write, stay sober)**
Client and partner names within commercial follow-up, current-case context.
Useful so the agent does not ask the same question twice.

**Tier 3 — Never written here**
- Secrets, tokens, API keys, passwords, `.env` content.
- Banking data, IBAN, card numbers.
- Personal details of third parties unrelated to the case (health, family,
  opinions). A first name and professional context is enough.
- Personal addresses and private phone numbers, unless already public.

## Where each tier applies

| Destination | Tiers allowed |
|---|---|
| `facts/<host>.jsonl` | 1 and 2 |
| `wiki/sessions/index-<host>.md` | 1 and 2 — **automatic scrub** of emails, phones, IBANs and tokens at generation time |
| `wiki/projects/` | 1 and 2 |
| Native memory (`MEMORY.md` / `USER.md`, never versioned, never synced) | 1, 2 and 3 |

The scrub rules live in `bin/session_index.py::_SCRUB_RULES`. If a pattern is
missing, **fix the script and regenerate** — hand-editing the index will be
overwritten by the next run.
