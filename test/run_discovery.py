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


def main() -> int:
    case_two_vault_identity()
    case_negative_twin()
    case_unset()
    case_walkup_first_marker_decides()
    case_moved_vault()
    case_many_vaults_one_wall()
    case_policy_denied_location()
    case_selector_position_and_parity()
    case_canonical_export()
    case_vault_without_engine()

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
    print("SUITE-NOTE: a verb invoked DIRECTLY (python3 bin/<verb>/run.py) still trusts whatever "
          "PLAINKEEP_HOME it is handed — validation belongs to the dispatcher, and every product "
          "surface goes through one. Nothing here gates that escape hatch.")
    print(f"\n{BOLD}Result:{RESET} {GREEN}{passed} passed{RESET}, "
          f"{(RED if failed else DIM)}{failed} failed{RESET}, {len(results)} checks")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
