#!/usr/bin/env python3
"""brain_decisions_gap.py — cron job: safety net for the decision journal.

Replaces `wiki_decisions_gap.sh`: a `.sh` script is launched via `shutil.which("bash")`
(cron/scheduler_script.py), and on a machine where WSL is installed
`C:\\WINDOWS\\system32\\bash.exe` comes before Git Bash — the WSL launcher then eats
a Windows path. A `.py` has no intermediate shell.

Behaviour: the report goes to stdout (readable as-is), plus commit/push of the working
stub `_to-complete-<host>.md`.

MEASURED GOTCHA 2026-09-29 — the credential helper makes any NETWORK git HANG under cron:

  `git fetch origin` **without the workaround never returns**; with it, exit 0 in <1 s.
  Measured on this host: `timeout 45 git fetch` -> exit 124 (hang);
  `timeout 45 git -c credential.helper= -c credential.helper=manager fetch` -> exit 0.
  Cause: `credential.helper = helper-selector` (git 2.53 system config) and
  `credential.helperselector.selected` never set — with no console, `git-credential-fill`
  never gets its helper choice. `GIT_TERMINAL_PROMPT=0` and `GCM_INTERACTIVE=never` do not
  help: the block is in the SELECTOR, upstream of GCM.
  This was NOT a path problem (the `git` in `C:\\Program Files\\Git\\cmd` is what
  `shutil.which` resolves too): the original "WSL bash / 127" diagnosis was a wrong lead.
  The real symptom was a job dying on the script's 3600 s timeout.

  Every git command MUST therefore receive `-c credential.helper= -c credential.helper=manager`,
  and every call must have a bounded delay: a hang must show up in seconds in the job report,
  not kill the job an hour later without printing anything.

INSTALL (each host):
    python brain/bin/brain.py install-cron
    hermes cron create "0 8 * * 1" --name "Fleet brain — decision journal safety net" \\
      --script brain_decisions_gap.py --no-agent --deliver local
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

# See MEASURED GOTCHA above: never call network git without this default disabled.
CREDENTIAL_OVERRIDE = ["-c", "credential.helper=", "-c", "credential.helper=manager"]
# A hanging network git command must show up, not kill the job.
GIT_TIMEOUT_SECONDS = 120
LOCAL_GIT_TIMEOUT_SECONDS = 60


def _utf8_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        except Exception:
            pass


def brain_dir() -> Path:
    forced = os.environ.get("BRAIN_DIR")
    if forced:
        return Path(forced)
    home = os.environ.get("HERMES_HOME") or os.environ.get("LOCALAPPDATA", "")
    if home and (Path(home) / "brain").is_dir():
        return Path(home) / "brain"
    return Path(os.environ.get("USERPROFILE") or os.path.expanduser("~")) / "brain"


def run(args: list[str], cwd: Path | None = None, timeout: float = GIT_TIMEOUT_SECONDS):
    """Run a command; returns (returncode, stdout, stderr). Never raises on timeout.

    A timeout returns code 124 and a human-readable message, so a hung git shows up in the
    job's report instead of silently consuming the script budget.
    """
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    try:
        p = subprocess.run(
            args, cwd=str(cwd) if cwd else None, capture_output=True, text=True,
            encoding="utf-8", errors="replace", stdin=subprocess.DEVNULL, env=env,
            timeout=timeout,
        )
        return p.returncode, p.stdout or "", p.stderr or ""
    except subprocess.TimeoutExpired:
        return 124, "", f"timeout ({timeout:.0f}s): {' '.join(args)}"
    except Exception as exc:  # noqa: BLE001 - le rapport doit sortir quoi qu'il arrive
        return 125, "", f"launch failure: {type(exc).__name__}: {exc}"


def git(brain: Path, *args: str, timeout: float = GIT_TIMEOUT_SECONDS):
    """`git -C <brain> <args>` with the credential-selector workaround."""
    return run(["git", "-C", str(brain), *CREDENTIAL_OVERRIDE, *args], timeout=timeout)


def main() -> int:
    _utf8_stdio()
    brain = brain_dir()
    gap = brain / "scripts" / "wiki_decisions_gap.py"

    if not gap.is_file():
        print(f"script not found: {gap} — run a brain sync")
        return 1

    days = os.environ.get("WIKI_GAP_DAYS", "7")
    code, out, err = run([sys.executable, str(gap), "--days", days, "--write-stub"],
                         timeout=LOCAL_GIT_TIMEOUT_SECONDS * 4)
    for stream in (out.strip(), err.strip()):
        if stream:
            print(stream)
    if code == 124:
        print("(the report generator timed out — incomplete report)")

    # The per-host stub is a working artifact: we push it so the report can be
    # read from any machine. A push failure must not hide the report (already
    # printed above) — but it must be SAID, not swallowed.
    if (brain / ".git").is_dir():
        dirty = (git(brain, "status", "--porcelain", timeout=LOCAL_GIT_TIMEOUT_SECONDS)[1] or "").strip()
        if dirty:
            git(brain, "add", "-A", timeout=LOCAL_GIT_TIMEOUT_SECONDS)
            git(brain, "commit", "-q", "-m", "brain: decision safety net", timeout=LOCAL_GIT_TIMEOUT_SECONDS)
            rc_pull, _, err_pull = git(brain, "pull", "--rebase", "-q", "--autostash", "origin", "main")
            rc_push, _, err_push = git(brain, "push", "-q", "origin", "main")
            if rc_pull != 0:
                print(f"(brain pull did not complete — report above still stands){_why(err_pull)}")
            if rc_push != 0:
                print(f"(stub push refused — report above still stands){_why(err_push)}")
            else:
                sha = (git(brain, "rev-parse", "--short", "HEAD", timeout=LOCAL_GIT_TIMEOUT_SECONDS)[1] or "").strip()
                print(f"published: {sha}")

    return 0 if code == 0 else code


def _why(stderr: str) -> str:
    """First useful git reason, so "refused" does not stay anonymous."""
    line = next((l.strip() for l in (stderr or "").splitlines() if l.strip()), "")
    return f" [{line}]" if line else ""


if __name__ == "__main__":
    sys.exit(main())
