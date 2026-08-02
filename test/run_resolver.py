#!/usr/bin/env python3
"""
run_resolver.py — multi-root verb resolution (proposal Part 2.1 + 0.2). Covers: strict precedence
(engine bin/ RESERVED > plugins/<pack>/ > $PLAINKEEP_PATH), plugin-verb dispatch end-to-end through ./plainkeep,
guardrail gating of a plugin verb (identical risk classes), plainkeep.json source tag + PLUGINS group +
shadowed-verb warning, and `plainkeep new verb` scaffolding into plugins/local/ (never bin/). Offline,
stdlib only; nothing is written into the real checkout at all.
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
from lib.vaultfx import dispatchable_vault
seal()   # hermetic: an empty throwaway registry, never the developer's real vault

REPO = Path(__file__).resolve().parents[1]
PY = sys.executable
GREEN, RED, DIM, BOLD, RESET = "\033[32m", "\033[31m", "\033[2m", "\033[1m", "\033[0m"
results = []


def check(name, cond, detail=""):
    results.append((name, bool(cond), detail))


def _mk_verb(d: Path, verb: str, risk: str = "read", run: str | None = None):
    d.mkdir(parents=True, exist_ok=True)
    (d / "cmd.json").write_text(json.dumps({
        "verb": verb, "summary": f"test verb {verb}", "usage": f"plainkeep {verb}",
        "risk": risk, "args": [], "reads": [], "writes": [],
    }), encoding="utf-8")
    if run is not None:
        (d / "run.py").write_text(run, encoding="utf-8")


# A runnable plugin verb that re-enters lib via PLAINKEEP_HOME (dispatcher-exported).
# A plugin verb bootstraps `lib` through $PLAINKEEP_ENGINE, not $PLAINKEEP_HOME (Phase 2 Task 2 —
# the same line `templates/verb/run.py` scaffolds). It read PLAINKEEP_HOME while the engine lived in
# the vault; a vault has no `bin/` to import from now, and the dispatcher exports the engine root
# for exactly this case. Keeping the old line here would have made this suite the last place in the
# repo that still believed the engine was in the vault.
_GREET = (
    "import os, sys\n"
    "from pathlib import Path\n"
    "sys.path.insert(0, str(Path(os.environ['PLAINKEEP_ENGINE']) / 'bin'))\n"
    "from lib import output\n"
    "def main(argv):\n"
    "    _, argv = output.parse_argv(argv)\n"
    "    return output.emit({'greet': 'hi'}, 'greetplug', human=lambda d: 'GREETPLUG_OK')\n"
    "if __name__ == '__main__':\n"
    "    raise SystemExit(main(sys.argv[1:]))\n"
)
_NEEDS = (
    "import sys\n"
    "def main(argv):\n"
    "    print('NEEDSYES_RAN')\n"
    "    return 0\n"
    "if __name__ == '__main__':\n"
    "    raise SystemExit(main(sys.argv[1:]))\n"
)


def _load_resolver():
    """Load bin/lib/resolver.py by path — the test dir shadows the `lib` namespace package, and the
    resolver has no `lib` imports, so a direct file load is both correct and isolated."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("plainkeep_resolver", REPO / "bin" / "lib" / "resolver.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _resolver_precedence():
    """In-process precedence: resolver reads PLAINKEEP_HOME/PLAINKEEP_PATH per call, ENGINE_BIN is the real bin/."""
    resolver = _load_resolver()
    with tempfile.TemporaryDirectory() as td:
        home, extern = Path(td) / "vault", Path(td) / "extern"
        # shadow the real engine `search`, plus a plugin-only verb, in pack packA
        _mk_verb(home / "plugins" / "packA" / "search", "search")
        _mk_verb(home / "plugins" / "packA" / "pfoo", "pfoo")
        # $PLAINKEEP_PATH root also defines search + pfoo (both should LOSE) and a unique pbar
        _mk_verb(extern / "search", "search")
        _mk_verb(extern / "pfoo", "pfoo")
        _mk_verb(extern / "pbar", "pbar")
        env0 = os.environ.get("PLAINKEEP_HOME"), os.environ.get("PLAINKEEP_PATH")
        os.environ["PLAINKEEP_HOME"] = str(home)
        os.environ["PLAINKEEP_PATH"] = str(extern)
        try:
            check("engine bin/ wins over plugin + PLAINKEEP_PATH (reserved)",
                  resolver.source_of("search") == "engine", str(resolver.resolve("search")))
            check("plugin pack wins over PLAINKEEP_PATH", resolver.source_of("pfoo") == "plugin:packA")
            check("PLAINKEEP_PATH root resolves last", resolver.source_of("pbar") == "plugin:extern")
            check("unknown verb resolves to None", resolver.resolve("nope-verb") is None)
            check("is_engine_verb true for a real engine verb", resolver.is_engine_verb("search"))
            check("is_engine_verb false for a plugin-only verb", not resolver.is_engine_verb("pbar"))
            check("shadowed() reports the engine collision", ("search", "packA") in resolver.shadowed())
            kv = resolver.known_verbs()
            check("known_verbs unions every root", {"search", "pfoo", "pbar"} <= kv)
            check("plugin_names lists contributing packs", set(resolver.plugin_names()) == {"packA", "extern"})
        finally:
            for k, v in zip(("PLAINKEEP_HOME", "PLAINKEEP_PATH"), env0):
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v


