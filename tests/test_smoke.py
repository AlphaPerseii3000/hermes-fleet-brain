#!/usr/bin/env python3
"""Smoke test — exercises the whole brain CLI end to end, stdlib only.

Run:  python tests/test_smoke.py
Exit: 0 = all pass. No third-party dependency, no network.

Covers: init, add, supersede resolution, search, stats, the per-host file rule,
the scrub rules, and the install-* commands against a throwaway HERMES_HOME.
Everything runs in a temp directory; the real brain and Hermes home are untouched.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
BRAIN_PY = REPO / "bin" / "brain.py"

PASS, FAIL = 0, 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}  {detail}")


def run(args, cwd=None, env=None, expect_rc=0):
    e = dict(os.environ)
    if env:
        e.update(env)
    r = subprocess.run(
        [sys.executable, *args], cwd=str(cwd) if cwd else None,
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        stdin=subprocess.DEVNULL, env=e,
    )
    if r.returncode != expect_rc:
        print(f"    cmd: {' '.join(map(str, args))}")
        print(f"    rc={r.returncode} expected={expect_rc}")
        print(f"    out: {(r.stdout or '')[:300]}")
        print(f"    err: {(r.stderr or '')[:300]}")
    return r


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="fleet-brain-test-"))
    brain = tmp / "brain"
    hermes = tmp / "hermes-home"
    hermes.mkdir(parents=True)
    env = {"HERMES_HOME": str(hermes), "BRAIN_HOST": "testhost", "GIT_AUTHOR_NAME": "t",
           "GIT_AUTHOR_EMAIL": "t@t.t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t.t"}
    try:
        # The real repo carries the engine (skill/, soul/, plugin/, scripts/, bin/)
        # inside it — that is the design: the documentation travels with the facts.
        # So the test simulates a real clone, not an empty init.
        shutil.copytree(REPO / "bin", brain / "bin")
        shutil.copytree(REPO / "skill", brain / "skill")
        shutil.copytree(REPO / "soul", brain / "soul")
        shutil.copytree(REPO / "plugin", brain / "plugin")
        shutil.copytree(REPO / "scripts", brain / "scripts")

        print("== init ==")
        r = run([str(BRAIN_PY), "--dir", str(brain), "init"], env=env)
        check("init creates the repo", (brain / ".git").is_dir())
        check("init creates facts/", (brain / "facts").is_dir())

        print("== add ==")
        r = run([str(BRAIN_PY), "--dir", str(brain), "add",
                 "SQLite in WAL mode through a file-sync corrupts the DB (measured).",
                 "--topic", "infra", "--tags", "gotcha,db"], env=env)
        f = brain / "facts" / "testhost.jsonl"
        check("fact file is facts/<host>.jsonl", f.is_file())
        entry = json.loads(f.read_text(encoding="utf-8").splitlines()[0])
        check("entry has id/ts/host/topic/fact/tags",
              all(k in entry for k in ("id", "ts", "host", "topic", "fact", "tags")))
        check("host attribution", entry["host"] == "testhost", entry.get("host"))

        print("== supersede ==")
        r = run([str(BRAIN_PY), "--dir", str(brain), "add", "Corrected fact line.",
                 "--supersedes", entry["id"]], env=env)
        lines = f.read_text(encoding="utf-8").splitlines()
        check("append-only: 2 lines, originals kept", len(lines) == 2)
        check("second entry carries supersedes", "supersedes" in json.loads(lines[1]))

        print("== search ==")
        r = run([str(BRAIN_PY), "--dir", str(brain), "search", "wal"], env=env)
        check("search finds the fact", "WAL" in r.stdout or "wal" in r.stdout.lower())

        print("== stats ==")
        r = run([str(BRAIN_PY), "--dir", str(brain), "stats"], env=env)
        check("stats lists the host", "testhost" in r.stdout)

        print("== install-skill / install-soul / install-plugin ==")
        r = run([str(BRAIN_PY), "--dir", str(brain), "install-skill"], env=env)
        check("skill installed", (hermes / "skills" / "productivity" / "fleet-brain" / "SKILL.md").is_file(),
              r.stdout[-200:])
        r = run([str(BRAIN_PY), "--dir", str(brain), "install-soul"], env=env)
        check("SOUL.md created with the section",
              (hermes / "SOUL.md").is_file() and "DECISION JOURNAL" in
              (hermes / "SOUL.md").read_text(encoding="utf-8"))
        r = run([str(BRAIN_PY), "--dir", str(brain), "install-plugin"], env=env)
        check("plugin installed",
              (hermes / "plugins" / "brain-boot" / "plugin.yaml").is_file())
        r = run([str(BRAIN_PY), "--dir", str(brain), "install-cron"], env=env)
        check("cron scripts installed",
              (hermes / "scripts" / "brain_wiki_index.py").is_file())

        print("== scrub ==")
        sys.path.insert(0, str(REPO / "bin"))
        import session_index  # type: ignore
        scrubbed = session_index.scrub("mail me at jane.doe@example.com or +41 79 123 45 67")
        check("email masked", "@" not in scrubbed, scrubbed)
        check("phone masked", "79" not in scrubbed.replace("<tel>", ""), scrubbed)
        tok = "ghp_" + "a" * 24
        check("token masked", tok not in session_index.scrub(f"token {tok}"), session_index.scrub(f"token {tok}"))

        print("== wiki-index (no state.db) ==")
        r = run([str(BRAIN_PY), "--dir", str(brain), "wiki-index"], env=env, expect_rc=1)
        check("no state.db is a clean, non-crashing failure", "nothing to index" in (r.stdout + r.stderr))

        print()
        print(f"PASS={PASS} FAIL={FAIL}")
        return 0 if FAIL == 0 else 1
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
