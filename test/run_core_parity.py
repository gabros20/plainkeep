#!/usr/bin/env python3
"""
run_core_parity.py — the PERMANENT, Python-owned differential oracle proving the compiled TS core
binary reproduces the untouched Python implementations byte-for-byte (proposal §3; advisor A5/B2).

INSTALLED-ENGINE MODE (Phase 2 Task 2). Every fixture is now TWO trees, not one:

    <base>/install/engine/<version>/     the ENGINE — bin/lib, the synthetic verb dirs, the shim,
    <base>/install/engine/current ->     ...reached through the `current` symlink, so both
                                         dispatchers' symlink resolution is exercised on every case
    <base>/vault/                        the DATA root — marker, plugins/, .venv, .logs

...and the vault ALSO carries a `bin/` of its own, which is the dual-run proof this task turns in.
That `bin/` is POISONED: `<vault>/bin/lib/guardrail.py` and friends exit 5 printing
`POISONED-VAULT-ENGINE`, and every declared engine verb gets a `<vault>/bin/<verb>/run.py` that
prints the same marker. Nothing may ever resolve to it. Before this task the floor spawned
`$PLAINKEEP_HOME/bin/lib/guardrail.py` and resolver.ts derived engine bin/ as `<home>/bin`, so those
files WERE the engine; now they are inert data sitting untouched beside it, and the marker assertion
in every comparator is what keeps that true rather than incidentally true. (It is also the reason a
vault carrying `bin/capture/` cannot shadow the reserved engine verb `capture`, which the old
derivation quietly allowed.)

Phase 1 drives the RESOLVER: it loads the language-neutral case catalogs under
test/cases/core-parity/, builds a throwaway fixture per case, then for every invocation runs BOTH
sides and compares exit code + exact stdout:
  * TS side  = the built binary's `--core-resolve <verb>` / `--core-api <spec>`.
  * PY side  = `python3 <vault>/bin/lib/resolver.py <verb>` for the CLI, and a `python3 -c` probe
               importing that same resolver.py and printing the same compact JSON shapes for the APIs.

The catalog + fixture-vault + comparison machinery here is the FOUNDATION later tasks extend: Task 3
added guardrail.json + a `--core-gate` comparator; Task 4 added dispatcher.json + a `dispatch`
comparator (the WHOLE dispatch, binary vs the bash floor reached through `PLAINKEEP_CORE=off
./plainkeep`) plus the shim/root-discovery checks in _shim_checks(); Task 5 adds the completion
catalog. New catalogs drop a JSON file next to resolver.json and (if they need a new comparator)
register one in COMPARATORS — the vault builder and runner are catalog-agnostic.

Binary discovery: $PLAINKEEP_CORE_BIN else <repo>/.local/bin/plainkeep-core. Absent (or not
executable) => print a LOUD single SKIP line and exit 0, UNLESS PLAINKEEP_REQUIRE_CORE=1, in which
case the same line is an error and the suite exits 1. The discovered binary is INSTALLED INTO EACH
FIXTURE'S ENGINE and invoked from there, never in place: since Task 2 the core derives its engine
root from its own execPath, so a binary run out of `<repo>/.local/bin/` would resolve the REPO's
verbs and the differential would compare the fixture against the repository. SKIP is visible, never a silent PASS.

The same rule covers the ONE other thing this oracle declines to do by default: on macOS, the
fault-signal cells marked "crash_noise" in a catalog are skipped unless PLAINKEEP_REQUIRE_CORE=1 or
PLAINKEEP_PARITY_FAULT_SIGNALS=1 is set (Task 8 item A — see CRASH_NOISE_OPT_INS below for the trade
and its cost). They print a SKIP line each and are counted separately on the summary line.
"""
from __future__ import annotations
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path
from lib.hermetic import seal
seal()   # hermetic: an empty throwaway registry, never the developer's real vault

REPO = Path(__file__).resolve().parents[1]
PY = sys.executable
ENGINE_SRC = REPO / "bin"
# The installed-engine layout, read from the engine module rather than restated: a fixture that
# spelled `engine/<version>/current` its own way would keep passing after the real layout moved.
sys.path.insert(0, str(ENGINE_SRC / "lib"))
import enginetree  # type: ignore  # noqa: E402
VERSIONS_DIRNAME = enginetree.VERSIONS_DIRNAME
ENGINE_VERSION = (REPO / "VERSION").read_text(encoding="utf-8").strip()
# The root shim (Task 4). Its `PLAINKEEP_CORE=off` path is the BASH FLOOR — the pre-core dispatcher,
# preserved verbatim — and is the reference side of the dispatcher differential matrix.
PLAINKEEP_SRC = REPO / "plainkeep"
CATALOG_DIR = REPO / "test" / "cases" / "core-parity"
GREEN, RED, YELLOW, DIM, BOLD, RESET = (
    "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[1m", "\033[0m",
)

SKIP_LINE = "SKIP core-parity: no core binary (build with: cd cli && bun run build)"

# The fault-signal gate (Task 8, item A). An invocation marked "crash_noise": true in a catalog kills
# a child with a signal whose macOS default action is "create core image" (man 3 signal: QUIT, ILL,
# TRAP, ABRT, EMT, FPE, BUS, SEGV, SYS). Every such death makes macOS write a .ips crash report into
# ~/Library/Logs/DiagnosticReports and pop a user notification — per run, on a machine that is not CI
# — which is why those cells are opt-in HERE and unconditional on the release/CI path. This is a
# DELIBERATE coverage trade, spelled out in dispatcher.json's signal-passthrough-matrix rationale and
# in ADR-013; a gated cell prints a visible SKIP and is counted in the summary, never a silent pass.
#
# THIS FILE IS NOT THE ONLY GATE. The same two variables also gate the bun-side signal sweep in
# cli/src/core/dispatch.test.ts, which kills children with the same class of signal and, being part of
# `bun test`, runs far more often than this suite. Gating only this file left the noise in place
# (measured: 5 crash reports per `bun test`). If a third place ever kills a child with a
# create-core-image signal, it belongs behind these same variables — do not invent a third name.
CRASH_NOISE_OPT_INS = ("PLAINKEEP_REQUIRE_CORE", "PLAINKEEP_PARITY_FAULT_SIGNALS")

# How many invocations the catalogs under test/cases/core-parity/ declare in TOTAL — platform- and
# mode-independent, because it counts what is DECLARED, not what a given run chooses to execute. It is
# pinned so that deleting a case (or an invocation) reddens instead of quietly shrinking coverage; see
# the accounting invariant at the bottom of main() for why a self-consistency check cannot do that job.
# ADDING cases is expected and welcome: raise this number in the same commit that adds them.
EXPECTED_CATALOG_INVOCATIONS = 210

results: list[tuple[str, bool, str]] = []
skipped: list[tuple[str, str]] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    results.append((name, bool(cond), detail))


def skip(name: str, why: str) -> None:
    skipped.append((name, why))


# The one line run_all.py parses out of a suite's stdout. It exists because run_all reduces a suite to
# PASS/FAIL on its EXIT STATUS alone, so anything a run declines to cover — a filtered run, a gated
# cell — reached the summary as an unqualified PASS however loudly the suite's own output said
# otherwise. A note is not a failure and never changes an exit code; it makes the qualification travel
# WITH the verdict instead of scrolling past above it.
SUITE_NOTE_PREFIX = "SUITE-NOTE:"


def suite_note(text: str) -> None:
    print(f"{SUITE_NOTE_PREFIX} {text}")


def _crash_noise_opted_in() -> bool:
    return any(os.environ.get(v) == "1" for v in CRASH_NOISE_OPT_INS)


def _crash_noise_skip(inv: dict) -> str | None:
    """Why this invocation is being skipped, or None if it runs.

    Gated only on macOS: the cost being avoided is a macOS crash report plus a notification, so on
    Linux these cells are free and skipping them would trade coverage for nothing. CI is Linux AND
    sets PLAINKEEP_REQUIRE_CORE=1, so the cells run there twice over.
    """
    if not inv.get("crash_noise"):
        return None
    if sys.platform != "darwin":
        return None
    if _crash_noise_opted_in():
        return None
    return (
        "fault-signal cell — killing a child with this signal writes a macOS crash report and pops a "
        "notification. NOT RUN, and therefore NOT PASSED. Run it with "
        "PLAINKEEP_PARITY_FAULT_SIGNALS=1 (or PLAINKEEP_REQUIRE_CORE=1, the CI/release path)."
    )


# --------------------------------------------------------------------------------------------------
# Fixture vault builder (catalog-agnostic — reused by every future parity catalog)
# --------------------------------------------------------------------------------------------------

_VERB_FILES = {
    # A minimal but valid cmd.json (guardrail catalogs will supply richer sidecars via the same hook).
    "cmd.json": lambda verb: json.dumps({"verb": verb, "summary": f"parity {verb}", "risk": "read"}),
    "run.py": lambda verb: "def main(argv):\n    return 0\n",
}


def _mark_vault(root: Path) -> str:
    """Make `root` a real vault: write `.plainkeep/vault.json` and return its id.

    Required since ADR-014 Task 1b — a directory PLAINKEEP_HOME points at is not a vault unless it
    carries a marker, and an unmarked one refuses (exit 2) before the gate. Written directly rather
    than by shelling out to `plainkeep vault register` on purpose: registering would also write into
    a REGISTRY, and a fixture must never touch the developer's real one."""
    vid = str(uuid.uuid4())
    d = root / ".plainkeep"
    d.mkdir(parents=True, exist_ok=True)
    (d / "vault.json").write_text(
        json.dumps({"schema": "plainkeep.vault/1", "id": vid,
                    "created": "2026-08-02T00:00:00+00:00"}, indent=2) + "\n",
        encoding="utf-8")
    return vid


def _content(spec) -> str:
    """The text a catalog writes into cmd.json / plugins.lock.json.

    Three additive forms:
      * object/scalar  -> json.dumps(spec)                (the normal, readable form)
      * str            -> written RAW                     (malformed-JSON fixtures)
      * list of SEGMENTS -> concatenated raw text, where a segment is a str or
        {"repeat": "<s>", "times": N}.

    The segment form exists for the pathologically-deep-JSON cases (fix wave r2 / IMP-2): a literal
    3000-deep array would be a multi-KB unreadable line AND would nest the CATALOG file itself that
    deep, which json.loads (which reads the catalog) raises RecursionError on. Segments keep the
    depth as an integer the catalog states out loud.
    """
    if isinstance(spec, str):
        return spec
    if isinstance(spec, list):
        out = []
        for seg in spec:
            if isinstance(seg, str):
                out.append(seg)
            elif isinstance(seg, dict) and set(seg) == {"repeat", "times"}:
                out.append(str(seg["repeat"]) * int(seg["times"]))
            else:
                raise ValueError(f"bad raw-text segment: {seg!r}")
        return "".join(out)
    return json.dumps(spec)


POISON_MARKER = "POISONED-VAULT-ENGINE"

# What a poisoned in-vault engine file does when something runs it: say so, loudly, on the channel
# the comparator reads, and exit off the happy path. Exit 5 rather than 0 so a dispatch that reached
# it cannot look like a pass even if the marker assertion were ever dropped.
_POISON_PY = (f"import sys\nsys.stderr.write({POISON_MARKER!r} + chr(10))\n"
              f"sys.stdout.write({POISON_MARKER!r} + chr(10))\nraise SystemExit(5)\n")


