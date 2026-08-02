#!/usr/bin/env python3
"""
run_plugin.py — frozen SDK + `plainkeep plugin` (proposal Parts 2.2 + 2.3). Covers: plugin.json schema
validation (good fixture + bad variants), the trust ceiling enforced by the guardrail PRE/POST trust,
lockfile round-trip (add → trust → update → remove), engine-verb collision refusal, min_ops_version
gate, and the api.py signature snapshot (fails on any silent removal/change of the public surface).

Offline, stdlib only. The plugin verb is driven directly (bin/plugin/run.py) with a throwaway
PLAINKEEP_HOME so nothing lands in the real vault; the ceiling is checked through bin/lib/guardrail.py.
"""
from __future__ import annotations
import inspect
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from lib.hermetic import seal
seal()   # hermetic: an empty throwaway registry, never the developer's real vault

REPO = Path(__file__).resolve().parents[1]
PY = sys.executable
PLUGIN = REPO / "bin" / "plugin" / "run.py"
GUARD = REPO / "bin" / "lib" / "guardrail.py"
FIX = REPO / "test" / "fixtures" / "plugin-good"
SNAP = REPO / "test" / "fixtures" / "api_snapshot.json"
GREEN, RED, DIM, BOLD, RESET = "\033[32m", "\033[31m", "\033[2m", "\033[1m", "\033[0m"
results = []


def check(name, cond, detail=""):
    results.append((name, bool(cond), detail))


def _env(home: Path) -> dict:
    return {**os.environ, "PLAINKEEP_HOME": str(home)}


def plugin(args, home: Path):
    return subprocess.run([PY, str(PLUGIN), *args], capture_output=True, text=True,
                          env=_env(home), cwd=str(REPO))


def gate(verb, args, home: Path):
    return subprocess.run([PY, str(GUARD), verb, *args], capture_output=True, text=True,
                          env=_env(home), cwd=str(REPO))


def _mk_pack(root: Path, manifest: dict, verbs):
    root.mkdir(parents=True, exist_ok=True)
    (root / "plugin.json").write_text(json.dumps(manifest), encoding="utf-8")
    for v in verbs:
        d = root / v
        d.mkdir(parents=True, exist_ok=True)
        (d / "cmd.json").write_text(json.dumps(
            {"verb": v, "summary": "x", "usage": f"plainkeep {v}", "risk": "read"}), encoding="utf-8")
        (d / "run.py").write_text("def main(a):\n    return 0\n", encoding="utf-8")


def _lock(home: Path) -> dict:
    f = home / "plugins" / "plugins.lock.json"
    return json.loads(f.read_text(encoding="utf-8")) if f.exists() else {}


# ------------------------------------------------------------------------------------------------
def _schema_and_install():
    with tempfile.TemporaryDirectory() as td:
        home = Path(td) / "vault"
        home.mkdir()

        # add without --yes is confirm-class → refused (exit 3), nothing installed
        r = plugin(["add", str(FIX)], home)
        check("add without --yes is refused (exit 3)", r.returncode == 3, f"rc={r.returncode} {r.stderr}")
        check("nothing installed when add refused", not (home / "plugins" / "greeter").exists())

        # good fixture installs, lockfile records it UNTRUSTED
        r = plugin(["add", str(FIX), "--yes"], home)
        check("good pack installs (exit 0)", r.returncode == 0, r.stdout + r.stderr)
        check("pack dir copied to plugins/<name>/", (home / "plugins" / "greeter" / "hello" / "run.py").exists())
        lock = _lock(home).get("plugins", {})
        entry = lock.get("greeter", {})
        check("lockfile records the pack", entry.get("version") == "0.1.0", str(entry))
        check("pack starts UNTRUSTED", entry.get("trusted") is False)
        check("lockfile records commit 'local' for a local path", entry.get("commit") == "local")
        check("lockfile records the declared verbs", [v["verb"] for v in entry.get("verbs", [])] == ["hello"])

        # re-adding the same name is refused (idempotency guard)
        r = plugin(["add", str(FIX), "--yes"], home)
        check("re-add of an installed pack is refused", r.returncode != 0 and "already installed" in r.stderr,
              f"rc={r.returncode} {r.stderr}")


