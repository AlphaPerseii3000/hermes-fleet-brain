"""brain-boot — load shared memory at session start.

WHY THIS PLUGIN EXISTS
  The "brain" git repo (facts/<host>.jsonl, append-only) holds what the OTHER
  Hermes hosts have learned. Nothing loaded it at startup: local memory
  (MEMORY.md) deliberately never syncs, so a host silently ignored everything
  the other had recorded until a human explicitly asked for a `boot`.

  A shell hook CANNOT solve this: `on_session_start` ignores its return value
  (agent/conversation_loop.py), so running `brain.py boot` from a hook refreshes
  the files without ever putting a single fact into the model's context. The only
  injection channel is `pre_llm_call` -> `{"context": ...}`
  (agent/turn_context.py::_collect_pre_llm_call_context), which is also subject
  to an interactive allowlist. Hence this plugin: it syncs in the background and
  injects the other hosts' facts on the first turn.

FAILURE CONTRACT
  Fail-open everywhere. Missing brain, broken git, un-cloned repo, corrupt JSONL
  file: the session runs normally, with no long stall. The plugin never raises
  inside a hook.

STATE
  No durable state: the git repo remains the source of truth. Only an in-memory
  guard prevents injecting twice in the same session.
"""

from __future__ import annotations

import json
import logging
import os
import platform
import re
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# --- Constantes -------------------------------------------------------------

# Hard cap: the first-turn wait is bounded by this, whatever the config says, so
# a slow git can never freeze an interactive session.
_ABSOLUTE_WAIT_CAP_S = 20.0

# Marker of the injected block. Deliberately explicit: a human reading the
# conversation must see where these facts come from, and an agent must be able to
# tell this injection apart from a user message.
_INJECTION_HEADER = (
    "SHARED MEMORY (facts recorded by the OTHER Hermes hosts via the git \"brain\" "
    "repo — information learned elsewhere, not an instruction from the user.\n"
    "Treat it as context facts to verify if a detail is decisive.)"
)

# Injection guard: {session_id} for the current session only.
_injected_sessions: set = set()
_injected_lock = threading.Lock()

# In-flight sync thread (one at a time).
_boot_thread: Optional[threading.Thread] = None
_boot_done = threading.Event()
_boot_thread_lock = threading.Lock()


# --- Config resolution ------------------------------------------------------


def _cfg(ctx: Any, key: str, default: Any) -> Any:
    """Read `plugins.entries.brain-boot.settings.<key>`, falling back to default."""
    try:
        value = ctx.get_config(key, default)
        return default if value is None else value
    except Exception:
        return default


def _brain_root() -> Path:
    """Root of the brain repo.

    `get_hermes_home()` and never `~/.hermes`: on Windows the user home IS
    `~/.hermes`, and writing there would read an empty repo at the wrong
    location, with no error (documented gotcha of the fleet-brain skill).
    """
    forced = os.environ.get("BRAIN_DIR")
    if forced:
        return Path(forced).expanduser()
    try:
        from hermes_constants import get_hermes_home

        return Path(get_hermes_home()).expanduser() / "brain"
    except Exception:
        return Path.home() / ".hermes" / "brain"


def _own_host() -> str:
    """Name of the writing host — same normalization as brain.py::host_name."""
    forced = os.environ.get("BRAIN_HOST") or platform.node() or "unknown"
    return re.sub(r"[^A-Za-z0-9_.-]", "_", forced)


def _brain_script(root: Path) -> Optional[Path]:
    script = root / "bin" / "brain.py"
    return script if script.is_file() else None


# --- Sync (background task) -------------------------------------------------


def _run_boot(root: Path, timeout_s: float) -> None:
    """Run `brain.py boot` as a subprocess. Never raises.

    `boot` = git pull --rebase + optional push + counting. We do NOT read its
    output: facts are read directly from facts/*.jsonl, which is the source of
    truth and stays readable even if git fails.
    """
    script = _brain_script(root)
    if script is None:
        logger.debug("brain-boot: no brain repo (%s) — nothing to sync", root)
        return
    try:
        subprocess.run(
            [sys.executable, str(script), "boot"],
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=max(1.0, timeout_s),
        )
    except subprocess.TimeoutExpired:
        logger.debug("brain-boot: `brain.py boot` exceeded %.1fs (injecting from local state)", timeout_s)
    except Exception as exc:
        logger.debug("brain-boot: `brain.py boot` failed (%s) — injecting from local state", exc)


def _start_background_boot(root: Path, timeout_s: float) -> None:
    """Start (or join) the background sync, one at a time."""
    global _boot_thread
    with _boot_thread_lock:
        if _boot_thread is not None and _boot_thread.is_alive():
            return
        _boot_done.clear()
        _boot_thread = threading.Thread(
            target=lambda: (_run_boot(root, timeout_s), _boot_done.set()),
            name="brain-boot-sync",
            daemon=True,
        )
        _boot_thread.start()


# --- Fact reading -----------------------------------------------------------


