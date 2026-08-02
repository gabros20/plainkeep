#!/usr/bin/env python3
"""run_sweepverb.py — exercises the real `plainkeep sweep` verb against a temp HOME (PLAINKEEP_SWEEP_HOME):
7-day promote → _swept/YYYY-MM/, 60-day trash, move-never-delete, idempotency."""
from __future__ import annotations
import os
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path
from lib.hermetic import seal
seal()   # hermetic: an empty throwaway registry, never the developer's real vault

REPO = Path(__file__).resolve().parents[1]
GREEN, RED, DIM, BOLD, RESET = "\033[32m", "\033[31m", "\033[2m", "\033[1m", "\033[0m"
DAY = 86400
results = []


def check(name, cond, detail=""):
    results.append((name, cond, detail))


def aged(p: Path, days: float):
    t = time.time() - days * DAY
    os.utime(p, (t, t))


def run(home):
    env = {**os.environ, "PLAINKEEP_SWEEP_HOME": str(home), "PLAINKEEP_HOME": str(home)}
    return subprocess.run([sys.executable, str(REPO / "bin" / "sweep" / "run.py")],
                          capture_output=True, text=True, env=env)


def main() -> int:
    with tempfile.TemporaryDirectory() as td:
        h = Path(td)
        desk = h / "Desktop"; desk.mkdir()
        old = desk / "old-screenshot.png"; old.write_text("x"); aged(old, 9)      # untouched 9d → promote
        fresh = desk / "today.pdf"; fresh.write_text("y")                          # fresh → stays
        # a file already in _swept for 61 days → should be trashed
        bucket = f"{datetime.now().year:04d}-{datetime.now().month:02d}"
        oldswept = desk / "_swept" / bucket; oldswept.mkdir(parents=True)
        stale = oldswept / "ancient.zip"; stale.write_text("z"); aged(stale, 61)

        r = run(h)
        check("sweep promotes untouched files", not old.exists()
              and any((desk / "_swept").rglob("old-screenshot.png")), r.stdout + r.stderr)
        check("sweep leaves fresh files alone", fresh.exists())
        check("sweep moves (never deletes) — file is in _swept", any((desk / "_swept").rglob("old-screenshot.png")))
        check("sweep trashes 60-day-old _swept items", not stale.exists()
              and (h / ".Trash" / "ancient.zip").exists(), r.stdout)

        # idempotency: second run is a no-op for the now-fresh state
        before = sorted(str(p) for p in (desk / "_swept").rglob("*"))
        r2 = run(h)
        after = sorted(str(p) for p in (desk / "_swept").rglob("*"))
        check("sweep is idempotent (second run no-op)", before == after and "0 promoted to _swept, 0 trashed" in r2.stdout, r2.stdout)

    print(f"{BOLD}Sweep verb (§9.4 decay machine) — {len(results)} checks{RESET}\n")
    passed = sum(1 for _, ok, _ in results if ok)
    for name, ok, detail in results:
        mark = f"{GREEN}PASS{RESET}" if ok else f"{RED}FAIL{RESET}"
        print(f"  {mark} {name:<46}" + (f" {DIM}{detail.strip()[:80]}{RESET}" if (detail and not ok) else ""))
    failed = len(results) - passed
    print(f"\n{BOLD}Result:{RESET} {GREEN}{passed} passed{RESET}, "
          f"{(RED if failed else DIM)}{failed} failed{RESET}, {len(results)} checks")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
