#!/usr/bin/env python3
"""run_setup.py — exercises script/setup + script/update against a throwaway clone, with the roots
and PATH redirected into a temp dir (PLAINKEEP_ROOTS_HOME / PLAINKEEP_BIN_DIR) so real ~/ is never touched."""
from __future__ import annotations
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
UPSTREAM = "https://example.com/template.git"
GREEN, RED, DIM, BOLD, RESET = "\033[32m", "\033[31m", "\033[2m", "\033[1m", "\033[0m"
results = []


def check(name, cond, detail=""):
    results.append((name, cond, detail))


def git(repo, *a):
    return subprocess.run(["git", "-C", str(repo), *a], capture_output=True, text=True)


def main() -> int:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        vault, home, bindir, compdir = tmp / "plainkeep", tmp / "home", tmp / "bin", tmp / "comp"
        cl = subprocess.run(["git", "clone", "-q", str(REPO), str(vault)], capture_output=True, text=True)
        if cl.returncode != 0:
            print("git clone failed:", cl.stderr); return 1
        # PLAINKEEP_CONFIG_HOME is hermetic and MANDATORY here: since ADR-014 Task 1b `script/setup`
        # registers the vault it creates, and the registry lives OUTSIDE every vault — in the real
        # ~/.config unless redirected. Without this the suite would write an entry into the
        # developer's own registry (and fail the moment their vault already holds the name).
        env = {**os.environ, "PLAINKEEP_ROOTS_HOME": str(home), "PLAINKEEP_BIN_DIR": str(bindir),
               "PLAINKEEP_COMP_DIR": str(compdir), "PLAINKEEP_HOME": str(vault),
               "PLAINKEEP_CONFIG_HOME": str(tmp / "config")}

        # dry-run changes nothing
        subprocess.run([str(vault / "script" / "setup"), "--lean", "--yes", "--dry-run",
                        "--upstream", UPSTREAM], capture_output=True, text=True, env=env)
        check("dry-run creates no symlink", not (bindir / "plainkeep").exists())
        check("dry-run installs no completion", not (compdir / "_plainkeep").exists())
        check("dry-run leaves test/ intact", bool(git(vault, "ls-files", "test/").stdout.strip()))

        # real run
        r = subprocess.run([str(vault / "script" / "setup"), "--lean", "--yes", "--no-commit",
                            "--upstream", UPSTREAM], capture_output=True, text=True, env=env)
        ok = r.returncode == 0
        check("setup completes", ok, r.stdout + r.stderr)
        check("plainkeep put on PATH (symlink)", (bindir / "plainkeep").is_symlink()
              and os.readlink(bindir / "plainkeep") == str(vault / "plainkeep"))
        check("zsh completion installed (symlink)", (compdir / "_plainkeep").is_symlink()
              and os.readlink(compdir / "_plainkeep") == str(vault / "script" / "completions" / "_plainkeep"))
        check("sibling roots created", (home / "work").is_dir() and (home / "files").is_dir())
        check("sibling roots are NOT inside the repo", not (vault / "work").exists())
        check("upstream remote set", git(vault, "remote", "get-url", "upstream").stdout.strip() == UPSTREAM)
        check("upstream is fetch-only (push disabled)",
              "DISABLED" in git(vault, "remote", "get-url", "--push", "upstream").stdout)
        check("lean: test/ dropped from the vault", not git(vault, "ls-files", "test/").stdout.strip())
        check("lean: engine kept (bin/)", bool(git(vault, "ls-files", "bin/").stdout.strip()))
        check("lean: design docs kept", bool(git(vault, "ls-files", "docs/design/").stdout.strip()))
        check("doctor passes after setup", subprocess.run(
            [str(vault / "plainkeep"), "doctor"], capture_output=True, text=True, env=env).returncode == 0)

        # update guards cleanly with no reachable remote
        u = subprocess.run([str(vault / "script" / "update"), "--remote", "nope"],
                           capture_output=True, text=True, env=env)
        check("update refuses a missing remote", u.returncode == 1 and "no 'nope' remote" in u.stderr, u.stderr)
        # engine manifest (path lines only, ignoring comments) excludes content + dev-only paths
        paths = [ln.strip() for ln in (vault / "script" / "engine.txt").read_text().splitlines()
                 if ln.strip() and not ln.startswith("#")]
        content = {"wiki", "tasks", "journal", "inbox", "test", "ref", "jobs"}
        check("engine manifest lists only framework paths",
              "bin" in paths and "skills" in paths and not (content & set(paths)), str(paths))

        # --- issue #5: script/update must survive a corrupted/invalid .plainkeep-engine-ref (never merge
        # against garbage) AND not abort on unhashable engine paths (the .codex/skills symlink). Each
        # case gets a FRESH clone (one update per vault = real usage; a reused vault would inherit the
        # prior run's staged state). `origin` = the local template clone, a reachable remote for the
        # real merge path. We copy the working-tree script so the test validates the CURRENT script.
        cur = git(vault, "rev-parse", "HEAD").stdout.strip()
        prev = git(vault, "rev-parse", "HEAD~1").stdout.strip()
        older = git(vault, "rev-parse", "HEAD~3").stdout.strip()

        def _update_case(ref_text, i):
            v = tmp / f"refcase{i}"
            subprocess.run(["git", "clone", "-q", str(REPO), str(v)], capture_output=True, text=True)
            shutil.copy(REPO / "script" / "update", v / "script" / "update")
            (v / ".plainkeep-engine-ref").write_text(ref_text)
            r = subprocess.run([str(v / "script" / "update"), "--remote", "origin", "--branch", "main"],
                               capture_output=True, text=True, env={**env, "PLAINKEEP_HOME": str(v)})
            return r, (v / ".plainkeep-engine-ref").read_text().strip()

        u, ref_after = _update_case(cur + prev, 1)  # two SHAs concatenated (the reported corruption)
        check("corrupted double-SHA ref → warns, no spurious conflicts, exit 0",
              u.returncode == 0 and "not a single 40-char SHA" in u.stderr and "CONFLICT" not in u.stdout,
              u.stdout + u.stderr)
        check("corrupted ref rewritten to a single 40-hex SHA", bool(re.fullmatch(r"[0-9a-f]{40}", ref_after)))
        u2, _ = _update_case("f" * 40, 2)  # valid format but not a fetched commit
        check("unfetched ref → warns 'not a fetched commit', exit 0",
              u2.returncode == 0 and "not a fetched commit" in u2.stderr, u2.stdout + u2.stderr)
        u3, _ = _update_case(older, 3)  # valid older base → exercises the REAL 3-way merge (.codex/skills symlink)
        check("valid older ref → real 3-way merge, no fatal/abort (.codex/skills symlink)",
              u3.returncode == 0 and "fatal" not in (u3.stdout + u3.stderr), u3.stdout + u3.stderr)

    print(f"{BOLD}Setup/update flow (script/setup, script/update) — {len(results)} checks{RESET}\n")
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