def _dispatch_and_gate():
    """End-to-end through ./plainkeep with a throwaway pack in a throwaway ENGINE-CARRYING vault.

    It used to build the pack inside the real checkout and dispatch with `PLAINKEEP_HOME=REPO`,
    because both dispatchers look for the engine under the selected root (report §6.3) and a bare
    temp dir has no `bin/lib`. The cost was that the four dispatches below appended four lines to the
    developer's own audit log on every green run, and that a crash between `_mk_verb` and the
    `finally` left a plugin pack in their vault. `dispatchable_vault` gives back a marked temp root
    plus the checkout's launcher, so the pack, the manifest and the log are all inside a directory
    that is deleted — and since Phase 2 Task 2 that IS the shipped shape: the engine is its own tree
    and the vault holds only the plugin packs this suite is about.
    """
    with tempfile.TemporaryDirectory() as td:
        home = Path(td) / "vault"
        home.mkdir(parents=True)
        _, launcher = dispatchable_vault(home, REPO)
        pack = home / "plugins" / "_restest"
        _mk_verb(pack / "greetplug", "greetplug", risk="read", run=_GREET)
        _mk_verb(pack / "needsyes", "needsyes", risk="confirm", run=_NEEDS)
        env = {**os.environ, "PLAINKEEP_HOME": str(home)}

        def ops(*args):
            return subprocess.run([str(launcher), *args], capture_output=True, text=True, env=env)

        r = ops("greetplug")
        check("plugin verb dispatches through ./plainkeep (exit 0)", r.returncode == 0, r.stdout + r.stderr)
        check("plugin verb actually ran", "GREETPLUG_OK" in r.stdout, r.stdout)

        r = ops("greetplug", "--json")
        try:
            env_obj = json.loads(r.stdout.strip())
        except Exception:
            env_obj = {}
        check("plugin verb emits a valid --json envelope",
              env_obj.get("ops_json") == 1 and env_obj.get("verb") == "greetplug" and env_obj.get("ok") is True,
              r.stdout)

        r = ops("needsyes")
        check("confirm-class plugin verb is gated (exit 3, not run)",
              r.returncode == 3 and "NEEDSYES_RAN" not in r.stdout, f"rc={r.returncode} {r.stderr}")
        r = ops("needsyes", "--yes")
        check("confirm-class plugin verb runs with --yes",
              r.returncode == 0 and "NEEDSYES_RAN" in r.stdout, f"rc={r.returncode} {r.stdout}{r.stderr}")


