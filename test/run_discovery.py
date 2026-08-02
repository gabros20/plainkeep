#!/usr/bin/env python3
"""
run_discovery.py — WHICH VAULT an invocation acts on (ADR-014, Phase 2 Task 1b).

`test/run_vault.py` gates identity: a vault has a marker and a registry entry. This suite gates the
thing identity exists FOR — that an invocation lands on the vault it was told to, and refuses rather
than guessing when it was not told.

**Why the two-vault test is the centre of this file, and why a single-vault test cannot substitute.**
"A bad root creates zero files" proves the WALL can deny a walled-off tree. It does not prove
SELECTION is correct, because there is nothing for a misselection to hit: one root, and every write
either lands there or nowhere. The failure this task exists to prevent needs two valid vaults — you
select A, and B is the one that gets written to. So every mechanism below is exercised with a valid
vault B sitting in the two positions that would win if selection leaked: the registry DEFAULT and the
CWD walk-up candidate. Then the whole enclosing sandbox is WALKED and every new file is attributed.

**Assertions are filesystem walks, not exit codes.** ADR-015 records the incident that decided this:
during its development a refusal exited 5 *having already written the note* — the inbox write
succeeded and the journal append refused. An exit code is checked too; it is never the proof.

**All three invocation paths.** The bash floor (`PLAINKEEP_CORE=off`), the compiled core through the
shim (`PLAINKEEP_CORE=require`), and the core binary invoked DIRECTLY. They run the same discovery
module by design (the core spawns `bin/lib/vaultroot.py --select`, which is the floor's own command),
so a divergence here means that sharing broke.

Hermetic: every invocation gets `PLAINKEEP_CONFIG_HOME` inside its own temp dir. Without it the
registry DEFAULT step reads the developer's real registry, and the developer's default is a real
vault full of real notes.

Offline, stdlib only.
"""
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
sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib import vaultfx  # noqa: E402

GREEN, RED, DIM, BOLD, RESET = "\033[32m", "\033[31m", "\033[2m", "\033[1m", "\033[0m"
results: list[tuple[str, bool, str]] = []

CORE_BIN = Path(os.environ.get("PLAINKEEP_CORE_BIN") or (REPO / ".local" / "bin" / "plainkeep-core"))
EXIT_USAGE, EXIT_DENY = 2, 5


def check(name: str, cond: bool, detail: str = "") -> None:
    results.append((name, bool(cond), detail))


# --------------------------------------------------------------------------------------------
# The three invocation paths, behind one signature. A case that only proved the floor refuses
# would be proving nothing about the binary a user actually runs.
# --------------------------------------------------------------------------------------------
def core_live() -> bool:
    if not (CORE_BIN.is_file() and os.access(CORE_BIN, os.X_OK)):
        return False
    return subprocess.run([str(CORE_BIN), "--core-selftest"],
                          capture_output=True).returncode == 0


PATHS = ["floor", "core", "direct"]


def invoke(path: str, argv: list[str], *, cwd: Path, env: dict) -> subprocess.CompletedProcess:
    """One invocation through one of the three paths. `env` is used VERBATIM (never merged with the
    ambient environment) so an unset variable is genuinely unset — "PLAINKEEP_HOME is not set" is
    half the cases here, and inheriting the developer's would quietly answer a different question."""
    if path == "floor":
        return _run([str(REPO / "plainkeep"), *argv], cwd, {**env, "PLAINKEEP_CORE": "off"})
    if path == "core":
        return _run([str(REPO / "plainkeep"), *argv], cwd,
                    {**env, "PLAINKEEP_CORE": "require", "PLAINKEEP_CORE_BIN": str(CORE_BIN)})
    return _run([str(CORE_BIN), *argv], cwd, env)


def _run(cmd: list[str], cwd: Path, env: dict) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=str(cwd), env=env, capture_output=True, text=True)


def base_env(sandbox: Path, **over) -> dict:
    """A deliberately MINIMAL environment: PATH, HOME and TMPDIR only, plus what a case sets.

    Minimal because half of what this suite asserts is about variables NOT being set, and because it
    doubles as the launchd/cron shape — a scheduled job wakes up with almost nothing, which is the
    environment where "it worked in my shell" stops being evidence."""
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(sandbox / "home"),
        "TMPDIR": os.environ.get("TMPDIR", "/tmp"),
        "PLAINKEEP_CONFIG_HOME": str(sandbox / "config"),
        "PLAINKEEP_ROOTS_HOME": str(sandbox / "home"),
    }
    env.update({k: str(v) for k, v in over.items()})
    return env


# --------------------------------------------------------------------------------------------
# Fixture: a sandbox holding two REAL vaults, a registry, and an engine each can dispatch through.
# --------------------------------------------------------------------------------------------
def make_vault(root: Path) -> str:
    """A vault the dispatcher can actually run in: the engine tree (Phase 1 still spawns
    `$PLAINKEEP_HOME/bin/lib/*.py`), the shim, and a marker."""
    root.mkdir(parents=True, exist_ok=True)
    os.symlink(REPO / "bin", root / "bin")
    shutil.copy2(REPO / "plainkeep", root / "plainkeep")
    os.chmod(root / "plainkeep", 0o755)
    return vaultfx.mark_vault(root)


def register(sandbox: Path, root: Path, name: str, *, default: bool = False) -> None:
    args = [sys.executable, str(REPO / "bin" / "vault" / "run.py"), "register", str(root),
            "--name", name, "--yes"]
    if default:
        args.append("--default")
    r = subprocess.run(args, capture_output=True, text=True,
                       env={**os.environ, "PLAINKEEP_HOME": str(root),
                            "PLAINKEEP_CONFIG_HOME": str(sandbox / "config")})
    if r.returncode != 0:                      # a broken fixture must be loud, not a silent skip
        raise RuntimeError(f"fixture register {name} failed: {r.returncode} {r.stdout}{r.stderr}")


def snapshot(root: Path) -> set[str]:
    """Every file under `root`, as paths relative to it. Symlinked directories are NOT followed —
    the fixtures symlink the engine in, and following that would walk the whole repo."""
    out: set[str] = set()
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = [d for d in dirnames if not os.path.islink(os.path.join(dirpath, d))]
        for f in filenames:
            out.add(os.path.relpath(os.path.join(dirpath, f), root))
    return out


def new_files(root: Path, before: set[str]) -> set[str]:
    return snapshot(root) - before


# --------------------------------------------------------------------------------------------
# A. THE TWO-VAULT IDENTITY TEST — the assertion this whole task exists for.
#
# Vaults A and B are both valid and both registered. B is made the registry DEFAULT *and* the CWD
# walk-up candidate, so it is what wins if selection leaks in either direction. A is then selected
# through EACH mechanism in turn, a unique token is captured, and the whole sandbox is walked.
# --------------------------------------------------------------------------------------------
def case_two_vault_identity() -> None:
    for path in PATHS:
        if path != "floor" and not core_live():
            continue
        for mech in ("--vault", "PLAINKEEP_HOME", "walk-up"):
            with tempfile.TemporaryDirectory() as td:
                sandbox = Path(os.path.realpath(td))
                (sandbox / "home").mkdir()
                a, b = sandbox / "vault-a", sandbox / "vault-b"
                make_vault(a)
                make_vault(b)
                register(sandbox, a, "alpha")
                # B is the DEFAULT: step 4 of the chain, i.e. what an empty selection falls to.
                register(sandbox, b, "beta", default=True)

                token = f"token-{path}-{mech}".replace("-", "")
                # cwd is INSIDE B for every mechanism, so B is also the walk-up candidate. When the
                # mechanism under test IS walk-up, cwd moves into A instead — that is the mechanism.
                cwd = a if mech == "walk-up" else b
                env = base_env(sandbox)
                argv = ["capture", token]
                if mech == "--vault":
                    argv = ["--vault", "alpha", *argv]
                elif mech == "PLAINKEEP_HOME":
                    env["PLAINKEEP_HOME"] = str(a)

                before = snapshot(sandbox)
                r = invoke(path, argv, cwd=cwd, env=env)
                created = new_files(sandbox, before)
                label = f"[{path}] select A via {mech}"

                check(f"{label}: the capture succeeded", r.returncode == 0,
                      f"rc={r.returncode} {r.stdout}{r.stderr}")

                # THE ASSERTION. Not "A has a note" — "the only new files ANYWHERE are under A".
                under_a = {f for f in created if f.startswith("vault-a" + os.sep)}
                elsewhere = created - under_a
                notes = [f for f in under_a if "inbox" in f]
                check(f"{label}: the note landed in A's inbox",
                      any(token in (sandbox / n).read_text(encoding="utf-8", errors="replace")
                          for n in notes), str(sorted(under_a)))
                check(f"{label}: a journal line landed in A", any("journal" in f for f in under_a),
                      str(sorted(under_a)))
                check(f"{label}: the audit log landed in A", any(".logs" in f for f in under_a),
                      str(sorted(under_a)))
                # ZERO changes under B, under $PWD, under the engine tree, under the config dir.
                check(f"{label}: ZERO new files under vault B", not elsewhere,
                      f"leaked: {sorted(elsewhere)}")


