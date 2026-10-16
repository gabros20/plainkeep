#!/usr/bin/env python3
"""run_get.py — exercises script/get, the install funnel (proposal Part 5.4): --dry-run changes
nothing, an existing install is refused (never clobbered), and --demo seeds a self-contained sandbox
where capture/search/week work. Fully offline — the clone source is the local repo (PLAINKEEP_GET_REPO)."""
from __future__ import annotations
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from lib.hermetic import seal
seal()   # hermetic: an empty throwaway registry, never the developer's real vault

REPO = Path(__file__).resolve().parents[1]
GET = str(REPO / "script" / "get")
GREEN, RED, DIM, BOLD, RESET = "\033[32m", "\033[31m", "\033[2m", "\033[1m", "\033[0m"
results = []


def check(name, cond, detail=""):
    results.append((name, cond, detail))


def run(args, target, extra=None):
    env = {**os.environ, "PLAINKEEP_GET_REPO": str(REPO), "PLAINKEEP_GET_DIR": str(target)}
    if extra:
        env.update(extra)
    return subprocess.run([GET, *args], capture_output=True, text=True, env=env)


def main() -> int:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)

        # --dry-run: prints the plan, clones nothing, runs no setup
        target = tmp / "dry"
        r = run(["--dry-run"], target)
        check("dry-run exits 0", r.returncode == 0, r.stderr)
        check("dry-run clones nothing", not target.exists())
        check("dry-run shows the clone step", "git clone" in r.stdout)
        check("dry-run shows the setup hand-off", "script/setup" in r.stdout)

        # Task 12: the real install is LEAN + non-interactive BY DEFAULT (a piped curl|sh must never
        # block on setup's lean prompt and must never silently fall through to FULL). The dry-run of
        # the real branch surfaces the exact setup args it would hand off.
        check("default real install passes --yes (non-interactive)", "--yes" in r.stdout, r.stdout)
        check("default real install passes --lean", "--lean" in r.stdout, r.stdout)
        check("default real install does NOT pass --full", "--full" not in r.stdout, r.stdout)

        # --full escape hatch: contributors keep the dev-only test/+ref/ tree.
        rf = run(["--full", "--dry-run"], tmp / "dryfull")
        check("--full real install passes --full, not --lean",
              rf.returncode == 0 and "--full" in rf.stdout and "--lean" not in rf.stdout,
              rf.stdout + rf.stderr)

        # --demo branch is UNCHANGED: it still installs the full tree in the throwaway dir.
        rd = run(["--demo", "--dry-run"], tmp / "drydemo")
        check("--demo still hands off --full --yes --no-commit",
              rd.returncode == 0 and "--full --yes --no-commit" in rd.stdout, rd.stdout + rd.stderr)

        # refuse to overwrite a non-empty existing install
        existing = tmp / "existing"
        existing.mkdir()
        (existing / "keep.md").write_text("mine", encoding="utf-8")
        r = run([], existing)
        check("refuses an existing non-empty install", r.returncode == 1, r.stdout + r.stderr)
        check("refusal names script/update as the alternative", "script/update" in r.stderr)
        check("refusal did not touch existing content", (existing / "keep.md").read_text() == "mine")

        # --demo: a self-contained sandbox; roots + binary all under the one demo dir
        demo = tmp / "demo"
        r = run(["--demo", "--yes"], demo)
        vault = demo / "plainkeep"
        check("demo exits 0", r.returncode == 0, r.stdout[-400:] + r.stderr[-400:])
        # The demo installs an ENGINE beside the vault, inside the same throwaway dir (Task 2) —
        # the whole promise of `--demo` is that deleting one directory removes everything, and the
        # engine's default install root is outside it.
        demo_engine = demo / ".engine" / "engine" / "current"
        check("demo installs an engine INSIDE the throwaway dir", (demo_engine / "plainkeep").is_file(),
              str(sorted(p.name for p in demo.iterdir())))
        check("demo's vault carries the cloned template", (vault / "bin").is_dir())
        check("demo seeds an example note", (vault / "wiki" / "notes" / "demo-welcome.md").exists())
        check("demo roots live inside the throwaway dir (not ~/)",
              (demo / "work").is_dir() and (demo / "files").is_dir())
        check("demo prints the one dir to delete to walk away", f"rm -rf '{demo}'" in r.stdout)

        env = {**os.environ, "PLAINKEEP_HOME": str(vault)}
        s = subprocess.run([str(demo_engine / "plainkeep"), "search", "demo", "--json"],
                           capture_output=True, text=True, env=env)
        rows = [ln for ln in s.stdout.splitlines() if ln.strip()]
        check("demo search returns hits", s.returncode == 0 and len(rows) > 1, s.stdout[:120])
        w = subprocess.run([str(demo_engine / "plainkeep"), "week"], capture_output=True, text=True, env=env)
        check("demo week runs", w.returncode == 0, w.stderr)

    print(f"{BOLD}script/get — install funnel (Part 5.4) — {len(results)} checks{RESET}\n")
    passed = sum(1 for _, ok, _ in results if ok)
    for name, ok, detail in results:
        mark = f"{GREEN}PASS{RESET}" if ok else f"{RED}FAIL{RESET}"
        print(f"  {mark} {name:<48}" + (f" {DIM}{str(detail).strip()[:80]}{RESET}" if (detail and not ok) else ""))
    failed = len(results) - passed
    print(f"\n{BOLD}Result:{RESET} {GREEN}{passed} passed{RESET}, "
          f"{(RED if failed else DIM)}{failed} failed{RESET}, {len(results)} checks")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