def _poison_vault_engine(vault: Path, verbs: list[str]) -> None:
    """Give the fixture vault a `bin/` of its own that must never be reached — the dual-run proof.

    The vault keeps this tree UNTOUCHED for the whole run (nothing in this task deletes anything),
    and every comparator asserts the marker never appears in either side's output. Two things it
    pins, both of which were live before Task 2:

      * the floor spawned `$PLAINKEEP_HOME/bin/lib/guardrail.py` — put that line back and every
        dispatch case reddens on a stderr marker rather than on a subtle diff;
      * resolver.ts derived engine bin/ as `<home>/bin`, so a verb directory a VAULT happened to
        carry resolved as an `engine` verb, which is the one source a plugin is forbidden to shadow.
    """
    lib = vault / "bin" / "lib"
    lib.mkdir(parents=True, exist_ok=True)
    for mod in ("guardrail.py", "resolver.py", "vaultroot.py", "manifest.py"):
        (lib / mod).write_text(_POISON_PY, encoding="utf-8")
    for verb in verbs:
        d = vault / "bin" / verb
        d.mkdir(parents=True, exist_ok=True)
        (d / "run.py").write_text(_POISON_PY, encoding="utf-8")
        (d / "cmd.json").write_text(
            json.dumps({"verb": verb, "summary": POISON_MARKER, "risk": "read"}), encoding="utf-8")


class Fixture:
    """A built fixture ENGINE + VAULT pair plus the env needed to invoke either side against it.

    Layout (Task 2 — the engine is no longer inside the vault):

        <base>/install/engine/<version>/bin/lib/       the whole bin/lib, copied
        <base>/install/engine/<version>/bin/<verb>/    `at: engine` verbs
        <base>/install/engine/<version>/plainkeep      the shim (the floor's entry point)
        <base>/install/engine/<version>/.local/bin/plainkeep-core   the binary under test
        <base>/install/engine/current -> <version>     what a dispatcher is actually invoked through
        <base>/vault/plugins/<pack>/<verb>/            plugin packs — these DO live in the vault
        <base>/vault/bin/                              poisoned, inert, never resolved
        <base>/vault/_roots/<ref> | <base>/vault/_home/<ref>        PLAINKEEP_PATH roots

    Both trees are REALPATH-canonical, so Python's `Path(__file__).resolve()` is a no-op against
    them and the engine/data disjointness check compares like with like.

    STANDING CONSTRAINT ON EVERY CASE AUTHOR, recorded here because it is a permanent property of
    this oracle: **sorted-set comparisons between the two sides agree only for ASCII names.**
    JavaScript's default `sort()` orders by UTF-16 code UNITS, Python's `sorted()` by code POINTS,
    so the two disagree for astral and some private-use characters. Verb and pack names are ASCII in
    practice, which is why resolver parity holds. A case with non-ASCII ARGUMENTS must therefore
    compare them POSITIONALLY / byte-exactly and never route them through a sorted comparison in this
    harness — the guardrail catalog already complies. (Found in Task 2, applied by Task 3.)
    """

    def __init__(self, spec: dict):
        # mkdtemp first, then guard the rest so a catalog authoring error (bad path:ref, unknown
        # verb location, unregistered file) cleans up its temp trees instead of leaking them (M-2).
        self.base = Path(os.path.realpath(tempfile.mkdtemp(prefix="pk-parity-")))
        try:
            self._build(spec)
        except Exception:
            shutil.rmtree(self.base, ignore_errors=True)
            raise

    def _build(self, spec: dict) -> None:
        # --- the ENGINE, in the real installed shape ------------------------------------------
        # `<base>/install/engine/<version>/` with a `current` symlink beside it, because that is what
        # `enginetree.install()` produces and what a dispatcher is reached through in production. The
        # symlink is not decoration: the floor resolves `$0` through it and the core realpaths its
        # execPath, and the two must land on ONE spelling or the disjointness check compares two
        # names for one directory. Running every case through `current/` is what keeps that honest.
        versions = self.base / "install" / VERSIONS_DIRNAME
        versions.mkdir(parents=True)
        self.engine = versions / ENGINE_VERSION
        self.engine.mkdir()
        self.engine_current = versions / "current"
        self.engine_current.symlink_to(self.engine)

        # The WHOLE bin/lib/, always. It used to be resolver.py alone (plus guardrail.py, copied by
        # the harnesses that need it). Since ADR-014 Task 1b resolver.py and guardrail.py resolve the
        # data root through lib/vaultroot.py, which reads lib/vaultreg.py, lib/wall.py and
        # lib/output.py — copying a hand-maintained closure would encode an import graph HERE that
        # lives THERE, and every future edit to it would fail as an ImportError from inside a temp
        # tree. bin/lib holds no run.py or cmd.json, so it is not a verb dir and the resolver
        # catalogs see exactly the verb surface they declare.
        shutil.copytree(ENGINE_SRC / "lib", self.engine / "bin" / "lib",
                        dirs_exist_ok=True, ignore=shutil.ignore_patterns("__pycache__"))
        # `enginetree.require_intact()` runs on EVERY invocation and asks for <engine>/VERSION, so a
        # fixture engine without one refuses before any comparison happens.
        shutil.copy2(REPO / "VERSION", self.engine / "VERSION")
        self.resolver_py = self.engine / "bin" / "lib" / "resolver.py"

        # --- the VAULT ---------------------------------------------------------------------------
        self.root = self.base / "vault"
        self.root.mkdir()
        self.home = self.root / "_home"
        self.home.mkdir()

        # The fixture vault is a real VAULT, because since Task 1b a directory is not one just
        # because PLAINKEEP_HOME points at it: an unmarked root refuses with exit 2 before the gate
        # runs. Every fixture invocation supplies PLAINKEEP_HOME explicitly, which is step 2 of the
        # chain — marker required, registration NOT (a fixture is deliberately never registered, and
        # that is also the shape of the canary migration ADR-014 calls mandatory evidence).
        self.vault_id = _mark_vault(self.root)

        # Completion-catalog addition (Task 5): `engine_from_repo` installs REAL engine verbs from
        # the repo's bin/ into the fixture, together with the whole bin/lib/ they import. The
        # completion catalog needs it because its reference side is the bash floor RUNNING
        # bin/__complete/run.py: a synthetic stub cannot produce the candidates being compared, and a
        # hand-written copy of the completion brain would be the drift this oracle exists to catch.
        # Only the named verb dirs are installed, so a case still controls its own verb surface.
        #
        # The WHOLE bin/lib/ goes in, deliberately, even though the measured transitive closure of
        # `from lib import completion` (plus every provider) is only six files — completion.py,
        # filing.py, manifest.py, notetype.py, paths.py, resolver.py
        # (.orchestrate/raw/task5-lib-closure.log). Copying the closure instead would encode an import
        # graph HERE that lives THERE, and the day a verb gains an import the fixture fails with an
        # ImportError from inside a temp vault — the confusing failure this note exists to prevent.
        # Copying the directory costs a few hundred KB per fixture and cannot go stale.
        #
        # Both per-side vault copies in _compare_dispatch() are copytree'd from THIS one built
        # fixture, so the two sides run byte-identical engine code by construction; a provider
        # fall-through reading different engine code on the two sides could otherwise compare equal
        # for the wrong reason. Verified once end to end: .orchestrate/raw/task5-side-identity.log.
        from_repo = spec.get("engine_from_repo", [])
        if from_repo:
            ignore = shutil.ignore_patterns("__pycache__")
            for verb in from_repo:
                src = ENGINE_SRC / verb
                if not src.is_dir():
                    raise ValueError(f"engine_from_repo names a verb the repo has no bin/ dir for: {verb!r}")
                shutil.copytree(src, self.engine / "bin" / verb, dirs_exist_ok=True, ignore=ignore)

        # Completion-catalog addition (Task 5): `vault_files` writes plain files under the vault
        # root, which is what the completion PROVIDERS read (wiki/**.md, tasks/<status>/T-*.md).
        # Paths are vault-RELATIVE by construction — that keeps field-guide item 3's problem out of
        # reach entirely, since nothing here needs remapping into the per-side vault copies the way
        # an absolute PLAINKEEP_PATH root would.
        for vf in spec.get("vault_files", []):
            rel = Path(vf["path"])
            if rel.is_absolute() or ".." in rel.parts:
                raise ValueError(f"vault_files path must be vault-relative: {vf['path']!r}")
            dst = self.root / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_text(_content(vf.get("text", "")), encoding="utf-8")

        # path_roots: ordered PLAINKEEP_PATH entries. Map ref -> (dir, env_entry_string).
        self._roots: dict[str, tuple[Path, str]] = {}
        self._root_order: list[str] = []
        for pr in spec.get("path_roots", []):
            ref = pr["ref"]
            if pr.get("under") == "home":
                d = self.home / ref
                entry = f"~/{ref}" if pr.get("tilde") else str(d)
            else:
                d = self.root / "_roots" / ref
                entry = str(d)
            d.mkdir(parents=True, exist_ok=True)
            self._roots[ref] = (d, entry)
            self._root_order.append(ref)

        for v in spec.get("verbs", []):
            self._make_verb(v["at"], v["verb"], v.get("files", ["run.py", "cmd.json"]),
                            v.get("cmd"), v.get("run"))

        # Dispatcher-catalog addition (Task 4): a fake $PLAINKEEP_HOME/.venv/bin/python3, so the
        # interpreter-selection probe (exists AND actually starts) can be driven from a catalog.
        #   "live"   — a SYMLINK to the running python3. CPython reports sys.executable as the
        #              symlink path, so a verb can tell that the venv interpreter was the one picked.
        #   "broken" — executable but exits 1 on `-c ''`, the exact "survived an ABI break" shape the
        #              probe exists for. Both sides must fall back to bare python3.
        venv = spec.get("venv")
        if venv is not None:
            vbin = self.root / ".venv" / "bin"
            vbin.mkdir(parents=True, exist_ok=True)
            vpy = vbin / "python3"
            if venv == "live":
                vpy.symlink_to(PY)
            elif venv == "broken":
                vpy.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
                vpy.chmod(0o755)
            else:
                raise ValueError(f"unknown venv fixture: {venv!r}")

        # Guardrail-catalog additions (additive; resolver.json declares neither): plugins_lock writes
        # plugins/plugins.lock.json verbatim (a str for a malformed-JSON fixture) or json.dumps()'d
        # (an object). verbs[].cmd overrides a verb's cmd.json CONTENT (str written raw, else dumped),
        # so risk/dry_run vary per verb — what _VERB_FILES' name-only lambda cannot express. Both go
        # through _content(), which also accepts the segment-list form for deep-nesting fixtures.
        pl = spec.get("plugins_lock")
        if pl is not None:
            pdir = self.root / "plugins"
            pdir.mkdir(parents=True, exist_ok=True)
            (pdir / "plugins.lock.json").write_text(_content(pl), encoding="utf-8")

        # THE DUAL-RUN PROOF (Task 2), built last so it can name every engine verb the case declared.
        # The vault gets a complete-looking `bin/` that must never be reached. See
        # `_poison_vault_engine` — and note that the case's OWN verb names are what get poisoned, so
        # a regression does not merely produce a wrong answer, it produces the marker.
        _poison_vault_engine(self.root, sorted(
            {v["verb"] for v in spec.get("verbs", []) if v["at"] == "engine"} | set(from_repo)))

    def install_core(self, binary: str) -> str:
        """Put the binary under test INSIDE this fixture's engine and return the path to invoke.

        Since Task 2 the core derives its engine root from its own execPath, so invoking the repo's
        `.local/bin/plainkeep-core` in place would resolve the REPOSITORY's verbs — the differential
        would compare a fixture against the source tree and pass for the wrong reason. Hardlinked
        rather than copied: the binary is ~64 MB and there is one fixture per case."""
        dst = self.engine / ".local" / "bin" / "plainkeep-core"
        dst.parent.mkdir(parents=True, exist_ok=True)
        if not dst.exists():
            try:
                os.link(binary, dst)
            except OSError:          # different filesystem — correctness over speed
                shutil.copy2(binary, dst)
            dst.chmod(0o755)
        # Invoked through `current/`, not through the version directory: that is how a real install
        # is reached, and it is what exercises the core's execPath realpath resolution.
        return str(self.engine_current / ".local" / "bin" / "plainkeep-core")

    def _base_dir(self, at: str) -> Path:
        if at == "engine":
            # THE ENGINE TREE, not `<vault>/bin` (Task 2). This one line is most of what "the parity
            # harness gains an installed-engine mode" means: every `at: engine` verb a catalog
            # declares is now installed where the code lives, and the resolvers find it there.
            return self.engine / "bin"
        if at.startswith("plugin:"):
            return self.root / "plugins" / at[len("plugin:"):]
        if at.startswith("path:"):
            ref = at[len("path:"):]
            if ref not in self._roots:
                raise ValueError(f"path:{ref} used by a verb but not declared in path_roots")
            return self._roots[ref][0]
        raise ValueError(f"unknown verb location: {at!r}")

    def _make_verb(self, at: str, verb: str, files: list[str], cmd=None, run=None) -> None:
        d = self._base_dir(at) / verb
        d.mkdir(parents=True, exist_ok=True)
        for f in files:
            # verbs[].run (Task 4) is to run.py what verbs[].cmd is to cmd.json: the CONTENT, so a
            # dispatcher case can ship a verb that exits 7, prints its argv, or kills itself. The
            # default stays the inert stub — resolver/guardrail cases never execute a verb.
            if f == "cmd.json" and cmd is not None:
                content = _content(cmd)
            elif f == "run.py" and run is not None:
                content = _content(run)
            else:
                content = _VERB_FILES[f](verb)
            (d / f).write_text(content, encoding="utf-8")

    def path_value(self, order: list[str] | None) -> str:
        refs = self._root_order if order is None else order
        return ":".join(self._roots[r][1] for r in refs)

    def env(self, path_order: list[str] | None) -> dict:
        e = dict(os.environ)
        e["PLAINKEEP_HOME"] = str(self.root)
        e["PLAINKEEP_PATH"] = self.path_value(path_order)
        e["HOME"] = str(self.home)
        # A HOSTILE inherited PLAINKEEP_ENGINE on EVERY invocation, not on one special case. ADR-014
        # D2 says caller input must not control where code is loaded from, and both dispatchers
        # overwrite this before anything reads it. Poisoning it universally means the property is
        # re-proved by all 203 catalog invocations rather than by one test that could rot: if either
        # dispatcher ever started honouring the inherited value, every case would fail on ENOENT.
        e["PLAINKEEP_ENGINE"] = str(self.base / "_engine_that_does_not_exist")
        return e

    def cleanup(self) -> None:
        shutil.rmtree(self.base, ignore_errors=True)


