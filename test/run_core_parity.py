#!/usr/bin/env python3
"""
run_core_parity.py — the PERMANENT, Python-owned differential oracle proving the compiled TS core
binary reproduces the untouched Python implementations byte-for-byte (proposal §3; advisor A5/B2).

Phase 1 drives the RESOLVER: it loads the language-neutral case catalogs under
test/cases/core-parity/, builds a throwaway fixture vault per case (engine bin/ = a COPY of
bin/lib/resolver.py plus synthetic verb dirs, so the Python side's file-derived ENGINE_BIN and the
binary's $PLAINKEEP_HOME/bin coincide), then for every invocation runs BOTH sides and compares exit
code + exact stdout:
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
case the same line is an error and the suite exits 1. SKIP is visible, never a silent PASS.
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
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PY = sys.executable
RESOLVER_SRC = REPO / "bin" / "lib" / "resolver.py"
GUARDRAIL_SRC = REPO / "bin" / "lib" / "guardrail.py"
ENGINE_SRC = REPO / "bin"
# The root shim (Task 4). Its `PLAINKEEP_CORE=off` path is the BASH FLOOR — the pre-core dispatcher,
# preserved verbatim — and is the reference side of the dispatcher differential matrix.
PLAINKEEP_SRC = REPO / "plainkeep"
CATALOG_DIR = REPO / "test" / "cases" / "core-parity"
GREEN, RED, YELLOW, DIM, BOLD, RESET = (
    "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[1m", "\033[0m",
)

SKIP_LINE = "SKIP core-parity: no core binary (build with: cd cli && bun run build)"

results: list[tuple[str, bool, str]] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    results.append((name, bool(cond), detail))


# --------------------------------------------------------------------------------------------------
# Fixture vault builder (catalog-agnostic — reused by every future parity catalog)
# --------------------------------------------------------------------------------------------------

_VERB_FILES = {
    # A minimal but valid cmd.json (guardrail catalogs will supply richer sidecars via the same hook).
    "cmd.json": lambda verb: json.dumps({"verb": verb, "summary": f"parity {verb}", "risk": "read"}),
    "run.py": lambda verb: "def main(argv):\n    return 0\n",
}


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


class Fixture:
    """A built fixture vault plus the env needed to invoke either side against it.

    Layout: <vault>/bin/lib/resolver.py (copy) + <vault>/bin/<verb>/ (engine verbs),
    <vault>/plugins/<pack>/<verb>/, PLAINKEEP_PATH roots under <vault>/_roots/<ref> (absolute) or
    <vault>/_home/<ref> (referenced as ~/<ref>). The vault root is REALPATH-canonical so Python's
    ENGINE_BIN `.resolve()` is a no-op and matches the binary's unresolved $PLAINKEEP_HOME/bin.
    """

    def __init__(self, spec: dict):
        # mkdtemp first, then guard the rest so a catalog authoring error (bad path:ref, unknown
        # verb location, unregistered file) cleans up its temp vault instead of leaking it (M-2).
        self.root = Path(os.path.realpath(tempfile.mkdtemp(prefix="pk-parity-")))
        try:
            self._build(spec)
        except Exception:
            shutil.rmtree(self.root, ignore_errors=True)
            raise

    def _build(self, spec: dict) -> None:
        self.home = self.root / "_home"
        self.home.mkdir()
        self.resolver_py = self.root / "bin" / "lib" / "resolver.py"
        self.resolver_py.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(RESOLVER_SRC, self.resolver_py)

        # Completion-catalog addition (Task 5): `engine_from_repo` installs REAL engine verbs from
        # the repo's bin/ into the fixture, together with the whole bin/lib/ they import. The
        # completion catalog needs it because its reference side is the bash floor RUNNING
        # bin/__complete/run.py: a synthetic stub cannot produce the candidates being compared, and a
        # hand-written copy of the completion brain would be the drift this oracle exists to catch.
        # Only the named verb dirs are installed, so a case still controls its own verb surface.
        from_repo = spec.get("engine_from_repo", [])
        if from_repo:
            ignore = shutil.ignore_patterns("__pycache__")
            shutil.copytree(ENGINE_SRC / "lib", self.root / "bin" / "lib",
                            dirs_exist_ok=True, ignore=ignore)
            for verb in from_repo:
                src = ENGINE_SRC / verb
                if not src.is_dir():
                    raise ValueError(f"engine_from_repo names a verb the repo has no bin/ dir for: {verb!r}")
                shutil.copytree(src, self.root / "bin" / verb, dirs_exist_ok=True, ignore=ignore)

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

    def _base_dir(self, at: str) -> Path:
        if at == "engine":
            return self.root / "bin"
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
        return e

    def cleanup(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)


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

def _run(cmd: list[str], env: dict) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, env=env, capture_output=True, text=True, encoding="utf-8")


def _compare_cli(binary: str, fx: Fixture, inv: dict) -> tuple[bool, str]:
    env = fx.env(inv.get("path_order"))
    verb = inv["verb"]
    ts = _run([binary, "--core-resolve", verb], env)
    py = _run([PY, str(fx.resolver_py), verb], env)
    ok = ts.returncode == py.returncode and ts.stdout == py.stdout
    detail = (
        f"verb={verb!r} ts=(rc={ts.returncode},out={ts.stdout!r}) "
        f"py=(rc={py.returncode},out={py.stdout!r}) pyerr={py.stderr.strip()!r}"
    )
    return ok, detail


def _compare_api(binary: str, fx: Fixture, inv: dict) -> tuple[bool, str]:
    env = fx.env(inv.get("path_order"))
    api = inv["api"]
    ts = _run([binary, "--core-api", api], env)
    penv = dict(env, PK_RESOLVER_PY=str(fx.resolver_py))
    py = _run([PY, "-c", _PROBE, api], penv)
    ok = ts.returncode == py.returncode and ts.stdout == py.stdout
    detail = (
        f"api={api!r} ts=(rc={ts.returncode},out={ts.stdout!r}) "
        f"py=(rc={py.returncode},out={py.stdout!r}) pyerr={py.stderr.strip()!r}"
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
    ts_root = Path(os.path.realpath(tempfile.mkdtemp(prefix="pk-gate-ts-")))
    py_root = Path(os.path.realpath(tempfile.mkdtemp(prefix="pk-gate-py-")))
    try:
        shutil.copytree(fx.root, ts_root, dirs_exist_ok=True)
        shutil.copytree(fx.root, py_root, dirs_exist_ok=True)
        shutil.copy2(GUARDRAIL_SRC, py_root / "bin" / "lib" / "guardrail.py")
        ts = _run([binary, "--core-gate", verb, *args], _gate_env(fx, inv, ts_root))
        py = _run([PY, str(py_root / "bin" / "lib" / "guardrail.py"), verb, *args],
                  _gate_env(fx, inv, py_root))
        ok_log, log_detail = _compare_log(_read_log(ts_root), _read_log(py_root))
        ok = (ts.returncode == py.returncode and ts.stdout == py.stdout
              and ts.stderr == py.stderr and ok_log)
        detail = (
            f"verb={verb!r} args={args!r} "
            f"ts=(rc={ts.returncode},out={ts.stdout!r},err={ts.stderr!r}) "
            f"py=(rc={py.returncode},out={py.stdout!r},err={py.stderr!r}) log[{log_detail}]"
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
# Note the shape has two halves and only ONE of them belongs here: `flags` are short-circuited before
# the gate and are genuinely not comparable against the floor, while `verbs` (dispatch.ts INTERCEPTS)
# are answered after the gate and MUST stay comparable — byte-parity with the Python verb is what
# makes an interception legitimate — so a verb appearing there must never cause a case to be skipped.
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

def _install_floor(root: Path) -> None:
    """Make a built fixture vault runnable by the bash floor: the shim at the vault root, plus
    guardrail.py next to the resolver.py the Fixture already copied (the floor spawns both)."""
    shutil.copy2(GUARDRAIL_SRC, root / "bin" / "lib" / "guardrail.py")
    dst = root / "plainkeep"
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
    core_root = Path(os.path.realpath(tempfile.mkdtemp(prefix="pk-disp-core-")))
    floor_root = Path(os.path.realpath(tempfile.mkdtemp(prefix="pk-disp-floor-")))
    aliases: list[Path] = []
    try:
        for r in (core_root, floor_root):
            # symlinks=True: a "live" venv fixture is a SYMLINK to the running python3; copying its
            # target instead would produce a python binary that cannot find its own framework.
            shutil.copytree(fx.root, r, dirs_exist_ok=True, symlinks=True)
            _install_floor(r)
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

        core = _run([binary, *argv], core_env)
        floor = _run([str(floor_root / "plainkeep"), *argv], floor_env)

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
        ok = (rc_ok and core.stdout == floor.stdout and err_ok and ok_log and mode_pinned)
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
        detail = (
            f"verb={verb!r} args={args!r} "
            f"core=(rc={core.returncode},out={core.stdout!r},err={core.stderr!r}) "
            f"floor=(rc={floor.returncode},out={floor.stdout!r},err={floor.stderr!r}) "
            f"log[{log_detail}] mode_pinned={mode_pinned} expect={want!r} excludes={nope!r} "
            f"expect_rc={want_rc!r} resolved_rc(core,floor)={exp!r}"
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
# Shim + root-discovery checks (Task 4)
#
# These are NOT TS-vs-Python differentials — they are behavioral assertions about the root `plainkeep`
# shim's own contract, which no catalog case can express because each needs a differently-shaped
# vault (an in-vault core binary, no core at all, a core that lies about being alive). They live here
# rather than in a bun test because the contract is a bash script's, and because the same Fixture
# builder already produces the vaults.
#
# The contract under test (proposal §3 / plan-phase1 "The shim + root-discovery contract"):
#   * a caller-supplied PLAINKEEP_HOME survives the shim AND the binary, unmodified;
#   * invoked directly with no PLAINKEEP_HOME, the binary derives home from its own execPath;
#   * a copied vault dispatches through its own copied core, with no reference back to the original;
#   * no core installed -> auto takes the bash floor SILENTLY, require fails loudly (exit 1), off
#     takes the floor;
#   * a core that is executable but fails --core-selftest -> auto takes the floor after EXACTLY ONE
#     warning line, require fails loudly. A broken core must never poison plainkeep.
# --------------------------------------------------------------------------------------------------

_SHIM_FIXTURE = {
    "verbs": [
        {"at": "engine", "verb": "v_ok", "cmd": {"verb": "v_ok", "risk": "read"},
         "run": "print('ok')\n"},
        {"at": "engine", "verb": "v_home", "cmd": {"verb": "v_home", "risk": "read"},
         "run": "import os\nprint(os.environ.get('PLAINKEEP_HOME', '<unset>'))\n"},
    ],
}


def _shim_env(home: Path | None, **over) -> dict:
    e = dict(os.environ)
    for k in ("PLAINKEEP_CORE", "PLAINKEEP_CORE_BIN", "PLAINKEEP_HOME", "PLAINKEEP_PATH"):
        e.pop(k, None)
    if home is not None:
        e["PLAINKEEP_HOME"] = str(home)
        e["HOME"] = str(home / "_home")
    for k, v in over.items():
        e[k] = str(v)
    return e


def _clone_vault(src: Path, holder: list[Path], prefix: str) -> Path:
    dst = Path(os.path.realpath(tempfile.mkdtemp(prefix=prefix))) / "vault"
    holder.append(dst.parent)
    shutil.copytree(src, dst, symlinks=True)
    return dst


def _shim_checks(binary: str) -> None:
    tmps: list[Path] = []
    try:
        fx = Fixture(_SHIM_FIXTURE)
    except Exception as e:  # a broken fixture must be a localized FAIL, not a traceback
        check("[shim] fixture-build", False, f"exception: {e!r}")
        return
    try:
        _install_floor(fx.root)
        base, shim = fx.root, str(fx.root / "plainkeep")

        # 1-2. A caller-supplied PLAINKEEP_HOME is the vault, whichever path took the dispatch: the
        # shim script lives in `base` but every read/write must happen in `other`.
        other = _clone_vault(base, tmps, "pk-shim-home-")
        for mode, extra in (("require", {"PLAINKEEP_CORE_BIN": binary}), ("off", {})):
            r = _run([shim, "v_home"], _shim_env(other, PLAINKEEP_CORE=mode, **extra))
            check(f"[shim] caller PLAINKEEP_HOME preserved through the shim (mode={mode})",
                  r.returncode == 0 and r.stdout.strip() == str(other),
                  f"rc={r.returncode} out={r.stdout!r} err={r.stderr!r} want={str(other)!r}")

        # 3. Direct binary invocation, no PLAINKEEP_HOME: home is two parents above the REAL path of
        # the executable, so a vault-local copy of the binary finds its own vault.
        withcore = _clone_vault(base, tmps, "pk-shim-core-")
        core_dst = withcore / ".local" / "bin" / "plainkeep-core"
        core_dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(binary, core_dst)
        core_dst.chmod(0o755)
        r = _run([str(core_dst), "v_home"], _shim_env(None))
        check("[shim] direct binary invocation derives PLAINKEEP_HOME from execPath",
              r.returncode == 0 and r.stdout.strip() == str(withcore),
              f"rc={r.returncode} out={r.stdout!r} err={r.stderr!r} want={str(withcore)!r}")

        # 4. A COPIED vault (the scout-Q1 shape: run_terminal/run_mcp copy a vault to temp) with no
        # PLAINKEEP_HOME and no PLAINKEEP_CORE_BIN: bash discovers home from $0, auto finds the
        # vault's own core, and the binary trusts the home the shim exported.
        copied = _clone_vault(withcore, tmps, "pk-shim-copy-")
        r = _run([str(copied / "plainkeep"), "v_home"], _shim_env(None, PLAINKEEP_CORE="auto"))
        check("[shim] copied vault dispatches through its OWN copied core",
              r.returncode == 0 and r.stdout.strip() == str(copied),
              f"rc={r.returncode} out={r.stdout!r} err={r.stderr!r} want={str(copied)!r}")

        # 5. No core installed at all (`base` has no .local/bin).
        r = _run([shim, "v_ok"], _shim_env(base, PLAINKEEP_CORE="auto"))
        check("[shim] absent core · auto falls back to the bash floor, silently",
              r.returncode == 0 and r.stdout == "ok\n" and r.stderr == "",
              f"rc={r.returncode} out={r.stdout!r} err={r.stderr!r}")
        r = _run([shim, "v_ok"], _shim_env(base, PLAINKEEP_CORE="require"))
        check("[shim] absent core · require fails loudly (exit 1), never silently falls back",
              r.returncode == 1 and r.stdout == "" and "require" in r.stderr,
              f"rc={r.returncode} out={r.stdout!r} err={r.stderr!r}")
        r = _run([shim, "v_ok"], _shim_env(base, PLAINKEEP_CORE="off"))
        check("[shim] absent core · off takes the bash floor",
              r.returncode == 0 and r.stdout == "ok\n",
              f"rc={r.returncode} out={r.stdout!r} err={r.stderr!r}")

        # 6. Executable but DEAD: passes `-x`, fails --core-selftest. The failure mode the probe
        # exists for (wrong platform, truncated download, missing dylib).
        broken = _clone_vault(base, tmps, "pk-shim-broken-")
        bpath = broken / ".local" / "bin" / "plainkeep-core"
        bpath.parent.mkdir(parents=True, exist_ok=True)
        bpath.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
        bpath.chmod(0o755)
        r = _run([str(broken / "plainkeep"), "v_ok"], _shim_env(broken, PLAINKEEP_CORE="auto"))
        warn_lines = [ln for ln in r.stderr.split("\n") if ln]
        check("[shim] broken-but-executable core · auto = bash floor + EXACTLY ONE warning line",
              r.returncode == 0 and r.stdout == "ok\n" and len(warn_lines) == 1
              and "liveness probe" in warn_lines[0],
              f"rc={r.returncode} out={r.stdout!r} err={r.stderr!r} warn_lines={len(warn_lines)}")
        r = _run([str(broken / "plainkeep"), "v_ok"], _shim_env(broken, PLAINKEEP_CORE="require"))
        check("[shim] broken-but-executable core · require fails loudly (exit 1)",
              r.returncode == 1 and r.stdout == "" and "no live core binary" in r.stderr,
              f"rc={r.returncode} out={r.stdout!r} err={r.stderr!r}")

        # 7. Mode pinning, positively both ways: a tracer core that IS live. `off` must never even
        # probe it (no marker file at all); auto/require must EXEC it with the verb's argv. This is
        # what makes "the matrix ran the floor" a claim with evidence behind it.
        for mode, want_exec in (("off", False), ("auto", True), ("require", True)):
            traced = _clone_vault(base, tmps, f"pk-shim-trace-{mode}-")
            marker = traced / "_core_was_used"
            tracer = traced / "_fake_core"
            _write_tracer(tracer, marker)
            r = _run([str(traced / "plainkeep"), "v_ok"],
                     _shim_env(traced, PLAINKEEP_CORE=mode, PLAINKEEP_CORE_BIN=str(tracer)))
            got = marker.read_text(encoding="utf-8") if marker.exists() else ""
            check(f"[shim] mode pinning · PLAINKEEP_CORE={mode} "
                  f"{'execs the core' if want_exec else 'never touches the core'}",
                  ("v_ok" in got) == want_exec and (r.stdout == "" if want_exec else r.stdout == "ok\n"),
                  f"rc={r.returncode} out={r.stdout!r} err={r.stderr!r} marker={got!r}")

        # 8. An unrecognized mode is a usage error, not a silent choice.
        r = _run([shim, "v_ok"], _shim_env(base, PLAINKEEP_CORE="Require"))
        check("[shim] unknown PLAINKEEP_CORE value is a loud usage error (exit 2)",
              r.returncode == 2 and r.stdout == "" and "unknown PLAINKEEP_CORE" in r.stderr,
              f"rc={r.returncode} out={r.stdout!r} err={r.stderr!r}")
    finally:
        fx.cleanup()
        for d in tmps:
            shutil.rmtree(d, ignore_errors=True)


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
            try:
                cmp = COMPARATORS[inv["compare"]]
                ok, detail = cmp(binary, fx, inv)
            except Exception as e:
                ok, detail = False, f"exception: {e!r}"
            label = inv.get("api") or inv.get("verb") or inv["compare"]
            check(f"[{catalog}] {case['name']} · {inv['compare']}:{label} #{i}", ok, detail)
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

    catalogs = _load_catalogs()
    ncases = 0
    for catalog, doc in catalogs:
        for case in doc.get("cases", []):
            if only and only not in f"{catalog}/{case['name']}":
                continue
            ncases += 1
            _run_case(binary, catalog, case)
    ncatalog_checks = len(results)
    if not only or only in "shim":
        _shim_checks(binary)

    print(f"{BOLD}core-parity differential oracle — {ncatalog_checks} checks across {ncases} "
          f"catalog cases + {len(results) - ncatalog_checks} shim/root-discovery checks "
          f"(binary: {binary}){RESET}\n")
    passed = sum(1 for _, ok, _ in results if ok)
    for name, ok, detail in results:
        mark = f"{GREEN}PASS{RESET}" if ok else f"{RED}FAIL{RESET}"
        print(f"  {mark} {name}" + (f"\n       {DIM}{detail}{RESET}" if (detail and not ok) else ""))
    failed = len(results) - passed
    label = f"{YELLOW}Result (FILTERED — not a gate):{RESET}" if only else f"{BOLD}Result:{RESET}"
    print(f"\n{label} {GREEN}{passed} passed{RESET}, "
          f"{(RED if failed else DIM)}{failed} failed{RESET}, {len(results)} checks")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
