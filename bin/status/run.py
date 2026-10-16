#!/usr/bin/env python3
"""plainkeep status — one-screen dashboard: tasks, inbox, last index, repo state (§4.1)."""
import subprocess
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lib import output, paths  # noqa: E402

DB = paths.PLAINKEEP_HOME / ".index" / "plainkeep.sqlite"


def _count(d: Path, pat: str) -> int:
    return len(list(d.glob(pat))) if d.exists() else 0


def _last_full_index():
    from lib import indexlib
    return indexlib.last_full_index_ts()


def _pending() -> int:
    from lib import indexlib
    return indexlib.pending_embeddings()


def main(argv):
    _, argv = output.parse_argv(argv)
    active = sorted((paths.TASKS / "active").glob("T-*.md")) if (paths.TASKS / "active").exists() else []
    waiting = sorted((paths.TASKS / "waiting").glob("T-*.md")) if (paths.TASKS / "waiting").exists() else []
    inbox = _count(paths.INBOX, "*.md")
    wiki = _count(paths.WIKI, "**/*.md")

    index_age = None
    stamp = _last_full_index()   # the last finished `plainkeep index`, not the sqlite mtime (writes touch that)
    if stamp is not None:
        index_age = int((datetime.now() - datetime.fromtimestamp(stamp)).total_seconds() // 60)
    git_dirty = None
    try:
        dirty = subprocess.run(["git", "-C", str(paths.PLAINKEEP_HOME), "status", "--porcelain"],
                               capture_output=True, text=True, timeout=10).stdout.strip()
        git_dirty = len(dirty.splitlines()) if dirty else 0
    except Exception:
        pass

    data = {
        "plainkeep_home": str(paths.PLAINKEEP_HOME),
        "active": len(active), "waiting": len(waiting),
        "active_tasks": [{"id": f.stem, "title": paths.title_of(f)} for f in active],
        "waiting_tasks": [{"id": f.stem, "title": paths.title_of(f)} for f in waiting],
        "inbox": inbox, "wiki": wiki,
        "index_age_min": index_age, "pending_embeddings": _pending(), "git_dirty": git_dirty,
    }

    def render(_):
        print(f"plainkeep status — {paths.PLAINKEEP_HOME}")
        print(f"  tasks:   {len(active)} active, {len(waiting)} waiting")
        for f in active[:7]:
            print(f"    • {f.stem}  {paths.title_of(f)}")
        if len(active) > 7:
            print(f"    … +{len(active)-7} more active")
        for f in waiting[:5]:
            print(f"    ⏸ {f.stem}  {paths.title_of(f)}")
        print(f"  inbox:   {inbox} item(s) to triage")
        print(f"  wiki:    {wiki} note(s)")
        if stamp is not None:
            print(f"  index:   built {index_age} min ago" if index_age else "  index:   built just now")
        elif DB.exists():
            print("  index:   partial (written note by note, never fully built — run: plainkeep index)")
        else:
            print("  index:   not built (run: plainkeep index)")
        if data["pending_embeddings"]:
            print(f"  vectors: {data['pending_embeddings']} note(s) pending embedding (run: plainkeep index)")
        if git_dirty is not None:
            print(f"  git:     {'clean' if git_dirty == 0 else str(git_dirty) + ' uncommitted change(s)'}")

    return output.emit(data, "status", human=render)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