# --------------------------------------------------------------------------------------------------
# The Python probe (imports the fixture's resolver.py, prints the same compact JSON as the binary)
# --------------------------------------------------------------------------------------------------

_PROBE = r"""
import importlib.util, json, os, sys
spec = importlib.util.spec_from_file_location("pk_resolver", os.environ["PK_RESOLVER_PY"])
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
api = sys.argv[1]
def out(x): print(json.dumps(x, separators=(",", ":"), ensure_ascii=False))
if api == "known_verbs": out(sorted(m.known_verbs()))
elif api == "iter_cmds": out([[str(p), s] for p, s in m.iter_cmds()])
elif api == "shadowed": out([[v, pk] for v, pk in m.shadowed()])
elif api == "plugin_names": out(m.plugin_names())
elif api.startswith("source_of:"): out(m.source_of(api[len("source_of:"):]))
elif api.startswith("resolve:"):
    r = m.resolve(api[len("resolve:"):]); out([str(r[0]), r[1]] if r else None)
else:
    sys.stderr.write("bad api spec\n"); sys.exit(2)
"""


# --------------------------------------------------------------------------------------------------
# Comparators — one per introspection surface. (Task 3 registers a "gate" comparator here.)
# --------------------------------------------------------------------------------------------------

def _run(cmd: list[str], env: dict, cwd: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, env=env, cwd=cwd, capture_output=True, text=True, encoding="utf-8")


# A cwd that is inside NO vault. Needed since Task 1b for any invocation that deliberately supplies
# no root: discovery's third step walks UP from $PWD, and this suite's own cwd is the repo — which,
# per the Task 1b instructions, the developer has registered as a vault. Without this, "nothing
# selected a root" checks resolve to the repo (or refuse for the wrong reason, as they did while
# these were being written) instead of reaching the refusal they are asserting.
def _nowhere(holder: list[Path]) -> str:
    d = Path(os.path.realpath(tempfile.mkdtemp(prefix="pk-no-vault-")))
    holder.append(d)
    return str(d)


# The dual-run assertion, applied by every comparator (Task 2). The vault's own `bin/` is present and
# untouched on disk for the whole run; nothing may ever resolve to it. An `in` test on both channels
# of both sides, because the poison writes to stdout AND stderr and either side reaching it is the
# same defect.
def _poison_reached(*outputs: str) -> bool:
    return any(POISON_MARKER in (o or "") for o in outputs)


def _compare_cli(binary: str, fx: Fixture, inv: dict) -> tuple[bool, str]:
    env = fx.env(inv.get("path_order"))
    verb = inv["verb"]
    core = fx.install_core(binary)
    ts = _run([core, "--core-resolve", verb], env)
    py = _run([PY, str(fx.resolver_py), verb], env)
    clean = not _poison_reached(ts.stdout, ts.stderr, py.stdout, py.stderr)
    ok = ts.returncode == py.returncode and ts.stdout == py.stdout and clean
    detail = (
        f"verb={verb!r} ts=(rc={ts.returncode},out={ts.stdout!r}) "
        f"py=(rc={py.returncode},out={py.stdout!r}) pyerr={py.stderr.strip()!r} "
        f"vault_bin_untouched={clean}"
    )
    return ok, detail


def _compare_api(binary: str, fx: Fixture, inv: dict) -> tuple[bool, str]:
    env = fx.env(inv.get("path_order"))
    api = inv["api"]
    core = fx.install_core(binary)
    ts = _run([core, "--core-api", api], env)
    penv = dict(env, PK_RESOLVER_PY=str(fx.resolver_py))
    py = _run([PY, "-c", _PROBE, api], penv)
    clean = not _poison_reached(ts.stdout, ts.stderr, py.stdout, py.stderr)
    ok = ts.returncode == py.returncode and ts.stdout == py.stdout and clean
    detail = (
        f"api={api!r} ts=(rc={ts.returncode},out={ts.stdout!r}) "
        f"py=(rc={py.returncode},out={py.stdout!r}) pyerr={py.stderr.strip()!r} "
        f"vault_bin_untouched={clean}"
    )
    return ok, detail


# Gate comparator (Task 3) — proves the ported guardrail main_cli() reproduces bin/lib/guardrail.py
# byte-for-byte on exit code, stdout, stderr, AND the appended audit-log line.
#
# Log isolation: both sides call _log(), which APPENDS to $PLAINKEEP_HOME/.logs/plainkeep.log. Run
# them against the SAME vault and the two lines interleave in one file — unattributable. So each side
# gets its OWN realpath-canonical copy of the built vault (distinct PLAINKEEP_HOME → distinct .logs),
# and only the Python side's copy also carries guardrail.py (next to the resolver.py it imports); the
# TS binary embeds its own guardrail. The timestamp differs by construction (two clocks), so the log
# line is compared as: assert BOTH timestamps match the UTC ISO-seconds+offset shape, then byte-
# compare the remainder after the first tab (verb+args, verdict, reason — including any tab/newline
# an arg carries, since split('\t', 1) only consumes the timestamp's separator).

# Python datetime.now(timezone.utc).isoformat(timespec="seconds") → e.g. 2026-07-29T10:00:00+00:00.
_LOG_TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+00:00$")


def _gate_env(fx: Fixture, inv: dict, root_copy: Path) -> dict:
    # Reuse the fixture env (PLAINKEEP_PATH is empty for gate cases — they use engine/plugins only),
    # then repoint PLAINKEEP_HOME + HOME at this side's private copy so its .logs is isolated.
    e = fx.env(inv.get("path_order"))
    e["PLAINKEEP_HOME"] = str(root_copy)
    e["HOME"] = str(root_copy / "_home")
    return e


def _read_log(root: Path) -> str:
    try:
        return (root / ".logs" / "plainkeep.log").read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""


def _compare_log(ts_log: str, py_log: str) -> tuple[bool, str]:
    # main_cli logs allow/confirm/deny but NOT the unknown-verb path (returns before _log) — so both
    # empty is a valid agreement.
    if ts_log == "" and py_log == "":
        return True, "no log (both empty)"
    if (ts_log == "") != (py_log == ""):
        return False, f"log presence differs ts={ts_log!r} py={py_log!r}"
    ts_ts, _, ts_rest = ts_log.rstrip("\n").partition("\t")
    py_ts, _, py_rest = py_log.rstrip("\n").partition("\t")
    shape_ok = bool(_LOG_TS_RE.match(ts_ts)) and bool(_LOG_TS_RE.match(py_ts))
    return (shape_ok and ts_rest == py_rest), (
        f"ts_ts={ts_ts!r} py_ts={py_ts!r} shape_ok={shape_ok} "
        f"ts_rest={ts_rest!r} py_rest={py_rest!r}"
    )


def _compare_gate(binary: str, fx: Fixture, inv: dict) -> tuple[bool, str]:
    verb = inv["verb"]
    args = inv.get("args", [])
    # Per-side VAULT copies only (Task 2). The engine is shared: it is read-only code, both sides run
    # byte-identical bytes out of it by construction, and what needed isolating was never the code —
    # it was `.logs/plainkeep.log`, which lives in the vault. The Python side is now handed the
    # ENGINE's guardrail.py rather than a copy sitting in its own vault; the copy in the vault is the
    # POISONED one, and running it is the failure this comparator now also detects.
    ts_root = Path(os.path.realpath(tempfile.mkdtemp(prefix="pk-gate-ts-")))
    py_root = Path(os.path.realpath(tempfile.mkdtemp(prefix="pk-gate-py-")))
    core = fx.install_core(binary)
    try:
        shutil.copytree(fx.root, ts_root, dirs_exist_ok=True)
        shutil.copytree(fx.root, py_root, dirs_exist_ok=True)
        ts = _run([core, "--core-gate", verb, *args], _gate_env(fx, inv, ts_root))
        py = _run([PY, str(fx.engine / "bin" / "lib" / "guardrail.py"), verb, *args],
                  _gate_env(fx, inv, py_root))
        ok_log, log_detail = _compare_log(_read_log(ts_root), _read_log(py_root))
        clean = not _poison_reached(ts.stdout, ts.stderr, py.stdout, py.stderr)
        ok = (ts.returncode == py.returncode and ts.stdout == py.stdout
              and ts.stderr == py.stderr and ok_log and clean)
        detail = (
            f"verb={verb!r} args={args!r} "
            f"ts=(rc={ts.returncode},out={ts.stdout!r},err={ts.stderr!r}) "
            f"py=(rc={py.returncode},out={py.stdout!r},err={py.stderr!r}) log[{log_detail}] "
            f"vault_bin_untouched={clean}"
        )
        return ok, detail
    finally:
        shutil.rmtree(ts_root, ignore_errors=True)
        shutil.rmtree(py_root, ignore_errors=True)


