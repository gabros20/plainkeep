#!/usr/bin/env python3
"""run_repo.py — exercises `plainkeep repo` health/adopt/nuke-modules/clone against temp roots."""
from __future__ import annotations
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from lib.hermetic import seal
seal()   # hermetic: an empty throwaway registry, never the developer's real vault

REPO = Path(__file__).resolve().parents[1]
GREEN, RED, DIM, BOLD, RESET = "\033[32m", "\033[31m", "\033[2m", "\033[1m", "\033[0m"
results = []


def check(name, cond, detail=""):
    results.append((name, cond, detail))


def git(repo, *a):
    return subprocess.run(["git", "-C", str(repo), *a], capture_output=True, text=True)


def mkrepo(path):
    path.mkdir(parents=True, exist_ok=True)
    git(path, "init", "-q", "-b", "main")
    git(path, "config", "user.email", "t@e"); git(path, "config", "user.name", "t")
    git(path, "config", "commit.gpgsign", "false")
    (path / "f.txt").write_text("x")
    git(path, "add", "-A"); git(path, "commit", "-qm", "init")


def run(ops, roots, *args):
    env = {**os.environ, "PLAINKEEP_HOME": str(ops), "PLAINKEEP_ROOTS_HOME": str(roots)}
    return subprocess.run([sys.executable, str(REPO / "bin" / "repo" / "run.py"), *args],
                          capture_output=True, text=True, env=env)


def main() -> int:
    with tempfile.TemporaryDirectory() as td:
        ops, roots = Path(td) / "ops", Path(td) / "home"
        (ops / "wiki" / "projects").mkdir(parents=True); (ops / "journal").mkdir()
        work = roots / "work"

        # a clean repo and a dirty one
        mkrepo(work / "products" / "clean-app")
        mkrepo(work / "labs" / "dirty-app")
        (work / "labs" / "dirty-app" / "new.txt").write_text("uncommitted")

        r = run(ops, roots, "health")
        check("health lists repos", "clean-app" in r.stdout and "dirty-app" in r.stdout, r.stdout)
        check("health flags the dirty repo", "dirty" in r.stdout and r.returncode == 1, r.stdout)

        # nuke-modules: an old node_modules gets reclaimed, a fresh one survives
        oldnm = work / "labs" / "dirty-app" / "node_modules"; oldnm.mkdir(); (oldnm / "x").write_text("1")
        os.utime(oldnm, (time.time() - 40 * 86400, time.time() - 40 * 86400))
        freshnm = work / "products" / "clean-app" / "node_modules"; freshnm.mkdir(); (freshnm / "y").write_text("2")
        r = run(ops, roots, "nuke-modules", "--stale", "30")
        check("nuke-modules removes the stale one", not oldnm.exists(), r.stdout)
        check("nuke-modules keeps the fresh one", freshnm.exists())

        # adopt: a loose repo elsewhere moves into the tree + gets a hub
        loose = Path(td) / "loose-thing"; mkrepo(loose)
        r = run(ops, roots, "adopt", str(loose), "--kind", "tools")
        check("adopt moves the repo into the kind", (work / "tools" / "loose-thing").is_dir() and not loose.exists(), r.stdout + r.stderr)
        check("adopt writes a wiki hub", (ops / "wiki" / "projects" / "loose-thing.md").exists())

        # clone: from a bare 'remote' recorded in a hub
        bare = Path(td) / "remote.git"; subprocess.run(["git", "init", "-q", "--bare", str(bare)])
        subprocess.run(["git", "-C", str(work / "products" / "clean-app"), "push", "-q", str(bare), "main"])
        (ops / "wiki" / "projects" / "restored.md").write_text(
            f"---\ntype: project\nremote: {bare}\n---\n# Restored\n")
        r = run(ops, roots, "clone", "restored", "--kind", "labs")
        check("clone restores from the hub's remote", (work / "labs" / "restored" / ".git").is_dir(), r.stdout + r.stderr)

    print(f"{BOLD}repo verb (~/work fleet) — {len(results)} checks{RESET}\n")
    passed = sum(1 for _, ok, _ in results if ok)
    for name, ok, detail in results:
        mark = f"{GREEN}PASS{RESET}" if ok else f"{RED}FAIL{RESET}"
        print(f"  {mark} {name:<44}" + (f" {DIM}{detail.strip()[:80]}{RESET}" if (detail and not ok) else ""))
    failed = len(results) - passed
    print(f"\n{BOLD}Result:{RESET} {GREEN}{passed} passed{RESET}, "
          f"{(RED if failed else DIM)}{failed} failed{RESET}, {len(results)} checks")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
