#!/usr/bin/env python3
"""
plainkeep week — weekly review (§16): shipped vs stalled, repo health, the compounding question, and
sweep done/ tasks into done/<year>/. Writes a review block into today's journal.
"""
import sys
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lib import output, paths, vaultio  # noqa: E402


def _d(s: str):
    try:
        return date.fromisoformat(s[:10])
    except Exception:
        return None


def main(argv):
    _, argv = output.parse_argv(argv)
    dry = "--dry-run" in argv
    today = date.today()
    week_ago = today.toordinal() - 7
    donedir = paths.TASKS / "done"
    activedir = paths.TASKS / "active"

    done_all = sorted(donedir.glob("T-*.md")) if donedir.exists() else []   # top-level (unswept)
    shipped = [f for f in done_all if (_d(paths.fm_field(f, "updated")) or date.min).toordinal() >= week_ago]
    active = sorted(activedir.glob("T-*.md")) if activedir.exists() else []
    stalled = [f for f in active if (_d(paths.fm_field(f, "updated")) or date.min).toordinal() < week_ago]
    dirty = len([ln for ln in paths.git("status", "--porcelain").splitlines() if ln.strip()])

    # sweep done/*.md -> done/<year>/  (--dry-run: count what WOULD be swept, move nothing)
    swept = 0
    for f in done_all:
        if dry:
            swept += 1
            continue
        yr = (paths.fm_field(f, "created")[:4]) or str(today.year)
        dest = donedir / yr
        vaultio.mkdir(dest)
        f.rename(dest / f.name)
        swept += 1

    block = [f"\n## Weekly review — {today.isoformat()}",
             f"- shipped (last 7d): {len(shipped)}"]
    block += [f"    ✓ {f.stem} {paths.title_of(f)}" for f in shipped]
    if stalled:
        block.append(f"- stalled (active, untouched 7d+): {len(stalled)}")
        block += [f"    … {f.stem} {paths.title_of(f)}" for f in stalled]
    block.append(f"- repo: {'clean' if dirty == 0 else str(dirty) + ' uncommitted'}")
    block.append(f"- swept {swept} done task(s) into done/<year>/")
    block.append("- ? what did you do by hand twice this week → a skill/verb candidate (§11)")
    note = paths.journal_path(today)
    if not dry:
        note, _ = paths.ensure_journal()
        with vaultio.open_append(note, encoding="utf-8") as fh:
            fh.write("\n".join(block) + "\n")
        paths.append_journal("weekly review")

    data = {
        "journal": str(note.relative_to(paths.PLAINKEEP_HOME)),
        "shipped": len(shipped), "shipped_ids": [f.stem for f in shipped],
        "stalled": len(stalled), "stalled_ids": [f.stem for f in stalled],
        "repo_dirty": dirty, "swept": swept, "dry_run": dry,
    }

    def render(_):
        print(f"weekly review{' (dry run — nothing written)' if dry else ''} -> {note.relative_to(paths.PLAINKEEP_HOME)}")
        print(f"  shipped: {len(shipped)} | stalled: {len(stalled)} | repo: "
              f"{'clean' if dirty == 0 else str(dirty)+' dirty'} | swept: {swept}")
        print("  ask: what did you do by hand twice this week? → skill/verb candidate")

    return output.emit(data, "week", human=render)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
