#!/usr/bin/env python3
"""wiki_decisions_gap.py — safety net for the decision journal.

WHY THIS SCRIPT EXISTS
  The rule "record decisions in brain/wiki/projects/" lives in SOUL.md: it is
  therefore *soft*. A model can forget it at the end of a long session, and nothing
  reports it. This script is the net: it finds sessions where a decision evidently
  happened and CHECKS that the journal covers it. What is missing gets SIGNALLED —
  never written silently.

WHAT THIS SCRIPT DOES NOT DO (measurement, not modesty)
  It does NOT write the entries in the agent's place. A real test showed that
  keyword-extracting decisions from the agent's prose produces ~75 candidates per
  day (227 / 3 days), including meta-discourse where the model talks about its own
  design: unusable as a journal. Writing stays the agent's job, the only one
  holding the RATIONALE in context. This script provides detection and comparison,
  not drafting.

DETECTION: four signals, weakest to strongest
  A) real durable writes (memory / fact_store) — clean signal, low recall
  B) explicit course changes ("instead of", "rollback", "fixed"...) — the STRONG
     signal, ~3 sessions/day measured: the one that matters
  C) the repo's project files touched in the session
  D) the session actually WROTE to the journal (write_file/patch on wiki/projects)
     — the most reliable: a fact, not prose, and language-independent

USAGE
  python scripts/wiki_decisions_gap.py --days 7                 # text report
  python scripts/wiki_decisions_gap.py --days 7 --json          # machine output
  python scripts/wiki_decisions_gap.py --days 7 --write-stub    # creates a
                                                                # "to complete" section
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

# --- Configuration ----------------------------------------------------------

# Signal B: phrasings that betray a course change or a rule being set.
#
# Precision vs recall: a weak marker alone is NOISY (measured: an astronomy
# calculation — "357.8 days per year instead of 365.25" — showed up as a
# decision). So we require either a STRONG marker alone, or a weak marker
# ACCOMPANIED by an action word (fixed, chose, dropped) in the same window.
#
# Markers are language-specific: these ship for English and French. Add your
# own language's phrasings here — the detector is intentionally just a word list.
_STRONG_MARKERS = [
    "rollback", "abandon", "replac", "fix", "i was wrong",
    "design flaw", "design error", "decision:", "rationale:",
    "never ", "forbidden", "banned",
    # French equivalents (kept: useful when an agent journals in French)
    "abandonn", "remplac", "corrig", "j'avais tort",
    "erreur de conception", "décision :", "motif :", "ne jamais", "interdit",
]
_WEAK_MARKERS = [
    "instead of", "rather than", "in the end", "went back on",
    "actually", "turns out", "i prefer", "i'd rather",
    # French equivalents
    "au lieu de", "plutot que", "plutôt que", "finalement",
    "revient sur", "en realite", "en réalité", "je prefere", "je préfère",
]
_ACTION_WORDS = [
    "i ", "we ", "i've", "we've", "chose", "changed", "rewrote", "kept",
    "left", "switched", "dropped",
    # French equivalents
    "je ", "nous ", "j'ai", "on a", "plutot que de", "choisi", "revu",
    "change", "modifi", "refait", "garde", "laisse",
]

# Durable writes: signal A. Tool names as stored in Hermes' message table.
_DURABLE_TOOLS = ("memory", "fact_store")

# Sources that are real conversations (the rest = cron noise). Extend as you
# enable more platforms: the platform name is stored per session.
_HUMAN_SOURCES = ("cli", "telegram", "whatsapp", "discord", "signal", "web", "oneshot")


def hermes_home() -> Path:
    return Path(
        os.environ.get("HERMES_HOME")
        or str(Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "hermes")
    )


def brain_dir() -> Path:
    return Path(os.environ.get("BRAIN_DIR") or (hermes_home() / "brain"))


def _host() -> str:
    """Host name, identical to the one brain.py uses (BRAIN_HOST else machine)."""
    import platform
    forced = os.environ.get("BRAIN_HOST") or platform.node() or "unknown"
    return re.sub(r"[^A-Za-z0-9_.-]", "_", forced)


def _dbs(home: Path) -> List[Tuple[str, Path]]:
    out: List[Tuple[str, Path]] = []
    main = home / "state.db"
    if main.is_file():
        out.append(("default", main))
    prof = home / "profiles"
    if prof.is_dir():
        for p in sorted(prof.iterdir()):
            if (p / "state.db").is_file():
                out.append((p.name, p / "state.db"))
    return out


# --- Detection --------------------------------------------------------------

def _hits(content: str) -> Optional[str]:
    """Return the triggered marker, or None. See the marker comment above."""
    low = content.lower()
    for mk in _STRONG_MARKERS:
        if mk in low:
            return mk
    for mk in _WEAK_MARKERS:
        i = low.find(mk)
        if i < 0:
            continue
        window = low[max(0, i - 400): i + 400]
        if any(aw in window for aw in _ACTION_WORDS):
            return mk
    return None


def scan(home: Path, days: int) -> List[dict]:
    since = (_dt.datetime.now() - _dt.timedelta(days=days)).timestamp()
    pats = _STRONG_MARKERS + _WEAK_MARKERS
    where = " OR ".join(["m.content LIKE ?"] * len(pats))
    args = [f"%{p}%" for p in pats]
    found: Dict[Tuple[str, str], dict] = {}

    for profile, db in _dbs(home):
        try:
            con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
        except sqlite3.Error as exc:
            print(f"  [ignore] {db}: {exc}", file=sys.stderr)
            continue
        try:
            # Signal B — the strong one.
            q = f"""
            SELECT s.id, s.title, s.source, s.started_at, m.id, m.content
            FROM messages m JOIN sessions s ON s.id = m.session_id
            WHERE m.role = 'assistant' AND s.started_at > ?
              AND s.source IN ({",".join("?" * len(_HUMAN_SOURCES))})
              AND ({where})
            ORDER BY s.started_at DESC
            """
            for sid, title, source, started, mid, content in con.execute(
                q, [since, *_HUMAN_SOURCES, *args]
            ):
                mk = _hits(content or "")
                if not mk:
                    continue  # weak marker without action word: noise, dropped
                key = (profile, sid)
                rec = found.setdefault(
                    key,
                    {
                        "profile": profile,
                        "session_id": sid,
                        "title": title or "(untitled)",
                        "source": source,
                        "started": _dt.datetime.fromtimestamp(float(started)).strftime("%Y-%m-%d %H:%M"),
                        "signals": [],
                        "durable_writes": 0,
                        "wrote_journal": False,
                        "excerpt": "",
                    },
                )
                if not rec["excerpt"] and content:
                    low = content.lower()
                    i = low.find(mk)
                    rec["excerpt"] = re.sub(
                        r"\s+", " ", content[max(0, i - 120): i + 240]
                    ).strip()

            # Signal A — durable writes.
            qa = f"""
            SELECT s.id, count(*)
            FROM messages m JOIN sessions s ON s.id = m.session_id
            WHERE s.started_at > ? AND m.tool_name IN ({",".join("?" * len(_DURABLE_TOOLS))})
            GROUP BY s.id
            """
            for sid, n in con.execute(qa, [since, *_DURABLE_TOOLS]):
                key = (profile, sid)
                if key in found:
                    found[key]["durable_writes"] = n

            # Signal C — sessions that touched the brain repo.
            qc = f"""
            SELECT DISTINCT s.id FROM messages m JOIN sessions s ON s.id = m.session_id
            WHERE s.started_at > ?
              AND (m.content LIKE '%wiki/projects%' OR m.content LIKE '%brain/wiki%')
            """
            for (sid,) in con.execute(qc, [since]):
                key = (profile, sid)
                if key in found:
                    found[key]["signals"].append("touched wiki/projects")

            # Signal D (the MOST reliable): did the session actually WRITE to the
            # journal? Precise and language-independent. MEASURED rationale: matching
            # the session title (generated in one language by the LLM) against journal
            # entries (written in another) fails — the two share no vocabulary. So we
            # no longer guess: we check whether a write_file/patch call actually
            # targeted wiki/projects during that session.
            qd = """
            SELECT DISTINCT s.id, m.tool_calls
            FROM messages m JOIN sessions s ON s.id = m.session_id
            WHERE s.started_at > ? AND m.tool_calls IS NOT NULL
              AND m.tool_calls LIKE '%wiki/projects%'
            """
            for sid, tc in con.execute(qd, [since]):
                key = (profile, sid)
                if key in found:
                    found[key]["wrote_journal"] = True
        except sqlite3.Error as exc:
            print(f"  [ignore] unexpected schema {db}: {exc}", file=sys.stderr)
        finally:
            con.close()

    return sorted(found.values(), key=lambda r: r["started"], reverse=True)


# --- Journal coverage -------------------------------------------------------

def journal_entries(brain: Path) -> Dict[str, List[dict]]:
    """Journal entries by project: [{date, text}]."""
    out: Dict[str, List[dict]] = {}
    root = brain / "wiki" / "projects"
    if not root.is_dir():
        return out
    for f in sorted(root.glob("*.md")):
        # Working files (`_to-complete-*.md`) are NOT a journal: counting them as a
        # project would let a session believe itself covered by its own mention.
        if f.name.startswith("_"):
            continue
        entries: List[dict] = []
        for line in f.read_text(encoding="utf-8").splitlines():
            # Tolerates markdown bold: `- **2026-09-25 — Decision: ...**` is the format
            # the agent actually uses (and the most readable). Without this tolerance,
            # the scan reports "0 entries" while the journal is full — measured bug.
            m = re.match(r"^\s*-\s*\**\s*(\d{4}-\d{2}-\d{2})\s*[—–-]\s*(.+)$", line)
            if m:
                entries.append({"date": m.group(1), "text": m.group(2).strip().strip("*").strip()})
                continue
            # Continuation of an entry (indented): append to the last one.
            if entries and re.match(r"^\s{2,}\S", line):
                entries[-1]["text"] += " " + line.strip()
        out[f.stem] = entries
    return out


def is_covered(session: dict, journal: Dict[str, List[dict]]) -> bool:
    """Is the session covered by the journal?

    Two criteria, most reliable first:

    1. **The session wrote to the journal** (signal D). Precise, language-
       independent, and it is the normal case: writing is an agent reflex, so the
       session that made the decision is the one that recorded it.
    2. Otherwise, session-title / day-entries matching. Fallback heuristic,
       deliberately coarse, for cases where the decision was recorded in a later
       session. Window = session day OR the next day (a 23:52 session records
       after midnight).

    A false positive (session wrongly seen as covered) costs less than a false
    negative (re-reading a decision already recorded, and the report loses its
    credibility).
    """
    if session.get("wrote_journal"):
        return True

    try:
        d0 = _dt.date.fromisoformat(session["started"][:10])
    except ValueError:
        return False
    days = {d0.isoformat(), (d0 + _dt.timedelta(days=1)).isoformat()}

    words = {
        w for w in re.findall(r"[a-zà-ÿ]{5,}", session["title"].lower())
        if w not in {"sessions", "session", "since", "after", "before", "between", "because"}
    }
    if not words:
        return False
    for entries in journal.values():
        for e in entries:
            if e["date"] not in days:
                continue
            txt = e["text"].lower()
            if sum(1 for w in words if w in txt) >= 2:
                return True
    return False


# --- Rendering --------------------------------------------------------------

def report(sessions: List[dict], journal: Dict[str, List[dict]], days: int) -> str:
    rows = []
    for s in sessions:
        rows.append({**s, "covered": is_covered(s, journal)})

    total = len(rows)
    miss = [r for r in rows if not r["covered"]]
    ent_week = sum(
        1 for entries in journal.values() for e in entries
        if e["date"] >= (_dt.date.today() - _dt.timedelta(days=days)).isoformat()
    )

    lines = [
        f"Decision journal — coverage over the last {days} days",
        "",
        f"  sessions with a decision signal : {total}",
        f"  journal entries in the period   : {ent_week}",
        f"  sessions NOT covered            : {len(miss)}",
        "",
    ]
    if not total:
        lines.append("No session with a decision signal in the period.")
        return "\n".join(lines)

    lines.append("To review (strong signal without a corresponding journal entry):")
    lines.append("")
    for r in miss[:15]:
        extra = f" · {r['durable_writes']} durable write(s)" if r["durable_writes"] else ""
        sig = f" · {', '.join(r['signals'])}" if r["signals"] else ""
        lines.append(f"  [{r['started']}] {r['source']} · {r['title'][:58]}{extra}{sig}")
        lines.append(f"      @session:{r['profile']}/{r['session_id']}")
        if r["excerpt"]:
            lines.append(f"      « {r['excerpt'][:170]} »")
        lines.append("")

    if len(miss) > 15:
        lines.append(f"  (+{len(miss) - 15} more — rerun with --json to see all)")
        lines.append("")

    lines.append("Reminder: this report SIGNALS, it does not write in your place. The")
    lines.append("rationale of a decision cannot be reconstructed after the fact — drafting")
    lines.append("is the agent's job.")
    return "\n".join(lines)


def write_stub(brain: Path, rows: List[dict], host: str) -> Optional[Path]:
    """Create/complete wiki/projects/_to-complete-<host>.md with the uncovered sessions.

    ONE FILE PER HOST, like the session index: each host scans ITS OWN sessions, so
    two hosts writing the same _to-complete.md would produce a git conflict on every
    run. Single writer = structurally impossible conflict.
    """
    miss = [r for r in rows if not r["covered"]]
    if not miss:
        return None
    f = brain / "wiki" / "projects" / f"_to-complete-{host}.md"
    stamp = _dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = [
        f"# To complete — {host}",
        "",
        f"Generated automatically on {stamp} by `scripts/wiki_decisions_gap.py`.",
        "These sessions carry a decision signal without a corresponding journal entry.",
        "For each one: add the dated entry in the relevant project's file",
        "(decision, rationale, replacement condition, rejected alternative),",
        "then remove the line here.",
        "",
    ]
    for r in miss:
        lines.append(f"- [{r['started']}] {r['title'][:70]}")
        lines.append(f"  @session:{r['profile']}/{r['session_id']}")
        if r["excerpt"]:
            lines.append(f"  lead: {r['excerpt'][:200]}")
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return f


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Decision journal safety net")
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--hermes-home", default=None)
    ap.add_argument("--brain-dir", default=None)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--write-stub", action="store_true")
    a = ap.parse_args(argv)

    home = Path(a.hermes_home) if a.hermes_home else hermes_home()
    brain = Path(a.brain_dir) if a.brain_dir else brain_dir()

    sessions = scan(home, a.days)
    journal = journal_entries(brain)
    rows = [{**s, "covered": is_covered(s, journal)} for s in sessions]

    if a.json:
        print(json.dumps(
            {
                "days": a.days,
                "sessions_with_signal": len(rows),
                "journal_projects": list(journal),
                "uncovered": [
                    {k: v for k, v in r.items() if k != "excerpt"}
                    for r in rows if not r["covered"]
                ],
            },
            ensure_ascii=False, indent=2,
        ))
    else:
        print(report(sessions, journal, a.days))

    if a.write_stub:
        host = os.environ.get("BRAIN_HOST") or _host()
        f = write_stub(brain, rows, host)
        print(f"\n[stub] {'written: ' + str(f) if f else 'nothing to report — journal up to date'}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
