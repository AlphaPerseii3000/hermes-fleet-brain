---
name: fleet-brain
description: "Use when sharing agent memory across machines via git. Append-only fact shards, first-turn injection, human-readable wiki and decision journals — no database ever synced."
version: 1.0
author: AlphaPerseii3000
license: MIT
tags: [hermes, memory, git, multi-host, cron]
metadata:
  hermes:
    tags: [hermes, memory, git, multi-host, cron]
    category: productivity
---

# Fleet Brain — shared memory between hosts (git)

## When to use

Triggers: "shared brain", "shared memory", "fleet brain", "what does the other
machine know". Start of session: fetch what was learned elsewhere (`boot`). A fact
learned here must be known by another host. Before claiming you know nothing about
a topic: maybe another host recorded it.

This is NOT a hosted memory service and it does NOT replace local memory. It is a
small git repo of derived, append-only memory that several Hermes hosts share.

## The four layers (never merge them)

| Layer | Role | Location | Syncs? |
|---|---|---|---|
| Native memory (`MEMORY.md` / `USER.md`) | Compact facts **always injected** into every prompt | `<HERMES_HOME>/memories/` | **Never** — a conflict loses a fact silently |
| **Facts** | What a host learned that others must know | `brain/facts/<host>.jsonl` | Yes, via git |
| **Wiki** | Long-term memory **readable by a human**: session index, decision journals | `brain/wiki/` | Yes, via git |
| Transcripts | Full conversation text | `<HERMES_HOME>/state.db` (SQLite + FTS5) | **Never** — WAL through a file-sync = corruption |

Rule: **local memory stays small and points at the wiki.** A fact worth reading
once a month has no business in a block injected on every turn.

## Facts: append-only shards, one writer per file

    brain/facts/<host>.jsonl    # written ONLY by that host

Each line is a self-contained JSON object:

    {"id":"<host>-<epoch>-<pid>","ts":"<ISO8601 UTC>","host":"<host>",
     "topic":"<topic>","fact":"<the fact>","tags":["a","b"],"source":"<url|null>"}

- **Append-only, never edit an existing line.** Correcting a fact = adding a new
  line with `"supersedes": "<id>"` (or a list of ids). A replaced fact is dated,
  never erased — the history of what you believed is the asset.
- Two hosts never write the same file ⇒ a git conflict is *structurally*
  impossible. That is the whole trick; do not "improve" it by merging shards.

## Usage

    python brain/bin/brain.py boot              # session start: pull (+ push if ahead)
    python brain/bin/brain.py add "the fact" --topic x --tags a,b --sync
    python brain/bin/brain.py list [--host H] [--topic T] [--tags a,b]
    python brain/bin/brain.py search "pattern"  # case-insensitive over all facts
    python brain/bin/brain.py stats             # must list MULTIPLE hosts
    python brain/bin/brain.py wiki-index --sync # human-readable session index
    python brain/bin/brain.py install-skill | install-soul | install-plugin | install-cron

Config: `BRAIN_DIR` (repo root; default `<HERMES_HOME>/brain`), `BRAIN_HOST`
(attribution name; default machine name), `BRAIN_BRANCH` (default `main`),
`BRAIN_AUTO_SYNC=1` (make `add` sync automatically).

## The wiki: memory a human can open

- `wiki/sessions/index-<host>.md` — one line per conversation, with
  `@session:<profile>/<id>` to reopen it. Regenerated from the local `state.db`,
  then **merged** with what is already there: a session purged by
  `sessions.retention_days` stays in the index. The index outlives the database.
- `wiki/projects/<project>.md` — dated decision entries. Format: decision,
  rationale, what would replace it, rejected alternative. Written **while the
  rationale is still in context**; reconstructing a "why" after the fact fails.
- `wiki/visibility.md` — what may be written, tiered. Secrets never.

## Injection: the plugin, not a shell hook