# --------------------------------------------------------------------------------------------
# B. THE NEGATIVE TWIN — an explicit failure never falls through.
#
# A is supplied EXPLICITLY and is invalid (unregistered / not a vault / a name nobody registered),
# while B is a perfectly good registered default sitting right there. The refusal must not become a
# capture into B. This is what no single-root test can show: with one root, "nothing was written"
# and "the right thing was written" are indistinguishable from a refusal's point of view.
# --------------------------------------------------------------------------------------------
def case_negative_twin() -> None:
    bad_cases = [
        ("--vault names an unregistered vault", "--vault-unregistered"),
        ("--vault names a path that is not a vault", "--vault-notavault"),
        ("PLAINKEEP_HOME points at an unmarked directory", "home-unmarked"),
        ("PLAINKEEP_HOME points at nothing at all", "home-missing"),
        ("PLAINKEEP_HOME is the EMPTY string (explicitly empty, not unset)", "home-empty"),
    ]
    for path in PATHS:
        if path != "floor" and not core_live():
            continue
        for label, kind in bad_cases:
            with tempfile.TemporaryDirectory() as td:
                sandbox = Path(os.path.realpath(td))
                (sandbox / "home").mkdir()
                b = sandbox / "vault-b"
                make_vault(b)
                register(sandbox, b, "beta", default=True)
                plain = sandbox / "not-a-vault"
                plain.mkdir()

                env = base_env(sandbox)
                argv = ["capture", "negative"]
                if kind == "--vault-unregistered":
                    argv = ["--vault", "alpha", *argv]
                elif kind == "--vault-notavault":
                    argv = ["--vault", str(plain), *argv]
                elif kind == "home-unmarked":
                    env["PLAINKEEP_HOME"] = str(plain)
                elif kind == "home-missing":
                    env["PLAINKEEP_HOME"] = str(sandbox / "gone")
                elif kind == "home-empty":
                    env["PLAINKEEP_HOME"] = ""

                before = snapshot(sandbox)
                # cwd is inside B: the walk-up candidate AND the registry default both point there,
                # so a fall-through has somewhere real to land.
                r = invoke(path, argv, cwd=b, env=env)
                created = new_files(sandbox, before)

                check(f"[{path}] {label}: refuses with EXIT_USAGE (2)", r.returncode == EXIT_USAGE,
                      f"rc={r.returncode} {r.stdout}{r.stderr}")
                check(f"[{path}] {label}: ZERO new files ANYWHERE in the sandbox", not created,
                      f"created: {sorted(created)}")


# --------------------------------------------------------------------------------------------
# C. Nothing selected a root at all — the fresh install, the agent shell, the cron job.
# --------------------------------------------------------------------------------------------
def case_unset() -> None:
    for path in PATHS:
        if path != "floor" and not core_live():
            continue
        with tempfile.TemporaryDirectory() as td:
            sandbox = Path(os.path.realpath(td))
            (sandbox / "home").mkdir()
            nowhere = sandbox / "nowhere"
            nowhere.mkdir()
            env = base_env(sandbox)          # no PLAINKEEP_HOME, no registry, cwd outside any vault
            before = snapshot(sandbox)
            r = invoke(path, ["capture", "unset"], cwd=nowhere, env=env)
            created = new_files(sandbox, before)
            out = r.stdout + r.stderr
            check(f"[{path}] nothing selected a root: refuses with EXIT_USAGE (2)",
                  r.returncode == EXIT_USAGE, f"rc={r.returncode} {out}")
            check(f"[{path}] nothing selected a root: ZERO files created", not created,
                  f"created: {sorted(created)}")
            check(f"[{path}] the refusal lists ALL FOUR mechanisms and what each one saw",
                  all(m in out for m in ("--vault", "PLAINKEEP_HOME",
                                         "marker walk-up", "registry default")), out)

    # A SANITIZED environment — the launchd/cron shape. It is a separate case rather than a variation
    # because "it works in my shell" is exactly the evidence that does not carry to 2am.
    #
    # PATH and HOME, and nothing else. HOME is what launchd gives a user agent, and it is also the
    # only way to make this hermetic — which is a FINDING, not a convenience: with HOME genuinely
    # absent, `vaultreg.config_dir()` falls back to `Path.home()`, which reads the PASSWD DATABASE and
    # finds the operator's real registry. Written the naive way (PATH only), this case dispatched
    # against the developer's own registered default and captured a note into their real vault while
    # asserting a refusal. That is defensible product behaviour — a cron job with no HOME still finds
    # the user's vaults — but it means an "empty environment" is never as empty as it looks, and it is
    # recorded in the Task 1b report.
    for label, extra in (("no registry at all", {}),
                         ("a registry with no default", {"seed_registry": True})):
        with tempfile.TemporaryDirectory() as td:
            sandbox = Path(os.path.realpath(td))
            (sandbox / "home").mkdir()
            nowhere = sandbox / "nowhere"
            nowhere.mkdir()
            if extra.get("seed_registry"):
                cfg = sandbox / "home" / ".config" / "plainkeep"
                cfg.mkdir(parents=True)
                (cfg / "registry.json").write_text(
                    json.dumps({"schema": "plainkeep.registry/1", "default": None, "vaults": []}),
                    encoding="utf-8")
            env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                   "HOME": str(sandbox / "home"), "PLAINKEEP_CORE": "off"}
            before = snapshot(sandbox)
            r = _run([str(REPO / "plainkeep"), "capture", "cron"], nowhere, env)
            check(f"[sanitized env · {label}] a launchd/cron-shaped launch refuses (2), never guesses",
                  r.returncode == EXIT_USAGE, f"rc={r.returncode} {r.stdout}{r.stderr}")
            check(f"[sanitized env · {label}] and creates nothing",
                  not new_files(sandbox, before), str(sorted(new_files(sandbox, before))))


