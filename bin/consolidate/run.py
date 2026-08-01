#!/usr/bin/env python3
"""
plainkeep consolidate [--automated] — the nightly dream-lite maintenance pass (§9, §15). Zero-LLM:
scans the wiki for orphans + stale notes, summarizes the day's activity (commits, completed tasks,
captures), and writes a digest into today's journal. Read + a single journal append; safe to cron.
"""
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lib import output, paths, vaultio  # noqa: E402


def _wiki_health():
    if not paths.WIKI.exists():
        return 0, [], []
    notes = {p.stem: p for p in paths.WIKI.rglob("*.md")}
    inbound = {s: 0 for s in notes}
    for s, p in notes.items():
        for tgt in paths.link_targets(p.read_text(encoding="utf-8")):
            if tgt in inbound and tgt != s:
                inbound[tgt] += 1
    orphans = sorted(s for s, p in notes.items()
                     if inbound[s] == 0 and paths.fm_field(p, "type") == "note"
                     and s not in ("index", "conventions"))
    cutoff = date.today().toordinal() - 180
    stale = []
    for s, p in notes.items():
        u = paths.fm_field(p, "updated")
        try:
            if date.fromisoformat(u[:10]).toordinal() < cutoff:
                stale.append(s)
        except Exception:
            pass
    return len(notes), orphans, sorted(stale)


def main(argv):
    _, argv = output.parse_argv(argv)
    dry = "--dry-run" in argv
    td = date.today().isoformat()
    n_notes, orphans, stale = _wiki_health()
    commits = len([ln for ln in paths.git("log", "--since=00:00:00", "--oneline").splitlines() if ln.strip()])
    done_today = [f for f in (paths.TASKS / "done").glob("T-*.md")
                  if paths.fm_field(f, "updated") == td] if (paths.TASKS / "done").exists() else []
    captures = len(list(paths.INBOX.glob("cap-*.md"))) if paths.INBOX.exists() else 0

    block = [f"\n## Consolidate — {td}",
             f"- wiki: {n_notes} notes | {len(orphans)} orphan(s) | {len(stale)} stale (180d+)",
             f"- today: {commits} commit(s) | {len(done_today)} task(s) completed | {captures} capture(s) waiting"]
    if orphans:
        block.append(f"- link these orphans: {', '.join(orphans[:12])}")
    if stale:
        block.append(f"- revisit stale: {', '.join(stale[:12])}")

    note = paths.journal_path(date.today())
    if not dry:
        note, _ = paths.ensure_journal()
        with vaultio.open_append(note, encoding="utf-8") as fh:
            fh.write("\n".join(block) + "\n")
        paths.append_journal("consolidated" + (" (automated)" if "--automated" in argv else ""))

    data = {"dry_run": dry, "journal": str(note.relative_to(paths.PLAINKEEP_HOME)),
            "notes": n_notes, "orphans": orphans, "stale": stale,
            "commits": commits, "done_today": len(done_today), "captures": captures}

    def render(_):
        head = "would consolidate ->" if dry else "consolidate ->"
        print(f"{head} {note.relative_to(paths.PLAINKEEP_HOME)}")
        print(f"  wiki: {n_notes} notes, {len(orphans)} orphans, {len(stale)} stale | "
              f"today: {commits} commits, {len(done_today)} done, {captures} to triage")
        if dry:
            print("  (dry run — nothing written)")

    return output.emit(data, "consolidate", human=render)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
