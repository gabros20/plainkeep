#!/usr/bin/env python3
"""
plainkeep orient [--json|--line] — one-call session bootstrap (proposal Part 3.4). Read-only: journal
tail (today+yesterday), active/waiting task counts + top items, inbox count, pending organize
proposals (glob defensively — a later package lands inbox/organize/), index/backup age, git
dirtiness, and recent notes. Three renders: human dashboard (default), `--json` (one envelope for
an agent), `--line` (≤60-char string cached to .cache/orient.line with a short TTL so a prompt/tmux
hook is cheap). Replaces the multi-step orientation ritual with a single safe call.
"""
import os
import subprocess
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lib import output, paths, vaultio  # noqa: E402

DB = paths.PLAINKEEP_HOME / ".index" / "plainkeep.sqlite"
LINE_CACHE = paths.PLAINKEEP_HOME / ".cache" / "orient.line"
LINE_MAX = 60


def _age_min(ts: float) -> int:
    return int((datetime.now().timestamp() - ts) // 60)


def _journal_tail(n: int = 10) -> list[str]:
    lines: list[str] = []
    for d in (date.today() - timedelta(days=1), date.today()):
        p = paths.journal_path(d)
        if p.exists():
            lines += [ln for ln in p.read_text(encoding="utf-8").splitlines() if ln.startswith("- ")]
    return lines[-n:]


def _tasks(status: str):
    d = paths.TASKS / status
    return sorted(d.glob("T-*.md")) if d.exists() else []


def _git_dirty():
    try:
        out = subprocess.run(["git", "-C", str(paths.PLAINKEEP_HOME), "status", "--porcelain"],
                             capture_output=True, text=True, timeout=10).stdout.strip()
        return len(out.splitlines()) if out else 0
    except Exception:
        return None


def _last_commit_age():
    try:
        out = subprocess.run(["git", "-C", str(paths.PLAINKEEP_HOME), "log", "-1", "--format=%ct"],
                             capture_output=True, text=True, timeout=10).stdout.strip()
        return _age_min(float(out)) if out.isdigit() else None
    except Exception:
        return None


def _recent_notes(n: int = 5) -> list[dict]:
    if not paths.WIKI.exists():
        return []
    notes = [p for p in paths.WIKI.rglob("*.md")]
    notes.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return [{"slug": p.stem, "path": str(p.relative_to(paths.PLAINKEEP_HOME))} for p in notes[:n]]


def _proposals() -> int:
    d = paths.INBOX / "organize"
    return len(list(d.glob("*.jsonl"))) if d.exists() else 0


def _gather() -> dict:
    active, waiting = _tasks("active"), _tasks("waiting")
    inbox = len(list(paths.INBOX.glob("*.md"))) if paths.INBOX.exists() else 0
    index_age = _age_min(DB.stat().st_mtime) if DB.exists() else None
    return {
        "active": len(active), "waiting": len(waiting),
        "inbox": inbox, "proposals": _proposals(),
        "index_age_min": index_age, "backup_age_min": _last_commit_age(),
        "git_dirty": _git_dirty(),
        "journal_tail": _journal_tail(),
        "top_tasks": [{"id": f.stem, "title": paths.title_of(f), "status": "active"} for f in active[:5]]
        + [{"id": f.stem, "title": paths.title_of(f), "status": "waiting"} for f in waiting[:3]],
        "recent_notes": _recent_notes(),
    }


def _line(data: dict) -> str:
    parts = [f"T{data['active']}/{data['waiting']}", f"in{data['inbox']}"]
    if data["proposals"]:
        parts.append(f"org{data['proposals']}")
    ia = data["index_age_min"]
    parts.append(f"idx{ia}m" if ia is not None else "idx-")
    gd = data["git_dirty"]
    parts.append("git-clean" if gd == 0 else (f"git*{gd}" if gd else "git?"))
    return " ".join(parts)[:LINE_MAX]


def _dashboard(data: dict):
    a, w = data["active"], data["waiting"]
    print(f"plainkeep orient — {paths.PLAINKEEP_HOME}")
    print(f"  tasks:   {a} active, {w} waiting")
    for t in data["top_tasks"]:
        mark = "•" if t["status"] == "active" else "⏸"
        print(f"    {mark} {t['id']}  {t['title']}")
    print(f"  inbox:   {data['inbox']} item(s) to triage" +
          (f", {data['proposals']} organize proposal file(s)" if data["proposals"] else ""))
    ia, ba, gd = data["index_age_min"], data["backup_age_min"], data["git_dirty"]
    print(f"  index:   {'built ' + str(ia) + ' min ago' if ia is not None else 'not built (run: plainkeep index)'}")
    print(f"  backup:  {'last commit ' + str(ba) + ' min ago' if ba is not None else 'no commits yet'}"
          + ("" if gd in (0, None) else f", {gd} uncommitted change(s)"))
    if data["recent_notes"]:
        print("  recent:  " + ", ".join(n["slug"] for n in data["recent_notes"]))
    if data["journal_tail"]:
        print("  journal:")
        for ln in data["journal_tail"]:
            print(f"    {ln}")


def main(argv):
    _, argv = output.parse_argv(argv)
    line_mode = "--line" in argv

    if line_mode:
        ttl = int(os.environ.get("PLAINKEEP_ORIENT_TTL", "30"))
        try:
            if ttl > 0 and LINE_CACHE.exists() \
                    and (datetime.now().timestamp() - LINE_CACHE.stat().st_mtime) < ttl:
                print(LINE_CACHE.read_text(encoding="utf-8").strip())
                return output.EXIT_OK
        except Exception:
            pass
        line = _line(_gather())
        try:
            vaultio.mkdir(LINE_CACHE.parent)
            vaultio.write_text(LINE_CACHE, line + "\n", encoding="utf-8")
        except Exception:
            pass
        print(line)
        return output.EXIT_OK

    data = _gather()
    data["line"] = _line(data)
    return output.emit(data, "orient", human=_dashboard)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