# --------------------------------------------------------------------------------------------
# C2. `$PWD` NO LONGER EXISTS — a worktree removed underneath a long-lived shell, a `git clean`,
# a temp dir the agent that made it deleted. The cwd is read before the chain runs, so before this
# case it crashed `os.getcwd()` with a FileNotFoundError traceback and exit 1 — a code that is off
# the frozen protocol, reached even when the operator DID say which vault they meant, and one the
# core inherits as-is (the trace is already on the shared stderr, so main.ts's "an enforcement
# binary must never reach the shell as a stack trace" guard never sees it).
#
# A deleted cwd is a mechanism that SAW NOTHING, not an unexpected condition: steps 1, 2 and 4 must
# still get to answer, and only step 3 is unavailable.
# --------------------------------------------------------------------------------------------
def case_deleted_cwd() -> None:
    with tempfile.TemporaryDirectory() as td:
        sandbox = Path(os.path.realpath(td))
        (sandbox / "home").mkdir()
        a = sandbox / "vault-a"
        make_vault(a)
        register(sandbox, a, "alpha", default=True)
        env = base_env(sandbox)

        def in_deleted_cwd(argv: list[str], extra: dict) -> subprocess.CompletedProcess:
            """Run `argv` from a directory that is unlinked between the chdir and the exec. `cwd=` on
            a deleted path is rejected by posix_spawn itself, so the deletion has to happen inside
            the child — hence the shell wrapper."""
            gone = sandbox / "gone"
            gone.mkdir(exist_ok=True)
            quoted = " ".join(f"'{a}'" for a in argv)
            return subprocess.run(
                ["/bin/sh", "-c", f"cd '{gone}' && rmdir '{gone}' && exec {quoted}"],
                cwd=str(gone), env={**env, **extra}, capture_output=True, text=True)

        # The CORE binary cannot be exercised here and that is not plainkeep's to fix: the bun
        # runtime refuses to start at all in a deleted cwd ("error: The current working directory was
        # deleted…", exit 1) before any plainkeep code runs. Measured directly against
        # `.local/bin/plainkeep-core`. What IS gated is the shape a real user meets — PLAINKEEP_CORE
        # defaults to `auto`, whose liveness probe fails for the same reason and degrades to the
        # floor, so the invocation still works. `require` is asserted separately below: it refuses,
        # by contract, and must not do so with a traceback.
        for path, extra in (("floor", {"PLAINKEEP_CORE": "off"}),
                            ("auto", {"PLAINKEEP_CORE": "auto",
                                      "PLAINKEEP_CORE_BIN": str(CORE_BIN)})):
            if path == "auto" and not core_live():
                continue
            shim = [str(REPO / "plainkeep")]

            # (a) The chain can still be ANSWERED — the registry default is step 4 and a deleted cwd
            # only takes step 3 away. Before the fix this was exit 1 with a traceback.
            before = snapshot(sandbox)
            r = in_deleted_cwd([*shim, "capture", "deletedcwd"], extra)
            created = new_files(sandbox, before)
            check(f"[{path}] a deleted $PWD still reaches the registry default (step 4), not a crash",
                  r.returncode == 0, f"rc={r.returncode} {r.stdout}{r.stderr}")
            check(f"[{path}] ...and the note landed in the selected vault",
                  any(f.startswith("vault-a" + os.sep + "inbox") for f in created), str(sorted(created)))
            check(f"[{path}] ...with no traceback on stderr", "Traceback" not in r.stderr, r.stderr)

            # (b) An EXPLICIT selection is honoured too: step 1 runs before the cwd is ever needed,
            # so `--vault` must not be defeated by where the shell happens to be standing.
            r = in_deleted_cwd([*shim, "--vault", "alpha", "capture", "deletedcwdsel"], extra)
            check(f"[{path}] a deleted $PWD does not defeat an explicit --vault", r.returncode == 0,
                  f"rc={r.returncode} {r.stdout}{r.stderr}")

            # (c) And with nothing else to fall back on it REFUSES on the protocol (2), naming what
            # the walk-up saw — never exit 1, which the protocol reserves for the unexpected.
            with tempfile.TemporaryDirectory() as td2:
                bare = Path(os.path.realpath(td2))
                (bare / "home").mkdir()
                bare_env = base_env(bare)
                gone2 = bare / "gone"
                gone2.mkdir()
                r = subprocess.run(
                    ["/bin/sh", "-c", f"cd '{gone2}' && rmdir '{gone2}' && exec '{REPO}/plainkeep' "
                                      f"capture x"],
                    cwd=str(gone2), env={**bare_env, **extra}, capture_output=True, text=True)
                out = r.stdout + r.stderr
                check(f"[{path}] a deleted $PWD with nothing else set refuses with EXIT_USAGE (2)",
                      r.returncode == EXIT_USAGE, f"rc={r.returncode} {out}")
                check(f"[{path}] ...and the refusal SAYS the cwd is gone rather than crashing",
                      "no longer exists" in out and "Traceback" not in out, out)

        # PLAINKEEP_CORE=require in a deleted cwd: the core cannot start (bun), so the shim's own
        # liveness contract answers — a plainkeep message, never a Python traceback. This is the
        # pre-existing `require`-with-no-live-core path, not a discovery refusal, and it is asserted
        # so the disclosure above is measured rather than reasoned.
        if core_live():
            gone = sandbox / "gone-req"
            gone.mkdir()
            r = subprocess.run(
                ["/bin/sh", "-c", f"cd '{gone}' && rmdir '{gone}' && exec '{REPO}/plainkeep' "
                                  f"capture x"],
                cwd=str(gone), env={**env, "PLAINKEEP_CORE": "require",
                                    "PLAINKEEP_CORE_BIN": str(CORE_BIN)},
                capture_output=True, text=True)
            out = r.stdout + r.stderr
            check("[require] a deleted $PWD reaches the shim's liveness refusal, not a traceback",
                  "no live core binary" in out and "Traceback" not in out, f"rc={r.returncode} {out}")


# --------------------------------------------------------------------------------------------
# D. The WALK-UP rule, which is the subtle one: the FIRST marker found going up DECIDES.
# --------------------------------------------------------------------------------------------
def case_walkup_first_marker_decides() -> None:
    with tempfile.TemporaryDirectory() as td:
        sandbox = Path(os.path.realpath(td))
        (sandbox / "home").mkdir()
        outer = sandbox / "outer"
        make_vault(outer)
        register(sandbox, outer, "outervault", default=True)

        # A nested, UNREGISTERED vault inside the registered one. Walking up from `inner/sub` finds
        # inner's marker FIRST. It must refuse THERE — not skip to `outer`, and not fall through to
        # the registry default (which is also `outer`). If it did either, a broken inner vault would
        # silently hand every keystroke to the outer one, which is the worst outcome in this task.
        inner = outer / "inner"
        inner.mkdir()
        vaultfx.mark_vault(inner)
        sub = inner / "sub"
        sub.mkdir()

        env = base_env(sandbox)
        before = snapshot(sandbox)
        r = _run([str(REPO / "plainkeep"), "capture", "walkup"], sub, {**env, "PLAINKEEP_CORE": "off"})
        out = r.stdout + r.stderr
        check("walk-up: the nearest UNREGISTERED marker refuses (2) — it does not skip to an ancestor",
              r.returncode == EXIT_USAGE, f"rc={r.returncode} {out}")
        check("walk-up: ...and does not fall through to the registry default either",
              not new_files(sandbox, before), str(sorted(new_files(sandbox, before))))
        check("walk-up: the refusal names the INNER marker, so it is diagnosable",
              str(inner) in out, out)

        # A MALFORMED nearest marker refuses too, and for its own reason. Same rule, different cause:
        # "unreadable" is never the same as "absent".
        (inner / ".plainkeep" / "vault.json").write_text("{not json", encoding="utf-8")
        before = snapshot(sandbox)
        r = _run([str(REPO / "plainkeep"), "capture", "walkup"], sub, {**env, "PLAINKEEP_CORE": "off"})
        check("walk-up: a MALFORMED nearest marker refuses (2), naming the file",
              r.returncode == EXIT_USAGE and "not valid JSON" in (r.stdout + r.stderr),
              f"rc={r.returncode} {r.stdout}{r.stderr}")
        check("walk-up: ...and still writes nothing", not new_files(sandbox, before))

        # A marker that is PRESENT but is not a regular file. Same rule again, and this is the shape
        # that used to slip through: the presence test was `is_file()`, which answers False for a
        # DIRECTORY and for a DANGLING SYMLINK alike — so the walk did not see a marker at `inner`
        # at all, skipped it, and selected `outer` with exit 0. That is precisely "a broken inner
        # vault silently hands your keystrokes to the outer one", the outcome _walk_up's own
        # docstring says it exists to prevent, and it also made steps 2 and 3 disagree about what
        # counts as a marker (PLAINKEEP_HOME=inner refused on both shapes, walk-up did not).
        # A partially-restored backup, an interrupted rsync, or a `.plainkeep` whose contents were
        # symlinked elsewhere reaches it.
        for shape, build in (
                ("a DIRECTORY", lambda p: p.mkdir()),
                ("a DANGLING SYMLINK", lambda p: p.symlink_to(sandbox / "nothing-here.json"))):
            shutil.rmtree(inner / ".plainkeep", ignore_errors=True)
            (inner / ".plainkeep").mkdir(parents=True)
            build(inner / ".plainkeep" / "vault.json")
            before = snapshot(sandbox)
            r = _run([str(REPO / "plainkeep"), "capture", "walkup"], sub,
                     {**env, "PLAINKEEP_CORE": "off"})
            out = r.stdout + r.stderr
            check(f"walk-up: a marker that is {shape} refuses (2) — it is NOT invisible",
                  r.returncode == EXIT_USAGE, f"rc={r.returncode} {out}")
            check(f"walk-up: ...naming the INNER marker, so the outer vault was never selected",
                  str(inner) in out and "not a regular file" in out, out)
            check(f"walk-up: ...and writes nothing", not new_files(sandbox, before),
                  str(sorted(new_files(sandbox, before))))
            # Step 2 and step 3 must agree about what "a marker" is — they did not before.
            r2 = _run([str(REPO / "plainkeep"), "capture", "walkup"], sandbox,
                      {**env, "PLAINKEEP_CORE": "off", "PLAINKEEP_HOME": str(inner)})
            check(f"walk-up and PLAINKEEP_HOME agree that {shape} is a broken marker, not an absent one",
                  r2.returncode == r.returncode, f"walkup={r.returncode} home={r2.returncode}")

        # Registered and well-formed: the SAME cwd now resolves, so the two refusals above are about
        # registration and validity — not about walk-up being broken.
        shutil.rmtree(inner / ".plainkeep")
        vaultfx.mark_vault(inner)
        os.symlink(REPO / "bin", inner / "bin")
        register(sandbox, inner, "innervault")
        before = snapshot(sandbox)
        r = _run([str(REPO / "plainkeep"), "capture", "walkupok"], sub, {**env, "PLAINKEEP_CORE": "off"})
        created = new_files(sandbox, before)
        check("walk-up: a REGISTERED nearest marker selects that vault", r.returncode == 0,
              f"rc={r.returncode} {r.stdout}{r.stderr}")
        check("walk-up: ...and the note landed in the INNER vault, not the outer one",
              any(f.startswith(os.path.join("outer", "inner", "inbox")) for f in created)
              and not any(f.startswith("outer" + os.sep + "inbox") for f in created),
              str(sorted(created)))