`plugin/brain-boot/` runs at session start: background sync, then first-turn
injection of the OTHER hosts' latest facts (bounded: 12 facts / 2,500 chars,
own host excluded, fail-open). Why a plugin: `on_session_start` ignores its return
value, so a hook can refresh files but cannot inject one fact into the model's
context — `pre_llm_call` is the only channel. Config under
`plugins.entries.brain-boot.settings`. `plugins.enabled` is an allow-list:
`hermes plugins enable brain-boot` is a separate, explicit step.

## Safety nets (cron, `--no-agent`, zero LLM cost)

- **Nightly** (`brain_wiki_index.py`): regenerate this host's index and push;
  fails loudly if `HEAD != origin/main`.
- **Weekly** (`brain_decisions_gap.py` → `wiki_decisions_gap.py`): find sessions
  with a decision signal and check the journal covers them. It **signals, it does
  not write** — keyword-extraction from agent prose gives ~75 candidates/day,
  unusable. Writing entries stays the agent's job.

**One scheduler per host.** Jobs do not share: create the cron jobs on EACH host.
Only the git repo is shared. Fixing a script in the repo is not enough —
`install-cron` AND `hermes cron edit <job_id> --script <name>.py` on each host.

## Pitfalls (each one measured the hard way)

- **Job scripts must be `.py`, never `.sh`.** A `.sh` is launched via
  `shutil.which("bash")`; on a Windows host where WSL is installed,
  `C:\WINDOWS\system32\bash.exe` wins over Git Bash, reads the Windows path as a
  POSIX path and exits 127 — every night, silently, while the same script works by
  hand. Diagnosis: `hermes cron runs <id>` shows `source=builtin` exit 127 while
  `source=direct` passes.
- **Network git under cron hangs on git 2.53's credential selector** unless every
  call carries `-c credential.helper= -c credential.helper=manager`, plus a
  bounded timeout. A hang must appear in seconds, not kill the job an hour later.
- **An empty remote** makes `git pull --rebase origin HEAD` fail
  ("couldn't find remote ref HEAD") — test with `ls-remote --heads` before pulling.
- **A clone of an empty repo lands on `master`, not `main`.** Each host then
  pushes its own branch and never sees the other's facts — silently. The script
  forces `-M main` on every sync. Symptom: `stats` lists ONE host.
- **`HERMES_HOME` set ignores the `cd`** — test with `--dir` in a sandbox, and the
  default path is `<HERMES_HOME>/brain`; a repo cloned elsewhere reads an empty
  brain with no error (`boot` says "0 facts").
- **Never forge the `host` field.** It is the writer's attribution; a forged value
  corrupts trust in every consumer.
- **Scrub before writing, never after.** Emails / phones / IBAN / tokens are
  masked by `bin/session_index.py::_SCRUB_RULES` at generation time. Fix the
  *script* if a pattern is missing — hand-editing the index gets overwritten at
  the next run.
- **Never build the CLI line by concatenating strings** — the shell eats inner
  quotes and a truncated fact syncs silently. Use proper quoting (`shlex.quote`
  in Python, single quotes in shell). No backticks in a fact passed on the command
  line either.
- **Never file-sync ANY state database.** `memory_store.db`, `state.db`: SQLite in
  WAL through a file-sync = corruption (measured, twice). What crosses machines is
  the derived artifact (index, facts), never the database.

## Recording what matters ("shared brain")

1. `boot` first — do not re-record what another host already knows.
2. Extract **durable, reusable** facts; a good fact answers "what will save me
   time next time". Not a session recap.
3. One fact per line, with `--topic` and `--tags`.
4. Write from YOUR host under YOUR host name. Never forge `host`.
5. `--sync` to push.
6. Never record: secrets, passwords, tokens, `.env` content, third-party personal
   data. The repo is private but readable by every host that clones it.

## Verify it really works

    python brain/bin/brain.py stats

Must list **several hosts**. One host = pushes do not converge (see the
`master`/`main` pitfall). For independent proof, clone the repo fresh into a new
directory and run `boot`: the other hosts' facts must appear.
