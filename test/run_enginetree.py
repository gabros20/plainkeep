#!/usr/bin/env python3
"""
run_enginetree.py — the ENGINE INSTALLER, exercised against real installed trees.

Why this suite exists, stated plainly: `bin/lib/enginetree.py` shipped with no suite of its own. It
was reached only sideways — `run_setup.py` asserted one `os.access` on `<engine>/VERSION`,
`run_core_parity.py` counted paths in an installed tree — and everything the module does when
something GOES WRONG (a `--force` over a live install, a hand-supplied `--version`, an interrupted
seal, a concurrent run) was unexercised. Fifteen findings landed there in one review.

The shape of every case below follows from that: drive the installer through the failure, then WALK
THE FILESYSTEM and look at what survived. An exit code is checked too, and it is never the proof —
the reviewer's worst finding was a command that exited 0.

HERMETIC, and specifically about the developer's real engine: every install here goes to a temp
`PLAINKEEP_ENGINE_HOME`. `~/.local/share/plainkeep` is never read and never written.

Offline, stdlib only.
"""
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
ENGINETREE = REPO / "bin" / "lib" / "enginetree.py"
VERSION = (REPO / "VERSION").read_text(encoding="utf-8").strip()
PY = sys.executable
GREEN, RED, DIM, BOLD, RESET = "\033[32m", "\033[31m", "\033[2m", "\033[1m", "\033[0m"
results: list[tuple[str, bool, str]] = []

EXIT_OK, EXIT_UNEXPECTED, EXIT_USAGE, EXIT_DENY = 0, 1, 2, 5


def check(name: str, cond: bool, detail: str = "") -> None:
    results.append((name, bool(cond), detail))


def et(root: Path, *args: str, **envextra) -> subprocess.CompletedProcess:
    """Run the installer CLI with the install root pointed at `root`."""
    env = {**os.environ, "PLAINKEEP_ENGINE_HOME": str(root), **envextra}
    env.pop("PLAINKEEP_ENGINE", None)
    return subprocess.run([PY, str(ENGINETREE), *args], capture_output=True, text=True, env=env)


def installed(root: Path) -> Path:
    return root / "engine" / VERSION


def healthy(root: Path) -> subprocess.CompletedProcess:
    """A normal, sealed, activated install of the repository into `root`."""
    return et(root, "--install", str(REPO))