# --------------------------------------------------------------------------------------------
# E. A MOVED vault: identified by marker id from inside; a stale registry entry fails LOUDLY.
# --------------------------------------------------------------------------------------------
def case_moved_vault() -> None:
    with tempfile.TemporaryDirectory() as td:
        sandbox = Path(os.path.realpath(td))
        (sandbox / "home").mkdir()
        here, there = sandbox / "here", sandbox / "there"
        make_vault(here)
        register(sandbox, here, "movable", default=True)
        shutil.move(str(here), str(there))

        env = base_env(sandbox)
        # From INSIDE the moved vault: the marker is found by walk-up, its id is registered, but the
        # registry says it lives somewhere else. Substituting the registered path would act on notes
        # that are not there; rescanning the disk to find the "real" one is the guess ADR-014 forbids.
        before = snapshot(sandbox)
        r = _run([str(REPO / "plainkeep"), "capture", "moved"], there, {**env, "PLAINKEEP_CORE": "off"})
        out = r.stdout + r.stderr
        check("moved vault: a STALE registry entry refuses (2) rather than substituting or rescanning",
              r.returncode == EXIT_USAGE, f"rc={r.returncode} {out}")
        check("moved vault: the refusal identifies it by MARKER ID and names rebind as the fix",
              "rebind" in out and "movable" in out, out)
        check("moved vault: nothing was written", not new_files(sandbox, before))

        # ...and the registry DEFAULT, which still points at the old path, refuses too rather than
        # quietly resolving to a directory that no longer exists.
        before = snapshot(sandbox)
        r = _run([str(REPO / "plainkeep"), "capture", "moved"], sandbox, {**env, "PLAINKEEP_CORE": "off"})
        check("moved vault: the stale registry DEFAULT refuses as well",
              r.returncode == EXIT_USAGE, f"rc={r.returncode} {r.stdout}{r.stderr}")
        check("moved vault: still nothing written", not new_files(sandbox, before))

        # An explicit rebind is the fix, and it works.
        r = subprocess.run([sys.executable, str(REPO / "bin" / "vault" / "run.py"),
                            "rebind", "movable", str(there), "--yes"],
                           capture_output=True, text=True,
                           env={**os.environ, "PLAINKEEP_HOME": str(there),
                                "PLAINKEEP_CONFIG_HOME": str(sandbox / "config")})
        before = snapshot(sandbox)
        r2 = _run([str(REPO / "plainkeep"), "capture", "rebound"], there, {**env, "PLAINKEEP_CORE": "off"})
        created = new_files(sandbox, before)
        check("moved vault: after an explicit rebind the same invocation succeeds",
              r.returncode == 0 and r2.returncode == 0, r.stdout + r.stderr + r2.stdout + r2.stderr)
        check("moved vault: ...into the NEW location", any(f.startswith("there" + os.sep) for f in created),
              str(sorted(created)))


# --------------------------------------------------------------------------------------------
# F. Registering N vaults must not widen the wall to N.
#
# The wall's vault segment is one root; this is the assertion that "registered" does not mean
# "authorized". It is checked at the wall itself (a write into a SIBLING registered vault, which the
# selected vault's own verb has no business making) rather than only through a verb, because a verb
# would never compute such a path on its own.
# --------------------------------------------------------------------------------------------
def case_many_vaults_one_wall() -> None:
    with tempfile.TemporaryDirectory() as td:
        sandbox = Path(os.path.realpath(td))
        (sandbox / "home").mkdir()
        roots = []
        for i in range(4):
            v = sandbox / f"v{i}"
            make_vault(v)
            register(sandbox, v, f"v{i}", default=(i == 0))
            roots.append(v)

        targets = [str(v / "inbox" / "x.md") for v in roots]
        probe = (
            "import json, sys\n"
            "sys.path.insert(0, " + repr(str(REPO / "bin" / "lib")) + ")\n"
            "import guardrail as g\n"
            "targets = " + repr(targets) + "\n"
            "print(json.dumps({'roots': g.VAULT_ROOTS,\n"
            "                  'verdicts': [g.classify({'kind': 'write', 'path': p}).verdict\n"
            "                               for p in targets]}))\n"
        )
        r = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True,
                           env={**os.environ, "PLAINKEEP_HOME": str(roots[1]),
                                "PLAINKEEP_ROOTS_HOME": str(sandbox / "home"),
                                "PLAINKEEP_CONFIG_HOME": str(sandbox / "config")})
        data = json.loads(r.stdout) if r.returncode == 0 else {"roots": [], "verdicts": []}
        check("4 registered vaults: the wall carries ONE root (plus its realpath), not four",
              set(data["roots"]) <= {str(roots[1]), os.path.realpath(roots[1])}
              and len(data["roots"]) >= 1, r.stdout + r.stderr)
        check("4 registered vaults: only the SELECTED one is writable; the other three are DENY",
              data["verdicts"] == ["deny", "allow", "deny", "deny"], str(data["verdicts"]))