def _manifest_surface():
    """plainkeep.json source tags + capabilities.plugins + render() PLUGINS group + shadowed warning."""
    with tempfile.TemporaryDirectory() as td:
        home = Path(td) / "vault"
        _mk_verb(home / "plugins" / "showpack" / "plug1", "plug1")
        _mk_verb(home / "plugins" / "showpack" / "wiki", "wiki")   # shadows the engine `wiki` → ignored
        env = {**os.environ, "PLAINKEEP_HOME": str(home)}
        subprocess.run([PY, "-c", "import sys; sys.path.insert(0,'bin'); from lib import manifest; manifest.write_manifest()"],
                       cwd=REPO, env=env, capture_output=True, text=True)
        doc = json.loads((home / "plainkeep.json").read_text())
        verbs = {v["verb"]: v for v in doc["verbs"]}
        check("plainkeep.json tags a plugin verb with source plugin:<pack>",
              verbs.get("plug1", {}).get("source") == "plugin:showpack", str(verbs.get("plug1")))
        check("plainkeep.json keeps engine source for engine verbs", verbs.get("wiki", {}).get("source") == "engine")
        check("shadowing plugin verb is NOT listed under the engine source's pack",
              verbs.get("wiki", {}).get("source") != "plugin:showpack")
        check("capabilities.plugins lists the pack", "showpack" in doc.get("capabilities", {}).get("plugins", []))

        render = subprocess.run(
            [PY, "-c", "import sys; sys.path.insert(0,'bin'); from lib import manifest; print(manifest.render())"],
            cwd=REPO, env=env, capture_output=True, text=True).stdout
        check("plainkeep help renders a PLUGINS group", "PLUGINS" in render and "plug1" in render, render[-400:])
        check("plainkeep help warns about the shadowed reserved verb",
              "IGNORED" in render and "wiki" in render, render[-400:])


def _new_verb_scaffold():
    """`plainkeep new verb` lands in plugins/local/ under the vault, never bin/, and says it survives update."""
    with tempfile.TemporaryDirectory() as td:
        ops_home, roots = Path(td) / "plainkeep", Path(td) / "home"
        (ops_home / "wiki").mkdir(parents=True)
        (ops_home / "journal").mkdir()
        shutil.copytree(REPO / "templates", ops_home / "templates")
        env = {**os.environ, "PLAINKEEP_HOME": str(ops_home), "PLAINKEEP_ROOTS_HOME": str(roots)}
        r = subprocess.run([PY, str(REPO / "bin" / "new" / "run.py"), "verb", "myverb", "--risk", "read"],
                           capture_output=True, text=True, env=env)
        d = ops_home / "plugins" / "local" / "myverb"
        check("new verb scaffolds into plugins/local/<name>/",
              (d / "run.py").exists() and (d / "cmd.json").exists(), r.stdout + r.stderr)
        check("new verb prints the survives-update note", "script/update" in (r.stdout + r.stderr), r.stdout)
        # The scaffolded stub bootstraps lib via PLAINKEEP_ENGINE (Phase 2 Task 2), so it must run
        # through the dispatcher pattern. This check used to assert `"PLAINKEEP_HOME" in stub` and
        # was GREEN on the template's own COMMENT about the pre-Task-2 line — a stale assertion
        # passing on prose. It now names the code line and the one module a plugin may import.
        stub = (d / "run.py").read_text() if (d / "run.py").exists() else ""
        check("scaffolded stub bootstraps lib via PLAINKEEP_ENGINE (not PLAINKEEP_HOME, not parents[1])",
              'sys.path.insert(0, str(Path(_ENGINE) / "bin"))' in stub, stub[:200])
        check("scaffolded stub imports exactly one lib module, the frozen SDK",
              [ln for ln in stub.splitlines() if ln.startswith("from lib import")] == ["from lib import api  # noqa: E402,F401"],
              str([ln for ln in stub.splitlines() if ln.startswith("from lib import")]))


def main() -> int:
    _resolver_precedence()
    _dispatch_and_gate()
    _manifest_surface()
    _new_verb_scaffold()

    print(f"{BOLD}multi-root verb resolution (Part 2.1 + 0.2) — {len(results)} checks{RESET}\n")
    passed = sum(1 for _, ok, _ in results if ok)
    for name, ok, detail in results:
        mark = f"{GREEN}PASS{RESET}" if ok else f"{RED}FAIL{RESET}"
        print(f"  {mark} {name:<52}" + (f" {DIM}{str(detail).strip()[:80]}{RESET}" if (detail and not ok) else ""))
    failed = len(results) - passed
    print(f"\n{BOLD}Result:{RESET} {GREEN}{passed} passed{RESET}, "
          f"{(RED if failed else DIM)}{failed} failed{RESET}, {len(results)} checks")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