def _bad_manifests():
    base = {"name": "bad", "version": "0.1.0", "min_ops_version": "4.0.0", "api": ">=1,<2",
            "verbs": [{"verb": "bfoo", "risk": "read", "reads": [], "writes": []}]}
    with tempfile.TemporaryDirectory() as td:
        home = Path(td) / "vault"; home.mkdir()
        root = Path(td) / "packs"

        # missing version
        p = root / "noversion"
        m = dict(base); m.pop("version")
        _mk_pack(p, m, ["bfoo"])
        r = plugin(["add", str(p), "--yes"], home)
        check("missing version rejected (exit 2)", r.returncode == 2 and "version" in r.stderr,
              f"rc={r.returncode} {r.stderr}")

        # api range excludes engine api 1.0
        p = root / "badapi"
        m = dict(base); m["api"] = ">=2,<3"
        _mk_pack(p, m, ["bfoo"])
        r = plugin(["add", str(p), "--yes"], home)
        check("out-of-range api rejected (exit 2)", r.returncode == 2 and "api" in r.stderr,
              f"rc={r.returncode} {r.stderr}")

        # declared verb has no cmd.json in the pack
        p = root / "novdir"
        _mk_pack(p, dict(base), [])   # no bfoo/ dir created
        r = plugin(["add", str(p), "--yes"], home)
        check("declared verb without a cmd.json rejected", r.returncode == 2 and "bfoo" in r.stderr,
              f"rc={r.returncode} {r.stderr}")

        # bad risk class
        p = root / "badrisk"
        m = dict(base); m["verbs"] = [{"verb": "bfoo", "risk": "nuke"}]
        _mk_pack(p, m, ["bfoo"])
        r = plugin(["add", str(p), "--yes"], home)
        check("unknown risk class rejected (exit 2)", r.returncode == 2 and "risk" in r.stderr,
              f"rc={r.returncode} {r.stderr}")

        # min_ops_version in the future
        p = root / "future"
        m = dict(base); m["name"] = "future"; m["min_ops_version"] = "9.9.9"
        _mk_pack(p, m, ["bfoo"])
        r = plugin(["add", str(p), "--yes"], home)
        check("future min_ops_version refused (exit 1)", r.returncode == 1 and "needs plainkeep" in r.stderr,
              f"rc={r.returncode} {r.stderr}")


def _collision_refusal():
    with tempfile.TemporaryDirectory() as td:
        home = Path(td) / "vault"; home.mkdir()
        p = Path(td) / "shadow"
        m = {"name": "shadow", "version": "0.1.0", "min_ops_version": "4.0.0", "api": ">=1,<2",
             "verbs": [{"verb": "wiki", "risk": "read", "reads": [], "writes": []}]}
        _mk_pack(p, m, ["wiki"])   # 'wiki' is a reserved engine verb
        r = plugin(["add", str(p), "--yes"], home)
        check("engine-verb collision refused (exit 2)",
              r.returncode == 2 and "collide" in r.stderr and "wiki" in r.stderr, f"rc={r.returncode} {r.stderr}")
        check("colliding pack not installed", not (home / "plugins" / "shadow").exists())