# --------------------------------------------------------------------------------------------
# G. A policy-denied LOCATION is a different refusal (5), decided before the marker is read.
# --------------------------------------------------------------------------------------------
def case_policy_denied_location() -> None:
    with tempfile.TemporaryDirectory() as td:
        sandbox = Path(os.path.realpath(td))
        (sandbox / "home").mkdir()
        # A perfectly well-formed, registered vault — in a place a vault may not be.
        synced = sandbox / "home" / "Library" / "Mobile Documents" / "vault"
        make_vault(synced)
        env = base_env(sandbox, PLAINKEEP_HOME=synced)
        before = snapshot(sandbox)
        r = _run([str(REPO / "plainkeep"), "capture", "icloud"], sandbox,
                 {**env, "PLAINKEEP_CORE": "off"})
        check("a vault inside a walled-off tree refuses with EXIT_DENY (5), not usage (2)",
              r.returncode == EXIT_DENY, f"rc={r.returncode} {r.stdout}{r.stderr}")
        check("...and writes nothing", not new_files(sandbox, before),
              str(sorted(new_files(sandbox, before))))

        dropbox = sandbox / "home" / "Dropbox" / "vault"
        make_vault(dropbox)
        env = base_env(sandbox, PLAINKEEP_HOME=dropbox)
        before = snapshot(sandbox)
        r = _run([str(REPO / "plainkeep"), "capture", "dropbox"], sandbox,
                 {**env, "PLAINKEEP_CORE": "off"})
        check("a vault inside a cloud-sync tree refuses with EXIT_DENY (5)",
              r.returncode == EXIT_DENY, f"rc={r.returncode} {r.stdout}{r.stderr}")
        check("...and writes nothing either", not new_files(sandbox, before))

        # A policy DENY is the strictest code in the protocol, so it has to carry a way to act on
        # it. It carried none: `_policy_verdict`'s VaultError had no hint at all.
        out = r.stdout + r.stderr
        check("a policy refusal says what to DO about it, not only what is wrong",
              "rebind" in out or "move the vault" in out, out)

        # THE FALSE POSITIVES. The markers were matched as bare SUBSTRINGS anywhere in the path, so
        # an ordinary directory whose name merely CONTAINS one was denied — with exit 5, the
        # strictest code there is, and a stated reason that is simply untrue. None of these is in a
        # sync tree or a walled-off tree. It failed closed, so it was never a safety hole; it was a
        # deny an operator could not argue with and could not act on. Selection has a much lower
        # tolerance for this than the write path does: a write can be re-pathed, a vault cannot.
        #
        # Asserted at the layer the finding names — SELECTION, i.e. `vaultroot.py --select` — for
        # every shape, and then end-to-end for the ones the guardrail's separate WRITE wall does not
        # also reject. That split is not a dodge, it is the residue: guardrail's `_walled` keeps the
        # substring semantics (its 59 recorded verdicts were taken against them, and changing them
        # is not this fix), so a path containing "icloud" is now selectable but still not writable.
        # Recorded as a SUITE-NOTE rather than quietly asserted away.
        for name in ("my.sync-notes", "Pictures-notes", "not-iCloudy"):
            v = sandbox / "home" / "notes" / name
            make_vault(v)
            env = base_env(sandbox, PLAINKEEP_HOME=v)
            sel = _run([sys.executable, str(REPO / "bin" / "lib" / "vaultroot.py"), "--select"],
                       sandbox, env)
            check(f"SELECTION accepts a vault at ~/notes/{name} — the marker is a substring there, "
                  f"not a path component", sel.returncode == 0,
                  f"rc={sel.returncode} {sel.stdout}{sel.stderr}")

            if "cloud" in name.lower():
                continue          # still walled by guardrail's write wall — see the note above
            before = snapshot(sandbox)
            r = _run([str(REPO / "plainkeep"), "capture", "falsepos"], sandbox,
                     {**env, "PLAINKEEP_CORE": "off"})
            check(f"...and ~/notes/{name} is usable end to end, not denied with exit 5",
                  r.returncode == 0, f"rc={r.returncode} {r.stdout}{r.stderr}")
            check(f"...and ~/notes/{name} actually got the note",
                  any(f.startswith(os.path.join("home", "notes", name, "inbox"))
                      for f in new_files(sandbox, before)),
                  str(sorted(new_files(sandbox, before))))

        # Two more shapes that must stay selectable, and they are the ones that pin the BOUNDARIES of
        # the two matchers rather than their middles: `Pictures-notes` is `$HOME/Pictures` plus
        # characters (the anchored prefix must stop at a path boundary), `Picturesque` is a component
        # that merely starts with the anchor's basename.
        for rel in (("home", "Pictures-notes"), ("home", "Picturesque", "notes")):
            v = sandbox.joinpath(*rel)
            make_vault(v)
            sel = _run([sys.executable, str(REPO / "bin" / "lib" / "vaultroot.py"), "--select"],
                       sandbox, base_env(sandbox, PLAINKEEP_HOME=v))
            check(f"SELECTION accepts ~/{os.path.join(*rel[1:])} — an anchored marker stops at a "
                  f"path boundary", sel.returncode == 0, f"rc={sel.returncode} {sel.stdout}{sel.stderr}")

        # THE DOCUMENTED FALSE POSITIVES — the price of the r3 widening, asserted rather than
        # discovered later. The component matcher accepts a component that BEGINS with a marker plus
        # a separator, because that is how the real macOS sync mounts are spelled
        # (`OneDrive-Personal`, `Dropbox (Team)`, `Dropbox.nosync`). These three are indistinguishable
        # from those by spelling, so they are refused too. They are pinned HERE, in the suite, with
        # the exit code and the remediation the operator gets — a trade that is only recorded in a
        # comment is a trade that gets silently reverted by the next person who trips over it.
        for name, why in (("dropbox-export", "'Dropbox' + '-'"),
                          ("OneDrive-old", "'OneDrive' + '-'"),
                          ("icloud-archive", "'iCloud' + '-'")):
            v = sandbox / "home" / "notes" / name
            make_vault(v)
            env = base_env(sandbox, PLAINKEEP_HOME=v)
            before = snapshot(sandbox)
            r = _run([str(REPO / "plainkeep"), "capture", "trade"], sandbox,
                     {**env, "PLAINKEEP_CORE": "off"})
            out = r.stdout + r.stderr
            check(f"ACCEPTED FALSE POSITIVE: ~/notes/{name} is refused (5) — {why} cannot be told "
                  f"from a real sync mount", r.returncode == EXIT_DENY,
                  f"rc={r.returncode} {out}")
            check(f"...and the refusal for ~/notes/{name} is ACTIONABLE (vault rebind), which is "
                  f"what makes the trade payable", "vault rebind" in out, out)
            check(f"...and ~/notes/{name} writes nothing", not new_files(sandbox, before),
                  str(sorted(new_files(sandbox, before))))

        # ...and the true positives stay denied, on the COMPONENT that really is a sync/walled tree.
        # Without this pair the case above would also pass against a policy that denies nothing.
        #
        # The last five are the r3 additions and they are the whole point of the widening: every one
        # of them SELECTED cleanly under the equality-only matcher, which is a git tree handed to a
        # live sync client. `~/Library/CloudStorage/<Provider>-<Account>` is THE macOS mount point
        # for OneDrive and Google Drive since Ventura, and `Dropbox (Team)` / `Dropbox Personal` /
        # `Dropbox.nosync` are Dropbox's own folder names. A suite that could not see this class was
        # the actual defect the r2 wave shipped.
        for rel, why in ((("home", "Dropbox", "notes", "vault"), "a Dropbox component"),
                         (("home", "x", "Syncthing", "vault"), "a Syncthing component"),
                         (("home", "x", ".sync", "vault"), "a literal .sync component"),
                         (("home", "x", "Google Drive", "vault"), "a Google Drive component"),
                         (("home", "iCloud Drive", "vault"), "the $HOME-anchored iCloud Drive"),
                         (("home", "Pictures", "vault"), "the $HOME-anchored Pictures"),
                         (("home", "Library", "Mobile Documents", "com~apple~CloudDocs", "vault"),
                          "the nested Mobile Documents component"),
                         (("home", "Library", "CloudStorage", "OneDrive-Personal", "vault"),
                          "OneDrive's REAL Ventura+ mount point"),
                         (("home", "Library", "CloudStorage", "GoogleDrive-me@gmail.com", "vault"),
                          "Google Drive's REAL Ventura+ mount point"),
                         (("home", "Dropbox (Acme Inc)", "vault"),
                          "Dropbox Business's own folder name"),
                         (("home", "Dropbox Personal", "vault"),
                          "Dropbox's combined-account folder name"),
                         (("home", "Dropbox.nosync", "vault"), "Dropbox's .nosync spelling")):
            v = sandbox.joinpath(*rel)
            make_vault(v)
            env = base_env(sandbox, PLAINKEEP_HOME=v)
            before = snapshot(sandbox)
            r = _run([str(REPO / "plainkeep"), "capture", "truepos"], sandbox,
                     {**env, "PLAINKEEP_CORE": "off"})
            check(f"still denied (5): {why}", r.returncode == EXIT_DENY,
                  f"rc={r.returncode} {r.stdout}{r.stderr}")
            check(f"...and writes nothing: {why}", not new_files(sandbox, before),
                  str(sorted(new_files(sandbox, before))))


