#!/usr/bin/env python3
"""session_index.py — human-readable index of a host's Hermes sessions.

WHY THIS SCRIPT EXISTS
  Transcripts live in <HERMES_HOME>/state.db (SQLite + FTS5), readable *only* by an
  agent or an SQL tool. A human cannot "open the brain and see what the agent
  knows". The measured need: re-read from a browser what was said on ANY of the
  machines.

  This script produces a markdown index, versioned in the "brain" repo, one line
  per session, with the @session:<profile>/<id> link that reopens it in Hermes.

INTENDED PROPERTIES (each one a choice, not a default)
  - Single writer: a host writes ONLY wiki/sessions/index-<host>.md. Two hosts
    never write the same file -> a git conflict is structurally impossible,
    no need to handle one.
  - Idempotent + survives retention: the index is REGENERATED from state.db on
    every run, then MERGED with the lines already present. A session purged from
    state.db (retention_days) stays in the index: the index becomes the long-term
    memory of the conversations.
  - No LLM: deterministic and free. Extracting durable *facts* from a session is
    a different job (curation), done elsewhere.
  - Never overwrite a session the user hid (hidden=1).

USAGE
  python bin/session_index.py [--hermes-home DIR] [--brain-dir DIR] [--dry-run]

  Callable from brain.py: `brain.py wiki-index`.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import re
import sqlite3
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# --- Redaction --------------------------------------------------------------
# The repo is PRIVATE but readable by any host that clones it and, via A2A, by an
# authorized peer. A session title may contain an email, a phone number or a token
# pasted by mistake. We mask BEFORE writing, never after.
_SCRUB_RULES: List[Tuple[re.Pattern, str]] = [
    (re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.]{2,}\b"), "<email>"),
    (re.compile(r"\b(?:IBAN\s*)?[A-Z]{2}\d{2}[A-Z0-9]{11,30}\b"), "<iban>"),
    (re.compile(r"(?<![\d])(?:\+41[\s.-]?|0)7[5-9](?:[\s.-]?\d){7}(?![\d])"), "<tel>"),
    (re.compile(r"\b(?:\+?1)?[\s.-]?\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}\b"), "<tel>"),
    (re.compile(r"\b(?:sk|pk|ghp|gho|xox[baprs])[-_][A-Za-z0-9_-]{16,}\b"), "<token>"),
    (re.compile(r"\b[A-Za-z0-9_-]{40,}\b"), "<token>"),
]

_MAX_TITLE = 90


def scrub(text: str) -> str:
    """Mask emails / phones / IBANs / tokens. Best-effort, never blocking."""
    if not text:
        return ""
    out = text
    for pat, repl in _SCRUB_RULES:
        out = pat.sub(repl, out)
    out = re.sub(r"\s+", " ", out).strip()
    return out


def shorten(text: str, n: int = _MAX_TITLE) -> str:
    text = scrub(text)
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


# --- Localisation -----------------------------------------------------------

def brain_dir_default() -> Path:
    home = os.environ.get("HERMES_HOME") or str(
        Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "hermes"
    )
    return Path(os.environ.get("BRAIN_DIR") or (Path(home) / "brain"))


def hermes_home_default() -> Path:
    return Path(
        os.environ.get("HERMES_HOME")
        or str(Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "hermes")
    )


def host_name_hint() -> str:
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import brain  # type: ignore

        return brain.host_name()
    except Exception:
        import socket

        return socket.gethostname()


# --- Lecture des sessions ---------------------------------------------------

_DAY = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]

# Sources that are real human conversations; the rest is background noise
# (cron runs) we keep but do NOT mix with real conversations.
_HUMAN_SOURCES = {"cli", "telegram", "whatsapp", "discord", "signal", "web", "oneshot", "subagent", "a2a", "peer"}


def session_dbs(hermes_home: Path) -> List[Tuple[str, Path]]:
    """[(profile_name, state.db path)] — default profile + secondary profiles."""
    out: List[Tuple[str, Path]] = []
    main = hermes_home / "state.db"
    if main.is_file():
        out.append(("default", main))
    profiles = hermes_home / "profiles"
    if profiles.is_dir():
        for p in sorted(profiles.iterdir()):
            db = p / "state.db"
            if db.is_file():
                out.append((p.name, db))
    return out


def read_sessions(profile: str, db_path: Path) -> List[dict]:
    """Non-empty, non-hidden sessions from one database. Read-only, fail-soft."""
    rows: List[dict] = []
    try:
        con = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        print(f"  [ignore] {db_path} unreadable: {exc}", file=sys.stderr)
        return rows
    try:
        cur = con.execute(
            """
            SELECT id, started_at, source, message_count, title
            FROM sessions
            WHERE COALESCE(hidden, 0) = 0
              AND COALESCE(message_count, 0) > 0
            ORDER BY started_at
            """
        )
        for sid, started, source, nmsg, title in cur:
            if not started:
                continue
            dt = _dt.datetime.fromtimestamp(float(started))
            rows.append(
                {
                    "id": sid,
                    "profile": profile,
                    "day": dt.strftime("%Y-%m-%d"),
                    "time": dt.strftime("%H:%M"),
                    "source": (source or "?").strip(),
                    "msgs": int(nmsg or 0),
                    "title": shorten(title or "(untitled)"),
                }
            )
    except sqlite3.Error as exc:
        print(f"  [ignore] unexpected schema in {db_path}: {exc}", file=sys.stderr)
    finally:
        con.close()
    return rows


# --- Rendu ------------------------------------------------------------------

_LINK_RE = re.compile(r"@session:([^/\s]+)/(\S+)\s*$")

# Index line: - `HH:MM` source · N msg · TITLE · @session:profile/id
_ROW_RE = re.compile(
    r"^-\s+`(?P<time>[\d:?]{4,5})`\s+(?P<source>\S+)\s+·\s+(?P<msgs>\d+)\s+msg\s+·\s+"
    r"(?P<title>.*?)\s+·\s+@session:(?P<profile>[^/\s]+)/(?P<id>\S+)\s*$"
)


def render(host: str, sessions: List[dict], generated_at: str) -> str:
    by_day: Dict[str, List[dict]] = {}
    for s in sessions:
        by_day.setdefault(s["day"], []).append(s)

    lines: List[str] = [
        f"# Session index — {host}",
        "",
        "Human-readable index of this host's Hermes conversations. Generated by "
        "`bin/session_index.py`; **one line = one session**.",
        "The `@session:` field reopens it in Hermes (the agent can re-read it with "
        "`session_search`).",
        "",
        "Transcripts stay LOCAL (`state.db`): what travels between hosts is this "
        "index, not the database.",
        "",
        f"Generated at: {generated_at} · {len(sessions)} session(s)",
        "",
    ]

    for day in sorted(by_day, reverse=True):
        try:
            d = _dt.date.fromisoformat(day)
            label = f"{day} ({_DAY[d.weekday()]})"
        except ValueError:
            label = day
        lines.append(f"## {label}")
        lines.append("")

        conv = [s for s in by_day[day] if s["source"] in _HUMAN_SOURCES]
        auto = [s for s in by_day[day] if s["source"] not in _HUMAN_SOURCES]

        if conv:
            lines.append("Conversations:")
            for s in sorted(conv, key=lambda x: x["time"]):
                lines.append(
                    f"- `{s['time']}` {s['source']} · {s['msgs']} msg · {s['title']} "
                    f"· @session:{s['profile']}/{s['id']}"
                )
            lines.append("")
        if auto:
            lines.append(f"Scheduled tasks ({len(auto)}):")
            for s in sorted(auto, key=lambda x: x["time"]):
                lines.append(
                    f"- `{s['time']}` {s['source']} · {s['msgs']} msg · {s['title']} "
                    f"· @session:{s['profile']}/{s['id']}"
                )
            lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def merge_existing(existing_text: str, sessions: List[dict]) -> List[dict]:
    """Re-inject sessions present in the index but absent from state.db.

    Reason: `sessions.retention_days` may purge a session; the index must remain
    the long-term memory. Without this merge, a later run would silently erase
    conversations from the history.
    """
    have = {(s["profile"], s["id"]) for s in sessions}
    out = list(sessions)
    day: Optional[str] = None
    src: Optional[str] = None
    for line in existing_text.splitlines():
        m = re.match(r"^##\s+(\d{4}-\d{2}-\d{2})", line)
        if m:
            day = m.group(1)
            src = None
            continue
        if line.startswith("Conversations"):  # also matches old "Conversations :"
            src = "human"
            continue
        if line.startswith("Scheduled tasks"):
            src = "auto"
            continue
        if not line.startswith("- ") or not day:
            continue
        rm = _ROW_RE.match(line)
        if not rm or src is None:
            continue
        profile, sid = rm.group("profile"), rm.group("id")
        if (profile, sid) in have:
            continue
        out.append(
            {
                "id": sid,
                "profile": profile,
                "day": day,
                "time": rm.group("time"),
                "source": rm.group("source"),
                "msgs": int(rm.group("msgs") or 0),
                "title": shorten(rm.group("title")),
                "_recovered": True,
            }
        )
        have.add((profile, sid))
    return out


def build(hermes_home: Path, brain_dir: Path, dry_run: bool = False) -> int:
    host = host_name_hint()
    dbs = session_dbs(hermes_home)
    if not dbs:
        print(f"no state.db under {hermes_home} — nothing to index.")
        return 1

    sessions: List[dict] = []
    for profile, db in dbs:
        got = read_sessions(profile, db)
        print(f"  {profile:16s} {len(got):4d} session(s)  ({db})")
        sessions.extend(got)

    out_dir = brain_dir / "wiki" / "sessions"
    out_file = out_dir / f"index-{host}.md"
    if out_file.is_file():
        sessions = merge_existing(out_file.read_text(encoding="utf-8"), sessions)

    stamp = _dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    text = render(host, sessions, stamp)

    if dry_run:
        print(text[:2000])
        print(f"[dry-run] {len(sessions)} sessions -> {out_file}")
        return 0

    out_dir.mkdir(parents=True, exist_ok=True)
    out_file.write_text(text, encoding="utf-8")
    print(f"[ok] {len(sessions)} session(s) indexed -> {out_file}")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Human-readable index of a host's Hermes sessions")
    ap.add_argument("--hermes-home", default=None)
    ap.add_argument("--brain-dir", default=None)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    hh = Path(a.hermes_home) if a.hermes_home else hermes_home_default()
    bd = Path(a.brain_dir) if a.brain_dir else brain_dir_default()
    print(f"hermes_home : {hh}")
    print(f"brain_dir   : {bd}")
    return build(hh, bd, a.dry_run)


if __name__ == "__main__":
    sys.exit(main())