# --------------------------------------------------------------------------------------------------
# Dispatcher differential matrix (Task 4) — the WHOLE dispatch, both ways.
#
# Reference side: the BASH FLOOR, reached as `PLAINKEEP_CORE=off <vault>/plainkeep <verb> <args...>`
# (the pre-core dispatcher, preserved verbatim inside the shim). Core side: the compiled binary
# invoked directly. Compared: exit status (including a signal death, which subprocess reports as a
# NEGATIVE returncode — the whole point of the signal cases), stdout, stderr, and the gate's audit
# log line. Same vault-copy-per-side isolation as the gate comparator, for the same reason.
#
# MODE PINNING (a run whose mode is ambiguous proves nothing): the floor side is handed a TRACER as
# its PLAINKEEP_CORE_BIN — a fake core that is executable and passes --core-selftest, so `auto` or
# `require` WOULD exec it — and every case asserts the tracer's marker file does not exist. That is a
# positive proof the floor side really took the bash path rather than silently re-entering the core.
# The core side needs no such proof: it IS the binary, invoked by absolute path with no shell in
# between.
#
# NOT COMPARABLE, BY DESIGN: the binary's identity/introspection flags are a SANCTIONED divergence
# from the floor — `plainkeep-core --version` prints the core identity and exits 0 (plan-phase1,
# "Binary introspection flags": the output must distinguish the core binary from the bash floor
# unambiguously, advisor B4), while the floor has no such concept and gates `--version` as an unknown
# verb (exit 4). Routing one of those argv shapes through this comparator would therefore report a
# difference that is CORRECT, so _intercepted_argv() rejects them up front with an explanation
# instead of leaving a future case author to rediscover it from a confusing diff.
# --------------------------------------------------------------------------------------------------

# The argv shapes runCore() answers ITSELF, before dispatch ever runs — ASKED OF THE BINARY, not
# restated here. This list used to be a hand-kept copy of cli.ts's branches, which was correct only
# until the next task added an interception: Tasks 5–7 do exactly that, and a stale copy would not
# have gone quietly wrong so much as gone LOUDLY wrong with the wrong explanation (the case fails
# with a raw stdout diff, and the next author rediscovers by hand the thing this guard exists to have
# already explained). `--core-api intercepts` is the same data that drives those branches.
#
# The shape has two halves and they are refused for DIFFERENT reasons, so they are read separately:
#
#   flags — short-circuited BEFORE the gate (`--version`, `--core-api`, …). The floor has no notion of
#           them at all, so comparing one across modes reports a difference that is correct.
#   verbs — answered after the gate (dispatch.ts INTERCEPTS), in two buckets:
#             comparable    — `__complete`. Byte-parity with the Python verb is what makes
#                             intercepting it legitimate, so these must NEVER be skipped: skipping
#                             them would remove the only evidence that the interception is honest.
#             noncomparable — `ui` (Task 6). Answered in-process but not comparable AS AN INVOCATION:
#                             a TUI paints a terminal, so its stdout is frames, cursor moves and a
#                             spinner whose frame count depends on how long a child took. Diffing
#                             that against the floor is not a strict test, it is a flaky one.
#
# BOTH refusals are structural, and the second one exists because a classification nothing reads is a
# comment rather than a gate: before this, a case author could write a dispatcher case for `ui` and
# the harness would happily run it and go intermittently red for a reason no diff would explain.
_INTERCEPTS_CACHE: dict | None = None


def _load_intercepts(binary: str) -> dict:
    global _INTERCEPTS_CACHE
    if _INTERCEPTS_CACHE is None:
        p = _run([binary, "--core-api", "intercepts"], dict(os.environ))
        if p.returncode != 0:
            # Fail loudly rather than falling back to a guess: a binary that cannot answer this is a
            # binary whose interception rules we do not know, and skipping the wrong cases silently
            # is the exact failure this probe exists to prevent.
            raise RuntimeError(
                f"binary could not answer --core-api intercepts (rc={p.returncode}, "
                f"stderr={p.stderr.strip()!r}) — rebuild it: cd cli && bun run build"
            )
        _INTERCEPTS_CACHE = json.loads(p.stdout)
    return _INTERCEPTS_CACHE


def _intercepted_argv(binary: str, argv: list[str]) -> str | None:
    flags = _load_intercepts(binary)["flags"]
    if not argv:
        return None
    head = argv[0]
    if head in flags["always"]:
        return head
    if head in flags["bare"] and len(argv) == 1:
        return head
    return None


def _noncomparable_verb(binary: str, argv: list[str]) -> str | None:
    """The VERB a case dispatches, when the binary has classified it as not comparable against the
    floor. Read from the binary's own registrations (`--core-api intercepts`), never from a list kept
    here — a hand-kept copy is correct only until the next task registers an interception.

    The verb is argv[0] after the dispatcher's normalization, which for the shapes a dispatcher case
    can express is argv[0] itself: an OMITTED verb is the default-verb path (bare `plainkeep`, which
    both dispatchers answer with `help`) and an EMPTY one is `help` too, and `help` is not
    intercepted (run.md D6). No normalization is reimplemented here for that reason."""
    if not argv:
        return None
    verbs = _load_intercepts(binary)["verbs"]
    return argv[0] if argv[0] in verbs.get("noncomparable", []) else None

def _install_floor(engine: Path) -> None:
    """Make a built fixture ENGINE runnable by the bash floor: the shim at the engine root.

    It took a VAULT root through Phase 1, which is the assumption Task 2 deletes — the launcher ships
    inside the tree it launches, and `$0` is how the floor finds the engine. The engine modules it
    spawns (guardrail.py, resolver.py, vaultroot.py and what they import) are beside it, because
    Fixture copies the WHOLE bin/lib into the engine."""
    dst = engine / "plainkeep"
    shutil.copy2(PLAINKEEP_SRC, dst)
    dst.chmod(0o755)