# --------------------------------------------------------------------------------------------
# G2. The $HOME-ANCHORED markers must survive a symlinked $HOME.
#
# `_under_prefix` compares the anchored markers against `vaultreg.canonical(root)`. If $HOME itself
# is a symlink — a network-mounted or relocated home, and every macOS temp dir — the two live in
# different spellings of one directory and the comparison silently answers "no". Every $HOME-anchored
# marker then stops existing, which is fail-OPEN on exactly the strictest rule in the file. The bare
# component markers are unaffected, which is why the equality half of this case is what proves the
# anchored half is really being exercised.
# --------------------------------------------------------------------------------------------
def case_anchored_markers_under_symlinked_home() -> None:
    with tempfile.TemporaryDirectory() as td:
        # NOT realpath'ed, deliberately: on macOS $TMPDIR is /var/… whose realpath is /private/var/…,
        # so this is a genuinely symlinked $HOME rather than a simulated one.
        raw = Path(td)
        if os.path.realpath(raw) == str(raw):        # a canonical $TMPDIR (most Linux): build one
            (raw / "real").mkdir()
            os.symlink(raw / "real", raw / "link")
            sandbox = raw / "link"
        else:
            sandbox = raw
        (sandbox / "home").mkdir()
        check("fixture: $HOME really is non-canonical, so this case tests what it says",
              os.path.realpath(sandbox / "home") != str(sandbox / "home"),
              f"{sandbox / 'home'} -> {os.path.realpath(sandbox / 'home')}")

        for rel, why in ((("home", "iCloud Drive", "vault"), "$HOME-anchored iCloud Drive"),
                         (("home", "Pictures", "vault"), "$HOME-anchored Pictures"),
                         (("home", "Library", "CloudStorage", "OneDrive-Personal", "vault"),
                          "$HOME-anchored Library/CloudStorage"),
                         (("home", "Dropbox", "vault"), "a BARE component (the control)")):
            v = sandbox.joinpath(*rel)
            make_vault(v)
            env = base_env(sandbox, PLAINKEEP_HOME=v)

            # SELECTION is asserted directly, and it has to be: guardrail's separate substring write
            # wall denies a write into `iCloud Drive` / `Pictures` anyway, so an end-to-end exit 5
            # would be satisfied by the WRONG layer and this case would pass against the bug it
            # exists for. `--select` is the layer the finding names.
            sel = _run([sys.executable, str(REPO / "bin" / "lib" / "vaultroot.py"), "--select"],
                       sandbox, env)
            check(f"a symlinked $HOME does not disable SELECTION's wall: {why}",
                  sel.returncode == EXIT_DENY, f"rc={sel.returncode} {sel.stdout}{sel.stderr}")

            before = snapshot(sandbox)
            r = _run([str(REPO / "plainkeep"), "capture", "symhome"], sandbox,
                     {**env, "PLAINKEEP_CORE": "off"})
            check(f"a symlinked $HOME does not disable the wall end to end: {why}",
                  r.returncode == EXIT_DENY, f"rc={r.returncode} {r.stdout}{r.stderr}")
            # Not even the audit log: a refusal at SELECTION happens before the vault is touched at
            # all, where a refusal at the write wall has already opened `.logs/plainkeep.log`.
            check(f"...and writes nothing under a symlinked $HOME: {why}",
                  not new_files(sandbox, before), str(sorted(new_files(sandbox, before))))


# --------------------------------------------------------------------------------------------
# H. The selector is PRE-VERB ONLY, and the two dispatchers agree byte-for-byte.
# --------------------------------------------------------------------------------------------
def case_selector_position_and_parity() -> None:
    with tempfile.TemporaryDirectory() as td:
        sandbox = Path(os.path.realpath(td))
        (sandbox / "home").mkdir()
        a = sandbox / "vault-a"
        make_vault(a)
        register(sandbox, a, "alpha", default=True)
        env = base_env(sandbox)

        # POST-verb `--vault` is the VERB's argument, not a selector. `capture --vault alpha x`
        # captures the literal words, and the note proves it: if the dispatcher had eaten the flag
        # the text would be missing them.
        before = snapshot(sandbox)
        r = _run([str(REPO / "plainkeep"), "capture", "--vault", "alpha", "postverb"], a,
                 {**env, "PLAINKEEP_CORE": "off"})
        created = new_files(sandbox, before)
        notes = [f for f in created if "inbox" in f]
        body = "\n".join((sandbox / n).read_text(encoding="utf-8", errors="replace") for n in notes)
        check("`capture --vault alpha x` is the VERB's argument — the words survive into the note",
              r.returncode == 0 and "--vault" in body and "alpha" in body, body[:400] or str(created))

        # PRE-verb, the two dispatchers must be indistinguishable. Compared on a REFUSAL, which is
        # where they could most easily diverge: the message is the discovery module's, and both sides
        # run that same module rather than a port of it.
        if core_live():
            bad = base_env(sandbox)
            f = _run([str(REPO / "plainkeep"), "--vault", "nosuch", "capture", "x"], a,
                     {**bad, "PLAINKEEP_CORE": "off"})
            c = _run([str(REPO / "plainkeep"), "--vault", "nosuch", "capture", "x"], a,
                     {**bad, "PLAINKEEP_CORE": "require", "PLAINKEEP_CORE_BIN": str(CORE_BIN)})
            d = _run([str(CORE_BIN), "--vault", "nosuch", "capture", "x"], a, bad)
            check("floor and core refuse an unknown --vault with the SAME code and the SAME bytes",
                  (f.returncode, f.stdout, f.stderr) == (c.returncode, c.stdout, c.stderr)
                  == (d.returncode, d.stdout, d.stderr),
                  f"floor={f.returncode}/{f.stderr!r} core={c.returncode}/{c.stderr!r} "
                  f"direct={d.returncode}/{d.stderr!r}")

        # `--vault` with no value is a usage error, not a silent selection of the verb name.
        r = _run([str(REPO / "plainkeep"), "--vault"], a, {**env, "PLAINKEEP_CORE": "off"})
        check("`--vault` with no value is a usage error (2)", r.returncode == EXIT_USAGE,
              f"rc={r.returncode} {r.stdout}{r.stderr}")


# --------------------------------------------------------------------------------------------
# I. The dispatchers export the CANONICAL path and the vault id to the child.
# --------------------------------------------------------------------------------------------
def case_canonical_export() -> None:
    with tempfile.TemporaryDirectory() as td:
        sandbox = Path(os.path.realpath(td))
        (sandbox / "home").mkdir()
        real = sandbox / "vault-real"
        vid = make_vault(real)
        register(sandbox, real, "alpha", default=True)
        alias = sandbox / "alias"
        os.symlink(real, alias)

        for path in PATHS:
            if path != "floor" and not core_live():
                continue
            env = base_env(sandbox, PLAINKEEP_HOME=alias)
            r = invoke(path, ["vault", "status", "--json"], cwd=sandbox, env=env)
            try:
                data = json.loads(r.stdout)["data"]
            except Exception:
                check(f"[{path}] canonical export: status is readable", False, r.stdout + r.stderr)
                continue
            # `home_env` is the RAW variable the verb was handed, not a canonicalized reading of
            # it. Asserting on `active_root` alone is not enough and this is measured, not argued: a
            # deliberate mutation that handed back the caller's spelling passed the whole suite,
            # because `vault status` canonicalizes before reporting `active_root`.
            check(f"[{path}] the caller's spelling ({alias.name}) is replaced by the canonical root",
                  data["home_env"] == str(real) and data["active_root"] == str(real),
                  f"home_env={data['home_env']} active_root={data['active_root']} want={real}")
            check(f"[{path}] PLAINKEEP_VAULT_ID reaches the verb", data["vault_id_env"] == vid,
                  f"{data['vault_id_env']} != {vid}")


