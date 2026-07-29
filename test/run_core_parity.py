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
adds guardrail.json + a `--core-gate` comparator, Tasks 4/5 add dispatcher/completion catalogs. New
catalogs drop a JSON file next to resolver.json and (if they need a new comparator) register one in
COMPARATORS — the vault builder and runner are catalog-agnostic.

Binary discovery: $PLAINKEEP_CORE_BIN else <repo>/.local/bin/plainkeep-core. Absent (or not
executable) => print a LOUD single SKIP line and exit 0, UNLESS PLAINKEEP_REQUIRE_CORE=1, in which
case the same line is an error and the suite exits 1. SKIP is visible, never a silent PASS.
"""
from __future__ import annotations
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PY = sys.executable
RESOLVER_SRC = REPO / "bin" / "lib" / "resolver.py"
GUARDRAIL_SRC = REPO / "bin" / "lib" / "guardrail.py"
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
            self._make_verb(v["at"], v["verb"], v.get("files", ["run.py", "cmd.json"]), v.get("cmd"))

        # Guardrail-catalog additions (additive; resolver.json declares neither): plugins_lock writes
        # plugins/plugins.lock.json verbatim (a str for a malformed-JSON fixture) or json.dumps()'d
        # (an object). verbs[].cmd overrides a verb's cmd.json CONTENT (str written raw, else dumped),
        # so risk/dry_run vary per verb — what _VERB_FILES' name-only lambda cannot express.
        pl = spec.get("plugins_lock")
        if pl is not None:
            pdir = self.root / "plugins"
            pdir.mkdir(parents=True, exist_ok=True)
            (pdir / "plugins.lock.json").write_text(
                pl if isinstance(pl, str) else json.dumps(pl), encoding="utf-8"
            )

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

    def _make_verb(self, at: str, verb: str, files: list[str], cmd=None) -> None:
        d = self._base_dir(at) / verb
        d.mkdir(parents=True, exist_ok=True)
        for f in files:
            if f == "cmd.json" and cmd is not None:
                content = cmd if isinstance(cmd, str) else json.dumps(cmd)
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


COMPARATORS = {
    "cli": _compare_cli,
    "api": _compare_api,
    "gate": _compare_gate,
}


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

    catalogs = _load_catalogs()
    for catalog, doc in catalogs:
        for case in doc.get("cases", []):
            _run_case(binary, catalog, case)

    ncases = sum(len(doc.get("cases", [])) for _, doc in catalogs)
    print(f"{BOLD}core-parity differential oracle — {len(results)} checks "
          f"across {ncases} cases (binary: {binary}){RESET}\n")
    passed = sum(1 for _, ok, _ in results if ok)
    for name, ok, detail in results:
        mark = f"{GREEN}PASS{RESET}" if ok else f"{RED}FAIL{RESET}"
        print(f"  {mark} {name}" + (f"\n       {DIM}{detail}{RESET}" if (detail and not ok) else ""))
    failed = len(results) - passed
    print(f"\n{BOLD}Result:{RESET} {GREEN}{passed} passed{RESET}, "
          f"{(RED if failed else DIM)}{failed} failed{RESET}, {len(results)} checks")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