def _trust_ceiling():
    with tempfile.TemporaryDirectory() as td:
        home = Path(td) / "vault"; home.mkdir()
        plugin(["add", str(FIX), "--yes"], home)   # installs greeter/hello (declared safe_write), untrusted

        # PRE-trust: an untrusted safe_write verb is capped at confirm (needs --yes)
        check("untrusted plugin verb gated at confirm (exit 3)", gate("hello", [], home).returncode == 3)
        check("untrusted plugin verb allowed with --yes", gate("hello", ["--yes"], home).returncode == 0)

        # trust needs --yes
        r = plugin(["trust", "greeter"], home)
        check("trust without --yes refused (exit 3)", r.returncode == 3, f"rc={r.returncode} {r.stderr}")

        r = plugin(["trust", "greeter", "--yes"], home)
        check("trust --yes succeeds (exit 0)", r.returncode == 0, r.stdout + r.stderr)
        e = _lock(home)["plugins"]["greeter"]
        check("lockfile marks the pack trusted", e.get("trusted") is True)
        check("lockfile records the accepted ceiling", e.get("accepted_ceiling") == "safe_write", str(e))

        # POST-trust: the declared risk stands — safe_write runs WITHOUT --yes
        check("trusted plugin verb runs at declared safe_write (exit 0)", gate("hello", [], home).returncode == 0)

        # trusting an unknown pack is a not-found
        check("trust of an unknown pack is not-found (exit 4)",
              plugin(["trust", "nope", "--yes"], home).returncode == 4)


def _update_and_remove():
    with tempfile.TemporaryDirectory() as td:
        home = Path(td) / "vault"; home.mkdir()
        src = Path(td) / "mutable"
        base = {"name": "mut", "version": "0.1.0", "min_ops_version": "4.0.0", "api": ">=1,<2",
                "verbs": [{"verb": "mfoo", "risk": "safe_write", "reads": [], "writes": []}]}
        _mk_pack(src, base, ["mfoo"])
        plugin(["add", str(src), "--yes"], home)
        plugin(["trust", "mut", "--yes"], home)
        check("pre-update trusted", _lock(home)["plugins"]["mut"]["trusted"] is True)

        # bump version, keep risk within the accepted ceiling → trust preserved
        m = dict(base); m["version"] = "0.2.0"
        (src / "plugin.json").write_text(json.dumps(m), encoding="utf-8")
        r = plugin(["update", "mut", "--yes"], home)
        check("update re-resolves the pin (exit 0)", r.returncode == 0, r.stdout + r.stderr)
        e = _lock(home)["plugins"]["mut"]
        check("update records the new version", e.get("version") == "0.2.0")
        check("update keeps trust when risk surface is unchanged", e.get("trusted") is True)

        # grow the risk surface beyond the accepted ceiling → trust is revoked
        m = dict(base); m["version"] = "0.3.0"
        m["verbs"] = [{"verb": "mfoo", "risk": "confirm", "reads": [], "writes": []}]
        (src / "plugin.json").write_text(json.dumps(m), encoding="utf-8")
        r = plugin(["update", "mut", "--yes"], home)
        check("update revokes trust when risk grows", _lock(home)["plugins"]["mut"]["trusted"] is False,
              r.stdout + r.stderr)

        # update without --yes is refused
        check("update without --yes refused (exit 3)", plugin(["update", "mut"], home).returncode == 3)

        # remove without --yes refused, then remove deletes dir + lock entry
        check("remove without --yes refused (exit 3)", plugin(["remove", "mut"], home).returncode == 3)
        r = plugin(["remove", "mut", "--yes"], home)
        check("remove succeeds (exit 0)", r.returncode == 0, r.stdout + r.stderr)
        check("remove deletes the pack dir", not (home / "plugins" / "mut").exists())
        check("remove deletes the lock entry", "mut" not in _lock(home).get("plugins", {}))


def _list_surface():
    with tempfile.TemporaryDirectory() as td:
        home = Path(td) / "vault"; home.mkdir()
        # empty list is valid
        r = plugin(["list", "--json"], home)
        head = json.loads(r.stdout.splitlines()[0]) if r.stdout.strip() else {}
        check("empty list emits a valid rows envelope", head.get("verb") == "plugin" and head.get("count") == 0,
              r.stdout)
        plugin(["add", str(FIX), "--yes"], home)
        r = plugin(["list", "--json"], home)
        lines = [json.loads(x) for x in r.stdout.splitlines() if x.strip()]
        rows = lines[1:]
        check("list shows the installed pack with trust state",
              any(x.get("name") == "greeter" and x.get("trusted") is False for x in rows), r.stdout)