def broken_source(tmp: Path) -> Path:
    """A source checkout that is missing one OWNED tree — so `_copy_owned` raises PART WAY THROUGH,
    which is the shape every interruption (^C, ENOSPC, SIGKILL) shares."""
    src = tmp / "broken-src"
    for rel in ("bin", "templates", "frontends"):
        shutil.copytree(REPO / rel, src / rel, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    shutil.copy2(REPO / "VERSION", src / "VERSION")
    shutil.copy2(REPO / "plainkeep", src / "plainkeep")
    return src                                  # no skills/operate-plainkeep — the last tree copied


def tree_files(root: Path) -> set[str]:
    return {str(p.relative_to(root)) for p in root.rglob("*")}


def _function_source(f: Path, name: str) -> str | None:
    """One function's own source text, by AST — so a check about what THIS function does is not
    answered by a line somewhere else in the same file."""
    import ast
    tree = ast.parse(f.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return ast.get_source_segment(f.read_text(encoding="utf-8"), node)
    return None


# --------------------------------------------------------------------------------------------
# IMPORTANT-2 — `--force` must not destroy the live install before it has a replacement.
# --------------------------------------------------------------------------------------------
def case_force_keeps_the_live_install(tmp: Path) -> None:
    root = tmp / "force"
    healthy(root)
    launcher = root / "engine" / "current" / "plainkeep"
    check("fixture: a healthy install has a live current/plainkeep", launcher.is_file())
    before = tree_files(installed(root))

    r = et(root, "--install", str(broken_source(tmp)), "--force")
    out = r.stdout + r.stderr
    check("force over a source missing an owned tree REFUSES", r.returncode != 0, out)
    # The proof is the filesystem, not the exit code: this used to remove the old tree on the way IN
    # and leave `engine/` holding nothing but a dangling `current`.
    check("...and the live engine is still there, complete", launcher.is_file()
          and tree_files(installed(root)) == before,
          f"launcher={launcher.is_file()} delta={sorted(before ^ tree_files(installed(root)))[:5]}")
    check("...and `current` is not dangling", (root / "engine" / "current").is_symlink()
          and (root / "engine" / "current").resolve().is_dir())
    v = et(root, "--verify", str(installed(root)))
    check("...and the surviving engine still verifies clean", v.returncode == EXIT_OK,
          v.stdout + v.stderr)


# --------------------------------------------------------------------------------------------
# IMPORTANT-3 — `--version` composes into a destination that gets rmtree'd. It is validated.
# --------------------------------------------------------------------------------------------
def case_version_is_not_a_path(tmp: Path) -> None:
    root = tmp / "vers"
    healthy(root)
    # Unrelated data in the shared XDG directory the install root is: `--version ..` resolved
    # `remove_version()` to `rmtree(<install root>)` and took all of this with it.
    (root / "precious-user-data.txt").write_text("keep me", encoding="utf-8")
    (root / "other-stuff").mkdir()
    (root / "other-stuff" / "keep.txt").write_text("me too", encoding="utf-8")

    for bad, why in ((".." , "the parent directory"), (".", "the install root itself"),
                     ("a/b", "a path with a separator"), (".hidden", "the installer's namespace"),
                     ("current", "the active-engine symlink")):
        r = et(root, "--install", str(REPO), "--version", bad, "--force")
        check(f"--version {bad!r} ({why}) is REFUSED", r.returncode == EXIT_USAGE
              and "Traceback" not in (r.stdout + r.stderr), f"rc={r.returncode} {r.stderr.strip()}")

    check("...and the unrelated data in the install root survived",
          (root / "precious-user-data.txt").is_file() and (root / "other-stuff" / "keep.txt").is_file())
    # `--version current` used to walk THROUGH the symlink and unseal the running engine.
    check("...and the ACTIVE engine is still sealed",
          not os.access(installed(root) / "VERSION", os.W_OK),
          "VERSION became writable")
    r = et(root, "--activate", "..")
    check("--activate is validated the same way", r.returncode == EXIT_USAGE
          and "Traceback" not in (r.stdout + r.stderr), f"rc={r.returncode} {r.stderr.strip()}")


# --------------------------------------------------------------------------------------------
# IMPORTANT-5 — the seal is VERIFIED, not only written. A half-sealed tree must be visible.
# --------------------------------------------------------------------------------------------
def case_the_seal_is_verified(tmp: Path) -> None:
    root = tmp / "seal"
    healthy(root)
    eng = installed(root)
    r = et(root, "--verify", str(eng))
    check("a sealed install verifies clean", r.returncode == EXIT_OK, r.stdout + r.stderr)

    # The window the rename-first order necessarily creates: the tree landed under its version name
    # and the process died before `_chmod_tree` ran. Nothing on any later path used to ask.
    subprocess.run(["chmod", "-R", "u+w", str(eng)], check=True)
    r = et(root, "--verify", str(eng))
    check("an UNSEALED installed tree is reported by --verify",
          r.returncode == EXIT_DENY and "WRITABLE" in (r.stdout + r.stderr), r.stdout + r.stderr)

    # ...and re-running the ordinary install REPAIRS it. Before, the only thing that moved this state
    # forward was `--force`, i.e. the destructive branch.
    r = et(root, "--install", str(REPO))
    check("...and a plain re-install (no --force) re-seals it", r.returncode == EXIT_OK,
          r.stdout + r.stderr)
    check("...and the tree is read-only again", not os.access(eng / "VERSION", os.W_OK))
    r = et(root, "--verify", str(eng))
    check("...and it verifies clean afterwards", r.returncode == EXIT_OK, r.stdout + r.stderr)

    # The immutability refusal is NOT dropped: a complete, sealed engine still refuses to be
    # reinstalled over without --force.
    r = et(root, "--install", str(REPO))
    check("a COMPLETE sealed engine still refuses a plain re-install",
          r.returncode != 0 and "already installed" in (r.stdout + r.stderr), r.stderr.strip())
    # A checkout is not an installed tree and must never be told it is unsealed.
    r = et(root, "--verify", str(REPO))
    check("the source CHECKOUT is not judged against the seal",
          "WRITABLE" not in (r.stdout + r.stderr), r.stdout + r.stderr)


# --------------------------------------------------------------------------------------------
# IMPORTANT-7 — what gets WRITTEN DOWN names `current`, not the version that happens to be active.
# --------------------------------------------------------------------------------------------
def case_persisted_paths_name_current(tmp: Path) -> None:
    # The two sites that PERSIST the launcher path, read per FUNCTION rather than per file, because
    # the distinction is the finding: `job run` spawns the launcher right now (`launcher()` is
    # correct there) while `_plist` writes it into a launchd job that outlives the invocation. A
    # file-wide grep would conflate exactly the two things this separation keeps apart. Asked FIRST,
    # so it is answered even if the runtime probe below cannot run at all.
    for rel, func in (("bin/job/run.py", "_plist"), ("bin/mcp/run.py", "_dispatcher_bin")):
        src = _function_source(REPO / rel, func)
        check(f"{rel}:{func}() persists stable_launcher(), not launcher()",
              src is not None and "enginetree.stable_launcher()" in src
              and "enginetree.launcher()" not in src, (src or "<not found>")[:200])

    root = tmp / "stable"
    healthy(root)
    eng = installed(root)
    got = subprocess.run(
        [PY, "-c", "import sys;sys.path.insert(0,sys.argv[1]);import enginetree;"
                   "print(enginetree.launcher());print(enginetree.stable_launcher())",
         str(eng / "bin" / "lib")],
        capture_output=True, text=True,
        env={**os.environ, "PLAINKEEP_ENGINE_HOME": str(root)})
    lines = got.stdout.strip().splitlines()
    check("enginetree exposes both a spawn path and a PERSISTED path", len(lines) == 2, got.stderr)
    if len(lines) != 2:
        return
    spawn, persisted = lines
    check("launcher() spells the VERSION (right for a spawn)", VERSION in spawn, spawn)
    check("stable_launcher() spells `current` (right for a plist / an MCP config)",
          persisted == str(root / "engine" / "current" / "plainkeep"), persisted)
    check("...and it names a file that exists", Path(persisted).is_file(), persisted)

    # The point of the stable name: activating another version RE-POINTS every persisted artefact
    # for free. A version-pinned plist keeps running the old engine, silently.
    et(root, "--install", str(REPO), "--version", "9.9.9-next")
    check("after activating a new version, the persisted path resolves to the NEW engine",
          os.path.realpath(persisted) == str(root / "engine" / "9.9.9-next" / "plainkeep"),
          os.path.realpath(persisted))



# --------------------------------------------------------------------------------------------
# MINOR-8 — two concurrent installs must not clobber each other's staging.
# --------------------------------------------------------------------------------------------
def case_concurrent_installs(tmp: Path) -> None:
    root = tmp / "race"
    healthy(root)
    launcher = root / "engine" / "current" / "plainkeep"
    env = {**os.environ, "PLAINKEEP_ENGINE_HOME": str(root)}
    env.pop("PLAINKEEP_ENGINE", None)
    procs = [subprocess.Popen([PY, str(ENGINETREE), "--install", str(REPO), "--force"],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
             for _ in range(2)]
    outs = [p.communicate() for p in procs]
    rcs = [p.returncode for p in procs]
    check("at least one of two concurrent installs SUCCEEDS", EXIT_OK in rcs,
          f"rcs={rcs} {[o[1][-200:] for o in outs]}")
    check("...and neither leaves the install without an engine", launcher.is_file(),
          f"rcs={rcs} {[o[1][-300:] for o in outs]}")
    v = et(root, "--verify", str(installed(root)))
    check("...and what survived is a complete, sealed engine", v.returncode == EXIT_OK,
          v.stdout + v.stderr)


# --------------------------------------------------------------------------------------------
# MINOR-9/10/11 — the diagnostics tell the truth about a broken install.
# --------------------------------------------------------------------------------------------
def case_diagnostics(tmp: Path) -> None:
    root = tmp / "diag"
    healthy(root)
    eng = installed(root)

    # MINOR-10: `current` left pointing at a tree that is gone. `os.path.realpath` does not require
    # the target to exist, so the one diagnostic that could say "the engine is gone" said nothing.
    subprocess.run(["chmod", "-R", "u+w", str(eng)], check=True)
    shutil.rmtree(eng)
    r = et(root, "--print", "current")
    check("--print current REFUSES when `current` dangles", r.returncode != EXIT_OK,
          f"rc={r.returncode} out={r.stdout.strip()!r}")

    # MINOR-11: a SIGKILL mid-copy leaves `.incoming-*` behind. It is debris, not a version.
    (root / "engine" / ".incoming-9.9.9.4242").mkdir(parents=True)
    healthy(root)
    r = et(root, "--print", "versions")
    listed = r.stdout.split()
    check("--print versions lists the installed version", VERSION in listed, r.stdout)
    check("...and NOT the `.incoming-*` staging debris",
          not any(v.startswith(".incoming") for v in listed), r.stdout)

    # MINOR-9: a real I/O failure is a refusal, not a traceback. An unwritable install root is the
    # cheapest honest way to produce one (EACCES inside `root.mkdir`).
    ro = tmp / "readonly-root"
    (ro / "engine").mkdir(parents=True)
    os.chmod(ro / "engine", 0o555)
    try:
        r = et(ro, "--install", str(REPO), "--version", "8.8.8-ro")
        check("an OSError renders as a plainkeep refusal, not a traceback",
              "Traceback" not in (r.stdout + r.stderr) and r.returncode == EXIT_UNEXPECTED
              and r.stderr.startswith("plainkeep:"), f"rc={r.returncode} {r.stderr[:300]}")
    finally:
        os.chmod(ro / "engine", 0o755)


# --------------------------------------------------------------------------------------------
# MINOR-12/13 — the completeness checks cover what they claim to.
# --------------------------------------------------------------------------------------------
def case_completeness_covers_the_right_paths(tmp: Path) -> None:
    root = tmp / "probe"
    healthy(root)
    eng = installed(root)
    subprocess.run(["chmod", "-R", "u+w", str(eng)], check=True)

    # MINOR-13: `bin/__complete/` is a REAL verb (run.py + cmd.json). The `__`-prefix exclusion was
    # meant for `__pycache__` and swallowed it, so it could vanish and `verify()` still said OK.
    hidden = eng / "bin" / "__complete" / "run.py"
    hidden.rename(hidden.with_suffix(".moved"))
    r = et(root, "--verify", str(eng))
    check("verify() sees bin/__complete/ — it is a verb, not a dunder directory",
          "__complete" in (r.stdout + r.stderr), r.stdout + r.stderr)
    hidden.with_suffix(".moved").rename(hidden)

    # MINOR-12: the modules `vaultroot.py` imports at MODULE SCOPE died with a raw traceback before
    # `require_intact()` could speak, so ADR-014 D2's "absent/unverified → refuse" was not delivered
    # for the ones whose absence produces the worst output.
    vault = tmp / "probe-vault"
    (vault / ".plainkeep").mkdir(parents=True)
    for missing in ("bin/lib/output.py", "bin/lib/wall.py", "bin/lib/vaultreg.py"):
        hurt = tmp / ("hurt-" + missing.replace("/", "-"))
        shutil.copytree(eng, hurt, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        (hurt / missing).unlink()
        r = subprocess.run([PY, str(hurt / "bin" / "lib" / "vaultroot.py"), "--select"],
                           capture_output=True, text=True,
                           env={**os.environ, "PLAINKEEP_HOME": str(vault)})
        out = r.stdout + r.stderr
        check(f"an engine missing {missing} REFUSES instead of tracebacking",
              "Traceback" not in out and "incomplete" in out, f"rc={r.returncode} {out[:300]}")
        check(f"...and the refusal for {missing} names the reinstall command",
              "--install" in out, out[:300])


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="pk-enginetree-") as td:
        tmp = Path(os.path.realpath(td))
        case_force_keeps_the_live_install(tmp)
        case_version_is_not_a_path(tmp)
        case_the_seal_is_verified(tmp)
        case_persisted_paths_name_current(tmp)
        case_concurrent_installs(tmp)
        case_diagnostics(tmp)
        case_completeness_covers_the_right_paths(tmp)
        # Every installed tree is sealed 0555, which TemporaryDirectory cannot remove.
        for p in tmp.rglob("*"):
            try:
                if p.is_dir() and not p.is_symlink():
                    p.chmod(0o755)
            except OSError:
                pass

    print(f"{BOLD}engine installer: enginetree (Phase 2 Task 2) — {len(results)} checks{RESET}\n")
    passed = sum(1 for _, ok, _ in results if ok)
    for name, ok, detail in results:
        mark = f"{GREEN}PASS{RESET}" if ok else f"{RED}FAIL{RESET}"
        print(f"  {mark} {name:<72}" + (f" {DIM}{detail.strip()[:110]}{RESET}" if (detail and not ok) else ""))
    failed = len(results) - passed
    print(f"\nSUITE-NOTE: every install here runs against a temp PLAINKEEP_ENGINE_HOME. The "
          f"developer's real engine at ~/.local/share/plainkeep is neither read nor written, and the "
          f"suite therefore says nothing about whether THAT install is intact — `plainkeep doctor` "
          f"is what asks that.")
    print(f"\n{BOLD}Result:{RESET} {GREEN}{passed} passed{RESET}, "
          f"{(RED if failed else DIM)}{failed} failed{RESET}, {len(results)} checks")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