# --------------------------------------------------------------------------------------------
# J. A registered vault that does NOT carry a copy of the engine.
#
# Every other fixture in this file is handed the engine by `make_vault` (it symlinks `bin/` and the
# shim in), which is what makes the two-vault identity test above dispatchable at all — and it is
# also what hid this: an ORDINARY second vault, which is what every real user's second vault looks
# like, is a directory with a marker and nothing else.
#
# Phase 1 still runs the engine from INSIDE the selected root (the floor spawns
# `$PLAINKEEP_HOME/bin/lib/guardrail.py`; the core's resolver looks under the same root), so such a
# vault cannot be dispatched for. That constraint is Phase 2 Task 2's to remove. What is gated HERE
# is the DIAGNOSIS: before this case both dispatchers failed at the far end of the dispatch, in two
# different ways and both untruthfully — the floor let CPython say "can't open file
# '<vault>/bin/lib/guardrail.py'" (exit 2, no plainkeep in the message), and the core's resolver,
# finding no verb under the root, said "unknown verb 'capture'" (exit 4) and sent the operator to
# `plainkeep help`, which fails identically. Neither mentioned the engine.
# --------------------------------------------------------------------------------------------
def case_vault_without_engine() -> None:
    with tempfile.TemporaryDirectory() as td:
        sandbox = Path(os.path.realpath(td))
        (sandbox / "home").mkdir()
        # A dispatchable vault (the default) and an ordinary notes vault beside it: marker, no engine.
        home_vault = sandbox / "vault-engine"
        make_vault(home_vault)
        register(sandbox, home_vault, "alpha", default=True)
        notes = sandbox / "mynotes"
        notes.mkdir()
        vaultfx.mark_vault(notes)
        register(sandbox, notes, "mynotes")

        env = base_env(sandbox)
        for argv, label in ((["--vault", "mynotes", "capture", "hello"], "capture"),
                            (["--vault", "mynotes", "help"], "help")):
            runs = {}
            for path in PATHS:
                if path != "floor" and not core_live():
                    continue
                before = snapshot(sandbox)
                r = invoke(path, argv, cwd=sandbox, env=env)
                runs[path] = r
                out = r.stdout + r.stderr
                check(f"[{path}] --vault a vault with no engine ({label}): refuses with EXIT_USAGE (2)",
                      r.returncode == EXIT_USAGE, f"rc={r.returncode} {out}")
                # The message has to be TRUE. "unknown verb 'capture'" was false (capture exists) and
                # the CPython traceback was not a plainkeep refusal at all.
                check(f"[{path}] ...naming the engine as the reason, not the verb ({label})",
                      "does not carry the plainkeep engine" in out and "unknown verb" not in out, out)
                check(f"[{path}] ...naming the vault and the missing file ({label})",
                      "mynotes" in out and str(notes / "bin" / "lib" / "guardrail.py") in out, out)
                check(f"[{path}] ...and writes nothing ({label})", not new_files(sandbox, before),
                      str(sorted(new_files(sandbox, before))))
            # The brief's "both dispatchers must agree byte-for-byte" — this invocation shape was
            # the one place they disagreed on BOTH text and exit code.
            if len(runs) == len(PATHS):
                sigs = {p: (r.returncode, r.stdout, r.stderr) for p, r in runs.items()}
                check(f"floor, core and direct refuse a no-engine vault identically ({label})",
                      len(set(sigs.values())) == 1,
                      "; ".join(f"{p}={s[0]}/{s[2]!r}" for p, s in sigs.items()))

        # The constraint is REAL, not an artefact of the probe: the same vault, once it carries the
        # engine, dispatches. Without this the case above would also pass against a build that
        # refused every --vault outright.
        os.symlink(REPO / "bin", notes / "bin")
        shutil.copy2(REPO / "plainkeep", notes / "plainkeep")
        os.chmod(notes / "plainkeep", 0o755)
        before = snapshot(sandbox)
        r = _run([str(REPO / "plainkeep"), "--vault", "mynotes", "capture", "nowitworks"], sandbox,
                 {**env, "PLAINKEEP_CORE": "off"})
        created = new_files(sandbox, before)
        check("...and once that vault DOES carry the engine, the same invocation succeeds into it",
              r.returncode == 0 and any(f.startswith("mynotes" + os.sep + "inbox") for f in created),
              f"rc={r.returncode} {r.stdout}{r.stderr} {sorted(created)}")

        # THE PARTIAL ENGINE — a vault carrying `bin/lib` and no verb directory. Total absence (all
        # of the above) was the only shape gated before, and the gap was not academic: this is the
        # shape `cli/src/core/vault-fixture.ts` builds, so it was the sanctioned bun fixture.
        #
        # It is the shape that breaks the byte-for-byte claim, because the two resolvers disagree
        # about where verbs live. `resolver.py`'s ENGINE_BIN is `__file__`-relative, so it follows
        # the `bin/lib` symlink back into the real checkout and finds EVERY verb; `resolver.ts`'s
        # `engineBin()` is data-relative and finds none. A one-file probe certified this root as
        # dispatchable and then the floor captured a note at exit 0 while the core refused at exit 4
        # — one argv, two dispatchers, different answers to "did a write happen", which is the single
        # thing `--select` exists to make impossible.
        partial = sandbox / "libonly"
        (partial / "bin").mkdir(parents=True)
        os.symlink(REPO / "bin" / "lib", partial / "bin" / "lib")
        shutil.copy2(REPO / "plainkeep", partial / "plainkeep")
        os.chmod(partial / "plainkeep", 0o755)
        vaultfx.mark_vault(partial)
        register(sandbox, partial, "libonly")

        runs = {}
        for path in PATHS:
            if path != "floor" and not core_live():
                continue
            before = snapshot(sandbox)
            r = invoke(path, ["--vault", "libonly", "capture", "partial"], cwd=sandbox, env=env)
            runs[path] = r
            out = r.stdout + r.stderr
            check(f"[{path}] a PARTIAL engine (bin/lib, no verb dir) refuses with EXIT_USAGE (2)",
                  r.returncode == EXIT_USAGE, f"rc={r.returncode} {out}")
            check(f"[{path}] ...naming the missing VERB DIRECTORY, not the gate file that is present",
                  "carries no verb directory" in out and "does not carry the plainkeep engine" in out,
                  out)
            # The half that actually caught the divergence: the floor USED to write here.
            check(f"[{path}] ...and a partial engine writes nothing", not new_files(sandbox, before),
                  str(sorted(new_files(sandbox, before))))
        if len(runs) == len(PATHS):
            sigs = {p: (r.returncode, r.stdout, r.stderr) for p, r in runs.items()}
            check("floor, core and direct refuse a PARTIAL engine identically",
                  len(set(sigs.values())) == 1,
                  "; ".join(f"{p}={s[0]}/{s[2]!r}" for p, s in sigs.items()))

        # ...and the probe still says YES to a root that really can dispatch — one verb is enough,
        # which is what keeps the widened probe from being "refuse anything unusual".
        os.symlink(REPO / "bin" / "capture", partial / "bin" / "capture")
        before = snapshot(sandbox)
        r = _run([str(REPO / "plainkeep"), "--vault", "libonly", "capture", "nowdispatchable"],
                 sandbox, {**env, "PLAINKEEP_CORE": "off"})
        created = new_files(sandbox, before)
        check("...and ONE verb directory is enough to make that same root dispatchable again",
              r.returncode == 0 and any(f.startswith("libonly" + os.sep + "inbox") for f in created),
              f"rc={r.returncode} {r.stdout}{r.stderr} {sorted(created)}")


# --------------------------------------------------------------------------------------------
# K. `vault status` reports the REAL mechanism (brief scope item 6).
#
# The verb re-runs `vaultroot.discover()` from inside the spawned process, where the dispatcher has
# already exported PLAINKEEP_HOME — so chain step 2 always won and `selected_by` was the constant
# string "PLAINKEEP_HOME" for every invocation that can exist. `saw` could never carry the walk-up
# or registry-default lines, `selection_error` could never be a chain refusal, and VaultError.saw
# had no reachable reader anywhere in production. That is the surface every other refusal in this
# task depends on for diagnosability, so this asserts each of the four mechanisms in turn: the
# suite as it stood could not tell a correct implementation from that one.
# --------------------------------------------------------------------------------------------
def case_status_reports_the_real_mechanism() -> None:
    with tempfile.TemporaryDirectory() as td:
        sandbox = Path(os.path.realpath(td))
        (sandbox / "home").mkdir()
        a, b = sandbox / "vault-a", sandbox / "vault-b"
        make_vault(a)
        make_vault(b)
        register(sandbox, a, "alpha")
        register(sandbox, b, "beta", default=True)

        cases = [
            ("--vault", ["--vault", "alpha", "vault", "status", "--json"], sandbox, {}),
            ("PLAINKEEP_HOME", ["vault", "status", "--json"], sandbox, {"PLAINKEEP_HOME": str(a)}),
            ("marker walk-up from $PWD", ["vault", "status", "--json"], a, {}),
            ("registry default", ["vault", "status", "--json"], sandbox, {}),
        ]
        for want, argv, cwd, over in cases:
            for path in PATHS:
                if path != "floor" and not core_live():
                    continue
                env = base_env(sandbox, **over)
                r = invoke(path, argv, cwd=cwd, env=env)
                try:
                    data = json.loads(r.stdout)["data"]
                except Exception:
                    check(f"[{path}] status via {want}: readable JSON", False, r.stdout + r.stderr)
                    continue
                check(f"[{path}] status names {want!r} as the mechanism that actually chose",
                      data["selected_by"] == want, f"got {data['selected_by']!r}")
                # The mechanism the DISPATCHER used has to reach the verb; a status that recomputed
                # it would be answering a different question and is what this replaces.
                check(f"[{path}] ...and it came from the dispatcher, not a recomputation",
                      data.get("selected_by_source") == "dispatcher",
                      str(data.get("selected_by_source")))

        # `saw` must now be able to carry the lines that were unreachable — steps 3 and 4 are only
        # asked at all once the re-run drops the PLAINKEEP_HOME the dispatcher exported.
        env = base_env(sandbox)
        r = invoke("floor", ["vault", "status", "--json"], cwd=a, env=env)
        data = json.loads(r.stdout)["data"]
        check("status: `saw` carries the walk-up line, which no invocation could reach before",
              "marker" in (data["saw"].get("marker walk-up from $PWD") or ""), json.dumps(data["saw"]))
        check("status: would_select answers the question it names — what the chain picks with "
              "PLAINKEEP_HOME out of the way", data["would_select"] == str(a),
              f"{data['would_select']} != {a}")

        # ...and step 4's line, which needs a cwd where the walk-up finds nothing — a chain that
        # stopped at step 3 must show step 4 as `not reached`, not as an empty string. Pointed at A
        # while standing outside every vault, so the two answers genuinely differ.
        r = invoke("floor", ["vault", "status", "--json"], cwd=sandbox,
                   env=base_env(sandbox, PLAINKEEP_HOME=a))
        data = json.loads(r.stdout)["data"]
        check("status: `saw` carries the registry-default line when the chain reaches step 4",
              "beta" in (data["saw"].get("registry default") or ""), json.dumps(data["saw"]))
        check("status: ...and would_select is then the DEFAULT while active_root is still A — the "
              "difference PLAINKEEP_HOME was hiding",
              data["would_select"] == str(b) and data["active_root"] == str(a),
              f"would={data['would_select']} active={data['active_root']}")

        # A chain REFUSAL reaching `selection_error` + `saw` — the reader VaultError.saw never had.
        # cwd is an unregistered marked vault, so the re-run refuses at step 3 while the invocation
        # itself succeeds via PLAINKEEP_HOME.
        stray = sandbox / "stray"
        stray.mkdir()
        vaultfx.mark_vault(stray)
        r = invoke("floor", ["vault", "status", "--json"], cwd=stray,
                   env=base_env(sandbox, PLAINKEEP_HOME=a))
        data = json.loads(r.stdout)["data"]
        check("status: a chain refusal reaches selection_error instead of being unreportable",
              data["selection_error"] is not None and "not in the registry" in data["selection_error"],
              str(data["selection_error"]))
        check("status: ...and the refusal's own `saw` is what gets rendered",
              str(stray) in (data["saw"].get("marker walk-up from $PWD") or ""),
              json.dumps(data["saw"]))
        check("status: ...while selected_by still reports what really chose this invocation",
              data["selected_by"] == "PLAINKEEP_HOME", str(data["selected_by"]))


