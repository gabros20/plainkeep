#!/usr/bin/env python3
"""
plainkeep sweep [--dry-run] — the §9.4 macOS-inbox decay machine. Desktop/Downloads files untouched for
7 days MOVE (never delete) into <zone>/_swept/YYYY-MM/; items that have sat in _swept for 60 days go
to the Trash. So you get a week to `plainkeep files ingest` what matters, then a 60-day net. Idempotent —
safe to run repeatedly and as the nightly job. Rescue is by ingest, not by reopening (§9.4).
"""
import os
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lib import output, paths, vaultio  # noqa: E402

HOME = Path(os.environ.get("PLAINKEEP_SWEEP_HOME", os.environ.get("HOME", "")))
SWEEP_DAYS = int(os.environ.get("PLAINKEEP_SWEEP_DAYS", "7"))
TRASH_DAYS = int(os.environ.get("PLAINKEEP_TRASH_DAYS", "60"))
ZONES = ["Desktop", "Downloads"]
DAY = 86400


def _age_days(p: Path) -> float:
    return (time.time() - p.stat().st_mtime) / DAY


def main(argv):
    _, argv = output.parse_argv(argv)
    dry = "--dry-run" in argv
    now = datetime.now()
    bucket = f"{now.year:04d}-{now.month:02d}"
    trash = HOME / ".Trash"
    promoted = trashed = 0
    events, rows = [], []

    def say(line):
        events.append(line)

    for zname in ZONES:
        zone = HOME / zname
        if not zone.is_dir():
            continue
        swept = zone / "_swept"

        # phase 1 — promote: untouched 7+ days → _swept/YYYY-MM/ (move, never delete)
        for item in sorted(zone.iterdir()):
            if item.name == "_swept" or item.name.startswith("."):
                continue
            if _age_days(item) >= SWEEP_DAYS:
                dest_dir = swept / bucket
                dest = dest_dir / item.name
                i = 2
                while dest.exists():
                    dest = dest_dir / f"{item.stem}-{i}{item.suffix}"; i += 1
                say(f"  {'would move' if dry else 'moved'}: {zname}/{item.name} -> _swept/{bucket}/")
                rows.append({"action": "promote", "zone": zname, "name": item.name, "bucket": bucket})
                if not dry:
                    vaultio.mkdir(dest_dir)
                    vaultio.move(str(item), str(dest))
                    os.utime(dest, None)  # stamp swept-time into mtime (starts the 60-day clock)
                promoted += 1

        # phase 2 — trash: in _swept 60+ days → Trash (still recoverable there)
        if swept.is_dir():
            for b in sorted(swept.iterdir()):
                if not b.is_dir():
                    continue
                for item in sorted(b.iterdir()):
                    if _age_days(item) >= TRASH_DAYS:
                        say(f"  {'would trash' if dry else 'trashed'}: _swept/{b.name}/{item.name}")
                        rows.append({"action": "trash", "bucket": b.name, "name": item.name})
                        if not dry:
                            trash.mkdir(parents=True, exist_ok=True)
                            tdest = trash / item.name
                            i = 2
                            while tdest.exists():
                                tdest = trash / f"{item.stem}-{i}{item.suffix}"; i += 1
                            shutil.move(str(item), str(tdest))
                        trashed += 1
                if not dry and b.is_dir() and not any(b.iterdir()):
                    b.rmdir()

    # phase 3 — share hygiene (Part 5.2): warn on expired shares + notes edited since sharing.
    share_warnings = _share_warnings()
    for w in share_warnings:
        say(f"  {'expired share' if w['kind']=='expired' else 'edited since share'}: "
            f"{w['id']} ({w['detail']})")
        rows.append({"action": w["kind"], "name": w["id"], "detail": w["detail"]})

    if not dry and (promoted or trashed):
        paths.append_journal(f"swept {promoted} to _swept, {trashed} to Trash")

    def render(_):
        for line in events:
            print(line)
        print(f"\nsweep: {promoted} promoted to _swept, {trashed} trashed"
              + (f", {len(share_warnings)} share warning(s)" if share_warnings else "")
              + (" (dry run)" if dry else ""))

    return output.emit_rows(rows, "sweep", human=render,
                            header={"promoted": promoted, "trashed": trashed,
                                    "share_warnings": len(share_warnings), "dry_run": dry})


def _share_warnings():
    """Read ~/plainkeep/.share/ledger.json (Part 5.2): flag active shares past their TTL, and active shares
    whose source note was edited AFTER it was shared (the published blob is now stale)."""
    import json
    led_path = paths.PLAINKEEP_HOME / ".share" / "ledger.json"
    try:
        shares = json.loads(led_path.read_text(encoding="utf-8")).get("shares", [])
    except Exception:
        return []
    now = int(time.time())
    warnings = []
    for s in shares:
        if s.get("revoked"):
            continue
        exp = s.get("expires_ts", 0)
        if exp and exp < now:
            warnings.append({"kind": "expired", "id": s.get("id", "?"),
                             "detail": f"expired {int((now-exp)/DAY)}d ago"})
            continue
        created = s.get("created_ts", 0)
        for rel in s.get("note_paths", []):
            p = paths.PLAINKEEP_HOME / rel
            try:
                if created and p.stat().st_mtime > created + 1:
                    warnings.append({"kind": "edited", "id": s.get("id", "?"),
                                     "detail": f"{rel} edited since shared"})
                    break
            except Exception:
                continue
    return warnings


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
