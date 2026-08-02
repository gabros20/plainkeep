#!/usr/bin/env python3
"""run_new.py — exercises `plainkeep new project` and `plainkeep new client` against temp ~/plainkeep + sibling roots
(PLAINKEEP_HOME + PLAINKEEP_ROOTS_HOME), asserting wiki hubs, the ~/work repo scaffold, and slug uniqueness."""
from __future__ import annotations
import json
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


def run(opshome, roots, *args):
    env = {**os.environ, "PLAINKEEP_HOME": str(opshome), "PLAINKEEP_ROOTS_HOME": str(roots)}
    return subprocess.run([sys.executable, str(REPO / "bin" / "new" / "run.py"), *args],
                          capture_output=True, text=True, env=env)


def main() -> int:
    with tempfile.TemporaryDirectory() as td:
        ops, roots = Path(td) / "ops", Path(td) / "home"
        (ops / "wiki").mkdir(parents=True)
        (ops / "journal").mkdir()
        shutil.copytree(REPO / "templates", ops / "templates")  # the verb reads templates/project-repo

        # project
        r = run(ops, roots, "project", "Acme Webapp", "--kind", "products")
        repo = roots / "work" / "products" / "acme-webapp"
        hub = ops / "wiki" / "projects" / "acme-webapp.md"
        check("new project creates the wiki hub", hub.exists(), r.stdout + r.stderr)
        check("new project scaffolds the ~/work repo", repo.is_dir() and (repo / "README.md").exists())
        check("repo is git-initialized", (repo / ".git").is_dir())
        check("template placeholders filled", "Acme Webapp" in (repo / "README.md").read_text()
              and "{{name}}" not in (repo / "README.md").read_text())
        check("repo lands under the routing kind", repo.parent.name == "products")
        check("sibling repo is NOT inside ~/plainkeep", not (ops / "work").exists())

        # client
        r = run(ops, roots, "client", "Globex")
        chub = ops / "wiki" / "clients" / "globex.md"
        ctree = roots / "files" / "clients" / "globex"
        check("new client creates the wiki hub", chub.exists(), r.stdout + r.stderr)
        check("new client creates the ~/files material tree", (ctree / "in").is_dir() and (ctree / "out").is_dir())

        # uniqueness
        r = run(ops, roots, "project", "Acme Webapp")
        check("new refuses a duplicate slug", r.returncode == 1 and "already exists" in (r.stdout + r.stderr), r.stderr)

        # ---- new verb: the scaffolder (issue #1 gap E). Retargeted (Part 0.2) — user verbs land in
        # plugins/local/<name>/ under the vault (PLAINKEEP_HOME), NEVER in bin/ (the update boundary). ----
        r = run(ops, roots, "verb", "zzscaffoldtest", "--summary", "temp test verb", "--risk", "read")
        d = ops / "plugins" / "local" / "zzscaffoldtest"
        check("new verb scaffolds plugins/local/<name>/{run.py,cmd.json}",
              (d / "run.py").exists() and (d / "cmd.json").exists(), r.stdout + r.stderr)
        check("new verb never writes into bin/", not (REPO / "bin" / "zzscaffoldtest").exists())
        if (d / "cmd.json").exists():
            cj = json.loads((d / "cmd.json").read_text())
            check("scaffolded cmd.json carries verb + declared risk",
                  cj.get("verb") == "zzscaffoldtest" and cj.get("risk") == "read", str(cj))
        if (d / "run.py").exists():
            stub = (d / "run.py").read_text()
            check("scaffolded run.py has placeholders filled", "{{" not in stub and "zzscaffoldtest" in stub, stub[:80])
        opsjson = json.loads((ops / "plainkeep.json").read_text()) if (ops / "plainkeep.json").exists() else {"verbs": []}
        entry = next((v for v in opsjson["verbs"] if v["verb"] == "zzscaffoldtest"), None)
        check("new verb regenerates the manifest (plainkeep.json)", entry is not None, "")
        check("scaffolded verb is tagged source plugin:local",
              entry is not None and entry.get("source") == "plugin:local", str(entry))
        run(ops, roots, "verb", "zzscaffoldtwo")   # no --risk → must default to confirm (§5)
        d2 = ops / "plugins" / "local" / "zzscaffoldtwo"
        cj2 = json.loads((d2 / "cmd.json").read_text()) if (d2 / "cmd.json").exists() else {}
        check("new verb defaults to confirm-class (§5)", cj2.get("risk") == "confirm", str(cj2))
        r = run(ops, roots, "verb", "Bad Name")
        check("new verb rejects an invalid name", r.returncode == 2, r.stderr)
        r = run(ops, roots, "verb", "wiki")
        check("new verb refuses a reserved engine verb", r.returncode == 1, r.stderr)

    print(f"{BOLD}new verb (scaffold project/client/verb) — {len(results)} checks{RESET}\n")
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