# --------------------------------------------------------------------------------------------
# L. A registry `path` that is not canonical must not make the chain contradict itself.
#
# `validate_registry` only ever checked that a path starts with "/", while vaultroot compared
# `entry["path"]` against the CANONICAL root. An entry spelled through a symlink — a hand edit, or a
# vault whose parent later became one — therefore got two different answers from three mechanisms:
# --vault and the registry default resolved fine (both go through validate(), which canonicalizes),
# while the walk-up from inside the same vault refused with "…but vault 'gg' is registered at
# <the link>" and a `rebind` remediation for a vault that never moved.
# --------------------------------------------------------------------------------------------
def case_noncanonical_registry_path() -> None:
    with tempfile.TemporaryDirectory() as td:
        sandbox = Path(os.path.realpath(td))
        (sandbox / "home").mkdir()
        holder = sandbox / "holder"
        holder.mkdir()
        real = holder / "realG"
        vid = make_vault(real)
        link = sandbox / "linkG"
        os.symlink(real, link)

        # Written by hand: `vault register` canonicalizes, so the only way to reach this state is a
        # hand edit or a parent that became a symlink after registration. Both happen.
        cfg = sandbox / "config"
        cfg.mkdir(parents=True, exist_ok=True)
        (cfg / "registry.json").write_text(json.dumps(
            {"schema": "plainkeep.registry/1", "default": vid,
             "vaults": [{"id": vid, "name": "gg", "path": str(link)}]}, indent=2) + "\n",
            encoding="utf-8")

        env = base_env(sandbox)
        for label, argv, cwd in (("--vault gg", ["--vault", "gg", "capture", "nc1"], sandbox),
                                 ("the registry default", ["capture", "nc2"], sandbox),
                                 ("walk-up from inside", ["capture", "nc3"], real)):
            before = snapshot(sandbox)
            r = _run([str(REPO / "plainkeep"), *argv], cwd, {**env, "PLAINKEEP_CORE": "off"})
            created = new_files(sandbox, before)
            check(f"a symlink-spelled registry path: {label} resolves the vault", r.returncode == 0,
                  f"rc={r.returncode} {r.stdout}{r.stderr}")
            check(f"...and {label} lands in the SAME canonical root as the others",
                  any(f.startswith(os.path.join("holder", "realG", "inbox")) for f in created),
                  str(sorted(created)))

        # ...and the registry's own duplicate check now sees two spellings of one vault as one path,
        # which is the fail-closed direction: an ambiguous registry refuses rather than last-wins.
        (cfg / "registry.json").write_text(json.dumps(
            {"schema": "plainkeep.registry/1", "default": None,
             "vaults": [{"id": vid, "name": "gg", "path": str(link)},
                        {"id": "11111111-1111-1111-1111-111111111111", "name": "hh",
                         "path": str(real)}]}, indent=2) + "\n", encoding="utf-8")
        r = _run([str(REPO / "plainkeep"), "--vault", "gg", "capture", "dup"], sandbox,
                 {**env, "PLAINKEEP_CORE": "off"})
        check("two spellings of one path in the registry are a DUPLICATE, not two vaults",
              r.returncode == EXIT_USAGE and "duplicate path" in (r.stdout + r.stderr),
              f"rc={r.returncode} {r.stdout}{r.stderr}")


def main() -> int:
    case_two_vault_identity()
    case_negative_twin()
    case_unset()
    case_deleted_cwd()
    case_walkup_first_marker_decides()
    case_moved_vault()
    case_many_vaults_one_wall()
    case_policy_denied_location()
    case_anchored_markers_under_symlinked_home()
    case_selector_position_and_parity()
    case_canonical_export()
    case_vault_without_engine()
    case_status_reports_the_real_mechanism()
    case_noncanonical_registry_path()

    print(f"{BOLD}Vault DISCOVERY (ADR-014 Task 1b) — {len(results)} checks{RESET}\n")
    passed = sum(1 for _, ok, _ in results if ok)
    for name, ok, detail in results:
        mark = f"{GREEN}PASS{RESET}" if ok else f"{RED}FAIL{RESET}"
        print(f"  {mark} {name:<86}" + (f" {DIM}{detail}{RESET}" if (detail and not ok) else ""))
    failed = len(results) - passed
    if not core_live():
        print(f"\nSUITE-NOTE: no live core binary at {CORE_BIN} — the `core` and `direct` invocation "
              f"paths were NOT exercised, so this run gates the bash floor only. Build it with "
              f"(cd cli && bun run build).")
    print("SUITE-NOTE: section G fixes the LOCATION policy for vault SELECTION only. guardrail.py's "
          "write wall still matches its markers as bare substrings — the semantics its 59 recorded "
          "verdicts were taken against — so a vault at a path merely CONTAINING 'icloud' can be "
          "selectable while every write into it is denied with an untrue reason. Converging the two "
          "matchers means re-recording those cases and is not this fix; it is registered in "
          "docs/followups.md.")
    print("SUITE-NOTE: section G's selection matcher accepts a path component that BEGINS with a "
          "sync marker plus a separator, not only one that equals it, because that is how the real "
          "mounts are spelled (~/Library/CloudStorage/OneDrive-Personal, 'Dropbox (Team)', "
          "'Dropbox.nosync'). Requiring equality made all of those SELECTABLE. The price is three "
          "names that are refused despite being innocent — ~/notes/dropbox-export, "
          "~/notes/OneDrive-old, ~/notes/icloud-archive — and they are asserted above rather than "
          "left to be rediscovered. Where a real sync mount and an innocent name cannot be told "
          "apart by spelling, this suite pins the REFUSAL: it is visible and carries a "
          "`vault rebind` remediation, where the miss is silent and leaves a .git inside a sync "
          "client. Section G2 pins the same wall against a symlinked $HOME.")
    print("SUITE-NOTE: the deleted-$PWD case (C2) does NOT gate the compiled core. The bun runtime "
          "refuses to start in a deleted cwd before any plainkeep code runs, so `plainkeep-core` "
          "exits 1 with bun's own message no matter what discovery does. Measured; the default "
          "PLAINKEEP_CORE=auto degrades to the floor for the same reason and IS gated.")
    print("SUITE-NOTE: a verb invoked DIRECTLY (python3 bin/<verb>/run.py) still trusts whatever "
          "PLAINKEEP_HOME it is handed — validation belongs to the dispatcher, and every product "
          "surface goes through one. Nothing here gates that escape hatch.")
    print(f"\n{BOLD}Result:{RESET} {GREEN}{passed} passed{RESET}, "
          f"{(RED if failed else DIM)}{failed} failed{RESET}, {len(results)} checks")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
