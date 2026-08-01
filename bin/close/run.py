#!/usr/bin/env python3
"""
plainkeep close — daily close: summarize the day into the journal and flag loose ends (§16).
Pure-shell fallback (no agent): assemble today's commits + completed tasks, and flag active tasks
with no progress today + uncommitted changes. (An agent would write prose; this writes the facts.)
"""
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lib import output, paths, vaultio  # noqa: E402


def main(argv):
    _, argv = output.parse_argv(argv)
    automated = "--automated" in argv  # used by the nightly nudge job (§15)
    dry = "--dry-run" in argv
    td = date.today().isoformat()
    note = paths.journal_path(date.today()) if dry else paths.ensure_journal()[0]

    commits = [ln for ln in paths.git("log", "--since=00:00:00", "--oneline").splitlines() if ln.strip()]
    done_today = [f for f in (paths.TASKS / "done").glob("T-*.md")
                  if paths.fm_field(f, "updated") == td] if (paths.TASKS / "done").exists() else []
    active = sorted((paths.TASKS / "active").glob("T-*.md")) if (paths.TASKS / "active").exists() else []
    no_progress = [f for f in active if paths.fm_field(f, "updated") != td]
    dirty = [ln for ln in paths.git("status", "--porcelain").splitlines() if ln.strip()]

    block = [f"\n## Close — {td}",
             f"- commits today: {len(commits)}"]
    block += [f"    {c}" for c in commits[:10]]
    block.append(f"- completed: {len(done_today)} task(s)")
    block += [f"    ✓ {f.stem} {paths.title_of(f)}" for f in done_today]
    if no_progress:
        block.append(f"- ⚠ {len(no_progress)} active task(s) with no progress today: "
                     + ", ".join(f.stem for f in no_progress))
    if dirty:
        block.append(f"- ⚠ {len(dirty)} uncommitted change(s) in ~/plainkeep (commit before backup)")
    if not dry:
        with vaultio.open_append(note, encoding="utf-8") as fh:
            fh.write("\n".join(block) + "\n")
        paths.append_journal("closed the day" + (" (automated)" if automated else ""))

    data = {
        "journal": str(note.relative_to(paths.PLAINKEEP_HOME)),
        "commits": len(commits), "completed": len(done_today),
        "no_progress": len(no_progress), "no_progress_ids": [f.stem for f in no_progress],
        "uncommitted": len(dirty), "dry_run": dry,
    }

    def render(_):
        print(f"day {'would close' if dry else 'closed'} -> {note.relative_to(paths.PLAINKEEP_HOME)}"
              + ("  (dry run — nothing written)" if dry else ""))
        print(f"  commits today: {len(commits)} | completed: {len(done_today)} | "
              f"active without progress: {len(no_progress)} | uncommitted: {len(dirty)}")
        if no_progress:
            print("  nudge: " + ", ".join(f.stem for f in no_progress) + " saw no progress today")

    return output.emit(data, "close", human=render)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
