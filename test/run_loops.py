#!/usr/bin/env python3
"""run_loops.py — exercises the daily/weekly rhythm verbs (start, close, week) in a temp PLAINKEEP_HOME."""
from __future__ import annotations
import os
import subprocess
import sys
import tempfile
from datetime import date
from pathlib import Path
from lib.hermetic import seal
seal()   # hermetic: an empty throwaway registry, never the developer's real vault

REPO = Path(__file__).resolve().parents[1]
TODAY = date.today().isoformat()
GREEN, RED, DIM, BOLD, RESET = "\033[32m", "\033[31m", "\033[2m", "\033[1m", "\033[0m"
results = []


def check(name, cond, detail=""):
    results.append((name, cond, detail))


def task(home, status, tid, title, created, updated):
    d = home / "tasks" / status
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{tid}.md").write_text(
        f"---\ntype: task\nid: {tid}\nstatus: {status}\ncreated: {created}\nupdated: {updated}\n"
        f"source: test\nrisk: green\nwhy: blocked on Acme keys\n---\n# {title}\n", encoding="utf-8")


def run(home, verb, *args):
    env = {**os.environ, "PLAINKEEP_HOME": str(home)}
    return subprocess.run([sys.executable, str(REPO / "bin" / verb / "run.py"), *args],
                          capture_output=True, text=True, env=env)


def journal_text(home) -> str:
    return "\n".join(p.read_text() for p in (home / "journal").rglob("*.md")) if (home / "journal").exists() else ""


def main() -> int:
    with tempfile.TemporaryDirectory() as td:
        h = Path(td)
        task(h, "active", "T-20260625-01", "Fresh active task", TODAY, TODAY)
        task(h, "active", "T-20260101-02", "Stale active task", "2026-01-01", "2026-01-01")
        task(h, "waiting", "T-20260625-03", "Blocked task", TODAY, TODAY)
        task(h, "done", "T-20260625-04", "Shipped today", TODAY, TODAY)

        # start
        r = run(h, "start")
        j = journal_text(h)
        check("start creates today's journal", (h / "journal").exists() and TODAY in j, r.stdout + r.stderr)
        check("start carries forward open tasks", "Carried forward" in j and "T-20260625-01" in j and "T-20260625-03" in j)
        check("start shows active count", "active:  2" in r.stdout or "active" in r.stdout, r.stdout)

        # close
        r = run(h, "close")
        j = journal_text(h)
        check("close writes a Close section", "## Close" in j, r.stdout + r.stderr)
        check("close counts a completion", "completed: 1" in j or "Shipped today" in j, j[-400:])
        check("close flags the stale active task", "T-20260101-02" in r.stdout or "no progress" in j, r.stdout)

        # week
        r = run(h, "week")
        yr_dir = h / "tasks" / "done" / "2026"
        top_done = list((h / "tasks" / "done").glob("T-*.md"))
        check("week sweeps done/ into done/<year>/", yr_dir.exists() and any(yr_dir.glob("T-*.md")), r.stdout + r.stderr)
        check("week empties top-level done/", len(top_done) == 0)
        check("week writes a review + lists stalled", "Weekly review" in journal_text(h)
              and ("T-20260101-02" in r.stdout or "stalled" in r.stdout), r.stdout)

    print(f"{BOLD}Daily/weekly rhythm (start, close, week) — {len(results)} checks{RESET}\n")
    passed = sum(1 for _, ok, _ in results if ok)
    for name, ok, detail in results:
        mark = f"{GREEN}PASS{RESET}" if ok else f"{RED}FAIL{RESET}"
        print(f"  {mark} {name:<42}" + (f" {DIM}{detail.strip()[:80]}{RESET}" if (detail and not ok) else ""))
    failed = len(results) - passed
    print(f"\n{BOLD}Result:{RESET} {GREEN}{passed} passed{RESET}, "
          f"{(RED if failed else DIM)}{failed} failed{RESET}, {len(results)} checks")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
