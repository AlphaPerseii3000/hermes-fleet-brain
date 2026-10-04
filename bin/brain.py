#!/usr/bin/env python3
"""brain — shared memory between Hermes hosts via git (append-only).

WHY THIS DESIGN
  Two Hermes hosts (say, a laptop and a desktop) want to share facts without
  ever touching memory_store.db (SQLite in WAL mode, corrupted by a file-sync)
  or MEMORY.md (injected into every prompt: a merge conflict silently loses
  a fact).

  Each host writes to ITS OWN file facts/<host>.jsonl, APPEND-ONLY.
  Two hosts never write the same file -> a git conflict is essentially
  impossible (the pathological case is two simultaneous pushes; pull
  --rebase resolves it). Conflicts are structurally avoided, not handled.

USAGE
  brain.py add "the fact"  [--tags a,b] [--topic x] [--source url]
  brain.py boot                # pull at session start (+ push if local is ahead)
  brain.py sync                # pull --rebase then push
  brain.py list   [--host H] [--topic T] [--tags a,b]
  brain.py search "pattern"    # case-insensitive search across all facts
  brain.py stats               # how many facts per host
  brain.py wiki-index [--sync] # human-readable session index (wiki/sessions/index-<host>.md)

  brain.py add ... --sync      # add + sync in one command

CONFIG
  BRAIN_DIR   : repo root (default: <HERMES_HOME>/brain)
  BRAIN_HOST  : host name used for attribution (default: machine name)
  BRAIN_AUTO_SYNC : "1" to make add run a sync automatically

The repo is a plain git repository. `origin` is a PRIVATE remote (GitHub or
a bare local path). Without an origin, local-only commands keep working.
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_DIR = Path(os.environ.get("BRAIN_DIR") or Path(os.environ.get("HERMES_HOME", ".")) / "brain")


def host_name() -> str:
    forced = os.environ.get("BRAIN_HOST")
    if forced:
        return re.sub(r"[^A-Za-z0-9_.-]", "_", forced)
    return re.sub(r"[^A-Za-z0-9_.-]", "_", platform.node() or "unknown")


def run(args, cwd, check=False, quiet=True):
    # stdin=DEVNULL is mandatory: under the Windows Task Scheduler (the Hermes
    # cron), the process has no console, GetStdHandle(STD_INPUT_HANDLE) is
    # invalid and subprocess.run raises OSError WinError 6 "invalid handle" —
    # the nightly brain_wiki_index job failed every night, *after* having
    # written the index.
    #
    # Git 2.53's credential helper SELECTOR must be bypassed:
    # credential.helperselector.selected is never set (nobody answered the
    # interactive prompt), so `git credential-helper-selector get` opens a
    # prompt with no console and blocks forever. Measured: `git ls-remote` to
    # GitHub went from 1.8 s to an infinite hang, so EVERY brain fetch/push
    # hung. We go explicitly through git-credential-manager, which already
    # caches the token.
    env = dict(os.environ)
    env["GCM_INTERACTIVE"] = "never"
    env["GIT_TERMINAL_PROMPT"] = "0"
    argv = list(args)
    # Callers already pass "git" first; the options must be inserted AFTER it,
    # or we get "git -c ... git remote" (silent failure, rc != 0 and empty stdout).
    if argv and os.path.basename(argv[0]) in {"git", "git.exe"}:
        argv[1:1] = ["-c", "credential.helper=", "-c", "credential.helper=manager"]
    r = subprocess.run(argv, cwd=str(cwd), capture_output=True, text=True,
                       stdin=subprocess.DEVNULL, env=env)
    if r.returncode != 0 and not quiet:
        print(f"  $ {' '.join(args)}\n  {r.stderr.strip()[:400]}")
    if check and r.returncode != 0:
        raise SystemExit(f"FAILED: {' '.join(args)}\n{r.stderr.strip()[:600]}")
    return r


def facts_dir(root: Path) -> Path:
    return root / "facts"


def ensure_layout(root: Path) -> None:
    facts_dir(root).mkdir(parents=True, exist_ok=True)
    readme = root / "README.md"
    if not readme.exists():
        readme.write_text(
            "# brain — shared memory between Hermes hosts\n\n"
            "Shared facts, append-only, one file per host: `facts/<host>.jsonl`\n\n"
            "Each line is a self-contained JSON object:\n\n"
            "```json\n"
            '{"id":"<host>-<ts>-<n>","ts":"<ISO8601 UTC>","host":"<host>",'
            '"topic":"<topic>","fact":"<the fact>","tags":["a","b"],"source":"<url|null>"}\n'
            "```\n\n"
            "Rule: **append-only, never edit an existing line**. Correcting a\n"
            "fact means adding a new line that supersedes it (`supersedes`\n"
            "field). That is what makes git merges conflict-free between hosts.\n\n"
            "NEVER put here: secrets, passwords, tokens, memory_store.db,\n"
            "MEMORY.md. These facts are readable by any authorized A2A peer.\n\n"
            "Usage: `python bin/brain.py --help`\n",
            encoding="utf-8",
        )
    gi = root / ".gitignore"
    if not gi.exists():
        gi.write_text("*.bak\n*.tmp\n__pycache__/\n", encoding="utf-8")


def _hermes_home() -> Optional[str]:
    """HERMES_HOME resolution: env first, then the platform default."""
    home = os.environ.get("HERMES_HOME") or str(Path(os.environ.get("LOCALAPPDATA", ".")) / "hermes")
    return home if os.path.isdir(home) else None


def cmd_install_plugin(a) -> int:
    """Install (or update) the brain-boot plugin in the local Hermes home.

    Why the plugin travels here: the skill alone is not enough. A skill is
    loaded by the MODEL, and only when the topic comes up — it cannot run code
    at startup. `brain.py boot` must run at every new session AND its result
    must enter the context: only a plugin hooked on `pre_llm_call` can do that
    (on_session_start ignores its return value).

    Idempotent; backs up a differing version before overwriting.
    Does NOT modify plugins.enabled: enabling stays an explicit gesture
    (`hermes plugins enable brain-boot`), so code is never enabled silently.
    """
    root = Path(a.dir)
    src_dir = root / "plugin" / "brain-boot"
    if not (src_dir / "plugin.yaml").is_file() or not (src_dir / "__init__.py").is_file():
        print(f"no plugin in the repo ({src_dir}) — nothing to install.")
        print("(the repo must contain plugin/brain-boot/{plugin.yaml,__init__.py}; run `sync` first)")
        return 1

    home = _hermes_home()
    if home is None:
        print("HERMES_HOME not found — set it and retry.")
        return 1

    dest_dir = Path(home) / "plugins" / "brain-boot"
    dest_dir.mkdir(parents=True, exist_ok=True)
    changed = []
    for name in ("plugin.yaml", "__init__.py"):
        src, dest = src_dir / name, dest_dir / name
        content = src.read_text(encoding="utf-8")
        if dest.is_file() and dest.read_text(encoding="utf-8") == content:
            print(f"[skip] {name} already up to date")
            continue
        if dest.is_file():
            bak = dest.with_name(f"{name}.bak-{time.strftime('%Y%m%d-%H%M%S')}")
            shutil.copy2(dest, bak)
            print(f"previous version backed up: {bak}")
        dest.write_text(content, encoding="utf-8")
        changed.append(name)
        print(f"[ok] {name} written")

    print(f"\nplugin installed: {dest_dir}")
    enabled = _plugin_is_enabled(home)
    if enabled is None:
        print("\n=> MISSING STEP: enable it with")
        print("     hermes plugins enable brain-boot")
        print("   (deliberately not automatic: enabling code is an explicit gesture)")
    elif enabled:
        print("\n=> already enabled in plugins.enabled (takes effect next session)")
    return 0


def _plugin_is_enabled(home: str) -> Optional[bool]:
    """True/False if config.yaml is readable, None if we cannot tell."""
    cfg = Path(home) / "config.yaml"
    try:
        text = cfg.read_text(encoding="utf-8")
    except OSError:
        return None
    # Deliberately simple parsing: config.yaml is written by Hermes, not by us,
    # and we do not want to depend on a YAML parser for a mere diagnostic.
    inside = False
    items: list = []
    collecting = False
    for line in text.splitlines():
        if re.match(r"^plugins:\s*$", line):
            inside, collecting = True, False
            continue
        if inside and line and not line.startswith((" ", "\t")):
            break  # end of the plugins: section
        if not inside:
            continue
        m = re.match(r"^\s+enabled:\s*(.*)$", line)
        if m:
            inline = m.group(1).strip()
            if inline and inline != "[]":
                # Inline form: enabled: [brain-boot, x]
                items.extend(p.strip() for p in inline.strip("[]").split(","))
                collecting = False
            elif inline == "[]":
                collecting = False
            else:
                collecting = True  # block list on the following lines
            continue
        if collecting:
            m2 = re.match(r"^\s+-\s*(.+?)\s*$", line)
            if m2:
                items.append(m2.group(1).strip().strip("\"'"))
            elif line.strip() and re.match(r"^\s+\w+:", line):
                collecting = False  # another key: the list is over
    return any("brain-boot" in i for i in items) if items else None


def cmd_install_cron(a) -> int:
    """Copy the cron job scripts (Python) into <HERMES_HOME>/scripts/.

    Why this command exists (measured on a Windows host, 2026-09-25): the cron
    jobs used to be `.sh`. A `.sh` script is launched via `shutil.which("bash")`
    (`cron/scheduler_script.py`), and on a machine where WSL is installed
    `C:\\WINDOWS\\system32\\bash.exe` comes BEFORE Git Bash on PATH and wins:
    that is the WSL launcher, which reads the Windows path of the script as a
    POSIX path and exits 127 ("No such file or directory"). The nightly job
    therefore failed every night, silently, while the same script worked by
    hand in a Git Bash shell.

    Job scripts are therefore Python: the venv interpreter runs directly, with
    no intermediate shell, so there is no `bash` to resolve.

    Idempotent. Creating/editing the job stays an explicit gesture (hermes cron).
    """
    root = Path(a.dir)
    src_dir = root / "scripts"
    jobs = {
        "brain_wiki_index.py": "30 3 * * *",
        "brain_decisions_gap.py": "0 8 * * 1",
    }
    home = _hermes_home()
    if home is None:
        print("HERMES_HOME not found — set it and retry.")
        return 1

    dest_dir = Path(home) / "scripts"
    dest_dir.mkdir(parents=True, exist_ok=True)
    installed: list[str] = []
    for name in jobs:
        src = src_dir / name
        if not src.is_file():
            print(f"[!] missing from the repo: {src} — run `sync` first")
            continue
        dest = dest_dir / name
        content = src.read_text(encoding="utf-8")
        if dest.is_file() and dest.read_text(encoding="utf-8") == content:
            print(f"[skip] already up to date: {dest}")
        else:
            if dest.is_file():
                bak = dest.with_suffix(f".py.bak-{time.strftime('%Y%m%d-%H%M%S')}")
                shutil.copy2(dest, bak)
                print("previous version backed up:", bak)
            dest.write_text(content, encoding="utf-8")
            print("[ok] installed:", dest)
        installed.append(name)

    if installed:
        print()
        print("Create/edit the jobs on THIS host (one scheduler per host):")
        for name in installed:
            print(f"  hermes cron create \"{jobs[name]}\" --name \"Fleet brain — {name}\" \\")
            print(f"    --script {name} --no-agent --deliver local")
        print()
        print("Replace an existing job: `hermes cron edit <job_id> --script <name>`")
    return 0


def cmd_install_skill(a) -> int:
    """Install (or update) the fleet-brain skill in the local Hermes home.

    Why: the skill lives IN the repo (skill/SKILL.md) so it travels with the
    facts. Without this step, a host clones the repo but has no knowledge of
    the convention -> it cannot know what "shared brain" means.

    Copies to <HERMES_HOME>/skills/productivity/fleet-brain/SKILL.md.
    Idempotent; backs up a differing version before overwriting.
    """
    root = Path(a.dir)
    src = root / "skill" / "SKILL.md"
    if not src.is_file():
        print(f"no skill in the repo ({src}) — nothing to install.")
        print("(the repo must contain skill/SKILL.md; run `sync` first)")
        return 1

    home = os.environ.get("HERMES_HOME") or str(Path(os.environ.get("LOCALAPPDATA", ".")) / "hermes")
    if not os.path.isdir(home):
        print(f"HERMES_HOME not found ({home}) — set it and retry.")
        return 1

    dest_dir = Path(home) / "skills" / "productivity" / "fleet-brain"
    dest = dest_dir / "SKILL.md"
    content = src.read_text(encoding="utf-8")

    if dest.is_file() and dest.read_text(encoding="utf-8") == content:
        print(f"[skip] skill already up to date: {dest}")
        return 0

    dest_dir.mkdir(parents=True, exist_ok=True)
    if dest.is_file():
        bak = dest.with_suffix(f".md.bak-{time.strftime('%Y%m%d-%H%M%S')}")
        shutil.copy2(dest, bak)
        print("previous version backed up:", bak)
    dest.write_text(content, encoding="utf-8")
    print("[ok] skill installed:", dest)
    print()
    print("It loads automatically when a shared-memory topic comes up")
    print("(triggers: shared brain, shared memory, fleet brain).")
    return 0


def cmd_add(a) -> int:
    root = Path(a.dir)
    ensure_layout(root)
    host = host_name()
    f = facts_dir(root) / f"{host}.jsonl"
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    entry = {
        "id": f"{host}-{int(time.time())}-{os.getpid()}",
        "ts": ts,
        "host": host,
        "topic": a.topic or "general",
        "fact": a.text,
        "tags": [t.strip() for t in (a.tags or "").split(",") if t.strip()],
        "source": a.source,
    }
    if a.supersedes:
        # Several possible ids ("id1,id2"): one clean fact can supersede the
        # truncated version AND its correction. A list is stored as-is; a single
        # id stays a string (compatibility with already-written facts).
        ids = [s.strip() for s in str(a.supersedes).split(",") if s.strip()]
        entry["supersedes"] = ids if len(ids) > 1 else ids[0]
    with open(f, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    print(f"added to {f.name}: {entry['id']}")
    if a.sync or os.environ.get("BRAIN_AUTO_SYNC") == "1":
        return do_sync(root, verbose=False)
    return 0


def _git_ok(root: Path) -> bool:
    return (root / ".git").is_dir()


def target_branch() -> str:
    return os.environ.get("BRAIN_BRANCH", "main")


def ensure_branch(root: Path) -> str:
    """Ensure the repo is on THE canonical branch.

    Real gotcha (measured): `git clone` of an EMPTY repo creates the default
    branch of the git version (`master`), while the other host pushes `main`.
    Both hosts then diverge SILENTLY: each pushes its own branch, each only
    sees its own facts, and nothing reports the problem. We therefore force
    the branch name on every sync.
    """
    want = target_branch()
    cur = run(["git", "rev-parse", "--abbrev-ref", "HEAD"], root).stdout.strip()
    if cur == want:
        return want
    has_commit = run(["git", "rev-parse", "--verify", "-q", "HEAD"], root).returncode == 0
    if has_commit:
        # Renomme la branche courante -> aucun historique perdu.
        run(["git", "branch", "-M", want], root)
    else:
        run(["git", "checkout", "-q", "-b", want], root)
    if cur and cur != "HEAD":
        print(f"  [branch] {cur} renamed/aligned to {want}")
    return want


def do_sync(root: Path, verbose: bool = True) -> int:
    if not _git_ok(root):
        print("no git repo here — local only (run `brain.py init` and add a remote to push)")
        return 0
    remote = run(["git", "remote"], root)
    if "origin" not in remote.stdout.split():
        print("origin not configured — pull/push impossible (local only)")
        print("  -> git -C \"%s\" remote add origin <private-url>" % root)
        return 0

    br = ensure_branch(root)

    run(["git", "add", "-A"], root)
    st = run(["git", "status", "--porcelain"], root)
    if st.stdout.strip():
        run(["git", "commit", "-m", f"brain: {host_name()} {datetime.now(timezone.utc).isoformat(timespec='seconds')}"], root)

    # The remote may be EMPTY (first push): pull/push on HEAD then fails with
    # "couldn't find remote ref HEAD". We test that the remote branch exists
    # BEFORE pulling.
    remote_has = run(["git", "ls-remote", "--heads", "origin", br], root).stdout.strip()
    if not remote_has:
        p = run(["git", "push", "-u", "origin", br], root)
        if p.returncode != 0:
            print("initial push refused:")
            if verbose:
                print("  " + (p.stderr.strip() or p.stdout.strip())[:500])
            return 1
        if verbose:
            print(f"sync ok (first push of {br} to origin)")
        return 0

    # pull --rebase: append-only guarantees there is nothing to merge line-by-line.
    r = run(["git", "pull", "--rebase", "--autostash", "--no-edit", "origin", br], root)
    if r.returncode != 0:
        run(["git", "rebase", "--abort"], root)
        print("pull/rebase failed — state left intact:")
        if verbose:
            print("  " + r.stderr.strip()[:500])
        return 1

    p = run(["git", "push", "origin", br], root)
    if p.returncode != 0:
        print("push refused:")
        if verbose:
            print("  " + (p.stderr.strip() or p.stdout.strip())[:500])
        return 1
    if verbose:
        print("sync ok (pull --rebase + push)")
    return 0


def cmd_sync(a) -> int:
    return do_sync(Path(a.dir), verbose=True)


def cmd_boot(a) -> int:
    """Run at session start: fetch the facts the other hosts learned."""
    root = Path(a.dir)
    ensure_layout(root)
    rc = do_sync(root, verbose=False)
    n = count(root)
    print(f"brain boot: {n['total']} facts loaded ({", ".join(f"{k}:{v}" for k, v in n['hosts'].items())})")
    return rc


def iter_entries(root: Path, host=None, topic=None, tags=None):
    want_tags = {t.strip().lower() for t in (tags or "").split(",") if t.strip()}
    for f in sorted(facts_dir(root).glob("*.jsonl")):
        if host and f.stem != host:
            continue
        for line in f.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except ValueError:
                print(f"  [ignore] invalid JSON line in {f.name}", file=sys.stderr)
                continue
            if topic and (e.get("topic") or "") != topic:
                continue
            if want_tags and not want_tags & {str(t).lower() for t in (e.get("tags") or [])}:
                continue
            yield e


def count(root: Path):
    hosts, total = {}, 0
    for e in iter_entries(root):
        h = e.get("host") or "?"
        hosts[h] = hosts.get(h, 0) + 1
        total += 1
    return {"total": total, "hosts": hosts}


def cmd_list(a) -> int:
    root = Path(a.dir)
    ents = list(iter_entries(root, a.host, a.topic, a.tags))
    for e in ents[-int(a.limit):] if a.limit else ents:
        tags = (" #" + " #".join(e.get("tags") or [])) if e.get("tags") else ""
        print(f"[{e.get('ts','')[:19]}] {e.get('host','?')}/{e.get('topic','?')}{tags}")
        print(f"    {e.get('fact','')}")
        if e.get("source"):
            print(f"    src: {e['source']}")
    print(f"\n{len(ents)} fact(s)")
    return 0


def cmd_search(a) -> int:
    root = Path(a.dir)
    pat = re.compile(a.pattern, re.I)
    hits = [e for e in iter_entries(root) if pat.search(e.get("fact", "")) or pat.search(" ".join(e.get("tags") or []))]
    for e in hits:
        print(f"[{e.get('ts','')[:19]}] {e.get('host','?')}/{e.get('topic','?')}")
        print(f"    {e.get('fact','')}")
    print(f"\n{len(hits)} result(s) for /{a.pattern}/i")
    return 0


def cmd_stats(a) -> int:
    root = Path(a.dir)
    c = count(root)
    print(f"total: {c['total']} facts")
    for h, n in sorted(c["hosts"].items()):
        print(f"  {h:20s} {n}")
    topics = {}
    for e in iter_entries(root):
        t = e.get("topic") or "?"
        topics[t] = topics.get(t, 0) + 1
    if topics:
        print("topics:")
        for t, n in sorted(topics.items(), key=lambda x: -x[1]):
            print(f"  {t:20s} {n}")
    return 0


def cmd_install_soul(a) -> int:
    """Append the decision-journal section to the host's SOUL.md.

    Why this lives in the repo: every host has a different SOUL.md (measured:
    one host's was 667 bytes on a single line, a generic prompt with no
    identity, no missions, no rules). The rule would never get there by manual
    copy-paste. Idempotent, and BACKS UP before modifying: SOUL.md drives the
    agent's behaviour, a failed write is worse than a failure.
    """
    root = Path(a.dir)
    src = root / "soul" / "journal-decisions.md"
    if not src.is_file():
        print(f"no section in the repo ({src}) — run `sync` first.")
        return 1

    home = _hermes_home()
    if home is None:
        print("HERMES_HOME not found — set it and retry.")
        return 1

    marker = "DECISION JOURNAL"
    marker_fr = "JOURNAL DE DECISIONS"
    marker_acc = "JOURNAL OF DECISIONS"
    soul = Path(home) / "SOUL.md"

    # We extract the section from the source file (everything after the '---' separator).
    raw = src.read_text(encoding="utf-8")
    idx = raw.find("\n---\n")
    section = raw[idx + 5:].strip() if idx >= 0 else raw.strip()
    if not section:
        print("empty section in the repo — nothing to install.")
        return 1

    if soul.is_file():
        current = soul.read_text(encoding="utf-8")
        if marker in current or marker_acc in current or marker_fr in current:
            print(f"[skip] {soul} already contains the section — nothing to do.")
            return 0
        bak = soul.with_name(f"SOUL.md.bak-{time.strftime('%Y%m%d-%H%M%S')}")
        shutil.copy2(soul, bak)
        print(f"previous version backed up: {bak}")
        new = current.rstrip() + "\n\n" + section + "\n"
    else:
        print(f"[info] no SOUL.md here — creating {soul}")
        new = section + "\n"

    soul.parent.mkdir(parents=True, exist_ok=True)
    soul.write_text(new, encoding="utf-8")
    print(f"[ok] section installed: {soul}")
    print("\nThe rule takes effect next session (SOUL.md is read at startup).")
    return 0


def cmd_wiki_index(a) -> int:
    """Regenerate this host's human-readable session index (wiki/sessions/index-<host>.md).

    One file per host => single writer => no git conflict possible. The index
    is merged with the version already present so it survives
    sessions.retention_days.
    """
    here = Path(__file__).resolve().parent
    sys.path.insert(0, str(here))
    try:
        import session_index  # type: ignore
    except ImportError as exc:
        print(f"session_index.py not found next to brain.py: {exc}")
        print("(run `sync` first — the script travels with the repo)")
        return 1

    root = Path(a.dir)
    if not _git_ok(root):
        print("no git repo here — index written locally only")

    rc = session_index.build(Path(a.hermes_home) if a.hermes_home else session_index.hermes_home_default(), root)
    if rc == 0 and (a.sync or os.environ.get("BRAIN_AUTO_SYNC") == "1"):
        return do_sync(root, verbose=False)
    return rc


def cmd_init(a) -> int:
    """Initialize the local repo (and optionally the remote)."""
    root = Path(a.dir)
    root.mkdir(parents=True, exist_ok=True)
    ensure_layout(root)
    if not _git_ok(root):
        run(["git", "init", "-b", "main"], root, check=True)
        print(f"git repo initialized: {root}")
    if a.remote:
        have = run(["git", "remote"], root).stdout.split()
        if "origin" in have:
            run(["git", "remote", "set-url", "origin", a.remote], root)
            print("origin updated")
        else:
            run(["git", "remote", "add", "origin", a.remote], root)
            print(f"origin added: {a.remote}")
    run(["git", "add", "-A"], root)
    if run(["git", "status", "--porcelain"], root).stdout.strip():
        run(["git", "commit", "-m", "brain: init"], root)
        print("initial commit created")
    print(f"layout : {facts_dir(root)}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(prog="brain", description="Shared memory between Hermes hosts (git, append-only)")
    ap.add_argument("--dir", default=str(DEFAULT_DIR), help=f"repo root (default {DEFAULT_DIR})")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("add", help="add a fact")
    p.add_argument("text")
    p.add_argument("--tags", default="")
    p.add_argument("--topic", default="general")
    p.add_argument("--source", default=None)
    p.add_argument("--supersedes", default=None, help="id of the fact this one replaces")
    p.add_argument("--sync", action="store_true")
    p.set_defaults(fn=cmd_add)

    sub.add_parser("boot", help="pull at session start").set_defaults(fn=cmd_boot)
    sub.add_parser("sync", help="pull --rebase + push").set_defaults(fn=cmd_sync)
    sub.add_parser("install-skill", help="install the fleet-brain skill locally").set_defaults(fn=cmd_install_skill)
    sub.add_parser("install-soul", help="add the decision journal section to the local SOUL.md").set_defaults(fn=cmd_install_soul)
    sub.add_parser("install-plugin", help="install the brain-boot plugin locally (auto-load at startup)").set_defaults(fn=cmd_install_plugin)
    sub.add_parser("install-cron", help="install the cron job scripts (Python) into <HERMES_HOME>/scripts/").set_defaults(fn=cmd_install_cron)

    p = sub.add_parser("list", help="list facts")
    p.add_argument("--host", default=None)
    p.add_argument("--topic", default=None)
    p.add_argument("--tags", default="")
    p.add_argument("--limit", default="0")
    p.set_defaults(fn=cmd_list)

    p = sub.add_parser("search", help="search a pattern")
    p.add_argument("pattern")
    p.set_defaults(fn=cmd_search)

    p = sub.add_parser("wiki-index", help="regenerate the human-readable session index (wiki/sessions/)")
    p.add_argument("--hermes-home", default=None)
    p.add_argument("--sync", action="store_true")
    p.set_defaults(fn=cmd_wiki_index)

    sub.add_parser("stats", help="counts").set_defaults(fn=cmd_stats)

    p = sub.add_parser("init", help="initialize the repo (and optionally the remote)")
    p.add_argument("remote", nargs="?", default=None)
    p.set_defaults(fn=cmd_init)

    a = ap.parse_args()
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