def _api_snapshot():
    """Import lib.api in a subprocess (test/ shadows the lib namespace) and diff every exported
    name's signature/type against the committed snapshot — the frozen-SDK contract test."""
    code = (
        "import sys, inspect, json\n"
        "sys.path.insert(0, 'bin')\n"
        "from lib import api\n"
        "snap = {}\n"
        "for n in api.__all__:\n"
        "    o = getattr(api, n)\n"
        "    snap[n] = ('callable ' + str(inspect.signature(o))) if callable(o) else ('value:' + type(o).__name__)\n"
        "print(json.dumps(snap, sort_keys=True))\n"
    )
    # PLAINKEEP_HOME is set because the SDK's import graph reaches lib/paths.py, which resolves the
    # data root at import and has no engine-relative fallback since ADR-014 Task 1b. The snapshot is
    # about SIGNATURES, not about any path, so the repo itself is the cheapest valid root.
    r = subprocess.run([PY, "-c", code], capture_output=True, text=True, cwd=str(REPO),
                       env={**os.environ, "PLAINKEEP_HOME": str(REPO)})
    check("lib.api imports cleanly", r.returncode == 0, r.stderr)
    try:
        live = json.loads(r.stdout)
    except Exception:
        live = {}
    committed = json.loads(SNAP.read_text(encoding="utf-8"))
    check("api __all__ matches the committed snapshot (no removals/additions)",
          set(live) == set(committed), f"live-only={set(live)-set(committed)} snap-only={set(committed)-set(live)}")
    drift = {n: (committed.get(n), live.get(n)) for n in committed if live.get(n) != committed.get(n)}
    check("no exported signature drifted", not drift, str(drift))
    check("PLAINKEEP_API_VERSION is 1.0", live.get("PLAINKEEP_API_VERSION") == "value:str" and _api_version() == "1.0")


def _api_version() -> str:
    r = subprocess.run([PY, "-c", "import sys; sys.path.insert(0,'bin'); from lib import api; print(api.PLAINKEEP_API_VERSION)"],
                       capture_output=True, text=True, cwd=str(REPO),
                       env={**os.environ, "PLAINKEEP_HOME": str(REPO)})  # same reason as _api_snapshot
    return r.stdout.strip()


def _api_runs_a_verb():
    """A plugin verb importing ONLY lib.api emits a valid --json envelope (the SDK is sufficient)."""
    with tempfile.TemporaryDirectory() as td:
        home = Path(td) / "vault"; home.mkdir()
        plugin(["add", str(FIX), "--yes"], home)
        run = home / "plugins" / "greeter" / "hello" / "run.py"
        # the stub bootstraps lib via PLAINKEEP_HOME/bin, so point it at the real engine bin
        r = subprocess.run([PY, str(run), "ada", "--json"], capture_output=True, text=True,
                           env={**os.environ, "PLAINKEEP_HOME": str(REPO)}, cwd=str(REPO))
        try:
            env_obj = json.loads(r.stdout.strip())
        except Exception:
            env_obj = {}
        check("plugin verb via lib.api emits a valid envelope",
              env_obj.get("ops_json") == 1 and env_obj.get("verb") == "hello" and env_obj.get("ok") is True,
              r.stdout + r.stderr)


def main() -> int:
    _schema_and_install()
    _bad_manifests()
    _collision_refusal()
    _trust_ceiling()
    _update_and_remove()
    _list_surface()
    _api_snapshot()
    _api_runs_a_verb()

    print(f"{BOLD}frozen SDK + plainkeep plugin (Part 2.2 + 2.3) — {len(results)} checks{RESET}\n")
    passed = sum(1 for _, ok, _ in results if ok)
    for name, ok, detail in results:
        mark = f"{GREEN}PASS{RESET}" if ok else f"{RED}FAIL{RESET}"
        print(f"  {mark} {name:<54}" + (f" {DIM}{str(detail).strip()[:80]}{RESET}" if (detail and not ok) else ""))
    failed = len(results) - passed
    print(f"\n{BOLD}Result:{RESET} {GREEN}{passed} passed{RESET}, "
          f"{(RED if failed else DIM)}{failed} failed{RESET}, {len(results)} checks")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
