#!/usr/bin/env python3
"""run_maintenance.py — exercises `plainkeep backup` (commit/push freshness nag) and `plainkeep consolidate`
(nightly dream-lite digest). Sets up a real temp git repo + bare remote for backup."""
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


def git(home, *a):
    return subprocess.run(["git", "-C", str(home), *a], capture_output=True, text=True)


def run(home, verb, *args):
    env = {**os.environ, "PLAINKEEP_HOME": str(home)}
    return subprocess.run([sys.executable, str(REPO / "bin" / verb / "run.py"), *args],
                          capture_output=True, text=True, env=env)


def init_repo(home, remote):
    git(home, "init", "-q", "-b", "main")
    git(home, "config", "user.email", "t@e"); git(home, "config", "user.name", "t")
    git(home, "config", "commit.gpgsign", "false")
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], capture_output=True)
    git(home, "remote", "add", "origin", str(remote))
    (home / "wiki").mkdir(parents=True, exist_ok=True)
    (home / "wiki" / "index.md").write_text("# index\n")
    git(home, "add", "-A"); git(home, "commit", "-qm", "init")
    git(home, "push", "-q", "-u", "origin", "main")


def main() -> int:
    # ---- backup ----
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as rd:
        h, remote = Path(td), Path(rd) / "origin.git"
        r = run(h, "backup")  # not a git repo yet
        check("backup flags a non-repo", r.returncode == 1 and "not a git repo" in r.stdout, r.stdout)
        init_repo(h, remote)
        r = run(h, "backup")  # clean + pushed
        check("backup: clean+pushed is safe (exit 0)", r.returncode == 0 and "pushed" in r.stdout, r.stdout)
        (h / "wiki" / "new.md").write_text("# new\n")
        r = run(h, "backup")  # uncommitted
        check("backup flags uncommitted work (exit 1)", r.returncode == 1 and "uncommitted" in r.stdout, r.stdout)
        git(h, "add", "-A"); git(h, "commit", "-qm", "more")  # committed but not pushed
        r = run(h, "backup")
        check("backup flags unpushed commits (exit 1)", r.returncode == 1 and "not pushed" in r.stdout, r.stdout)

    # ---- consolidate ----
    with tempfile.TemporaryDirectory() as td:
        h = Path(td)
        wn = h / "wiki" / "notes"; wn.mkdir(parents=True)
        (wn / "alpha.md").write_text("---\ntype: note\nupdated: 2026-06-20\n---\n# Alpha\nsee [[beta]]\n")
        (wn / "beta.md").write_text("---\ntype: note\nupdated: 2026-06-20\n---\n# Beta\n")
        (wn / "lonely.md").write_text("---\ntype: note\nupdated: 2026-06-20\n---\n# Lonely\n")
        (wn / "ancient.md").write_text("---\ntype: note\nupdated: 2024-01-01\n---\n# Ancient\n")
        dd = h / "tasks" / "done"; dd.mkdir(parents=True)
        (dd / "T-20260625-01.md").write_text(f"---\ntype: task\nupdated: {TODAY}\n---\n# Shipped\n")
        (h / "inbox").mkdir(); (h / "inbox" / "cap-x.md").write_text("a thought")

        r = run(h, "consolidate")
        j = "\n".join(p.read_text() for p in (h / "journal").rglob("*.md")) if (h / "journal").exists() else ""
        check("consolidate writes a digest", r.returncode == 0 and "## Consolidate" in j, r.stdout + r.stderr)
        check("consolidate finds the orphan (lonely)", "lonely" in j, j[-500:])
        check("consolidate finds the stale note (ancient)", "ancient" in j, j[-500:])
        check("consolidate counts today's completion + captures", "1 task(s) completed" in j and "1 capture" in j, j[-500:])

    print(f"{BOLD}Maintenance verbs (backup, consolidate) — {len(results)} checks{RESET}\n")
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
