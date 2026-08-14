#!/usr/bin/env python3
"""
run_trust.py — the "trust" wave (proposal Parts 0.1/0.3/0.4), offline + stdlib only:
  1. exit-code protocol + self-teaching errors — guardrail CLI maps confirm→3, deny→5, unknown→4
     (with a did-you-mean), allow→0, and the dispatcher PROPAGATES those instead of flattening to 1.
  2. script/update 3-way merge — a scripted two-repo fixture (upstream + clone) proves fast-path,
     clean merge, and conflict-marker behavior against a tracked .plainkeep-engine-ref.
  3. doctor additions — sync-wall (no .git under iCloud/Dropbox/Syncthing), <2 push-remote warning,
     and the frontmatter Properties round-trip check.
"""
from __future__ import annotations
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from lib.hermetic import scratch_root, seal
seal()   # hermetic: an empty throwaway registry, never the developer's real vault

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib import vaultfx  # noqa: E402

# The engine modules loaded IN-PROCESS below resolve the data root at import and have no
# engine-relative fallback since ADR-014 Task 1b, so a root has to be selected before the
# first import. It used to be the CHECKOUT, on the reasoning that only pure functions run
# in-process. That holds for them and not for what INHERITS the variable: the direct
# `bin/lib/guardrail.py` subprocess below took it and appended to the real vault's audit log
# on every green run. A marked throwaway vault answers the same import-time requirement.
os.environ.setdefault("PLAINKEEP_HOME", scratch_root())
GREEN, RED, DIM, BOLD, RESET = "\033[32m", "\033[31m", "\033[2m", "\033[1m", "\033[0m"
results = []


def check(name, cond, detail=""):
    results.append((name, bool(cond), detail))


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    sys.modules[name] = m
    spec.loader.exec_module(m)
    return m


def git(repo, *a):
    return subprocess.run(["git", "-C", str(repo), *a], capture_output=True, text=True)


def _git_init(repo):
    git(repo, "init", "-q", "-b", "main")  # explicit: script/update defaults to --branch main
    git(repo, "config", "user.email", "t@example.com")
    git(repo, "config", "user.name", "Test")


# ---------------------------------------------------------------------------
# 1. exit-code protocol
# ---------------------------------------------------------------------------
def test_exit_codes():
    bg = _load(REPO / "bin" / "lib" / "guardrail.py", "trust_guardrail")

    # pure verdict→code map (Part 0.3): allow 0, confirm 3, deny 5
    check("exit_code_for(allow) == 0", bg.exit_code_for(bg.Decision(bg.ALLOW, "", "read")) == 0)
    check("exit_code_for(confirm) == 3", bg.exit_code_for(bg.Decision(bg.CONFIRM, "", "confirm")) == 3)
    check("exit_code_for(deny) == 5", bg.exit_code_for(bg.Decision(bg.DENY, "", "deny")) == 5)
    check("EXIT constants match the protocol",
          (bg.EXIT_OK, bg.EXIT_USAGE, bg.EXIT_CONFIRM, bg.EXIT_NOT_FOUND, bg.EXIT_DENY) == (0, 2, 3, 4, 5))

    # remediation is the exact re-run line, not a class name
    check("confirm remediation prints the exact re-run",
          bg._remediation("archive", ["foo"]) == "re-run: plainkeep archive foo --yes")

    # the guardrail CLI end-to-end against an isolated bin/ (no confirm/deny verb ships today)
    with tempfile.TemporaryDirectory() as td:
        # The WHOLE bin/lib, not a two-file closure: since ADR-014 Task 1b guardrail.py resolves the
        # data root through lib/vaultroot.py (which reads vaultreg.py, wall.py and output.py), and a
        # hand-listed closure would encode an import graph here that lives there.
        binlib = Path(td) / "bin" / "lib"
        binlib.parent.mkdir(parents=True)
        shutil.copytree(REPO / "bin" / "lib", binlib, ignore=shutil.ignore_patterns("__pycache__"))

        def verb(name, risk, dry_run=False):
            d = Path(td) / "bin" / name
            d.mkdir(parents=True)
            body = {"risk": risk}
            if dry_run:
                body["dry_run"] = True
            (d / "cmd.json").write_text(json.dumps(body))
            (d / "run.py").write_text("")

        verb("confirmy", "confirm")
        verb("denyy", "deny")
        verb("dryable", "confirm", dry_run=True)

        # PLAINKEEP_HOME is the isolated fixture, marked so it validates (Task 1b): the guardrail CLI
        # is being exercised over ITS verb surface, and its audit log must land there and not in the
        # developer's vault.
        vaultfx.mark_vault(Path(td))

        def cli(*args):
            return subprocess.run([sys.executable, str(binlib / "guardrail.py"), *args],
                                  capture_output=True, text=True,
                                  env={**os.environ, "PLAINKEEP_HOME": str(td)})

        r = cli("confirmy")
        check("CLI confirm-class → exit 3 + remediation",
              r.returncode == 3 and "re-run: plainkeep confirmy --yes" in r.stderr, r.stderr)
        r = cli("confirmy", "--yes")
        check("CLI confirm-class with --yes → exit 0", r.returncode == 0, r.stderr)
        r = cli("denyy")
        check("CLI deny-class → exit 5", r.returncode == 5, r.stderr)
        r = cli("dryable", "--dry-run")
        check("CLI --dry-run downgrades confirm → exit 0", r.returncode == 0, r.stderr)
        r = cli("confirmyy")
        check("CLI unknown verb → exit 4 + did-you-mean",
              r.returncode == 4 and "confirmy" in r.stderr and "did you mean" in r.stderr, r.stderr)

    # the real dispatcher must PROPAGATE the code (not `|| exit 1`) for an unknown verb.
    # Against a throwaway engine-carrying vault, not the checkout: the ALLOW branch below logs, and
    # that log line used to land in the developer's own vault.
    with tempfile.TemporaryDirectory() as td:
        dh = Path(td)
        vaultfx.dispatchable_vault(dh, REPO)
        denv = {**os.environ, "PLAINKEEP_HOME": str(dh)}
        d = subprocess.run([str(REPO / "plainkeep"), "serch"], capture_output=True, text=True, env=denv)
        check("dispatcher unknown verb → exit 4 (did-you-mean: search)",
              d.returncode == 4 and "search" in d.stderr, d.stderr)
        d = subprocess.run([str(REPO / "plainkeep"), "help"], capture_output=True, text=True, env=denv)
        check("dispatcher allow verb → exit 0", d.returncode == 0, d.stderr)


