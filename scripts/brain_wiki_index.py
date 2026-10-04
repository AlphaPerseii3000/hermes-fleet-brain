#!/usr/bin/env python3
"""brain_wiki_index.py — cron job: refresh THIS host's human-readable session index.

WHY THIS SCRIPT EXISTS (and why it is Python, not shell)
  It replaces `brain_wiki_index.sh`. Same job, but no longer dependent on `bash`.

  Measured (Windows host, 2026-09-25). Hermes resolves the shell for cron scripts
  (.sh) via `shutil.which("bash")` in `cron/scheduler_script.py`. On a machine
  where WSL is installed, `C:\\WINDOWS\\system32\\bash.exe` sits on PATH *before*
  Git Bash and wins: that is the WSL launcher. But the scheduler passes the
  script path in Windows form (`C:\\Users\\...\\brain_wiki_index.sh`); WSL bash reads
  it as a POSIX path, eats the backslashes and returns:

      /bin/bash: C:UsersYourNameAppDataLocalhermesscriptsbrain_wiki_index.sh:
      No such file or directory                      (exit 127)

  Result: the nightly job failed every night, the index stayed frozen, and nothing
  explained why (the job said "ok" when run by hand, from a Git Bash shell where
  `bash` resolves correctly). The same `.sh` works where bash = Git Bash.

  A .py script goes through the venv's Python interpreter, with no intermediate
  shell: no `bash` to resolve, no path to be reinterpreted. It is the only fix
  that travels with the repo and survives `hermes update`.

INSTALL (each host):
    python brain/bin/brain.py install-cron
    hermes cron create "30 3 * * *" --name "Fleet brain — session index" \\
      --script brain_wiki_index.py --no-agent --deliver local
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def _utf8_stdio() -> None:
    """Hermes decodes cron script output as UTF-8 (lossy). Writing anything else
    (cp1252 by default on Windows) produces silent mojibake in the report."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        except Exception:
            pass


def brain_dir() -> Path:
    """BRAIN_DIR, else <HERMES_HOME>/brain, else ~/brain — same convention as the .sh."""
    forced = os.environ.get("BRAIN_DIR")
    if forced:
        return Path(forced)
    home = os.environ.get("HERMES_HOME") or os.environ.get("LOCALAPPDATA", "")
    if home and (Path(home) / "brain").is_dir():
        return Path(home) / "brain"
    return Path(os.environ.get("USERPROFILE") or os.path.expanduser("~")) / "brain"


def run(args: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess:
    """stdin=DEVNULL: on Windows an invalid stdin handle makes subprocess.run
    raise OSError WinError 6 (already paid for once in brain.py)."""
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    return subprocess.run(
        args, cwd=str(cwd) if cwd else None, capture_output=True, text=True,
        encoding="utf-8", errors="replace", stdin=subprocess.DEVNULL, env=env,
    )


def main() -> int:
    _utf8_stdio()
    brain = brain_dir()
    brain_py = brain / "bin" / "brain.py"

    if not brain_py.is_file():
        print(f"brain not found: {brain_py} (BRAIN_DIR={os.environ.get('BRAIN_DIR')})")
        print("clone the repo into <HERMES_HOME>/brain or set BRAIN_DIR.")
        return 1

    r = run([sys.executable, str(brain_py), "wiki-index", "--sync"])
    out = (r.stdout or "").strip()
    err = (r.stderr or "").strip()
    if out:
        print(out)
    if err:
        print(err)

    if r.returncode != 0:
        print(f"wiki-index FAILED (code {r.returncode})")
        return r.returncode

    # Proof the push happened: local hash == remote hash. An index written but not
    # pushed leaves the other host on a stale version — a silent failure.
    local = run(["git", "-C", str(brain), "rev-parse", "HEAD"])
    remote = run(["git", "-C", str(brain), "rev-parse", "origin/main"])
    lh = (local.stdout or "").strip()
    rh = (remote.stdout or "").strip()
    if lh and lh == rh:
        print(f"published: {lh} (origin/main identical)")
        return 0
    print(f"WARNING: not published (local={lh or '?'} remote={rh or '?'})")
    if (local.stderr or "").strip():
        print((local.stderr or "").strip()[:400])
    return 1


if __name__ == "__main__":
    sys.exit(main())
