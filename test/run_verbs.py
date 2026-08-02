#!/usr/bin/env python3
"""
run_verbs.py — exercises the daily-driver verbs (capture, task, status, help) end-to-end through
their real run.py scripts, in a temp PLAINKEEP_HOME so the repo isn't polluted. Stdlib only.
"""
from __future__ import annotations
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from lib.hermetic import seal
seal()   # hermetic: an empty throwaway registry, never the developer's real vault

REPO = Path(__file__).resolve().parents[1]
GREEN, RED, DIM, BOLD, RESET = "\033[32m", "\033[31m", "\033[2m", "\033[1m", "\033[0m"
results = []


def check(name, cond, detail=""):
    results.append((name, cond, detail))


def run(home: Path, verb: str, *args):
    env = {**os.environ, "PLAINKEEP_HOME": str(home)}
    return subprocess.run([sys.executable, str(REPO / "bin" / verb / "run.py"), *args],
                          capture_output=True, text=True, env=env)


def main() -> int:
    with tempfile.TemporaryDirectory() as td:
        home = Path(td)

        # capture
        r = run(home, "capture", "remember to test the webhook retry path")
        caps = list((home / "inbox").glob("cap-*.md"))
        check("capture writes an inbox note", r.returncode == 0 and len(caps) == 1, r.stdout + r.stderr)
        check("capture appends a journal line",
              any((home / "journal").rglob("*.md")) and "captured:" in
              "\n".join(p.read_text() for p in (home / "journal").rglob("*.md")))

        # capture via stdin
        env = {**os.environ, "PLAINKEEP_HOME": str(home)}
        r = subprocess.run([sys.executable, str(REPO / "bin/capture/run.py")], input="piped thought",
                           capture_output=True, text=True, env=env)
        check("capture reads stdin", len(list((home / "inbox").glob("cap-*.md"))) == 2, r.stderr)

        # task add / list / show / move / done
        r = run(home, "task", "add", "Fix Designatives webhook timeout")
        tasks = list((home / "tasks" / "active").glob("T-*.md"))
        check("task add creates an active task", r.returncode == 0 and len(tasks) == 1, r.stdout + r.stderr)
        tid = tasks[0].stem
        body = tasks[0].read_text()
        check("task file has §7.2 shape", all(s in body for s in
              ("type: task", f"id: {tid}", "status: active", "## Outcome", "## Log")))
        r = run(home, "task", "list")
        check("task list shows the task", tid in r.stdout, r.stdout)
        r = run(home, "task", "move", tid, "waiting")
        check("task move -> waiting (folder + frontmatter)",
              (home / "tasks" / "waiting" / f"{tid}.md").exists()
              and "status: waiting" in (home / "tasks" / "waiting" / f"{tid}.md").read_text(), r.stdout + r.stderr)
        r = run(home, "task", "done", tid)
        check("task done -> done/", (home / "tasks" / "done" / f"{tid}.md").exists(), r.stdout + r.stderr)

        # status
        r = run(home, "status")
        check("status runs and reports", r.returncode == 0 and "tasks:" in r.stdout, r.stdout + r.stderr)

        # help + manifest
        r = run(home, "help")
        check("help lists built verbs", all(v in r.stdout for v in ("capture", "task", "status", "search")), r.stdout)
        r = run(home, "help", "capture")
        check("help <verb> shows usage", "usage:" in r.stdout and "safe_write" in r.stdout, r.stdout)
        check("help generated plainkeep.json", (home / "plainkeep.json").exists())
        if (home / "plainkeep.json").exists():
            m = json.loads((home / "plainkeep.json").read_text())
            check("plainkeep.json has the verb manifest", any(c["verb"] == "task" for c in m["verbs"]))

    print(f"{BOLD}Daily-driver verbs (capture, task, status, help) — {len(results)} checks{RESET}\n")
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