# ---------------------------------------------------------------------------
# 2. script/update 3-way merge
# ---------------------------------------------------------------------------
def _foo(repo):
    return (Path(repo) / "engine" / "foo.txt")


def test_update_merge():
    with tempfile.TemporaryDirectory() as td:
        up, vault = Path(td) / "upstream", Path(td) / "vault"
        (up / "engine").mkdir(parents=True)
        (up / "script").mkdir(parents=True)
        # a minimal upstream engine: the REAL update script + an engine.txt + a data file
        shutil.copy(REPO / "script" / "update", up / "script" / "update")
        (up / "script" / "update").chmod(0o755)
        (up / "script" / "engine.txt").write_text("engine/foo.txt\n")
        _foo(up).write_text("L1\nL2\nL3\n")
        _git_init(up)
        git(up, "add", "-A"); git(up, "commit", "-q", "-m", "v1")
        v1 = git(up, "rev-parse", "HEAD").stdout.strip()

        subprocess.run(["git", "clone", "-q", str(up), str(vault)], capture_output=True, text=True)
        git(vault, "config", "user.email", "t@example.com"); git(vault, "config", "user.name", "Test")
        git(vault, "remote", "add", "upstream", str(up))

        def update():
            return subprocess.run([str(vault / "script" / "update")], capture_output=True, text=True)

        # --- first run: no ref → fast-path + loud warning, ref written ---
        r = update()
        ref = vault / ".plainkeep-engine-ref"
        check("first update warns about the missing ref", "no .plainkeep-engine-ref" in r.stderr, r.stderr)
        check("first update writes .plainkeep-engine-ref = upstream HEAD",
              ref.exists() and ref.read_text().strip() == v1, r.stdout + r.stderr)
        git(vault, "add", "-A"); git(vault, "commit", "-q", "-m", "sync v1")

        # --- diverge in DIFFERENT regions → clean 3-way merge ---
        _foo(up).write_text("L1\nL2\nL3-upstream\n")
        git(up, "commit", "-q", "-am", "v2")
        v2 = git(up, "rev-parse", "HEAD").stdout.strip()
        _foo(vault).write_text("L1-local\nL2\nL3\n")
        git(vault, "commit", "-q", "-am", "local edit")
        r = update()
        merged = _foo(vault).read_text()
        check("clean 3-way merge keeps BOTH sides",
              "L1-local" in merged and "L3-upstream" in merged and "<<<<<<<" not in merged, merged)
        check("clean merge is reported + no conflicts", "merged engine/foo.txt" in r.stdout
              and "conflict" not in r.stdout.lower(), r.stdout)
        check("ref advances to the new upstream HEAD", ref.read_text().strip() == v2, ref.read_text())
        git(vault, "add", "-A"); git(vault, "commit", "-q", "-m", "sync v2")

        # --- diverge on the SAME line → conflict markers surfaced ---
        _foo(up).write_text("L1-local\nL2-upstream\nL3-upstream\n")
        git(up, "commit", "-q", "-am", "v3")
        _foo(vault).write_text("L1-local\nL2-local\nL3-upstream\n")
        git(vault, "commit", "-q", "-am", "conflicting edit")
        r = update()
        conflicted = _foo(vault).read_text()
        check("same-line divergence produces conflict markers",
              "<<<<<<<" in conflicted and ">>>>>>>" in conflicted, conflicted)
        check("conflict is surfaced in the summary",
              "CONFLICT" in r.stdout and "engine/foo.txt" in r.stdout, r.stdout)


