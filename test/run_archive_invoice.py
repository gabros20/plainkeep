#!/usr/bin/env python3
"""run_archive_invoice.py — `plainkeep archive` (git-bundle a dead repo) + `plainkeep invoice` (draft, never
sends), against temp ~/plainkeep + sibling roots."""
from __future__ import annotations
import os
import shutil
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


def git(repo, *a):
    return subprocess.run(["git", "-C", str(repo), *a], capture_output=True, text=True)


def mkrepo(path):
    path.mkdir(parents=True, exist_ok=True)
    git(path, "init", "-q", "-b", "main")
    git(path, "config", "user.email", "t@e"); git(path, "config", "user.name", "t")
    git(path, "config", "commit.gpgsign", "false")
    (path / "f.txt").write_text("x"); git(path, "add", "-A"); git(path, "commit", "-qm", "init")


def run(verb, ops, roots, *args):
    env = {**os.environ, "PLAINKEEP_HOME": str(ops), "PLAINKEEP_ROOTS_HOME": str(roots)}
    return subprocess.run([sys.executable, str(REPO / "bin" / verb / "run.py"), *args],
                          capture_output=True, text=True, env=env)


def main() -> int:
    with tempfile.TemporaryDirectory() as td:
        ops, roots = Path(td) / "ops", Path(td) / "home"
        (ops / "wiki" / "projects").mkdir(parents=True)
        (ops / "wiki" / "clients").mkdir(parents=True)
        (ops / "journal").mkdir()
        (ops / "templates").mkdir()
        shutil.copy(REPO / "templates" / "tax-formula.md", ops / "templates" / "tax-formula.md")

        # archive
        repo = roots / "work" / "labs" / "dead-app"; mkrepo(repo)
        (ops / "wiki" / "projects" / "dead-app.md").write_text(
            "---\ntype: project\nstatus: active\nupdated: 2026-01-01\n---\n# Dead App\n\n## Timeline\n")
        r = run("archive", ops, roots, "dead-app")
        bundle = roots / "work" / "archive" / "2026" / "dead-app.bundle"
        check("archive creates a bundle", bundle.exists(), r.stdout + r.stderr)
        restored = Path(td) / "restored"
        cl = subprocess.run(["git", "clone", "-q", str(bundle), str(restored)], capture_output=True, text=True)
        check("bundle restores to a working repo (git clone)",
              cl.returncode == 0 and (restored / "f.txt").exists(), cl.stderr)
        check("archive removes the working tree", not repo.exists())
        check("archive marks the hub archived",
              "status: archived" in (ops / "wiki" / "projects" / "dead-app.md").read_text())

        # invoice
        (ops / "wiki" / "clients" / "acme.md").write_text(
            "---\ntype: client\ntitle: Acme Inc\n---\n# Acme Inc\n")
        r = run("invoice", ops, roots, "acme", "--amount", "1000", "--desc", "Design sprint")
        draft = list((roots / "files" / "clients" / "acme" / "out").glob("invoice-*.md")) \
            if (roots / "files" / "clients" / "acme" / "out").exists() else []
        check("invoice writes a draft to the client's out/", len(draft) == 1, r.stdout + r.stderr)
        if draft:
            body = draft[0].read_text()
            check("invoice computes VAT (1000 + 27% = 1270)",
                  "1000.00" in body and "270.00" in body and "1270.00" in body, body)
            check("invoice is clearly a DRAFT / not sent", "DRAFT" in body and "not sent" in body.lower())
        check("invoice prints it never transmits", "never transmits" in r.stdout, r.stdout)
        r = run("invoice", ops, roots, "nobody", "--amount", "5")
        check("invoice refuses an unknown client", r.returncode == 1, r.stderr)

    print(f"{BOLD}archive + invoice (work/business) — {len(results)} checks{RESET}\n")
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
