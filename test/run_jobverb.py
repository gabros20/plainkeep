#!/usr/bin/env python3
"""run_jobverb.py — exercises the `plainkeep job` verb: list (with legality flags), run <name>
(manual fallback), apply (render launchd plists, skipping non-schedulable jobs). Temp PLAINKEEP_HOME."""
from __future__ import annotations
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib import vaultfx  # noqa: E402
GREEN, RED, DIM, BOLD, RESET = "\033[32m", "\033[31m", "\033[2m", "\033[1m", "\033[0m"
results = []

REGISTRY = {
    "external_allowlist": [],
    "jobs": {
        "index":       {"command": "plainkeep index",      "schedule": {"interval_minutes": 60}, "risk": "read"},
        "consolidate": {"command": "plainkeep consolidate", "schedule": {"daily": "02:30"},       "risk": "safe_write"},
        "danger":      {"command": "plainkeep capture x",   "schedule": {"daily": "09:00"},       "risk": "confirm"},
    },
}


def check(name, cond, detail=""):
    results.append((name, cond, detail))


def run(home, *args):
    env = {**os.environ, "PLAINKEEP_HOME": str(home)}
    return subprocess.run([sys.executable, str(REPO / "bin" / "job" / "run.py"), *args],
                          capture_output=True, text=True, env=env)


def main() -> int:
    with tempfile.TemporaryDirectory() as td:
        h = Path(td)
        (h / "wiki" / "notes").mkdir(parents=True)
        (h / "wiki" / "notes" / "alpha.md").write_text(
            "---\ntype: note\nupdated: 2026-06-20\n---\n# Alpha\nretrieval and ranking\n")
        (h / "jobs").mkdir()
        (h / "jobs" / "registry.json").write_text(json.dumps(REGISTRY), encoding="utf-8")
        # `plainkeep job run` now re-enters the DISPATCHER (Task 9, "one door") instead of calling the
        # verb's run.py directly — so the temp PLAINKEEP_HOME needs a real dispatcher tree. Symlink the
        # engine `plainkeep` + `bin` in; `job run index`/`consolidate` then pass the guardrail like any call.
        os.symlink(REPO / "plainkeep", h / "plainkeep")
        os.symlink(REPO / "bin", h / "bin")
        vaultfx.mark_vault(h)   # Task 1b: the dispatcher validates the root before it dispatches

        r = run(h, "list")
        check("job list shows the jobs", "index" in r.stdout and "consolidate" in r.stdout, r.stdout)
        check("job list flags a non-schedulable job", "danger" in r.stdout and "not schedulable" in r.stdout, r.stdout)

        r = run(h, "run", "index")
        check("job run index builds the index (rc 0)", r.returncode == 0 and (h / ".index" / "plainkeep.sqlite").exists(), r.stdout + r.stderr)

        r = run(h, "run", "consolidate")
        j = "\n".join(p.read_text() for p in (h / "journal").rglob("*.md")) if (h / "journal").exists() else ""
        check("job run consolidate writes a digest", r.returncode == 0 and "## Consolidate" in j, r.stdout + r.stderr)

        r = run(h, "run", "danger")
        check("job run refuses a non-schedulable job", r.returncode == 1 and "refusing" in (r.stdout + r.stderr), r.stdout + r.stderr)

        r = run(h, "apply")
        ld = h / "jobs" / "launchd"
        check("job apply renders schedulable plists", (ld / "com.plainkeep.index.plist").exists() and (ld / "com.plainkeep.consolidate.plist").exists(), r.stdout + r.stderr)
        check("job apply skips non-schedulable", not (ld / "com.plainkeep.danger.plist").exists() and "skipped" in r.stdout, r.stdout)
        if (ld / "com.plainkeep.index.plist").exists():
            pl = (ld / "com.plainkeep.index.plist").read_text()
            check("plist is well-formed launchd", "StartInterval" in pl and "com.plainkeep.index" in pl and "PLAINKEEP_HOME" in pl, pl[:200])
        r = run(h, "run", "nope")
        check("job run rejects an unknown job name", r.returncode == 2, r.stdout + r.stderr)

    print(f"{BOLD}Jobs scheduler verb (job list/run/apply) — {len(results)} checks{RESET}\n")
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