def _write_tracer(path: Path, marker: Path) -> None:
    """A fake core binary: executable, passes --core-selftest (exit 0 for any argv), and RECORDS that
    it was invoked. Used to pin dispatcher mode — and, in the shim checks, to prove the opposite."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f'#!/bin/sh\nprintf \'%s\\n\' "$@" >> "{marker}"\nexit 0\n', encoding="utf-8")
    path.chmod(0o755)


def _symlink_alias(root: Path, holder: list[Path]) -> Path:
    """A second, NON-canonical path to `root` (a symlink named `vault` in a fresh temp dir) — the
    macOS /tmp vs /private/tmp shape. `holder` collects the temp dirs for cleanup."""
    d = Path(os.path.realpath(tempfile.mkdtemp(prefix="pk-alias-")))
    holder.append(d)
    alias = d / "vault"
    alias.symlink_to(root)
    return alias


# expect_returncodes vocabulary. A cell may be a literal returncode, or one of three SYMBOLIC forms
# resolved against the signal the case names in args[0] — symbolic so the catalog stays portable:
# SIGBUS is 10 on macOS and 7 on Linux, so a literal would pin one platform and lie on the other.
#
#   "signal"   — died by that signal (returncode -N), the floor's behavior for every terminating signal
#   "fallback" — survived the re-raise and exited 128+N, the shell rendering (bun IGNORES the signal)
#   "sigtrap"  — died by SIGTRAP instead (bun's crash handler intercepted the re-raise)
#
# The last two name the two measured classes of core/floor divergence, so a case says WHICH failure it
# is pinning rather than just an opaque number. See .orchestrate/raw/task4-fix2-signal-matrix.log.
def _signal_number(name: str) -> int:
    return int(getattr(signal, name))


def _expected_rc(spec, signum: int) -> int:
    if isinstance(spec, bool):
        raise ValueError(f"expect_returncodes cell must not be a bool: {spec!r}")
    if isinstance(spec, int):
        return spec
    if spec == "signal":
        return -signum
    if spec == "fallback":
        return 128 + signum
    if spec == "sigtrap":
        return -int(signal.SIGTRAP)
    raise ValueError(f"unknown expect_returncodes form: {spec!r}")


def _dispatch_env(fx: Fixture, inv: dict, home: Path) -> dict:
    e = fx.env(inv.get("path_order"))
    e["PLAINKEEP_HOME"] = str(home)
    e["HOME"] = str(home / "_home")
    # Never let the OUTER suite's mode leak in: run_all.py is run once with PLAINKEEP_CORE=require
    # and once with =off, and this comparator must drive both sides itself either way.
    e.pop("PLAINKEEP_CORE", None)
    e.pop("PLAINKEEP_CORE_BIN", None)
    # ...and never let the DEVELOPER's PYTHONPATH decide what a case sees. The plugin spawn contract
    # (Task 3) PREPENDS to whatever the caller had, so an ambient value would appear as a third,
    # machine-specific entry in the child's path and turn `plugin-spawn-environment`'s exact-stdout
    # assertion into a property of the shell it was run from. The MERGE with a caller's value is
    # asserted where it can be controlled (test/run_pluginsdk.py); what this comparator owns is that
    # the two dispatchers produce the same thing from the same start.
    e.pop("PYTHONPATH", None)
    # invocations[].env — a HOSTILE CALLER'S ENVIRONMENT, stated per invocation. Every knob above
    # exists to make the two sides start from the same clean place; this one exists for the cases
    # where the caller's environment is the subject rather than the noise. `PLAINKEEP_PLUGIN_PACK`
    # preset by a re-entrant plugin verb (or by a shell `export`) is the case it was added for: the
    # engine-verb negative was only ever asserted from a FRESH dispatch, where the variable was never
    # there to begin with, so neither dispatcher REMOVING it went uncovered. A null value UNSETS.
    for k, v in (inv.get("env") or {}).items():
        if v is None:
            e.pop(k, None)
        else:
            e[k] = str(v)
    return e


def _compare_dispatch(binary: str, fx: Fixture, inv: dict) -> tuple[bool, str]:
    # An OMITTED verb means no argv at all — the default-verb path (`plainkeep` bare). Distinct from
    # an EMPTY verb string, which bash's `${1:-help}` also turns into `help` but which does reach the
    # dispatcher as an argument.
    verb = inv.get("verb")
    args = inv.get("args", [])
    argv = list(args) if verb is None else [verb, *args]
    # Fail the CASE, not the binary: this argv can never be compared across modes (see above).
    flag = _intercepted_argv(binary, argv)
    if flag is not None:
        return False, (
            f"invalid dispatcher case: {flag!r} is intercepted by the binary before dispatch, so it "
            f"is a SANCTIONED floor/core divergence (core: identity, exit 0; floor: unknown verb, "
            f"exit 4 — plan-phase1 'Binary introspection flags', advisor B4). Comparing it across "
            f"modes proves nothing. Assert the flag's behavior in a bun test or a shim check instead."
        )
    # The symmetric refusal for an interception the binary declares NON-comparable. Same shape as the
    # flag refusal above and for the same reason: fail the CASE with an explanation, rather than let
    # the comparator produce a diff whose meaning the next author has to rediscover.
    nc = _noncomparable_verb(binary, argv)
    if nc is not None:
        return False, (
            f"invalid dispatcher case: {nc!r} is answered IN-PROCESS by the binary and is registered "
            f"NON-comparable (--core-api intercepts → verbs.noncomparable), so its stdout is not a "
            f"byte stream this comparator can diff: a TUI paints a TERMINAL (frames, cursor moves, a "
            f"spinner whose frame count depends on how long a child took) and a server is a SESSION "
            f"rather than an invocation. Comparing it across modes would be FLAKY, not strict — it "
            f"would fail on timing rather than on behavior. Assert it where it can be asserted "
            f"honestly instead: drive {nc!r} end-to-end from its own harness (test/run_tui_pty.py for "
            f"`ui`, the MCP driver for `mcp`), or a bun test for its pure seams. If this case only "
            f"cares about the GATE — the verdict, exit code, stderr and audit line, all of which ARE "
            f"comparable for an intercepted verb — rewrite it as compare: \"gate\", which runs the "
            f"gate on both sides and never dispatches."
        )
    # An exclusion with nothing positive beside it is a case that passes in the failure it exists to
    # catch: an EMPTY stdout satisfies every "must not contain", so a completion path that silently
    # returned NOTHING would go green while asserting a hidden verb is absent. Refused structurally,
    # here, rather than left to each case author to remember — the same reasoning that made
    # agreement-only returncode cells useless in Task 4's signal matrix.
    if inv.get("expect_stdout_excludes") is not None and not (
        inv.get("expect_stdout_contains") or inv.get("expect_stdout")
    ):
        return False, (
            "invalid dispatcher case: expect_stdout_excludes needs a NON-EMPTY positive assertion in "
            "the same invocation (expect_stdout_contains, or an exact expect_stdout). Without one, "
            "empty stdout satisfies the exclusion and the case is green in exactly the failure mode "
            "it is meant to detect. To assert that output is empty, use expect_stdout: \"\"."
        )
    # Per-side VAULT copies; the ENGINE is shared (Task 2). Sharing it is stronger than copying it,
    # not weaker: the two sides now run the SAME BYTES rather than two copies asserted to be
    # identical, which is what the note on `engine_from_repo` above was working to guarantee by hand.
    # What still needs isolating is the vault, because that is where `.logs/plainkeep.log` is.
    core_root = Path(os.path.realpath(tempfile.mkdtemp(prefix="pk-disp-core-")))
    floor_root = Path(os.path.realpath(tempfile.mkdtemp(prefix="pk-disp-floor-")))
    aliases: list[Path] = []
    core_bin = fx.install_core(binary)
    _install_floor(fx.engine)
    try:
        for r in (core_root, floor_root):
            # symlinks=True: a "live" venv fixture is a SYMLINK to the running python3; copying its
            # target instead would produce a python binary that cannot find its own framework.
            shutil.copytree(fx.root, r, dirs_exist_ok=True, symlinks=True)
        marker = floor_root / "_core_was_used"
        tracer = floor_root / "_fake_core"
        _write_tracer(tracer, marker)

        core_home = floor_home = None
        if inv.get("home_via_symlink"):
            core_home = _symlink_alias(core_root, aliases)
            floor_home = _symlink_alias(floor_root, aliases)
        core_env = _dispatch_env(fx, inv, core_home or core_root)
        floor_env = dict(_dispatch_env(fx, inv, floor_home or floor_root),
                         PLAINKEEP_CORE="off", PLAINKEEP_CORE_BIN=str(tracer))

        # Both sides are reached through `current/`, which is a symlink into the version directory —
        # the shape a real install has, and the one that makes the floor's `$0` chain and the core's
        # execPath realpath agree on one canonical engine root.
        core = _run([core_bin, *argv], core_env)
        floor = _run([str(fx.engine_current / "plainkeep"), *argv], floor_env)

        ok_log, log_detail = _compare_log(_read_log(core_root), _read_log(floor_root))
        mode_pinned = not marker.exists()
        # expect_returncodes states each side's exact status instead of only asserting the two agree.
        # Agreement alone is too weak twice over: it passes when both sides are wrong together, and it
        # cannot express a KNOWN divergence at all, so a divergent signal simply went uncovered — which
        # is how a whole class of them stayed invisible until spec re-review r2. Every signal cell now
        # carries one, whether it agrees or not, so a delivery change in ANY direction reddens a named
        # case: a divergence cell fails if the core starts matching the floor (a future bun fixing
        # delivery — the good outcome, which must still be noticed), and an agreement cell fails if it
        # stops. Everything else (stdout, stderr, the audit line) is still compared for agreement.
        want_rc = inv.get("expect_returncodes")
        if want_rc is None:
            rc_ok = core.returncode == floor.returncode
            exp = None
        else:
            signum = _signal_number(args[0]) if args else 0
            exp = (_expected_rc(want_rc["core"], signum), _expected_rc(want_rc["floor"], signum))
            rc_ok = (core.returncode, floor.returncode) == exp
        # expect_stderr_divergence pins the SECOND facet of the fault-signal divergence, found by the
        # exhaustive sweep and not by any returncode table: when bun's crash handler intercepts a
        # re-raised fault signal it also DUMPS A CRASH REPORT to stderr ("Bun v1.3.14 … macOS Silicon
        # …"), where the floor prints nothing at all. That is the user-visible half — terminal noise
        # attributed to plainkeep — so it is asserted in both directions rather than papered over by
        # relaxing the stderr comparison.
        want_err = inv.get("expect_stderr_divergence")
        if want_err is None:
            err_ok = core.stderr == floor.stderr
        else:
            err_ok = (want_err["core_contains"] in core.stderr
                      and core.stderr != "" and floor.stderr == want_err["floor"])
        # The dual-run proof, on the comparator that can actually reach a verb (Task 2). The vault
        # each side runs against carries a full poisoned `bin/` — put back either of the two
        # pre-Task-2 derivations and this goes red with the marker in stderr rather than with a diff
        # nobody can read.
        clean = not _poison_reached(core.stdout, core.stderr, floor.stdout, floor.stderr)
        ok = (rc_ok and core.stdout == floor.stdout and err_ok and ok_log and mode_pinned and clean)
        # An absolute assertion on top of the differential: two sides that agree on the WRONG answer
        # (e.g. both ignoring a live venv) would still compare equal. Cases that care state what the
        # shared stdout must contain.
        want = inv.get("expect_stdout_contains")
        if want is not None and want not in core.stdout:
            ok = False
        # The negative half, and it is not symmetric decoration: for `__complete` the differential
        # alone cannot prove an ABSENCE. Two sides that both wrongly LISTED a hidden verb compare
        # equal and pass, so hidden-verb filtering — the one completion behavior whose whole content
        # is "this name must not appear" — needs an assertion that no agreement can satisfy.
        nope = inv.get("expect_stdout_excludes")
        if nope is not None:
            for s in ([nope] if isinstance(nope, str) else nope):
                if s in core.stdout or s in floor.stdout:
                    ok = False
        # The strongest form, for outputs small enough to state in full: BOTH sides' stdout must equal
        # this byte for byte. It is the only way to assert an output is EMPTY (a `contains` cannot,
        # since every string contains ""), which `__complete` needs in two directions — no candidates
        # prints nothing at all, and one empty candidate prints exactly one newline.
        want_exact = inv.get("expect_stdout")
        if want_exact is not None and (core.stdout != want_exact or floor.stdout != want_exact):
            ok = False
        detail = (
            f"verb={verb!r} args={args!r} "
            f"core=(rc={core.returncode},out={core.stdout!r},err={core.stderr!r}) "
            f"floor=(rc={floor.returncode},out={floor.stdout!r},err={floor.stderr!r}) "
            f"log[{log_detail}] mode_pinned={mode_pinned} vault_bin_untouched={clean} "
            f"expect={want!r} excludes={nope!r} "
            f"expect_exact={want_exact!r} expect_rc={want_rc!r} resolved_rc(core,floor)={exp!r}"
        )
        return ok, detail
    finally:
        for d in (core_root, floor_root, *aliases):
            shutil.rmtree(d, ignore_errors=True)


COMPARATORS = {
    "cli": _compare_cli,
    "api": _compare_api,
    "gate": _compare_gate,
    "dispatch": _compare_dispatch,
}


# --------------------------------------------------------------------------------------------------
# Shim + root-discovery + ENGINE-TREE checks (Task 4, extended in Phase 2 Task 2)
#
# These are NOT TS-vs-Python differentials — they are behavioral assertions about the `plainkeep`
# shim's own contract, which no catalog case can express because each needs a differently-shaped
# ENGINE (one with a core binary, one with none, one with a core that lies about being alive, one
# with a file missing). They live here rather than in a bun test because the contract is a bash
# script's, and because the same Fixture builder already produces the trees.
#
# The contract under test (proposal §3 / plan-phase1 "The shim + root-discovery contract", plus
# ADR-014 D2/D3 as Task 2 turns them on):
#   * a caller-supplied PLAINKEEP_HOME survives the shim AND the binary, unmodified;
#   * invoked directly with no PLAINKEEP_HOME, the binary REFUSES — no vault from execPath;
#   * a copied ENGINE dispatches through its own copied core, with no reference back to the original;
#   * no core installed -> auto takes the bash floor SILENTLY, require fails loudly (exit 1), off
#     takes the floor;
#   * a core that is executable but fails --core-selftest -> auto takes the floor after EXACTLY ONE
#     warning line, require fails loudly. A broken core must never poison plainkeep;
#   * (Task 2) the engine and the data root are DISJOINT, refused in both directions and in both
#     dispatchers; an INCOMPLETE engine refuses before it dispatches; PLAINKEEP_ENGINE is REPLACED
#     rather than honoured; and verb discovery reads the installed tree and NOT the repository.
# --------------------------------------------------------------------------------------------------

_SHIM_FIXTURE = {
    "verbs": [
        {"at": "engine", "verb": "v_ok", "cmd": {"verb": "v_ok", "risk": "read"},
         "run": "print('ok')\n"},
        {"at": "engine", "verb": "v_home", "cmd": {"verb": "v_home", "risk": "read"},
         "run": "import os\nprint(os.environ.get('PLAINKEEP_HOME', '<unset>'))\n"},
        # Task 2: what the verb was TOLD the engine is, and where its own code actually sits. The
        # two must agree, and neither may be the value the caller exported.
        {"at": "engine", "verb": "v_engine", "cmd": {"verb": "v_engine", "risk": "read"},
         "run": "import os, pathlib\n"
                "print(os.environ.get('PLAINKEEP_ENGINE', '<unset>'))\n"
                "print(pathlib.Path(__file__).resolve().parents[2])\n"},
    ],
}


def _shim_env(home: Path | None, **over) -> dict:
    e = dict(os.environ)
    for k in ("PLAINKEEP_CORE", "PLAINKEEP_CORE_BIN", "PLAINKEEP_HOME", "PLAINKEEP_PATH",
              "PLAINKEEP_ENGINE", "PLAINKEEP_ENGINE_HOME"):
        e.pop(k, None)
    # HERMETIC REGISTRY, and this is not belt-and-braces. Since Task 1b an invocation with no
    # PLAINKEEP_HOME falls through to the marker walk-up and then to the registry DEFAULT — so
    # without this, a shim check run on a machine whose developer has registered a vault (which the
    # Task 1b instructions require them to do) silently dispatches against that REAL vault. It was
    # observed doing exactly that while this suite was being updated: two checks failed with
    # "unknown verb 'v_home'" because they had resolved to the repo instead of the fixture.
    e["PLAINKEEP_CONFIG_HOME"] = str(Path(tempfile.gettempdir()) / "pk-parity-no-registry")
    if home is not None:
        e["PLAINKEEP_HOME"] = str(home)
        e["HOME"] = str(home / "_home")
    for k, v in over.items():
        e[k] = str(v)
    return e


def _clone_tree(src: Path, holder: list[Path], prefix: str, name: str) -> Path:
    dst = Path(os.path.realpath(tempfile.mkdtemp(prefix=prefix))) / name
    holder.append(dst.parent)
    shutil.copytree(src, dst, symlinks=True)
    return dst


def _clone_vault(src: Path, holder: list[Path], prefix: str) -> Path:
    return _clone_tree(src, holder, prefix, "vault")


def _clone_engine(src: Path, holder: list[Path], prefix: str) -> Path:
    """A second ENGINE tree, complete and standalone. Separate from `_clone_vault` since Task 2 for
    the reason the whole task exists: these are two different kinds of thing, and a helper that
    copies "the fixture" without saying which one is how the distinction gets lost again."""
    return _clone_tree(src, holder, prefix, "engine")


def _shim_checks(binary: str) -> None:
    tmps: list[Path] = []
    try:
        fx = Fixture(_SHIM_FIXTURE)
    except Exception as e:  # a broken fixture must be a localized FAIL, not a traceback
        check("[shim] fixture-build", False, f"exception: {e!r}")
        return
    try:
        _install_floor(fx.engine)
        # THE TWO TREES, named apart. `base` is the ENGINE (it holds the shim and the verbs); the
        # vault is data and holds a poisoned `bin/` that nothing may reach.
        base, shim = fx.engine, str(fx.engine / "plainkeep")
        core_in_engine = fx.install_core(binary)
        vault = _clone_vault(fx.root, tmps, "pk-shim-vault-")

        # 1-2. A caller-supplied PLAINKEEP_HOME is the vault, whichever path took the dispatch: the
        # shim script lives in the ENGINE and every read/write must happen in `vault`.
        for mode, extra in (("require", {"PLAINKEEP_CORE_BIN": core_in_engine}), ("off", {})):
            r = _run([shim, "v_home"], _shim_env(vault, PLAINKEEP_CORE=mode, **extra))
            check(f"[shim] caller PLAINKEEP_HOME preserved through the shim (mode={mode})",
                  r.returncode == 0 and r.stdout.strip() == str(vault),
                  f"rc={r.returncode} out={r.stdout!r} err={r.stderr!r} want={str(vault)!r}")

        # 2b. TASK 2 — the dual-run proof, at the level of a whole dispatch. `vault` carries a
        # complete-looking `bin/v_ok/run.py` that prints POISONED-VAULT-ENGINE, and a
        # `bin/lib/guardrail.py` that exits 5. Neither is reached: the engine's `v_ok` is.
        for mode, extra in (("require", {"PLAINKEEP_CORE_BIN": core_in_engine}), ("off", {})):
            r = _run([shim, "v_ok"], _shim_env(vault, PLAINKEEP_CORE=mode, **extra))
            check(f"[shim] the vault's own bin/ is DATA, never the engine (mode={mode})",
                  r.returncode == 0 and r.stdout == "ok\n"
                  and not _poison_reached(r.stdout, r.stderr)
                  and (vault / "bin" / "v_ok" / "run.py").is_file(),
                  f"rc={r.returncode} out={r.stdout!r} err={r.stderr!r} "
                  f"vault_bin_still_there={(vault / 'bin' / 'v_ok' / 'run.py').is_file()}")

        # 2c. TASK 2 — PLAINKEEP_ENGINE is an OUTPUT. A caller who exports a hostile value has it
        # REPLACED, and the replacement is what the verb sees. Asserted against the verb's OWN
        # `__file__`-derived answer rather than against a path this test computed, so it cannot pass
        # by both sides sharing one mistake.
        evil = str(Path(tempfile.gettempdir()) / "pk-evil-engine")
        for mode, extra in (("require", {"PLAINKEEP_CORE_BIN": core_in_engine}), ("off", {})):
            r = _run([shim, "v_engine"],
                     _shim_env(vault, PLAINKEEP_CORE=mode, PLAINKEEP_ENGINE=evil, **extra))
            lines = r.stdout.strip().split("\n")
            exported = lines[0] if lines else ""
            selflocated = lines[1] if len(lines) > 1 else ""
            check(f"[shim] a hostile inherited PLAINKEEP_ENGINE is REPLACED, not honoured "
                  f"(mode={mode})",
                  r.returncode == 0 and exported == selflocated == str(base) and exported != evil,
                  f"rc={r.returncode} exported={exported!r} selflocated={selflocated!r} "
                  f"evil={evil!r} want={str(base)!r} err={r.stderr!r}")

        # 3. REWRITTEN IN TASK 1b, and the rewrite is the point. This check used to assert
        # "direct binary invocation derives PLAINKEEP_HOME from execPath" — the behavior ADR-014 D2
        # deletes, because for an INSTALLED `~/.local/bin/plainkeep-core` that derivation resolves to
        # `~` and every guarded write then lands wherever the binary happens to live. A binary sitting
        # inside an engine, invoked directly with nothing selecting a root, must REFUSE.
        withcore = _clone_engine(base, tmps, "pk-shim-core-")
        core_dst = withcore / ".local" / "bin" / "plainkeep-core"
        core_dst.parent.mkdir(parents=True, exist_ok=True)
        if not core_dst.exists():
            shutil.copy2(binary, core_dst)
        core_dst.chmod(0o755)
        nowhere = _nowhere(tmps)
        r = _run([str(core_dst), "v_home"], _shim_env(None), cwd=nowhere)
        # Asserted by SIDE EFFECT as well as by exit code: the refusal must leave no audit log
        # behind, which is the "no audit-log append before a root is validated" half of the contract
        # and the half an exit code cannot show.
        check("[shim] direct binary invocation REFUSES — no vault is derived from execPath",
              r.returncode == 2 and "no vault selected" in r.stderr
              and not (withcore / ".logs").exists(),
              f"rc={r.returncode} out={r.stdout!r} err={r.stderr!r} "
              f"logs={(withcore / '.logs').exists()}")

        # 4. A COPIED ENGINE with no PLAINKEEP_CORE_BIN: `auto` must find the copied engine's OWN
        # core rather than the original's. `base` deliberately has no core of its own in this check —
        # `install_core` put one in `fx.engine`, and `withcore` is a clone that carries it — so the
        # dispatch succeeding through the clone is evidence the default core location is
        # ENGINE-relative and followed the copy.
        r = _run([str(withcore / "plainkeep"), "v_home"], _shim_env(vault, PLAINKEEP_CORE="auto"))
        check("[shim] copied engine dispatches through its OWN copied core",
              r.returncode == 0 and r.stdout.strip() == str(vault),
              f"rc={r.returncode} out={r.stdout!r} err={r.stderr!r} want={str(vault)!r}")

        # 4b. ...and with NOTHING selecting a root, the copied engine refuses instead of adopting
        # some vault. `$0` names the ENGINE and only the engine.
        r = _run([str(withcore / "plainkeep"), "v_home"], _shim_env(None, PLAINKEEP_CORE="auto"),
                 cwd=nowhere)
        check("[shim] copied engine with no selection REFUSES — $0 names code, never data",
              r.returncode == 2 and "no vault selected" in r.stderr,
              f"rc={r.returncode} out={r.stdout!r} err={r.stderr!r}")

        # 5. No core installed at all. `nocore` is a clone of the engine with its `.local/` removed.
        nocore = _clone_engine(base, tmps, "pk-shim-nocore-")
        shutil.rmtree(nocore / ".local", ignore_errors=True)
        nocore_shim = str(nocore / "plainkeep")
        r = _run([nocore_shim, "v_ok"], _shim_env(vault, PLAINKEEP_CORE="auto"))
        check("[shim] absent core · auto falls back to the bash floor, silently",
              r.returncode == 0 and r.stdout == "ok\n" and r.stderr == "",
              f"rc={r.returncode} out={r.stdout!r} err={r.stderr!r}")
        r = _run([nocore_shim, "v_ok"], _shim_env(vault, PLAINKEEP_CORE="require"))
        check("[shim] absent core · require fails loudly (exit 1), never silently falls back",
              r.returncode == 1 and r.stdout == "" and "require" in r.stderr,
              f"rc={r.returncode} out={r.stdout!r} err={r.stderr!r}")
        r = _run([nocore_shim, "v_ok"], _shim_env(vault, PLAINKEEP_CORE="off"))
        check("[shim] absent core · off takes the bash floor",
              r.returncode == 0 and r.stdout == "ok\n",
              f"rc={r.returncode} out={r.stdout!r} err={r.stderr!r}")

        # 6. Executable but DEAD: passes `-x`, fails --core-selftest. The failure mode the probe
        # exists for (wrong platform, truncated download, missing dylib).
        broken = _clone_engine(nocore, tmps, "pk-shim-broken-")
        bpath = broken / ".local" / "bin" / "plainkeep-core"
        bpath.parent.mkdir(parents=True, exist_ok=True)
        bpath.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
        bpath.chmod(0o755)
        r = _run([str(broken / "plainkeep"), "v_ok"], _shim_env(vault, PLAINKEEP_CORE="auto"))
        warn_lines = [ln for ln in r.stderr.split("\n") if ln]
        check("[shim] broken-but-executable core · auto = bash floor + EXACTLY ONE warning line",
              r.returncode == 0 and r.stdout == "ok\n" and len(warn_lines) == 1
              and "liveness probe" in warn_lines[0],
              f"rc={r.returncode} out={r.stdout!r} err={r.stderr!r} warn_lines={len(warn_lines)}")
        r = _run([str(broken / "plainkeep"), "v_ok"], _shim_env(vault, PLAINKEEP_CORE="require"))
        check("[shim] broken-but-executable core · require fails loudly (exit 1)",
              r.returncode == 1 and r.stdout == "" and "no live core binary" in r.stderr,
              f"rc={r.returncode} out={r.stdout!r} err={r.stderr!r}")

        # 7. Mode pinning, positively both ways: a tracer core that IS live. `off` must never even
        # probe it (no marker file at all); auto/require must EXEC it with the verb's argv. This is
        # what makes "the matrix ran the floor" a claim with evidence behind it.
        for mode, want_exec in (("off", False), ("auto", True), ("require", True)):
            traced = _clone_engine(nocore, tmps, f"pk-shim-trace-{mode}-")
            marker = traced / "_core_was_used"
            tracer = traced / "_fake_core"
            _write_tracer(tracer, marker)
            r = _run([str(traced / "plainkeep"), "v_ok"],
                     _shim_env(vault, PLAINKEEP_CORE=mode, PLAINKEEP_CORE_BIN=str(tracer)))
            got = marker.read_text(encoding="utf-8") if marker.exists() else ""
            check(f"[shim] mode pinning · PLAINKEEP_CORE={mode} "
                  f"{'execs the core' if want_exec else 'never touches the core'}",
                  ("v_ok" in got) == want_exec and (r.stdout == "" if want_exec else r.stdout == "ok\n"),
                  f"rc={r.returncode} out={r.stdout!r} err={r.stderr!r} marker={got!r}")

        # 8. An unrecognized mode is a usage error, not a silent choice.
        r = _run([shim, "v_ok"], _shim_env(vault, PLAINKEEP_CORE="Require"))
        check("[shim] unknown PLAINKEEP_CORE value is a loud usage error (exit 2)",
              r.returncode == 2 and r.stdout == "" and "unknown PLAINKEEP_CORE" in r.stderr,
              f"rc={r.returncode} out={r.stdout!r} err={r.stderr!r}")

        # ----------------------------------------------------------------------------------------
        # 9. TASK 2 — DISJOINTNESS (ADR-014 D3), refused in BOTH directions and in BOTH dispatchers.
        #
        # Task 1b wrote the rule and could not turn it on: while the engine was `<vault>/bin`, every
        # existing vault violated it. It becomes satisfiable in this task, so it is enforced in this
        # task. Exit 5 (`EXIT_DENY`) — a refusal about WHERE, like the walled-off/cloud-sync verdict
        # it sits beside — never exit 2, which means "nothing was selected".
        #
        # Three shapes, and the third is the one a single naive `startswith` gets wrong:
        #   same      — the data root IS the engine root
        #   inside    — the data root is UNDER the engine root
        #   contains  — the engine root is under the DATA root (the Phase 1 shape, i.e. every vault
        #               that still has a copy of the engine in it)
        # ----------------------------------------------------------------------------------------
        overlap_engine = _clone_engine(base, tmps, "pk-shim-overlap-")
        # A marked vault AT the engine root, and one marked ancestor CONTAINING it, so each case is
        # refused for the disjointness reason and not for a missing marker.
        _mark_vault(overlap_engine)
        container = overlap_engine.parent
        _mark_vault(container)
        inside = overlap_engine / "bin"          # a real directory under the engine
        _mark_vault(inside)
        overlap_cases = (
            ("the data root IS the engine root", overlap_engine, "IS the engine tree"),
            ("the data root is INSIDE the engine tree", inside, "is inside the engine tree"),
            ("the engine tree is INSIDE the data root", container, "is inside it"),
        )
        for label, home, expect in overlap_cases:
            for mode, extra in (("off", {}),
                                ("require", {"PLAINKEEP_CORE_BIN": str(
                                    overlap_engine / ".local" / "bin" / "plainkeep-core")})):
                r = _run([str(overlap_engine / "plainkeep"), "v_ok"],
                         _shim_env(home, PLAINKEEP_CORE=mode, **extra), cwd=nowhere)
                check(f"[engine] disjointness · {label} → EXIT_DENY (5) (mode={mode})",
                      r.returncode == 5 and expect in r.stderr and r.stdout == "",
                      f"rc={r.returncode} out={r.stdout!r} err={r.stderr!r} want={expect!r}")
        # ...and the refusal is byte-identical across the two dispatchers, which is the whole reason
        # discovery is ONE shared module rather than a port and a differential.
        f_out = _run([str(overlap_engine / "plainkeep"), "v_ok"],
                     _shim_env(overlap_engine, PLAINKEEP_CORE="off"), cwd=nowhere)
        c_out = _run([str(overlap_engine / ".local" / "bin" / "plainkeep-core"), "v_ok"],
                     _shim_env(overlap_engine), cwd=nowhere)
        check("[engine] disjointness · floor and core refuse byte-identically",
              (f_out.returncode, f_out.stdout, f_out.stderr)
              == (c_out.returncode, c_out.stdout, c_out.stderr),
              f"floor=({f_out.returncode},{f_out.stdout!r},{f_out.stderr!r}) "
              f"core=({c_out.returncode},{c_out.stdout!r},{c_out.stderr!r})")

        # 10. TASK 2 — an INCOMPLETE engine refuses BEFORE it dispatches, in both dispatchers.
        #
        # This is the inverted `require_engine`: Task 1b asked whether the VAULT carried the engine,
        # which stopped being a sensible question the moment the engine moved out. The seam is the
        # same one (both dispatchers run it, so the two refusals are one string), the subject is now
        # the code. Driven by REMOVING a file the probe names — a guard that is never exercised
        # against the failure it describes is a green test of nothing.
        for missing in ("bin/lib/resolver.py", "VERSION"):
            hurt = _clone_engine(base, tmps, "pk-shim-incomplete-")
            (hurt / missing).unlink()
            for mode, extra in (("off", {}),
                                ("require", {"PLAINKEEP_CORE_BIN": str(
                                    hurt / ".local" / "bin" / "plainkeep-core")})):
                r = _run([str(hurt / "plainkeep"), "v_ok"],
                         _shim_env(vault, PLAINKEEP_CORE=mode, **extra))
                check(f"[engine] an engine missing {missing} REFUSES before dispatch (mode={mode})",
                      r.returncode == 2 and "is incomplete" in r.stderr and missing in r.stderr
                      and r.stdout == "",
                      f"rc={r.returncode} out={r.stdout!r} err={r.stderr!r}")

        # 11. TASK 2 — VERB DISCOVERY DOES NOT READ THE REPOSITORY.
        #
        # The gate asks for this to be PROVEN, not inferred from a passing test that would also pass
        # if the repo tree were being read. Two independent proofs, because either one alone is
        # weak:
        #
        #   (a) POSITIVE-BY-ABSENCE. The installed engine has `v_ok` DELETED from it while the
        #       repository's `bin/` still carries 35 real verbs including `capture` and `help`.
        #       Dispatching `capture` — a verb that unquestionably exists in the repo — must answer
        #       "unknown verb" (exit 4). A dispatcher reading the repo tree resolves it and exits 0.
        #       This proof cannot pass for the wrong reason: there is no arrangement in which the
        #       repo is read AND `capture` is unknown.
        #
        #   (b) UNREADABLE SOURCE. A full copy of the repository's engine-owned tree is made, an
        #       engine is installed FROM it, and then every directory in the copy's `bin/` is
        #       chmod 0000. Any `open()` under it now fails with EACCES. The dispatch still has to
        #       succeed. (The copy stands in for the repository: making the real `bin/` unreadable
        #       mid-suite would break every other suite in the batch and leave the checkout wedged
        #       if this process died between the chmod and its restore.)
        stripped = _clone_engine(base, tmps, "pk-shim-norepo-")
        shutil.rmtree(stripped / "bin" / "v_ok")
        r = _run([str(stripped / "plainkeep"), "capture", "x"],
                 _shim_env(vault, PLAINKEEP_CORE="off"))
        repo_has = (ENGINE_SRC / "capture" / "run.py").is_file()
        check("[engine] no-repo-read (a) · a verb the REPO has but the installed engine does not "
              "is unknown (exit 4)",
              repo_has and r.returncode == 4 and "capture" in r.stderr,
              f"rc={r.returncode} out={r.stdout!r} err={r.stderr!r} repo_has_capture={repo_has}")
        r = _run([str(stripped / ".local" / "bin" / "plainkeep-core"), "capture", "x"],
                 _shim_env(vault))
        check("[engine] no-repo-read (a) · ...and the core answers the same",
              repo_has and r.returncode == 4 and "capture" in r.stderr,
              f"rc={r.returncode} out={r.stdout!r} err={r.stderr!r}")
        _no_repo_read_unreadable_source(binary, tmps, vault)

        # 12. TASK 2 — the ownership manifest is present in the installed tree, ENUMERATED. A table
        # nothing executes is paperwork: this installs from the real repository through the real
        # installer and checks every path the manifest claims, plus the counts, so a verb quietly
        # dropping its `cmd.json` reddens here rather than at a user's shell.
        _installed_manifest_checks(tmps)
    finally:
        fx.cleanup()
        for d in tmps:
            shutil.rmtree(d, ignore_errors=True)


def _no_repo_read_unreadable_source(binary: str, tmps: list[Path], vault: Path) -> None:
    """Proof (b): install an engine from a source tree, then make that source tree UNREADABLE and
    dispatch anyway.

    Restored in a `finally` whatever happens, because a 0000 directory that outlives the run is a
    temp dir nothing can delete. The copy is what gets locked, never the repository — see the note
    at the call site."""
    src = Path(os.path.realpath(tempfile.mkdtemp(prefix="pk-norepo-src-")))
    tmps.append(src)
    inst = Path(os.path.realpath(tempfile.mkdtemp(prefix="pk-norepo-inst-")))
    tmps.append(inst)
    ignore = shutil.ignore_patterns("__pycache__", "*.pyc")
    for rel in enginetree.OWNED_TREES:
        d = src / rel
        d.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(REPO / rel, d, ignore=ignore, symlinks=True)
    for rel in enginetree.OWNED_FILES:
        shutil.copy2(REPO / rel, src / rel)
    (src / ".local" / "bin").mkdir(parents=True, exist_ok=True)
    shutil.copy2(binary, src / ".local" / "bin" / "plainkeep-core")
    env = _shim_env(vault, PLAINKEEP_ENGINE_HOME=inst, PLAINKEEP_CORE="off")
    r = _run([PY, str(src / "bin" / "lib" / "enginetree.py"), "--install", str(src)], env)
    if r.returncode != 0:
        check("[engine] no-repo-read (b) · install from a source checkout", False,
              f"rc={r.returncode} out={r.stdout!r} err={r.stderr!r}")
        return
    installed = inst / VERSIONS_DIRNAME / "current"
    locked = [d for d in [src / "bin", *(src / "bin").rglob("*")] if d.is_dir()]
    try:
        for d in sorted(locked, key=lambda p: len(p.parts), reverse=True):
            d.chmod(0o000)
        unreadable = not os.access(src / "bin" / "lib", os.R_OK)
        out = _run([str(installed / "plainkeep"), "help"], env)
        core = _run([str(installed / ".local" / "bin" / "plainkeep-core"), "help"], env)
        check("[engine] no-repo-read (b) · both dispatchers work with the SOURCE tree's bin/ "
              "chmod 0000",
              unreadable and out.returncode == 0 and core.returncode == 0
              and "plainkeep capture" in out.stdout and out.stdout == core.stdout,
              f"source_unreadable={unreadable} floor=(rc={out.returncode},err={out.stderr[:200]!r}) "
              f"core=(rc={core.returncode},err={core.stderr[:200]!r})")
    finally:
        for d in sorted(locked, key=lambda p: len(p.parts)):
            try:
                d.chmod(0o755)
            except OSError:
                pass


# The ownership manifest, as COUNTS as well as paths. Pinned like EXPECTED_CATALOG_INVOCATIONS and
# for the same reason: a per-verb loop shrinks silently when a verb directory disappears, because
# there is one fewer thing to iterate. Raise these in the commit that adds a verb or a lib module.
#
# The plan section's prose says "×34" verbs and "×19" lib modules. Both were already stale when this
# task started — measured at BASE 79b6b6e: 35 verb directories (35 run.py, 35 cmd.json) and 23 lib
# modules, which Task 1a/1b/1c grew (vaultreg, vaultroot, wall) and Task 2 grew again
# (enginetree). Task 3 adds `pluginenv` — 25. The numbers below are what the tree HAS, not what the
# plan remembered.
EXPECTED_ENGINE_VERBS = 35
EXPECTED_ENGINE_LIB_MODULES = 26


def _installed_manifest_checks(tmps: list[Path]) -> None:
    """Install the REAL engine from the repository and check every path the ownership table assigns
    to it is present in the installed tree — and that the running dispatcher resolves FROM it."""
    inst = Path(os.path.realpath(tempfile.mkdtemp(prefix="pk-manifest-")))
    tmps.append(inst)
    env = dict(os.environ, PLAINKEEP_ENGINE_HOME=str(inst))
    env.pop("PLAINKEEP_HOME", None)
    r = _run([PY, str(ENGINE_SRC / "lib" / "enginetree.py"), "--install", str(REPO)], env)
    check("[manifest] the real repository installs as an engine tree",
          r.returncode == 0, f"rc={r.returncode} out={r.stdout!r} err={r.stderr!r}")
    if r.returncode != 0:
        return
    root = Path(os.path.realpath(inst / VERSIONS_DIRNAME / "current"))

    # (1) The enumerated set, path by path — the four the ownership table assigns to the engine
    # beyond `bin/**` and VERSION included, because those are the ones no task had moved before this
    # one and the ones an installer is most likely to forget.
    for rel in ("VERSION", "plainkeep", "bin/lib/resolver.py", "bin/lib/enginetree.py",
                "bin/ui/version.txt", "bin/share/worker/worker.js",
                "templates/verb/run.py", "templates/verb/cmd.json",
                "skills/operate-plainkeep/SKILL.md"):
        check(f"[manifest] installed tree carries {rel}", (root / rel).is_file(),
              f"missing under {root}")
    ray = sorted((root / "frontends" / "raycast").glob("*.sh"))
    check("[manifest] installed tree carries frontends/raycast/*.sh", len(ray) >= 4,
          f"{[p.name for p in ray]}")

    # (2) The COUNTS, pinned. Every verb directory carries BOTH sidecars, and every lib module made
    # the trip: an installer that copied `bin/` but skipped a dotted or oddly-named child would pass
    # a path-by-path spot check and fail this.
    verbs = sorted(d for d in (root / "bin").iterdir() if d.is_dir() and d.name != "lib")
    with_run = [d for d in verbs if (d / "run.py").is_file()]
    with_cmd = [d for d in verbs if (d / "cmd.json").is_file()]
    check(f"[manifest] {EXPECTED_ENGINE_VERBS} verb dirs, each with run.py AND cmd.json",
          len(verbs) == len(with_run) == len(with_cmd) == EXPECTED_ENGINE_VERBS,
          f"dirs={len(verbs)} run.py={len(with_run)} cmd.json={len(with_cmd)} "
          f"expected={EXPECTED_ENGINE_VERBS}; if you ADDED a verb, raise EXPECTED_ENGINE_VERBS in "
          f"the same commit — never edit it to make this pass")
    libs = sorted((root / "bin" / "lib").glob("*.py"))
    check(f"[manifest] {EXPECTED_ENGINE_LIB_MODULES} bin/lib modules installed",
          len(libs) == EXPECTED_ENGINE_LIB_MODULES,
          f"got {len(libs)}: {[p.name for p in libs]}")

    # (3) RESOLVED FROM IT, which is the half a presence check cannot give. The installed engine's
    # own resolver is asked where `capture` lives, with PLAINKEEP_HOME pointed at a throwaway vault:
    # the answer must be inside the installed tree and nowhere near the repository.
    vault = Path(os.path.realpath(tempfile.mkdtemp(prefix="pk-manifest-vault-")))
    tmps.append(vault)
    _mark_vault(vault)
    renv = dict(env, PLAINKEEP_HOME=str(vault))
    got = _run([PY, str(root / "bin" / "lib" / "resolver.py"), "capture"], renv)
    resolved = got.stdout.strip()
    check("[manifest] the installed engine resolves its verbs FROM the installed tree",
          got.returncode == 0 and resolved.startswith(str(root))
          and not resolved.startswith(str(REPO) + "/"),
          f"rc={got.returncode} resolved={resolved!r} root={str(root)!r}")

    # (4) The engine's own VERSION is what the manifest reports, read as `<engine>/VERSION` —
    # `manifest.py:VERSION_FILE` is `BIN.parent / "VERSION"`, which is the line the plan section
    # calls load-bearing: move `bin/` without it and every plainkeep.json reports 0.0.0.
    #
    # `-I` (isolated) and a deliberately hostile cwd, together, are the check. Without `-I` the
    # spawned interpreter puts its CWD at sys.path[0], ahead of the `<engine>/bin` this passes in —
    # and `bin/lib/` has no `__init__.py` (a namespace package) while `test/lib/` has one (a regular
    # package). Python's path scan remembers a namespace portion and keeps looking, and the first
    # REGULAR package wins, so from `test/` the import resolved to the SUITE's own `lib` and the check
    # failed with `cannot import name 'manifest' from 'lib'` — a false red about the engine, produced
    # entirely by where the contributor happened to be standing (`cd test && python3 run_all.py`,
    # which test/README.md presents as normal, exited 1; the repo root exited 0).
    #
    # So cwd is PINNED to `test/`, the directory that does the shadowing: this check must read the
    # INSTALLED engine and nothing else, and running it from the one place that can prove that is how
    # the property stays proved. Drop the `-I` and this goes red from every directory, not just one.
    ver = _run([PY, "-I", "-c",
                "import sys;sys.path.insert(0,sys.argv[1]);"
                "from lib import manifest;print(manifest._engine_version())",
                str(root / "bin")], renv, cwd=str(REPO / "test"))
    check("[manifest] manifest.py reads <engine>/VERSION, not 0.0.0",
          ver.stdout.strip() == ENGINE_VERSION,
          f"got {ver.stdout.strip()!r} want {ENGINE_VERSION!r} err={ver.stderr[:200]!r}")
    # ...and the same question asked from the repo root gives the same answer. The bug was that these
    # two disagreed; one of them alone cannot show that they now agree.
    ver_root = _run([PY, "-I", "-c",
                     "import sys;sys.path.insert(0,sys.argv[1]);"
                     "from lib import manifest;print(manifest._engine_version())",
                     str(root / "bin")], renv, cwd=str(REPO))
    check("[manifest] ...and the answer does not depend on the caller's cwd",
          ver_root.stdout.strip() == ver.stdout.strip() == ENGINE_VERSION,
          f"from test/={ver.stdout.strip()!r} from repo root={ver_root.stdout.strip()!r} "
          f"want {ENGINE_VERSION!r} err={ver_root.stderr[:200]!r}")


# --------------------------------------------------------------------------------------------------
# Catalog runner
# --------------------------------------------------------------------------------------------------

def _load_catalogs() -> list[tuple[str, dict]]:
    out = []
    for path in sorted(CATALOG_DIR.glob("*.json")):
        out.append((path.stem, json.loads(path.read_text(encoding="utf-8"))))
    return out


def _run_case(binary: str, catalog: str, case: dict) -> None:
    # Build inside a guard so a single case's authoring error (bad fixture, unknown comparator) is a
    # localized FAIL, not a traceback that nukes the whole permanent oracle (M-2). Fixture.__init__
    # already cleans up its own temp vault on a build failure.
    try:
        fx = Fixture(case["fixture"])
    except Exception as e:
        check(f"[{catalog}] {case['name']} · fixture-build", False, f"exception: {e!r}")
        return
    try:
        for i, inv in enumerate(case["invocations"]):
            label = inv.get("api") or inv.get("verb") or inv["compare"]
            args = inv.get("args") or []
            # The signal cells all share one verb name, so the label alone would not say WHICH cell was
            # skipped — and an unattributable skip is the thing this gate must not become.
            detail_label = f"{label}({args[0]})" if args and isinstance(args[0], str) else label
            name = f"[{catalog}] {case['name']} · {inv['compare']}:{detail_label} #{i}"
            why = _crash_noise_skip(inv)
            if why is not None:
                skip(name, why)
                continue
            try:
                cmp = COMPARATORS[inv["compare"]]
                ok, detail = cmp(binary, fx, inv)
            except Exception as e:
                ok, detail = False, f"exception: {e!r}"
            check(name, ok, detail)
    finally:
        fx.cleanup()


def _discover_binary() -> str | None:
    cand = os.environ.get("PLAINKEEP_CORE_BIN") or str(REPO / ".local" / "bin" / "plainkeep-core")
    p = Path(cand)
    if p.is_file() and os.access(cand, os.X_OK):
        return cand
    return None


def main() -> int:
    binary = _discover_binary()
    if binary is None:
        if os.environ.get("PLAINKEEP_REQUIRE_CORE") == "1":
            print(f"{RED}{BOLD}{SKIP_LINE}{RESET}", file=sys.stderr)
            print(f"{RED}PLAINKEEP_REQUIRE_CORE=1 — a missing core binary is a FAILURE.{RESET}", file=sys.stderr)
            return 1
        print(f"{YELLOW}{BOLD}{SKIP_LINE}{RESET}")
        return 0

    # PLAINKEEP_PARITY_ONLY=<substring> runs only the cases whose "<catalog>/<name>" contains it (the
    # shim block matches on "shim"). It exists because the signal cases kill real children, which on
    # macOS writes a crash report and pops a user notification per fault signal — so iterating on one
    # case must not mean re-running all of them. A filtered run is announced loudly and is NEVER a
    # gate: the summary says so, in place of the usual result line.
    only = os.environ.get("PLAINKEEP_PARITY_ONLY")
    if only:
        print(f"{YELLOW}{BOLD}FILTERED RUN — PLAINKEEP_PARITY_ONLY={only!r}. "
              f"This is a development aid, NOT a gate.{RESET}\n")
        # STRUCTURAL, not typographic. run_all.py reduces a suite to PASS/FAIL on its exit status, so
        # a filtered run used to reach its summary as an unqualified PASS no matter how loudly this
        # said otherwise (quality review r2, N2). SUITE_NOTE lines are parsed by run_all.py and
        # reprinted under that suite's summary line, so the qualification travels with the verdict.
        suite_note(f"FILTERED RUN (PLAINKEEP_PARITY_ONLY={only!r}) — NOT A GATE")

    catalogs = _load_catalogs()
    ncases = 0
    ninvocations = 0
    for catalog, doc in catalogs:
        for case in doc.get("cases", []):
            if only and only not in f"{catalog}/{case['name']}":
                continue
            ncases += 1
            ninvocations += len(case.get("invocations", []))
            _run_case(binary, catalog, case)
    ncatalog_checks = len(results)
    if not only or only in "shim":
        _shim_checks(binary)
    nshim = len(results) - ncatalog_checks

    # THE ACCOUNTING INVARIANT (quality review r2, M-8). Two clauses, because one of them alone would
    # have been theatre:
    #
    #   (1) run + skipped == DECLARED. Every invocation the catalogs declare lands in exactly one pile
    #       — a check or a visible skip. Catches a fixture that failed to build (that case contributes
    #       one FAIL instead of its N invocations) and any double-counting.
    #   (2) DECLARED == EXPECTED_CATALOG_INVOCATIONS, a pinned number. This is the clause that closes
    #       M-8's actual hazard, and clause (1) does NOT: deleting a case lowers the declared count and
    #       the run count TOGETHER, so a self-consistency check balances perfectly while coverage
    #       quietly shrinks. Only an expectation from outside the run can notice that.
    #
    # Until this existed, "195 ran + 8 skipped = 203 declared" was arithmetic a HUMAN did across two
    # log files — the same shape as every defect this suite has caught for passing on the wrong
    # grounds. CI runs every cell on Linux either way, so what this protects is precisely the local
    # macOS run that the crash-noise gate created. It is one assertion about the suite's own
    # bookkeeping, deliberately not a skip-category design (M-4 stays batched).
    #
    # The pin is skipped for a FILTERED run, which declares a subset by construction and is already
    # "not a gate".
    accounted = ncatalog_checks + len(skipped)
    balanced = accounted == ninvocations
    pinned = only is not None or ninvocations == EXPECTED_CATALOG_INVOCATIONS
    detail = ""
    if not balanced:
        detail = (
            f"the catalogs declare {ninvocations} invocation(s) but {ncatalog_checks} ran and "
            f"{len(skipped)} were skipped, totalling {accounted}. Something was neither run nor "
            f"skipped: look for a 'fixture-build' FAIL above. "
        )
    if not pinned:
        detail += (
            f"the catalogs now declare {ninvocations} invocation(s), expected "
            f"{EXPECTED_CATALOG_INVOCATIONS}. If you ADDED cases, raise EXPECTED_CATALOG_INVOCATIONS "
            f"in this file and say so in the commit. If you did not add any, coverage has SHRUNK — "
            f"find what stopped being declared. Never edit the number to make this pass."
        )
    check(
        f"[accounting] all {ninvocations} declared catalog invocations are accounted for "
        f"({ncatalog_checks} run + {len(skipped)} skipped)"
        + ("" if only else f", and the catalogs still declare {EXPECTED_CATALOG_INVOCATIONS}"),
        balanced and pinned,
        detail,
    )

    print(f"{BOLD}core-parity differential oracle — {ncatalog_checks} checks across {ncases} "
          f"catalog cases + {nshim} shim/root-discovery checks + 1 accounting invariant "
          f"(binary: {binary}){RESET}\n")
    passed = sum(1 for _, ok, _ in results if ok)
    for name, ok, detail in results:
        mark = f"{GREEN}PASS{RESET}" if ok else f"{RED}FAIL{RESET}"
        print(f"  {mark} {name}" + (f"\n       {DIM}{detail}{RESET}" if (detail and not ok) else ""))
    # Skips print in the SAME list as the results and are counted on the summary line, so a run that
    # did not exercise a cell cannot be read off as a run that exercised it and liked what it saw.
    for name, why in skipped:
        print(f"  {YELLOW}{BOLD}SKIP{RESET} {name}\n       {YELLOW}{why}{RESET}")
    failed = len(results) - passed
    label = f"{YELLOW}Result (FILTERED — not a gate):{RESET}" if only else f"{BOLD}Result:{RESET}"
    nskip = len(skipped)
    print(f"\n{label} {GREEN}{passed} passed{RESET}, "
          f"{(RED if failed else DIM)}{failed} failed{RESET}, "
          f"{(YELLOW if nskip else DIM)}{nskip} skipped{RESET}, {len(results)} checks run")
    if nskip:
        print(f"{YELLOW}{BOLD}{nskip} fault-signal cell(s) NOT RUN on this machine — this run does "
              f"not gate them. Re-run with PLAINKEEP_PARITY_FAULT_SIGNALS=1 before a release, or let "
              f"CI (PLAINKEEP_REQUIRE_CORE=1) do it.{RESET}")
        suite_note(f"{nskip} fault-signal cell(s) NOT RUN (macOS crash-report noise) — "
                   f"re-run with PLAINKEEP_PARITY_FAULT_SIGNALS=1 to gate them")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