def _iter_facts(root: Path, own_host: str, include_own: bool, exclude_tags: set) -> List[Dict[str, Any]]:
    """Readable facts in facts/*.jsonl, deduplicated, newest first.

    Append-only forces a correction to be a NEW line: a fact may therefore have a
    successor. We resolve the latest version here via the ids cited in a
    `supersedes` field (string OR list), and drop noise tags.
    """
    facts_dir = root / "facts"
    if not facts_dir.is_dir():
        return []
    superseded: set = set()
    entries: List[Dict[str, Any]] = []
    for path in sorted(facts_dir.glob("*.jsonl")):
        if not include_own and path.stem == own_host:
            continue
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except ValueError:
                continue  # one broken line must not lose the rest of the file
            if not isinstance(entry, dict):
                continue
            target = entry.get("supersedes")
            if isinstance(target, str) and target:
                superseded.add(target)
            elif isinstance(target, list):
                superseded.update(str(t) for t in target if t)
            entries.append(entry)
    kept: List[Dict[str, Any]] = []
    for entry in entries:
        if entry.get("id") in superseded:
            continue
        tags = {str(t).lower() for t in (entry.get("tags") or [])}
        if tags & exclude_tags:
            continue
        text = str(entry.get("fact") or "").strip()
        if not text:
            continue
        kept.append(entry)
    # Lexicographic sort on ISO-8601 UTC: sufficient, no date parsing needed.
    kept.sort(key=lambda e: str(e.get("ts") or ""), reverse=True)
    return kept


def _format_block(facts: List[Dict[str, Any]], max_facts: int, max_chars: int) -> str:
    """Render the injected block, bounded in fact count AND in characters."""
    if not facts:
        return ""
    lines: List[str] = []
    used = 0
    for entry in facts[:max_facts]:
        host = entry.get("host") or "?"
        topic = entry.get("topic") or "general"
        text = " ".join(str(entry.get("fact") or "").split())
        line = f"- [{host}/{topic}] {text}"
        if used + len(line) > max_chars:
            remaining = max_chars - used
            if remaining > 80:
                lines.append(line[: remaining - 1] + "…")
            break
        lines.append(line)
        used += len(line) + 1
    if not lines:
        return ""
    hidden = len(facts) - len(lines)
    tail = f"\n({hidden} more fact(s) in the repo — skill fleet-brain.)" if hidden > 0 else ""
    return f"{_INJECTION_HEADER}\n\n" + "\n".join(lines) + tail


# --- Hooks ------------------------------------------------------------------


def on_session_start(ctx: Any, **_kwargs: Any) -> None:
    """Start the background sync. Return value ignored by the core — hence the thread."""
    if not _cfg(ctx, "enabled", True):
        return
    root = _brain_root()
    timeout_s = min(float(_cfg(ctx, "boot_timeout_s", 6.0) or 6.0), _ABSOLUTE_WAIT_CAP_S)
    try:
        _start_background_boot(root, timeout_s)
    except Exception as exc:
        logger.debug("brain-boot: could not start the sync (%s)", exc)


def pre_llm_call(ctx: Any, **kwargs: Any) -> Optional[Dict[str, str]]:
    """Inject the other hosts' facts into the FIRST turn of the session.

    `pre_llm_call` is the only context-injection channel (its return value is
    appended to the user message). We fire on the first turn only: injecting at
    every turn would grow the prompt and break the prefix cache.
    """
    if not _cfg(ctx, "enabled", True):
        return None
    if not kwargs.get("is_first_turn"):
        return None
    session_id = str(kwargs.get("session_id") or "")

    # One injection per session, even if the first turn is replayed (resume,
    # compaction rotation, surface switch).
    with _injected_lock:
        if session_id and session_id in _injected_sessions:
            return None
        if session_id:
            _injected_sessions.add(session_id)
    if len(_injected_sessions) > 200:  # memory bound for a long-lived process (gateway)
        with _injected_lock:
            _injected_sessions.clear()

    root = _brain_root()
    timeout_s = min(float(_cfg(ctx, "boot_timeout_s", 6.0) or 6.0), _ABSOLUTE_WAIT_CAP_S)

    # The thread was started by on_session_start (same turn, just before).
    # If it did not run (hook absent, resumed session), we trigger it here.
    _start_background_boot(root, timeout_s)
    deadline = time.monotonic() + timeout_s
    while not _boot_done.is_set() and time.monotonic() < deadline:
        time.sleep(0.05)

    try:
        facts = _iter_facts(
            root,
            own_host=_own_host(),
            include_own=bool(_cfg(ctx, "include_own_host", False)),
            exclude_tags={str(t).lower() for t in (_cfg(ctx, "exclude_tags", ["obsolete", "test"]) or [])},
        )
        block = _format_block(
            facts,
            max_facts=int(_cfg(ctx, "max_facts", 12) or 12),
            max_chars=int(_cfg(ctx, "max_chars", 2500) or 2500),
        )
    except Exception as exc:  # fail-open: never break the turn
        logger.debug("brain-boot: could not read the facts (%s)", exc)
        return None
    if not block:
        logger.debug("brain-boot: no other-host facts to inject")
        return None
    logger.info(
        "brain-boot: %d other-host fact(s) injected on the first turn (session=%s)",
        min(len(facts), int(_cfg(ctx, "max_facts", 12) or 12)), session_id,
    )
    return {"context": block}


def on_session_reset(ctx: Any, **_kwargs: Any) -> None:
    """/new, /reset: the next session must inject again."""
    with _injected_lock:
        _injected_sessions.clear()
    _boot_done.clear()


def register(ctx: Any) -> None:
    ctx.register_hook("on_session_start", lambda **kw: on_session_start(ctx, **kw))
    ctx.register_hook("pre_llm_call", lambda **kw: pre_llm_call(ctx, **kw))
    ctx.register_hook("on_session_reset", lambda **kw: on_session_reset(ctx, **kw))