# ---------------------------------------------------------------------------
# 3. doctor additions (Part 0.4)
# ---------------------------------------------------------------------------
def _well_formed(home: Path):
    shutil.copy(REPO / "AGENTS.md", home / "AGENTS.md")
    shutil.copy(REPO / "CLAUDE.md", home / "CLAUDE.md")
    (home / "skills" / "operate-plainkeep").mkdir(parents=True)
    shutil.copy(REPO / "skills" / "operate-plainkeep" / "SKILL.md", home / "skills" / "operate-plainkeep" / "SKILL.md")
    (home / ".codex").mkdir(); (home / ".claude").mkdir()
    (home / ".codex" / "config.toml").write_text('sandbox_mode="workspace-write"\n')
    (home / ".claude" / "settings.json").write_text('{"permissions":{"allow":["Bash(plainkeep:*)"]}}')
    os.symlink("../skills", home / ".codex" / "skills")
    os.symlink("../skills", home / ".claude" / "skills")


def _doctor(home, roots):
    env = {**os.environ, "PLAINKEEP_HOME": str(home), "PLAINKEEP_ROOTS_HOME": str(roots)}
    subprocess.run([sys.executable, str(REPO / "bin" / "doctor" / "run.py"), "--init"],
                   capture_output=True, text=True, env=env)
    subprocess.run([sys.executable, str(REPO / "bin" / "help" / "run.py")],
                   capture_output=True, text=True, env=env)
    return subprocess.run([sys.executable, str(REPO / "bin" / "doctor" / "run.py")],
                          capture_output=True, text=True, env=env)


def test_update_manifest_two_pass():
    """A path ADDED to script/engine.txt must arrive in the SAME run that adds it.

    engine.txt is itself an engine path, so the walk reads the manifest it started with while
    refreshing that manifest for next time. One pass therefore pulled the new list but not the
    files it named — which is how a vault ended up with `pyproject.toml` in its manifest, absent
    from disk, and `script/setup` refusing "source tree is missing pyproject.toml".
    """
    with tempfile.TemporaryDirectory() as td:
        up, vault = Path(td) / "upstream", Path(td) / "vault"
        (up / "engine").mkdir(parents=True)
        (up / "script").mkdir(parents=True)
        shutil.copy(REPO / "script" / "update", up / "script" / "update")
        (up / "script" / "update").chmod(0o755)
        (up / "script" / "engine.txt").write_text("engine/foo.txt\nscript\n")
        _foo(up).write_text("L1\n")
        _git_init(up)
        git(up, "add", "-A"); git(up, "commit", "-q", "-m", "v1")

        subprocess.run(["git", "clone", "-q", str(up), str(vault)], capture_output=True, text=True)
        git(vault, "config", "user.email", "t@example.com"); git(vault, "config", "user.name", "Test")
        git(vault, "remote", "add", "upstream", str(up))

        def update():
            return subprocess.run([str(vault / "script" / "update")], capture_output=True, text=True)

        update()                                   # establish the ref
        git(vault, "add", "-A"); git(vault, "commit", "-q", "-m", "sync v1")

        # upstream adds a NEW engine file and lists it, in one commit
        (up / "engine" / "bar.txt").write_text("BAR\n")
        (up / "script" / "engine.txt").write_text("engine/foo.txt\nengine/bar.txt\nscript\n")
        git(up, "add", "-A"); git(up, "commit", "-q", "-m", "v2: add bar to the manifest")

        r = update()
        bar = vault / "engine" / "bar.txt"
        check("manifest change is detected and announced",
              "engine.txt changed" in r.stdout, r.stdout + r.stderr)
        check("a path added to engine.txt arrives in the SAME run",
              bar.is_file() and bar.read_text() == "BAR\n",
              f"exists={bar.is_file()} stdout={r.stdout}")
        check("the newly pulled path is STAGED, like every other engine path",
              "engine/bar.txt" in git(vault, "diff", "--cached", "--name-only").stdout, r.stdout)
        check("ref still advances after the extra pass",
              (vault / ".plainkeep-engine-ref").read_text().strip()
              == git(up, "rev-parse", "HEAD").stdout.strip())

        # a stable manifest must NOT trigger a second pass (and must not loop)
        r2 = update()
        check("a run that does not change the manifest re-runs nothing",
              "engine.txt changed" not in r2.stdout, r2.stdout)


