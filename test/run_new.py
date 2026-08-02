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
import time
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
    # `new client`'s in/out/work mkdir goes through the write seam since Task 1c, and the wall's
    # ~/files anchor and `paths.FILES_ROOT` both read PLAINKEEP_TEST_HOME first — an inherited one
    # would relocate this fixture out from under the assertions (as run_pathwall.py notes).
    env.pop("PLAINKEEP_TEST_HOME", None)
    return subprocess.run([sys.executable, str(REPO / "bin" / "new" / "run.py"), *args],
                          capture_output=True, text=True, env=env)


def case_scaffold_through_a_sealed_engine() -> None:
    """`new verb`, run out of an INSTALLED engine rather than out of this checkout.

    This is the gap that let a BLOCKING bug ship. Every case above invokes `REPO/bin/new/run.py` —
    the repository, which is a writable engine that is never sealed. `enginetree.install()` seals an
    installed tree at 0444/0555, `shutil.copytree` PRESERVES source modes, and `templates/verb` is
    engine-owned: so through any normally-installed engine the scaffold landed read-only and `_fill`
    could not substitute a single placeholder. It failed halfway and left an unwritable
    `plugins/local/<name>/` still full of `{{name}}` — which the resolver then dispatched, exit 0,
    printing the raw template text.

    So the fixture here is the SEALED tree, and both halves are asserted: that the scaffold works,
    and that a scaffold which cannot complete leaves nothing behind."""
    with tempfile.TemporaryDirectory(prefix="pk-new-sealed-") as td:
        tmp = Path(os.path.realpath(td))
        inst, ops, roots = tmp / "install", tmp / "vault", tmp / "home"
        for d in ((ops / "wiki"), (ops / "journal"), roots):
            d.mkdir(parents=True)
        env = {**os.environ, "PLAINKEEP_ENGINE_HOME": str(inst)}
        env.pop("PLAINKEEP_ENGINE", None)
        r = subprocess.run([sys.executable, str(REPO / "bin" / "lib" / "enginetree.py"),
                            "--install", str(REPO)], capture_output=True, text=True, env=env)
        eng = Path(os.path.realpath(inst / "engine" / "current"))
        check("fixture: the engine installs and is SEALED",
              r.returncode == 0 and not os.access(eng / "templates" / "verb" / "run.py", os.W_OK),
              r.stdout + r.stderr)

        venv = {**os.environ, "PLAINKEEP_HOME": str(ops), "PLAINKEEP_ROOTS_HOME": str(roots)}
        venv.pop("PLAINKEEP_TEST_HOME", None)
        r = subprocess.run([sys.executable, str(eng / "bin" / "new" / "run.py"),
                            "verb", "sealedscaffold", "--risk", "read", "--summary", "from a seal"],
                           capture_output=True, text=True, env=venv)
        d = ops / "plugins" / "local" / "sealedscaffold"
        out = r.stdout + r.stderr
        check("new verb through a SEALED engine succeeds", r.returncode == 0 and "Traceback" not in out,
              out[-400:])
        check("...and the scaffolded files exist", (d / "run.py").is_file() and (d / "cmd.json").is_file())
        if (d / "run.py").is_file():
            check("...and they are WRITABLE — the engine's 0444 seal did not travel with the copy",
                  os.access(d / "run.py", os.W_OK) and os.access(d, os.W_OK),
                  oct((d / "run.py").stat().st_mode))
            check("...and every placeholder was substituted",
                  "{{" not in (d / "run.py").read_text() and "{{" not in (d / "cmd.json").read_text(),
                  (d / "run.py").read_text()[:120])

        # THE OTHER HALF: a scaffold that cannot finish leaves NOTHING under the verb's name, rather
        # than a half-written verb the resolver will happily serve. Forced by making the destination
        # parent's own leaf name unavailable — `plugins/local/halfway` already exists as a FILE, so
        # the rename into place is the step that fails.
        (ops / "plugins" / "local").mkdir(parents=True, exist_ok=True)
        blocker = ops / "plugins" / "local" / "halfway"
        blocker.write_text("not a directory", encoding="utf-8")
        r = subprocess.run([sys.executable, str(eng / "bin" / "new" / "run.py"),
                            "verb", "halfway"], capture_output=True, text=True, env=venv)
        check("a scaffold that cannot complete REFUSES", r.returncode != 0, (r.stdout + r.stderr)[-300:])
        check("...and leaves no `.pk-scaffolding-*` debris behind",
              not list((ops / "plugins" / "local").glob(".pk-scaffolding-*")),
              str(list((ops / "plugins" / "local").glob("*"))))
        check("...and never turned the blocker into a half-written verb",
              blocker.is_file() and blocker.read_text() == "not a directory")

        # DEBRIS FROM A KILL. The `except` above cleans every interruption that raises; a `SIGKILL`
        # between `_fill` and the rename raises nothing, and the staging leaf then stayed forever —
        # `enginetree.install()` sweeps its own abandoned staging and this did not. Simulated by
        # planting a leaf with an old mtime, which is what such a kill leaves; a FRESH one is planted
        # beside it because the sweep must never touch a scaffold that another process is building
        # right now.
        local = ops / "plugins" / "local"
        stale = local / ".pk-scaffolding-killed.80356"
        stale.mkdir(parents=True, exist_ok=True)
        (stale / "run.py").write_text("{{name}}", encoding="utf-8")
        old = time.time() - (25 * 60 * 60)
        os.utime(stale / "run.py", (old, old))
        os.utime(stale, (old, old))
        live = local / ".pk-scaffolding-other.99999"
        live.mkdir(exist_ok=True)
        (live / "run.py").write_text("{{name}}", encoding="utf-8")
        r = subprocess.run([sys.executable, str(eng / "bin" / "new" / "run.py"), "verb", "sweeper"],
                           capture_output=True, text=True, env=venv)
        check("a later `new verb` SWEEPS the debris a kill left behind",
              r.returncode == 0 and not stale.exists(),
              f"rc={r.returncode} left={sorted(p.name for p in local.glob('.pk-scaffolding-*'))}")
        check("...and does NOT touch a staging leaf young enough to be live",
              live.is_dir(), str(sorted(p.name for p in local.glob(".pk-scaffolding-*"))))
        check("...and the debris was never a dispatchable verb anyway (dot-prefixed)",
              not (local / ".pk-scaffolding-killed.80356").exists()
              and (local / "sweeper" / "run.py").is_file())
        shutil.rmtree(live, ignore_errors=True)

        # AN UNWRITABLE `plugins/local/`. Atomicity already held here — nothing is left behind — but
        # the SHAPE was a raw `PermissionError` traceback, where `enginetree.main()` prints one line.
        ro = ops / "plugins" / "local"
        mode = ro.stat().st_mode
        os.chmod(ro, 0o555)
        try:
            r = subprocess.run([sys.executable, str(eng / "bin" / "new" / "run.py"), "verb", "ro1"],
                               capture_output=True, text=True, env=venv)
            out = r.stdout + r.stderr
            check("new verb into an unwritable plugins/local REFUSES without a traceback",
                  r.returncode != 0 and "Traceback" not in out
                  and "scaffolding verb 'ro1' failed" in out, out[-300:])
        finally:
            os.chmod(ro, mode)
        check("...and left no residue", not list(ro.glob(".pk-scaffolding-*"))
              and not (ro / "ro1").exists(), str(sorted(p.name for p in ro.glob("*"))))

        for p in tmp.rglob("*"):                 # the sealed tree cannot be removed as-is
            try:
                if p.is_dir() and not p.is_symlink():
                    p.chmod(0o755)
            except OSError:
                pass


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

    case_scaffold_through_a_sealed_engine()

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
