# Architecture — the four layers, and why they never merge

The core insight is not "store memory in git". It is that an agent's memory is
**four different things**, and every attempt to merge them fails in a specific,
measurable way. Keep them separate, and each one becomes simple.

| Layer | What it is | Lives in | Syncs? |
|---|---|---|---|
| Native memory | Compact always-injected facts (identity, preferences) | `<HERMES_HOME>/memories/` (`MEMORY.md`, `USER.md`) | **Never** — a merge conflict loses a fact silently |
| Facts (this repo) | What each host learned that others must know | `facts/<host>.jsonl` | Yes, via git |
| Wiki | Human-readable long-term memory: session index, decision journals | `wiki/` | Yes, via git |
| Transcripts | Full conversation text | `<HERMES_HOME>/state.db` (SQLite + FTS5) | **Never** — WAL through a file-sync = corruption |

Rule: **local memory stays small and points at the wiki.** A fact worth reading
once a month has no business inside a block injected on every turn.

## The fact layer: append-only shards

```
facts/
├── laptop.jsonl     # written only by the laptop
└── desktop.jsonl    # written only by the desktop
```

Each line is a self-contained JSON object:

```json
{"id":"laptop-1758628800-1234","ts":"2026-09-24T06:00:00+00:00","host":"laptop",
 "topic":"infra","fact":"...","tags":["gotcha"],"source":null,
 "supersedes":"laptop-1758628000-987"}
```

Properties, each load-bearing:

- **One writer per file.** Two hosts never write the same file, so a git merge
  conflict is structurally impossible — not "handled", impossible.
- **Append-only.** Correcting a fact means adding a line that supersedes it
  (`supersedes` field). The history of what you believed, and when, is kept.
- **Pull --rebase, push.** On every sync and at session start. Nothing to
  merge line-by-line, because no two writers ever touch the same line.

## The wiki layer: built for humans

The facts answer "what does the other host know". The wiki answers "what did we
do last week, on which machine, and why":

- `wiki/sessions/index-<host>.md` — one line per conversation, generated from
  the local `state.db` by `bin/session_index.py`, then **merged** with the
  existing index: a session purged by retention stays in the index. The index
  outlives the database that produced it. One file per host — no conflicts.
- `wiki/projects/<project>.md` — dated decision entries: decision, why, what
  would replace it, what was rejected. Written by the agent, while the reason
  is still in context.
- `wiki/visibility.md` — what may be written here, what never may.

The session index is a **derived artifact**: what travels is the index, never
the database. That is the whole trick.

## The injection layer: first turn, bounded

At session start a plugin (`plugin/brain-boot/`) syncs the repo in the
background and injects **the facts of the other hosts** into the first turn:

- **First turn only.** Injecting at every turn would grow the prompt and break
  the prefix cache on every message.
- **Bounded**: 12 facts / 2,500 characters by default. A fact that matters
  often belongs in native memory (small, local); the block is for what is
  *missing* locally.
- **Own host excluded.** What this host wrote is already in its local memory;
  the missing information is what the *other* hosts learned.
- **Fail-open.** Missing repo, broken git, corrupt JSONL: the session runs
  normally. A memory plugin must never take the agent down.

Why a plugin and not a shell hook: `on_session_start` ignores its return value,
so a hook can refresh files but cannot inject a single fact into the model's
context. `pre_llm_call` is the only injection channel — hence a plugin.

## Freshness: the safety net

- A nightly cron (`--no-agent`, zero LLM cost) regenerates this host's session
  index and pushes; it fails loudly if the push did not happen
  (`HEAD == origin/main`).
- A weekly cron scans recent sessions for decisions that were never journaled
  and signals the gap. It **signals, it does not write**: keyword-extracting
  decisions from an agent's prose produces ~75 candidates/day — unusable.
  Writing entries stays the agent's job, the only one holding the *why* in
  context.
