# Fleet Brain — shared memory for a fleet of Hermes agents

![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)
![Python: stdlib only](https://img.shields.io/badge/Python-3.9%2B%20stdlib-green.svg)
![No databases](https://img.shields.io/badge/state-never%20synced-red.svg)

**One git repo. Several machines. Your agents remember together — and you can read what they know.**

Two hosts running [Hermes Agent](https://github.com/NousResearch/hermes-agent) learn different things. Fleet Brain makes what each one learns available to the other — without syncing a single database, and without a merge conflict ever crossing your path.

> Facts are append-only **per host**, so a git conflict is structurally impossible — not handled, impossible. Session transcripts stay local. The agent's memory store is never file-synced (SQLite in WAL through a file-sync corrupts it — measured, twice). What travels is facts, and a human-readable wiki the agent maintains for you.

---

## The problem

You run an agent on two machines (a laptop and a desktop, a work PC and a home server). Each is smart. Together, they are two strangers:

1. **Native memory never syncs.** `MEMORY.md` is injected into every prompt, so a merge conflict loses a fact *silently*. Every sync tool that touches it is a data-loss bug waiting for a bad day.
2. **State databases never sync either.** `state.db` / `memory_store.db` are SQLite + WAL. Put them through any file-sync and you get corruption, not replication.
3. **So each host forgets what the other learned.** You fix a nasty gotcha on the laptop. On the desktop, the same bug bites again next week.

Fleet Brain is the middle path: a small git repo that carries **derived, append-only, readable** memory. Nothing else.

## The design in one picture

```
        laptop (Hermes)                          desktop (Hermes)
   ┌────────────────────┐                  ┌────────────────────┐
   │ MEMORY.md  (local) │                  │ MEMORY.md  (local) │   never synced
   │ state.db   (local) │                  │ state.db   (local) │   never synced
   └─────────┬──────────┘                  └─────────┬──────────┘
             │  add facts                                    │  add facts
             ▼                                               ▼
   ┌──────────────────────────────────────────────────────────────┐
   │  brain repo (private, git)                                   │
   │  facts/laptop.jsonl   ← only the laptop writes this          │
   │  facts/desktop.jsonl  ← only the desktop writes this         │
   │  wiki/sessions/index-laptop.md   ← one file per host         │
   │  wiki/projects/<project>.md      ← decision journals         │
   └──────────────────────────────────────────────────────────────┘
             │                        │
             │  session start: pull --rebase + push (background)
             │  first turn: inject the OTHER host's latest facts (bounded)
             ▼                        ▼
        laptop prompt            desktop prompt
```

## Quick start

```bash
# 1. Clone INTO your Hermes home — the default path the scripts expect
git clone https://github.com/AlphaPerseii3000/hermes-fleet-brain.git "$HERMES_HOME/brain"
cd "$HERMES_HOME/brain"

# 2. First sync. Must report facts (even 0) WITHOUT --dir:
python bin/brain.py boot

# 3. Make the convention known to your agent (skill + SOUL rule):
python bin/brain.py install-skill
python bin/brain.py install-soul

# 4. Auto-load at session start (one plugin per machine):
python bin/brain.py install-plugin
hermes plugins enable brain-boot
hermes plugins validate "$HERMES_HOME/plugins/brain-boot"

# 5. Optional: nightly index + weekly decision safety net
python bin/brain.py install-cron     # prints the two `hermes cron create` lines
```

Then, **on each machine** (one scheduler per host — jobs do not share across hosts):

```bash
python bin/brain.py add "The nightly index script must be .py, never .sh: WSL bash eats the Windows path (exit 127)." \
  --topic infra --tags gotcha --sync
python bin/brain.py stats            # must list MORE THAN ONE host
```

`stats` listing a single host means the pushes do not converge — see the `master`/`main` gotcha below.

## What travels, what never does

| Layer | What it is | Lives in | Crosses machines? |
|---|---|---|---|
| Native memory | Identity, preferences — injected into every prompt | `<HERMES_HOME>/memories/` | **Never** — a merge loses a fact silently |
| Facts | What each host learned that the others need | `facts/<host>.jsonl` (this repo) | Yes — append-only |
| Wiki | Human-readable long-term memory: session index, decision journals | `wiki/` (this repo) | Yes |
| Transcripts | Full conversation text | `<HERMES_HOME>/state.db` (SQLite) | **Never** — WAL + file-sync = corruption |

## The four layers

See [`docs/architecture.md`](docs/architecture.md) for the full doctrine — why each layer exists, why they never merge, and every property of the fact format. Short version:

- **Facts** (`facts/<host>.jsonl`): one writer per file → conflicts structurally impossible. Append-only; correcting a fact means adding a line that supersedes it (`supersedes` field). The history of what you believed, and when, is kept.
- **Wiki** (`wiki/`): built for humans. `sessions/index-<host>.md` — one line per conversation, regenerated from the local `state.db` then **merged**, so a session purged by retention stays in the index. `projects/<project>.md` — dated decision entries: decision, rationale, replacement condition, rejected alternative. Written while the rationale is still in context.
- **Injection** (`plugin/brain-boot/`): at session start, sync in the background, then inject the *other* hosts' latest facts into the **first turn only** (default: 12 facts / 2,500 chars). First turn only, because injecting at every turn grows the prompt and breaks the prefix cache. Fail-open everywhere: a missing repo or broken git never takes the agent down.
- **Safety nets** (two cron jobs, `--no-agent`, zero LLM cost): nightly session-index refresh that fails loudly if the push didn't happen (`HEAD == origin/main`); weekly scan for decisions that were never journaled. The weekly job **signals, it does not write** — keyword-extracting decisions from agent prose yields ~75 candidates/day, unusable. Writing stays the agent's job.

## The gotchas we paid for

Each of these cost a real debugging session. They ship solved in the code:

| Gotcha | Symptom | Fix in this repo |
|---|---|---|
| Cron scripts in `.sh`, WSL installed | Nightly job fails `exit 127` every night while working fine by hand (WSL bash eats the Windows path) | Job scripts are `.py` — no shell to resolve |
| Git 2.53 credential selector, under cron | **Any** network git hangs forever (fetch went 1.8 s → ∞) | `-c credential.helper= -c credential.helper=manager` on every git call + bounded timeouts |
| Cloning an EMPTY repo | Lands on `master` while the other host pushes `main` → both hosts silently see only their own facts (`stats` shows one host) | `git branch -M main` forced on every sync |
| `~/.hermes` vs `HERMES_HOME` on Windows | Repo read from an empty directory, with no error | `get_hermes_home()` everywhere, never the literal path |
| Windows Task Scheduler + `subprocess` | `OSError WinError 6: invalid handle` kills the job mid-write | `stdin=DEVNULL` on every subprocess call |
| Two hosts writing the same working file | Git conflict on every cron run | One file per host, always (`_to-complete-<host>.md`, `index-<host>.md`) |
| Secrets pasted into a session title | Leaks into the committed index | Scrub (emails / phones / IBAN / tokens) **before** writing — fix the script, never the file |
| A fact attributed to the wrong host | Corrupts trust in the whole store | `host` field is set by the writer, never forged |

## Non-goals

- **No RAG, no embeddings, no vector DB.** Facts and wiki pages are read directly. (Upgrading `brain.py search` to SQLite FTS5 is on the roadmap — the index will stay git-ignored and cron-rebuilt, consistent with the never-sync-a-DB rule.)
- **No transcript sync.** Ever. That is the point.
- **No daemon, no server.** Everything is a stdlib Python script + git + your agent.
- **Not multi-user.** One human, a few of *your* machines, one private repo.

## Repo layout

```
bin/brain.py            # the CLI: add / boot / sync / list / search / stats / wiki-index
bin/session_index.py    # builds the human-readable session index (merges, scrubs)
plugin/brain-boot/      # Hermes plugin: background sync + first-turn injection
scripts/                # the two cron jobs (Python, not shell)
skill/SKILL.md          # the convention, installed by `install-skill`
soul/journal-decisions.md  # the standing rule, installed by `install-soul`
docs/                   # architecture + visibility doctrine
examples/               # what a fact and a decision entry look like
tests/test_smoke.py     # end-to-end smoke test (stdlib only)
```

## Verification

```bash
python tests/test_smoke.py     # exercises init/add/supersede/install-*/scrub end to end
python bin/brain.py stats      # on a live fleet: must list MORE THAN ONE host
```

## Credits

Built to run on [Hermes Agent](https://github.com/NousResearch/hermes-agent) by Nous Research. The append-only-shards-over-git pattern, the "compile once, read often" wiki, and the decision-journal discipline were developed together with the agent this repo runs on — every gotcha above is a real, dated measurement kept in the very files this system manages.

## License

MIT — use it, fork it, adapt it.