def test_doctor():
    # a healthy git vault, one remote, a churny note → WARN (not FAIL) on remotes + frontmatter
    with tempfile.TemporaryDirectory() as td:
        home, roots = Path(td) / "ops", Path(td) / "roots"
        home.mkdir(); roots.mkdir()
        _well_formed(home)
        _git_init(home)
        git(home, "remote", "add", "origin", "https://example.com/plainkeep.git")
        nd = home / "wiki" / "notes"
        nd.mkdir(parents=True)
        (nd / "churn.md").write_text("---\ntype: note\ntitle: Churn\ntags: [a, b]\n---\n# Churn\n")
        r = _doctor(home, roots)
        check("doctor: healthy vault does not FAIL on the new checks", r.returncode == 0, r.stdout)
        check("doctor: warns on a single push remote", "push remote" in r.stdout, r.stdout)
        check("doctor: flags frontmatter Properties would churn",
              "churn" in r.stdout and "churn.md" in r.stdout, r.stdout)
        check("doctor: ~/plainkeep not under a cloud-sync tree", "not under a cloud-sync tree" in r.stdout, r.stdout)

    # a clean-frontmatter vault is reported stable
    with tempfile.TemporaryDirectory() as td:
        home, roots = Path(td) / "ops", Path(td) / "roots"
        home.mkdir(); roots.mkdir()
        _well_formed(home)
        nd = home / "wiki" / "notes"; nd.mkdir(parents=True)
        (nd / "ok.md").write_text("---\ntype: note\ntitle: Ok\ntags: []\n---\n# Ok\n")
        r = _doctor(home, roots)
        check("doctor: clean frontmatter is Properties-stable",
              "Obsidian-Properties stable" in r.stdout, r.stdout)

    # a vault whose path resolves under a cloud-sync tree → FAIL
    with tempfile.TemporaryDirectory() as td:
        home, roots = Path(td) / "Dropbox" / "ops", Path(td) / "roots"
        home.mkdir(parents=True); roots.mkdir()
        _well_formed(home)
        r = _doctor(home, roots)
        check("doctor: FAILs when ~/plainkeep is under Dropbox",
              r.returncode == 1 and "cloud-sync" in r.stdout, r.stdout)

    # a work repo whose .git is under a cloud-sync tree → FAIL
    with tempfile.TemporaryDirectory() as td:
        home, roots = Path(td) / "ops", Path(td) / "roots"
        home.mkdir(); roots.mkdir()
        _well_formed(home)
        synced_repo = roots / "work" / "Syncthing" / "proj"
        synced_repo.mkdir(parents=True)
        (synced_repo / ".git").mkdir()
        r = _doctor(home, roots)
        check("doctor: FAILs when a ~/work repo .git is under Syncthing",
              r.returncode == 1 and "cloud-sync tree" in r.stdout, r.stdout)


def main() -> int:
    test_exit_codes()
    test_update_merge()
    test_update_manifest_two_pass()
    test_doctor()

    print(f"{BOLD}Trust wave: exit codes + update merge + doctor walls — {len(results)} checks{RESET}\n")
    passed = sum(1 for _, ok, _ in results if ok)
    for name, ok, detail in results:
        mark = f"{GREEN}PASS{RESET}" if ok else f"{RED}FAIL{RESET}"
        print(f"  {mark} {name:<52}" + (f" {DIM}{detail.strip()[:80]}{RESET}" if (detail and not ok) else ""))
    failed = len(results) - passed
    print(f"\n{BOLD}Result:{RESET} {GREEN}{passed} passed{RESET}, "
          f"{(RED if failed else DIM)}{failed} failed{RESET}, {len(results)} checks")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
