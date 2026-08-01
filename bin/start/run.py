#!/usr/bin/env python3
"""plainkeep start — daily start: open today's journal, carry forward open tasks, show the dashboard (§16)."""
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lib import output, paths, vaultio  # noqa: E402


def _tasks(status):
    d = paths.TASKS / status
    return sorted(d.glob("T-*.md")) if d.exists() else []


def main(argv):
    _, argv = output.parse_argv(argv)
    dry = "--dry-run" in argv
    active, waiting = _tasks("active"), _tasks("waiting")
    inbox = len([p for p in paths.INBOX.glob("*.md")]) if paths.INBOX.exists() else 0

    # --dry-run: preview today's journal path + carry-forward without creating/seeding/logging.
    note = paths.journal_path(date.today())
    created = not note.exists()
    if not dry:
        note, created = paths.ensure_journal()
        if created:  # seed the new day's note with what's carried forward
            lines = ["\n## Carried forward\n"]
            for f in active:
                lines.append(f"- [ ] {f.stem} {paths.title_of(f)}")
            for f in waiting:
                lines.append(f"- ⏸ {f.stem} {paths.title_of(f)} (waiting: {paths.fm_field(f, 'why') or '—'})")
            lines.append("\n## Log\n")
            with vaultio.open_append(note, encoding="utf-8") as fh:
                fh.write("\n".join(lines) + "\n")

    y = paths.journal_path(date.today() - timedelta(days=1))
    data = {
        "date": date.today().isoformat(),
        "journal": str(note.relative_to(paths.PLAINKEEP_HOME)), "created": created,
        "active": len(active), "waiting": len(waiting), "inbox": inbox,
        "active_tasks": [{"id": f.stem, "title": paths.title_of(f)} for f in active],
        "waiting_tasks": [{"id": f.stem, "title": paths.title_of(f),
                           "why": paths.fm_field(f, "why") or None} for f in waiting],
        "yesterday": str(y.relative_to(paths.PLAINKEEP_HOME)) if y.exists() else None,
        "dry_run": dry,
    }

    def render(_):
        print(f"good morning — {date.today().isoformat()}" + ("  (dry run — nothing written)" if dry else ""))
        print(f"  journal: {note.relative_to(paths.PLAINKEEP_HOME)}"
              + (" (would create)" if (dry and created) else " (created)" if created else ""))
        print(f"  active:  {len(active)} task(s)")
        for f in active[:10]:
            print(f"    • {f.stem} {paths.title_of(f)}")
        if waiting:
            print(f"  waiting: {len(waiting)}")
            for f in waiting[:5]:
                print(f"    ⏸ {f.stem} {paths.title_of(f)} — {paths.fm_field(f, 'why') or 'why: ?'}")
        if inbox:
            print(f"  inbox:   {inbox} item(s) to triage  (plainkeep triage)")
        if y.exists():
            print(f"  yesterday: {y.relative_to(paths.PLAINKEEP_HOME)}")

    output.emit(data, "start", human=render)
    if not dry:
        paths.append_journal("started the day")
    return output.EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
