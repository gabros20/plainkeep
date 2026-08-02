#!/usr/bin/env python3
"""
run_pluginsdk.py — SDK COMPATIBILITY and the plugin dependency contract (Phase 2 Task 3, ADR-018).

WHY A SECOND PLUGIN SUITE. `run_plugin.py` snapshots what the frozen SDK EXPORTS. This one is about
where the interpreter LOOKS, which is a different question and one a signature snapshot structurally
cannot answer: an api.py whose every signature is unchanged is still broken for every plugin in the
world if `from lib import api` no longer resolves. Task 2 moved the engine out of the vault and did
exactly that, so the acceptance gate here is behavioural end to end.

THE CENTRAL CASE, and the one thing this file must never be allowed to fake: the EXISTING plugin
fixture — `test/fixtures/plugin-good/`, BYTE FOR BYTE, still bootstrapping the SDK through
`$PLAINKEEP_HOME/bin` the way every plugin ever scaffolded does — imports and runs against a
relocated engine, in a vault with no `bin/` at all. Section A asserts the fixture's sha256 against
the committed file before it runs it, because a suite that edits the fixture to make it pass has
tested the edit.

Every dispatch below goes through the REAL dispatcher (the `plainkeep` launcher), never by importing
a module and asserting what it returns. `PYTHONPATH` containing the right string proves nothing.

HERMETIC: `seal()` for the registry, a temp vault per case, and the one installed-engine case points
`PLAINKEEP_ENGINE_HOME` at a temp root. The developer's `~/.local/share/plainkeep`, `~/.config` and
real vault are never read or written.

Offline, stdlib only — except the ONE case that runs pip, which installs a wheel this file builds by
hand from a temp directory with `--no-index`, and which SKIPS (loudly) where pip is unavailable.
"""
from __future__ import annotations
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path
from lib.hermetic import seal
from lib.vaultfx import mark_vault
seal()   # hermetic: an empty throwaway registry, never the developer's real vault

REPO = Path(__file__).resolve().parents[1]
PY = sys.executable
LAUNCHER = REPO / "plainkeep"
ENGINETREE = REPO / "bin" / "lib" / "enginetree.py"
FIX = REPO / "test" / "fixtures" / "plugin-good"
VERSION = (REPO / "VERSION").read_text(encoding="utf-8").strip()
GREEN, RED, YEL, DIM, BOLD, RESET = "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[1m", "\033[0m"
results: list[tuple[str, bool, str]] = []
skips: list[tuple[str, str]] = []

# The probe every env-shape case runs as its verb body. NORMALIZED to labels: what matters is which
# entry is where and whether the SDK is reachable, not the temp path it happens to live at.
# RAW: the child source below carries \n escapes that must survive into the generated run.py.
PROBE = r"""import importlib.util, json, os, subprocess, sys
from pathlib import Path
engine = os.environ.get("PLAINKEEP_ENGINE") or ""
home = os.environ.get("PLAINKEEP_HOME") or ""
label = {str(Path(home) / ".plugin-deps"): "DEPS", str(Path(engine) / "bin"): "ENGINE_BIN"}
def shape(raw):
    return [label.get(x, x) for x in raw.split(os.pathsep)] if raw else []
# `lib.api`, not `lib`: `lib` alone is ambiguous — any directory named lib on the child's path
# answers it, including the repository's own test/lib when the harness cwd leaks in. `lib.api` is
# the SDK and nothing else is. The child also runs with cwd=<vault> so the ambient cwd cannot
# decide the answer at all.
# find_spec RAISES ModuleNotFoundError for a submodule whose PARENT is absent, which is the case
# this child exists to detect — so the absence is caught, never allowed to become empty stdout.
CHILD = ("import importlib.util, json, os, sys\n"
         "try:\n"
         "    found = importlib.util.find_spec('lib.api') is not None\n"
         "except Exception:\n"
         "    found = False\n"
         "print(json.dumps({'pp': os.environ.get('PYTHONPATH') or '', 'lib': found,"
         " 'pack': os.environ.get('PLAINKEEP_PLUGIN_PACK')}))")
kw = {"capture_output": True, "text": True, "cwd": home or None}
early = subprocess.run([sys.executable, "-c", CHILD], **kw)
before = os.environ.get("PYTHONPATH") or ""
from lib import api                                    # noqa: E402  (the SDK import under test)
after = os.environ.get("PYTHONPATH") or ""
late = subprocess.run([sys.executable, "-c", CHILD], **kw)
def child(r):
    d = json.loads(r.stdout or "{}")
    d["pp"] = shape(d.get("pp") or "")
    return d
print(json.dumps({"pack": os.environ.get("PLAINKEEP_PLUGIN_PACK"),
                  "before": shape(before), "after": shape(after),
                  "api": api.PLAINKEEP_API_VERSION,
                  "early_child": child(early), "late_child": child(late)}, sort_keys=True))
"""

# An engine verb body: it must NOT import the SDK the way a plugin does (an engine verb self-locates),
# so it reports the environment only.
ENGINE_PROBE = """import importlib.util, json, os
print(json.dumps({"pack": os.environ.get("PLAINKEEP_PLUGIN_PACK"),
                  "pythonpath": os.environ.get("PYTHONPATH"),
                  "sdk_importable": importlib.util.find_spec("lib") is not None}, sort_keys=True))
"""


def check(name: str, cond: bool, detail: str = "") -> None:
    results.append((name, bool(cond), detail))


def skip(name: str, why: str) -> None:
    skips.append((name, why))


def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _lock(vault: Path) -> dict:
    """The lockfile, or {} — never an exception. A suite that dies on a missing file reports NOTHING,
    and a mutation run needs to see which checks went red, not a traceback."""
    try:
        return json.loads((vault / "plugins" / "plugins.lock.json").read_text(encoding="utf-8"))
    except Exception:
        return {}


# --------------------------------------------------------------------------------------------------
# Fixture plumbing
# --------------------------------------------------------------------------------------------------
def make_vault(td: Path, name: str = "vault") -> Path:
    root = td / name
    root.mkdir(parents=True)
    mark_vault(root)
    return root


def env_for(vault: Path, td: Path, **over) -> dict:
    e = {**os.environ, "PLAINKEEP_HOME": str(vault), "PLAINKEEP_CONFIG_HOME": str(td / "_cfg"),
         "HOME": str(td / "_home"), "PLAINKEEP_CORE": "off"}
    # A hostile inherited value on every invocation, like the parity harness: the dispatcher must
    # replace it, and a plugin that loaded through the caller's value would import from nowhere.
    e["PLAINKEEP_ENGINE"] = str(td / "_engine_that_does_not_exist")
    e.pop("PYTHONPATH", None)
    e.pop("PLAINKEEP_PATH", None)
    e.pop("PLAINKEEP_CORE_BIN", None)
    e.update(over)
    os.makedirs(td / "_cfg", exist_ok=True)
    os.makedirs(td / "_home", exist_ok=True)
    return e


def pk(vault: Path, td: Path, *args: str, launcher: Path | None = None, **over):
    return subprocess.run([str(launcher or LAUNCHER), *args], capture_output=True, text=True,
                          env=env_for(vault, td, **over))


def make_pack(root: Path, name: str, verb: str, body: str, manifest_extra: dict | None = None,
              risk: str = "read") -> Path:
    """A pack directory in the exact engine shape, ready for `plugin add` or a direct copy."""
    d = root / verb
    d.mkdir(parents=True)
    (d / "cmd.json").write_text(json.dumps(
        {"verb": verb, "summary": "probe", "usage": f"plainkeep {verb}", "risk": risk}), encoding="utf-8")
    (d / "run.py").write_text(body, encoding="utf-8")
    manifest = {"name": name, "version": "0.1.0", "min_ops_version": "4.0.0", "api": ">=1,<2",
                "verbs": [{"verb": verb, "risk": risk, "reads": [], "writes": []}]}
    manifest.update(manifest_extra or {})
    (root / "plugin.json").write_text(json.dumps(manifest), encoding="utf-8")
    return root


# The pre-Task-2 bootstrap, verbatim — the two lines every scaffolded plugin carries and the reason
# this task exists. Written out here rather than imported so that a case using it is unmistakably
# testing the STALE shape.
STALE_BOOTSTRAP = ('import os, sys\nfrom pathlib import Path\n'
                   'sys.path.insert(0, str(Path(os.environ["PLAINKEEP_HOME"]) / "bin"))\n')


def install_pack(vault: Path, src: Path, name: str) -> None:
    """Copy a pack into the vault the way `plugin add` would, without the confirm dance."""
    shutil.copytree(src, vault / "plugins" / name, ignore=shutil.ignore_patterns("__pycache__"))


# --------------------------------------------------------------------------------------------------
# A. The byte-unmodified fixture, against a relocated engine, in a vault with no bin/
# --------------------------------------------------------------------------------------------------
def case_unmodified_fixture() -> None:
    with tempfile.TemporaryDirectory() as t:
        td = Path(t)
        vault = make_vault(td)
        install_pack(vault, FIX, "greeter")
        run_py = vault / "plugins" / "greeter" / "hello" / "run.py"

        # (1) THE FIXTURE IS THE COMMITTED ONE. If this ever fails, every check below is worthless.
        check("fixture: installed run.py is byte-identical to the committed fixture",
              sha(run_py) == sha(FIX / "hello" / "run.py"), f"{sha(run_py)} vs {sha(FIX / 'hello' / 'run.py')}")
        check("fixture: still bootstraps through $PLAINKEEP_HOME/bin (the stale shape)",
              'os.environ["PLAINKEEP_HOME"]) / "bin"' in run_py.read_text(encoding="utf-8"))
        check("fixture vault has NO bin/ — the engine is elsewhere", not (vault / "bin").exists())

        # (2) THE GATE: it runs, through the real dispatcher, and emits a valid envelope.
        r = pk(vault, td, "hello", "ada", "--json", "--yes")
        env_obj = {}
        try:
            env_obj = json.loads(r.stdout.strip())
        except Exception:
            pass
        check("unmodified fixture runs through the real dispatcher (exit 0)", r.returncode == 0,
              f"rc={r.returncode} {r.stdout[:120]} {r.stderr[:200]}")
        check("unmodified fixture emits a valid --json envelope",
              env_obj.get("ops_json") == 1 and env_obj.get("verb") == "hello" and env_obj.get("ok") is True,
              r.stdout[:200])
        check("the SDK it imported is the frozen one",
              env_obj.get("data", {}).get("greeting") == "hello ada", r.stdout[:200])

        # (3) THE CONTROL, and it is what makes (2) mean something. The same file, same environment,
        # invoked DIRECTLY instead of through the dispatcher: the injection is the only difference,
        # and without it the import fails. A green (2) with a green control would be a green test of
        # nothing — it would mean `lib` was reachable for some other reason.
        direct = subprocess.run([PY, str(run_py), "ada"], capture_output=True, text=True,
                                env=env_for(vault, td))
        check("CONTROL: the same fixture run OUTSIDE a dispatch cannot import the SDK",
              direct.returncode != 0 and "No module named 'lib'" in direct.stderr,
              f"rc={direct.returncode} {direct.stderr[-200:]}")


def case_installed_engine() -> None:
    """The same fixture against a REAL installed engine: versioned, sealed read-only, reached through
    `current`, with `PLAINKEEP_ENGINE_HOME` in a temp dir so the developer's install is untouched."""
    with tempfile.TemporaryDirectory() as t:
        td = Path(t)
        root = td / "engines"
        inst = subprocess.run([PY, str(ENGINETREE), "--install", str(REPO)],
                              capture_output=True, text=True,
                              env={**os.environ, "PLAINKEEP_ENGINE_HOME": str(root)})
        if inst.returncode != 0:
            skip("installed-engine dispatch", f"engine install failed: {inst.stderr[-200:]}")
            return
        launcher = root / "engine" / "current" / "plainkeep"
        check("installed engine: sealed tree is read-only",
              not os.access(root / "engine" / VERSION / "bin" / "lib" / "api.py", os.W_OK))
        vault = make_vault(td)
        install_pack(vault, FIX, "greeter")
        r = pk(vault, td, "hello", "ada", "--json", "--yes", launcher=launcher)
        check("unmodified fixture runs through an INSTALLED, SEALED engine (exit 0)",
              r.returncode == 0, f"rc={r.returncode} {r.stdout[:120]} {r.stderr[:300]}")
        # ...and it is the INSTALLED tree that answered, not the repository the test runs from. A
        # second pack reports the engine root it was actually given; asserting on the greeting alone
        # would pass just as well if the repo had served the SDK.
        install_pack(vault, make_pack(td / "wherepack", "where", "v_where",
                                      STALE_BOOTSTRAP + "from lib import api\n"
                                      "print(os.environ['PLAINKEEP_ENGINE'])\n"), "where")
        w = pk(vault, td, "v_where", "--yes", launcher=launcher)
        # realpath both sides: the dispatcher canonicalizes (macOS /var -> /private/var), and two
        # spellings of one directory comparing unequal is the exact bug Task 2's disjointness check
        # was written about.
        check("...and the engine that served the SDK is the installed tree, not the repo",
              w.returncode == 0 and w.stdout.strip().startswith(os.path.realpath(root))
              and not w.stdout.strip().startswith(str(REPO)),
              f"rc={w.returncode} {w.stdout[:200]} {w.stderr[:200]}")
        # The vault never grew a bin/, and the engine tree never grew a note.
        check("installed-engine run: vault still has no bin/", not (vault / "bin").exists())


# --------------------------------------------------------------------------------------------------
# B. The precedence inversion — PINNED, not wished away
# --------------------------------------------------------------------------------------------------
def case_precedence() -> None:
    with tempfile.TemporaryDirectory() as t:
        td = Path(t)
        vault = make_vault(td)
        src = make_pack(td / "shadowpack", "shadowy", "v_shadow",
                        STALE_BOOTSTRAP + "from lib import api\nprint(api.WHOSE)\n")
        # a `lib` of its own, beside run.py — the shape the preflight looks for
        own = src / "v_shadow" / "lib"
        own.mkdir()
        (own / "__init__.py").write_text("", encoding="utf-8")
        (own / "api.py").write_text("WHOSE = 'the-plugins-own-lib'\n", encoding="utf-8")
        install_pack(vault, src, "shadowy")

        r = pk(vault, td, "v_shadow", "--yes")
        # PINNED: `sys.path[0]` is the verb's own directory and precedes PYTHONPATH, so the plugin's
        # own `lib` wins. This is a REVERSAL of the pre-Task-2 behaviour (the scaffold's insert(0, …)
        # put the engine first) and it is silent. The test states which way it actually goes, so a
        # change in EITHER direction is visible.
        check("precedence: a pack's own top-level `lib` SHADOWS the SDK (sys.path[0] wins)",
              r.returncode == 0 and "the-plugins-own-lib" in r.stdout,
              f"rc={r.returncode} out={r.stdout[:120]} err={r.stderr[:200]}")

        # ...and the preflight makes that case visible instead of leaving it to be discovered.
        sys.path.insert(0, str(REPO / "bin" / "lib"))
        import pluginenv  # noqa: E402
        check("preflight: sdk_shadows() names the pack and verb",
              pluginenv.sdk_shadows(vault) == [("shadowy", "v_shadow")],
              str(pluginenv.sdk_shadows(vault)))
        check("preflight: a pack with no top-level lib is not flagged",
              pluginenv.sdk_shadows(make_vault(td, "clean")) == [])

        d = pk(vault, td, "doctor", "--json", "--yes")
        rows = [json.loads(x) for x in d.stdout.splitlines() if x.strip().startswith("{")]
        msgs = " ".join(str(x.get("check", "")) + str(x.get("msg", "")) + json.dumps(x) for x in rows)
        check("preflight: doctor warns about the shadowing pack",
              "shadowy/v_shadow" in msgs and "SHADOWS" in msgs, msgs[-300:])

        # and at install time, where the pack can still be looked at
        add = pk(make_vault(td, "vault2"), td, "plugin", "add", str(src), "--yes")
        check("preflight: `plugin add` warns on stderr at install time",
              "SHADOWS" in add.stderr and "shadowy/v_shadow" in add.stderr,
              f"rc={add.returncode} {add.stderr[:300]}")


# --------------------------------------------------------------------------------------------------
# C. PYTHONPATH: what is added, to WHICH spawns, and exactly what leaks
# --------------------------------------------------------------------------------------------------
def _probe_output(vault: Path, td: Path, verb: str, **over) -> dict:
    r = pk(vault, td, verb, "--yes", **over)
    try:
        return json.loads(r.stdout.strip().splitlines()[-1])
    except Exception:
        return {"_rc": r.returncode, "_out": r.stdout, "_err": r.stderr}


def case_pythonpath_scope() -> None:
    with tempfile.TemporaryDirectory() as t:
        td = Path(t)
        vault = make_vault(td)
        install_pack(vault, make_pack(td / "probepack", "prober", "v_probe",
                                      STALE_BOOTSTRAP + PROBE), "prober")

        d = _probe_output(vault, td, "v_probe")
        check("plugin spawn: PLAINKEEP_PLUGIN_PACK names the pack", d.get("pack") == "prober", str(d)[:300])
        check("plugin spawn: PYTHONPATH is exactly [deps, engine/bin]",
              d.get("before") == ["DEPS", "ENGINE_BIN"], str(d.get("before")))
        check("plugin spawn: the SDK is importable and frozen at 1.0", d.get("api") == "1.0", str(d)[:200])

        # the MERGE with a caller's own value — prepended, never replaced
        d2 = _probe_output(vault, td, "v_probe", PYTHONPATH="/caller/one:/caller/two")
        check("plugin spawn: a caller's PYTHONPATH is PREPENDED to, not replaced",
              d2.get("before") == ["DEPS", "ENGINE_BIN", "/caller/one", "/caller/two"], str(d2.get("before")))

        # THE SCRUB: after the SDK import the engine entry is gone from this process's environment,
        # so a child does not inherit it — while the dependency overlay, which the pack declared and
        # its own helper scripts have the same claim on, is kept.
        check("scrub: after `from lib import api`, ENGINE_BIN is gone from PYTHONPATH",
              d.get("after") == ["DEPS"], str(d.get("after")))
        late = d.get("late_child", {})
        check("leak: a child spawned AFTER the SDK import does not see the engine path",
              late.get("pp") == ["DEPS"], str(late))
        check("leak: that child cannot import the SDK", late.get("lib") is False, str(late))

        # THE WINDOW THAT REMAINS, asserted rather than described. A child spawned BEFORE the SDK
        # import inherits the full path — the scrub cannot run earlier than the import that triggers
        # it. This check exists so the disclosure in ADR-018 D2 is pinned to a measured behaviour and
        # goes red if it ever changes.
        early = d.get("early_child", {})
        check("leak (disclosed): a child spawned BEFORE the SDK import DOES inherit the engine path",
              early.get("pp") == ["DEPS", "ENGINE_BIN"] and early.get("lib") is True, str(early))
        check("leak: PLAINKEEP_PLUGIN_PACK is inherited by children (it is not scrubbed)",
              early.get("pack") == "prober" and late.get("pack") == "prober", str(late))


def case_engine_verb_untouched() -> None:
    """The negative half: an ENGINE verb is spawned with none of it. Proved on a REAL installed
    engine built from a source copy carrying an extra verb — the only way to reach an engine verb
    that reports its own environment without writing into the repository's bin/."""
    with tempfile.TemporaryDirectory() as t:
        td = Path(t)
        source = td / "src"
        shutil.copytree(REPO, source, symlinks=True,
                        ignore=shutil.ignore_patterns("__pycache__", ".git", "node_modules", ".venv",
                                                      ".index", ".logs", "site", "docs"))
        probe = source / "bin" / "v_engprobe"
        probe.mkdir()
        (probe / "cmd.json").write_text(json.dumps(
            {"verb": "v_engprobe", "summary": "probe", "usage": "plainkeep v_engprobe", "risk": "read"}),
            encoding="utf-8")
        (probe / "run.py").write_text(ENGINE_PROBE, encoding="utf-8")
        root = td / "engines"
        inst = subprocess.run([PY, str(ENGINETREE), "--install", str(source)], capture_output=True,
                              text=True, env={**os.environ, "PLAINKEEP_ENGINE_HOME": str(root)})
        if inst.returncode != 0:
            skip("engine-verb environment", f"engine install failed: {inst.stderr[-300:]}")
            return
        launcher = root / "engine" / "current" / "plainkeep"
        vault = make_vault(td)
        r = pk(vault, td, "v_engprobe", "--yes", launcher=launcher, PYTHONPATH="/caller/one")
        try:
            d = json.loads(r.stdout.strip().splitlines()[-1])
        except Exception:
            d = {}
        check("engine verb: no PLAINKEEP_PLUGIN_PACK", d.get("pack") is None,
              f"rc={r.returncode} {r.stdout[:200]} {r.stderr[:200]}")
        check("engine verb: PYTHONPATH is the caller's, untouched", d.get("pythonpath") == "/caller/one",
              str(d))
        check("engine verb: the engine tree is NOT on its path (it self-locates through __file__)",
              d.get("sdk_importable") is False, str(d))


# --------------------------------------------------------------------------------------------------
# D. The dependency contract
# --------------------------------------------------------------------------------------------------
DEPENDENT = STALE_BOOTSTRAP + "from lib import api\nimport zzdemo\nprint(zzdemo.VALUE)\n"


def _wheel(dirpath: Path) -> Path:
    """A pure-python wheel, built by hand: a zip with a dist-info. No build backend, no network — pip
    installs it with `--no-index --find-links`, which is what makes the ONE pip case offline."""
    dirpath.mkdir(parents=True, exist_ok=True)
    whl = dirpath / "zzdemo-1.0.0-py3-none-any.whl"
    with zipfile.ZipFile(whl, "w") as z:
        z.writestr("zzdemo/__init__.py", "VALUE = 'from-the-overlay'\n")
        z.writestr("zzdemo-1.0.0.dist-info/METADATA",
                   "Metadata-Version: 2.1\nName: zzdemo\nVersion: 1.0.0\n")
        z.writestr("zzdemo-1.0.0.dist-info/WHEEL",
                   "Wheel-Version: 1.0\nGenerator: hand\nRoot-Is-Purelib: true\nTag: py3-none-any\n")
        z.writestr("zzdemo-1.0.0.dist-info/RECORD", "")
    return whl


def case_dependency_resolution() -> None:
    with tempfile.TemporaryDirectory() as t:
        td = Path(t)
        vault = make_vault(td)
        src = make_pack(td / "needy", "needy", "v_needy", DEPENDENT,
                        {"dependencies": ["zzdemo>=1.0"]})
        add = pk(vault, td, "plugin", "add", str(src), "--yes")
        check("declared dependencies are recorded in the lockfile",
              _lock(vault).get("plugins", {}).get("needy", {}).get("dependencies") == ["zzdemo>=1.0"],
              add.stdout[:200] + add.stderr[:200])

        # (1) DECLARED BUT NOT INSTALLED: the refusal names the pack and the module, and says sync.
        r = pk(vault, td, "v_needy", "--yes")
        check("declared-but-missing: refuses with the pack AND the module named",
              r.returncode == 1 and "needy" in r.stderr and "zzdemo" in r.stderr
              and "plugin sync" in r.stderr, f"rc={r.returncode} {r.stderr[:300]}")
        check("declared-but-missing: no traceback is printed", "Traceback" not in r.stderr, r.stderr[:300])

        # (2) THE OVERLAY RESOLVES. Populated by hand here so the resolution path is tested without
        # pip — the pip path itself is case (4).
        overlay = vault / ".plugin-deps" / "zzdemo"
        overlay.mkdir(parents=True)
        (overlay / "__init__.py").write_text("VALUE = 'from-the-overlay'\n", encoding="utf-8")
        r = pk(vault, td, "v_needy", "--yes")
        check("a declared dependency in the overlay RESOLVES through the real dispatcher",
              r.returncode == 0 and "from-the-overlay" in r.stdout,
              f"rc={r.returncode} {r.stdout[:120]} {r.stderr[:300]}")

        # (3) UNDECLARED: a different message, because the operator's next move is different.
        src2 = make_pack(td / "sloppy", "sloppy", "v_sloppy",
                         STALE_BOOTSTRAP + "from lib import api\nimport zznotdeclared\n")
        pk(vault, td, "plugin", "add", str(src2), "--yes")
        r = pk(vault, td, "v_sloppy", "--yes")
        check("undeclared-missing: refuses with the pack AND the module named",
              r.returncode == 1 and "sloppy" in r.stderr and "zznotdeclared" in r.stderr,
              f"rc={r.returncode} {r.stderr[:300]}")
        check("undeclared-missing: says the pack does not DECLARE it",
              "does not declare" in r.stderr, r.stderr[:300])
        check("undeclared-missing: no traceback is printed", "Traceback" not in r.stderr, r.stderr[:300])


def case_sync_with_pip() -> None:
    probe = subprocess.run([PY, "-m", "pip", "--version"], capture_output=True, text=True)
    if probe.returncode != 0:
        skip("plugin sync (real pip)", "python3 -m pip is not available on this interpreter")
        return
    with tempfile.TemporaryDirectory() as t:
        td = Path(t)
        vault = make_vault(td)
        wheels = td / "wheels"
        _wheel(wheels)
        src = make_pack(td / "needy", "needy", "v_needy", DEPENDENT, {"dependencies": ["zzdemo>=1.0"]})
        pk(vault, td, "plugin", "add", str(src), "--yes")
        r = pk(vault, td, "plugin", "sync", "needy", "--yes",
               "--no-index", f"--find-links={wheels}")
        check("plugin sync installs the declared dependency (exit 0)", r.returncode == 0,
              f"rc={r.returncode} {r.stdout[:200]} {r.stderr[:400]}")
        check("plugin sync writes into the vault's overlay, nowhere else",
              (vault / ".plugin-deps" / "zzdemo" / "__init__.py").exists())
        lock = _lock(vault)
        want = f"{sys.version_info.major}.{sys.version_info.minor}"
        check("plugin sync records the interpreter it built the overlay for",
              lock.get("overlay", {}).get("python") == want, str(lock.get("overlay")))
        v = pk(vault, td, "v_needy", "--yes")
        check("the synced dependency is importable by the plugin verb",
              v.returncode == 0 and "from-the-overlay" in v.stdout,
              f"rc={v.returncode} {v.stdout[:120]} {v.stderr[:300]}")

        # doctor notices an overlay built for another interpreter — the ONE thing that invalidates it
        lock["overlay"]["python"] = "2.7"
        (vault / "plugins" / "plugins.lock.json").write_text(json.dumps(lock), encoding="utf-8")
        d = pk(vault, td, "doctor", "--json", "--yes")
        check("doctor warns when the overlay was built for another interpreter",
              "overlay was installed for python 2.7" in d.stdout, d.stdout[-300:])


def case_sync_refuses_shadowing_overlay() -> None:
    """An overlay that grows a top-level `lib` shadows the SDK for every plugin in the vault. It is
    ahead of the engine on the path (so declared deps beat the engine tree's incidental names), so
    this is refused rather than reordered."""
    probe = subprocess.run([PY, "-m", "pip", "--version"], capture_output=True, text=True)
    if probe.returncode != 0:
        skip("plugin sync refuses a shadowing overlay", "python3 -m pip is not available")
        return
    with tempfile.TemporaryDirectory() as t:
        td = Path(t)
        vault = make_vault(td)
        wheels = td / "wheels"
        wheels.mkdir()
        whl = wheels / "zzlibby-1.0.0-py3-none-any.whl"
        with zipfile.ZipFile(whl, "w") as z:
            z.writestr("lib/__init__.py", "# a distribution that lands a top-level `lib`\n")
            z.writestr("zzlibby-1.0.0.dist-info/METADATA",
                       "Metadata-Version: 2.1\nName: zzlibby\nVersion: 1.0.0\n")
            z.writestr("zzlibby-1.0.0.dist-info/WHEEL",
                       "Wheel-Version: 1.0\nGenerator: hand\nRoot-Is-Purelib: true\nTag: py3-none-any\n")
            z.writestr("zzlibby-1.0.0.dist-info/RECORD", "")
        src = make_pack(td / "libby", "libby", "v_libby", STALE_BOOTSTRAP + "from lib import api\n",
                        {"dependencies": ["zzlibby"]})
        pk(vault, td, "plugin", "add", str(src), "--yes")
        r = pk(vault, td, "plugin", "sync", "libby", "--yes",
               "--no-index", f"--find-links={wheels}")
        check("sync REFUSES an overlay that shadows the SDK with a top-level `lib` (exit 5)",
              r.returncode == 5 and "SHADOWS" in r.stderr, f"rc={r.returncode} {r.stderr[:300]}")


def case_dependency_grammar() -> None:
    sys.path.insert(0, str(REPO / "bin" / "plugin"))
    with tempfile.TemporaryDirectory() as t:
        td = Path(t)
        vault = make_vault(td)
        good = ["httpx", "httpx>=0.27", "httpx >= 0.27", "ruamel.yaml", "pandas[excel]==2.0.1",
                "a-b_c!=1.0,<2"]
        bad = ["--index-url=http://evil", "-r requirements.txt", "https://evil/x.whl", ".",
               "./local", "pkg; python_version<'3'", "pkg && rm -rf /", "$(whoami)", ""]
        for spec in good:
            src = make_pack(td / f"ok{good.index(spec)}", f"okpack{good.index(spec)}",
                            f"v_ok{good.index(spec)}", "def main(a):\n    return 0\n",
                            {"dependencies": [spec]})
            r = pk(vault, td, "plugin", "add", str(src), "--yes")
            check(f"dependency grammar accepts {spec!r}", r.returncode == 0,
                  f"rc={r.returncode} {r.stderr[:200]}")
        for spec in bad:
            src = make_pack(td / f"bad{bad.index(spec)}", f"badpack{bad.index(spec)}",
                            f"v_bad{bad.index(spec)}", "def main(a):\n    return 0\n",
                            {"dependencies": [spec]})
            r = pk(vault, td, "plugin", "add", str(src), "--yes")
            check(f"dependency grammar REFUSES {spec!r} (exit 2)",
                  r.returncode == 2 and "dependencies[0]" in r.stderr,
                  f"rc={r.returncode} {r.stderr[:200]}")
            check(f"...and nothing was installed for {spec!r}",
                  not (vault / "plugins" / f"badpack{bad.index(spec)}").exists())


def case_new_dependency_revokes_trust() -> None:
    with tempfile.TemporaryDirectory() as t:
        td = Path(t)
        vault = make_vault(td)
        src = make_pack(td / "mut", "mut", "v_mut", "def main(a):\n    return 0\n")
        pk(vault, td, "plugin", "add", str(src), "--yes")
        pk(vault, td, "plugin", "trust", "mut", "--yes")
        def lock():
            return _lock(vault).get("plugins", {}).get("mut", {})
        check("pre-update the pack is trusted", lock().get("trusted") is True)

        m = json.loads((src / "plugin.json").read_text())
        m["version"] = "0.2.0"
        (src / "plugin.json").write_text(json.dumps(m), encoding="utf-8")
        pk(vault, td, "plugin", "update", "mut", "--yes")
        check("an update with no new dependency KEEPS trust", lock().get("trusted") is True)

        m["version"] = "0.3.0"
        m["dependencies"] = ["httpx"]
        (src / "plugin.json").write_text(json.dumps(m), encoding="utf-8")
        r = pk(vault, td, "plugin", "update", "mut", "--yes")
        check("an update that ADDS a dependency revokes trust (re-consent required)",
              lock().get("trusted") is False, r.stdout[:200])
        check("...and the new declaration is named in the output", "httpx" in r.stdout, r.stdout[:200])
        check("...and it is recorded", lock().get("dependencies") == ["httpx"])

        # dropping one is not a growth
        m["version"] = "0.4.0"
        pk(vault, td, "plugin", "trust", "mut", "--yes")
        m["dependencies"] = []
        (src / "plugin.json").write_text(json.dumps(m), encoding="utf-8")
        pk(vault, td, "plugin", "update", "mut", "--yes")
        check("dropping a dependency is not a growth and keeps trust",
              lock().get("trusted") is True)


# --------------------------------------------------------------------------------------------------
# E. THE OVERLAY IS NOT A PLUGIN PACK (fix wave r1, review BLOCKING 1)
#
# The overlay used to live at `plugins/.deps/` — inside the directory `resolver._plugin_packs()`
# enumerates, which appended EVERY subdirectory. So any distribution that ships `<pkg>/run.py` (an
# ordinary layout) became the dispatchable verb `<pkg>`, attributed to a "pack" named `.deps` that no
# lockfile recorded, that `plugin list` never showed, and that no user consented to.
#
# WHAT THIS CASE ASSERTS IS THE OUTCOME, NOT THE MECHANISM: a wheel installed the sanctioned way
# (`plugin add` then `plugin sync`) CANNOT BE DISPATCHED. A test that the dot filter exists, or that
# the constant says `.plugin-deps`, would pass against any number of new ways to reach the same hole.
# The wheel is built here to be maximally verb-shaped — package directory, `run.py`, `cmd.json` — so
# the only thing standing between it and a dispatch is the property under test.
# --------------------------------------------------------------------------------------------------
def _verbish_wheel(dirpath: Path, name: str = "zzverbish") -> Path:
    """A pure-python wheel whose package directory is EXACTLY the shape of a plainkeep verb."""
    dirpath.mkdir(parents=True, exist_ok=True)
    whl = dirpath / f"{name}-1.0.0-py3-none-any.whl"
    with zipfile.ZipFile(whl, "w") as z:
        z.writestr(f"{name}/__init__.py", "VALUE = 'ordinary package'\n")
        z.writestr(f"{name}/cmd.json",
                   json.dumps({"verb": name, "summary": "not a verb", "risk": "read"}))
        z.writestr(f"{name}/run.py",
                   "print('EXECUTED FROM INSIDE THE DEPENDENCY OVERLAY')\n\n\n"
                   "def main(argv):\n"
                   "    print('EXECUTED FROM INSIDE THE DEPENDENCY OVERLAY')\n"
                   "    return 0\n\n\n"
                   "if __name__ == '__main__':\n"
                   "    raise SystemExit(main([]))\n")
        z.writestr(f"{name}-1.0.0.dist-info/METADATA",
                   f"Metadata-Version: 2.1\nName: {name}\nVersion: 1.0.0\n")
        z.writestr(f"{name}-1.0.0.dist-info/WHEEL",
                   "Wheel-Version: 1.0\nGenerator: hand\nRoot-Is-Purelib: true\nTag: py3-none-any\n")
        z.writestr(f"{name}-1.0.0.dist-info/RECORD", "")
    return whl


# Asked of the REAL resolver, in a subprocess, with the vault's own environment — the same module the
# dispatcher asks. `plugin_names()` is what `plainkeep.json` publishes as `capabilities.plugins`, so
# it is the surface through which a bogus pack would reach help, completion, the TUI and MCP.
_RESOLVER_PROBE = """
import json, sys
sys.path.insert(0, sys.argv[1])
import resolver
print(json.dumps({"packs": resolver.plugin_names(),
                  "source": resolver.source_of(sys.argv[2]),
                  "known": sys.argv[2] in resolver.known_verbs()}))
"""


def _resolver_view(vault: Path, td: Path, verb: str) -> dict:
    # cwd=<vault>: `python -c` puts the current directory on sys.path, and the suite is run both
    # from the repo root and from `test/`. Neither may be able to answer this probe's imports.
    r = subprocess.run([PY, "-c", _RESOLVER_PROBE, str(REPO / "bin" / "lib"), verb],
                       capture_output=True, text=True, env=env_for(vault, td), cwd=str(vault))
    try:
        return json.loads(r.stdout.strip().splitlines()[-1])
    except Exception:
        return {"error": r.stderr[-200:]}


def _undispatchable(vault: Path, td: Path, where: str, expect_packs: list[str]) -> None:
    """The property, asserted through the REAL dispatcher and the REAL resolver. `--yes` is supplied
    deliberately: the untrusted-pack ceiling is NOT what is under test, and a case that leaned on it
    would go green for the wrong reason the day the ceiling changed."""
    d = pk(vault, td, "zzverbish", "--yes")
    check(f"{where}: A WHEEL'S CONTENT CANNOT BE DISPATCHED AS A VERB (unknown verb, exit 4)",
          d.returncode == 4 and "unknown verb" in d.stderr,
          f"rc={d.returncode} out={d.stdout[:160]!r} err={d.stderr[:200]!r}")
    check(f"{where}: ...and none of its code ran",
          "EXECUTED FROM INSIDE THE DEPENDENCY OVERLAY" not in (d.stdout + d.stderr), d.stdout[:200])
    rr = subprocess.run([PY, str(REPO / "bin" / "lib" / "resolver.py"), "--dispatch", "zzverbish"],
                        capture_output=True, text=True, env=env_for(vault, td))
    check(f"{where}: ...the resolver's own --dispatch has no answer for it (exit 4)",
          rr.returncode == 4 and not rr.stdout.strip(), f"rc={rr.returncode} {rr.stdout[:160]!r}")
    view = _resolver_view(vault, td, "zzverbish")
    check(f"{where}: ...it is not in known_verbs() and has no source",
          view.get("known") is False and view.get("source") is None, str(view))
    check(f"{where}: ...and no pack was invented for it (plainkeep.json capabilities)",
          view.get("packs") == expect_packs, str(view))


def _unpack_wheel(whl: Path, dest: Path) -> None:
    """Unpack a wheel exactly where pip's `--target` would have put it — the real bytes, no network."""
    dest.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(whl) as z:
        z.extractall(dest)


def case_overlay_is_not_a_pack() -> None:
    """OFFLINE and always runs. The wheel is real (built by `_verbish_wheel`) and its payload is put
    where the overlay is and where the overlay USED to be — so this is red at b1b3f6e on the legacy
    half without needing pip, an index, or a network to be reachable."""
    with tempfile.TemporaryDirectory() as t:
        td = Path(t)
        vault = make_vault(td)
        wheels = td / "wheels"
        whl = _verbish_wheel(wheels)
        src = make_pack(td / "carrier", "carrier", "v_carrier", "def main(a):\n    return 0\n",
                        {"dependencies": ["zzverbish"]})
        pk(vault, td, "plugin", "add", str(src), "--yes")

        # (1) THE CURRENT LOCATION. The overlay is outside `plugins/` entirely, which is what makes
        # the property structural rather than a rule something has to consult.
        _unpack_wheel(whl, vault / ".plugin-deps")
        landed = vault / ".plugin-deps" / "zzverbish"
        check("overlay: the wheel's payload really is a verb-shaped directory in the vault",
              (landed / "run.py").exists() and (landed / "cmd.json").exists(), str(landed))
        check("overlay: it is NOT inside plugins/, the tree the resolver enumerates",
              not (vault / "plugins" / ".deps").exists()
              and not (vault / "plugins" / ".plugin-deps").exists(),
              str(sorted(p.name for p in (vault / "plugins").iterdir())))
        _undispatchable(vault, td, "overlay", ["carrier"])

        # (2) THE LEGACY LOCATION, and it is not a curiosity: every vault that ran `plugin sync`
        # before the overlay moved still has `plugins/.deps/` on disk with exactly this content in
        # it. Moving the overlay does nothing for those vaults, so the resolver's dot filter is
        # asserted on the case it exists for — in both dispatchers, via the parity catalog too.
        _unpack_wheel(whl, vault / "plugins" / ".deps")
        check("legacy: the payload is present at the pre-move location",
              (vault / "plugins" / ".deps" / "zzverbish" / "run.py").exists())
        _undispatchable(vault, td, "legacy plugins/.deps", ["carrier"])


def case_overlay_end_to_end_with_pip() -> None:
    """The same property reached the SANCTIONED way — `plugin add` then `plugin sync` — so the case
    above cannot be satisfied by an overlay layout pip would never actually produce."""
    probe = subprocess.run([PY, "-m", "pip", "--version"], capture_output=True, text=True)
    if probe.returncode != 0:
        skip("an installed wheel cannot be dispatched as a verb (end to end)",
             "python3 -m pip is not available on this interpreter")
        return
    with tempfile.TemporaryDirectory() as t:
        td = Path(t)
        vault = make_vault(td)
        wheels = td / "wheels"
        _verbish_wheel(wheels)
        src = make_pack(td / "carrier", "carrier", "v_carrier",
                        STALE_BOOTSTRAP + "from lib import api\nimport zzverbish\n"
                        "print(zzverbish.VALUE)\n", {"dependencies": ["zzverbish"]})
        pk(vault, td, "plugin", "add", str(src), "--yes")
        r = pk(vault, td, "plugin", "sync", "carrier", "--yes", "--no-index", f"--find-links={wheels}")
        if r.returncode != 0:
            skip("an installed wheel cannot be dispatched as a verb (end to end)",
                 f"pip install failed: {(r.stderr or r.stdout)[-200:]}")
            return
        landed = vault / ".plugin-deps" / "zzverbish"
        check("end to end: `plugin sync` really did install a verb-shaped package",
              (landed / "run.py").exists() and (landed / "cmd.json").exists(), str(landed))
        _undispatchable(vault, td, "end to end", ["carrier"])
        # AND THE OVERLAY STILL WORKS — the fix moved the dependency overlay, it did not disable it.
        v = pk(vault, td, "v_carrier", "--yes")
        check("end to end: the declaring pack still imports the package it declared",
              v.returncode == 0 and "ordinary package" in v.stdout,
              f"rc={v.returncode} {v.stdout[:120]} {v.stderr[:300]}")


# --------------------------------------------------------------------------------------------------
# F. NOTHING BUT THE LOCKFILE'S DECLARATIONS REACHES PIP (fix wave r1, review BLOCKING 2)
#
# `sync` used to accept `--pip-arg=<anything>` and splice it into pip's argv AHEAD of the `--`
# terminator, which made it a REQUIREMENT channel rather than a flag channel: `--pip-arg=zzpwn`
# installed a package no pack declared onto every plugin verb's PYTHONPATH, the command reported only
# the declared ones, and the lockfile's audit trail never mentioned it. DEP_RE — the consent gate this
# verb exists to be — was bypassed entirely, and `bin/mcp/run.py` passes a free-form `args` array
# through verbatim, so no human had to be present.
# --------------------------------------------------------------------------------------------------
_PIP_ARGV_PROBE = """
import importlib.util, json, sys
spec = importlib.util.spec_from_file_location("pk_plugin", sys.argv[1])
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
from pathlib import Path
print(json.dumps(m._pip_argv(Path("/tmp/overlay"), ["httpx>=0.27"], ["--no-index"])))
"""


def case_pip_argv_is_closed() -> None:
    with tempfile.TemporaryDirectory() as t:
        td = Path(t)
        vault = make_vault(td)
        # A pack that declares NOTHING, on purpose. The refusal has to happen before any pip runs —
        # that is what "the flag cannot add a requirement" means — so the case must not need pip, an
        # index or a network to reach the boundary. With no declarations there is nothing legitimate
        # for pip to do, and anything that lands in the overlay came from the flag.
        src = make_pack(td / "needy", "needy", "v_needy", "def main(a):\n    return 0\n")
        pk(vault, td, "plugin", "add", str(src), "--yes")

        # (1) EVERY WAY THE OLD CHANNEL WAS REACHED, refused with exit 2 — and refused rather than
        # ignored, because a silently dropped flag teaches a caller nothing about the boundary.
        refusals = [
            ("--pip-arg=zzundeclared", "the exact reproduction: a bare word is a pip REQUIREMENT"),
            ("--pip-arg=--no-index", "the flag is gone even for values that used to be benign"),
            ("--pip-arg=--index-url=https://evil/simple", "index steering"),
            ("--index-url=https://evil/simple", "index steering, spelled directly"),
            ("--extra-index-url=https://evil/simple", "a second index is still an index"),
            ("-i", "pip's short index flag"),
            ("-r", "a requirements FILE is a requirement channel"),
            ("-e", "an editable install is a path channel"),
            ("--pre", "pre-release selection is still steering"),
        ]
        for flag, why in refusals:
            r = pk(vault, td, "plugin", "sync", "needy", "--yes", flag)
            check(f"sync REFUSES {flag!r} ({why})",
                  r.returncode == 2 and "unknown option" in r.stderr,
                  f"rc={r.returncode} {r.stderr[:200]}")
            check(f"...and nothing was installed for {flag!r}",
                  not (vault / ".plugin-deps").exists() or
                  not any((vault / ".plugin-deps").iterdir()),
                  str(vault / ".plugin-deps"))

        # (2) THE TWO SURVIVING OPTIONS CANNOT STEER EITHER. `--find-links` accepts a URL from pip,
        # and a URL there is an index by another name.
        r = pk(vault, td, "plugin", "sync", "needy", "--yes", "--find-links=https://evil/wheels")
        check("sync REFUSES a --find-links URL (an index by another name)",
              r.returncode == 2 and "local directory" in r.stderr, f"rc={r.returncode} {r.stderr[:200]}")
        r = pk(vault, td, "plugin", "sync", "needy", "--yes", f"--find-links={td / 'nope'}")
        check("sync REFUSES a --find-links directory that does not exist (exit 4)",
              r.returncode == 4, f"rc={r.returncode} {r.stderr[:200]}")

        # (3) THE ARGV ITSELF, pinned. `_pip_argv` is factored for exactly this: the requirements sit
        # AFTER `--`, and the option fragment is the only thing that precedes them.
        # cwd=<vault>, for the same reason the PROBE child at the top of this file uses it: `python -c`
        # puts the CURRENT DIRECTORY on sys.path, so running the suite from `test/` would offer the
        # repository's own `test/lib` as an answer for the `from lib import …` this module performs.
        # The vault has no `lib` of any kind, so the ambient cwd cannot decide what gets imported.
        pr = subprocess.run([PY, "-c", _PIP_ARGV_PROBE, str(REPO / "bin" / "plugin" / "run.py")],
                            capture_output=True, text=True, env=env_for(vault, td), cwd=str(vault))
        try:
            argv = json.loads(pr.stdout.strip().splitlines()[-1])
        except Exception:
            argv = []
        check("_pip_argv puts every requirement AFTER pip's `--` terminator",
              "--" in argv and argv.index("--") == len(argv) - 2 and argv[-1] == "httpx>=0.27",
              f"{argv} {pr.stderr[-200:]}")
        check("_pip_argv installs with --target into the overlay and nothing else",
              "--target" in argv and "/tmp/overlay" in argv and "--no-input" in argv, str(argv))


def case_sync_audit_trail() -> None:
    """What the lockfile records must be what LANDED. The old record named only the declarations, so
    a package installed through `--pip-arg` left no trace at all."""
    probe = subprocess.run([PY, "-m", "pip", "--version"], capture_output=True, text=True)
    if probe.returncode != 0:
        skip("plugin sync audit trail", "python3 -m pip is not available on this interpreter")
        return
    with tempfile.TemporaryDirectory() as t:
        td = Path(t)
        vault = make_vault(td)
        wheels = td / "wheels"
        _wheel(wheels)
        src = make_pack(td / "needy", "needy", "v_needy", DEPENDENT, {"dependencies": ["zzdemo>=1.0"]})
        pk(vault, td, "plugin", "add", str(src), "--yes")
        r = pk(vault, td, "plugin", "sync", "needy", "--yes", "--no-index", f"--find-links={wheels}")
        check("audit trail: sync succeeded", r.returncode == 0, f"rc={r.returncode} {r.stderr[:300]}")
        ov = _lock(vault).get("overlay", {})
        check("audit trail: records the requirements handed to pip",
              ov.get("requirements") == ["zzdemo>=1.0"], str(ov))
        check("audit trail: records every option that reached pip",
              ov.get("pip_options") == ["--no-index", f"--find-links={wheels}"], str(ov))
        # WHAT LANDED, read off the overlay's own dist-info rather than assumed from the request. An
        # ABSENT overlay is a failing check, never an exception: a suite that dies reports nothing,
        # and this check's whole job is to be legible when it goes red.
        try:
            on_disk = sorted(d.name[: -len(".dist-info")] for d in (vault / ".plugin-deps").iterdir()
                             if d.name.endswith(".dist-info"))
        except Exception as e:
            on_disk = [f"<no overlay: {e}>"]
        recorded = sorted(f"{c['name']}-{c['version']}" for c in ov.get("contents") or [])
        check("audit trail: `contents` equals the distributions actually in the overlay",
              recorded == on_disk and recorded == ["zzdemo-1.0.0"], f"{recorded} vs {on_disk}")


# --------------------------------------------------------------------------------------------------
# G. THE PACK MARKER IS REPLACED, NOT ADDED (fix wave r1, review IMPORTANT)
#
# `case_engine_verb_untouched` above asserts the engine-verb negative from a FRESH dispatch, where
# PLAINKEEP_PLUGIN_PACK was never in the environment to begin with. Neither dispatcher REMOVED it
# when it was: a plugin verb that re-enters the dispatcher (the documented pattern) passes the marker
# down, `pluginenv.attach()` is gated on nothing else, and every descendant that imports `lib.api`
# then reports its own ModuleNotFoundError as some innocent pack's fault. Exporting the variable in a
# shell armed the same hook for everything after it.
# --------------------------------------------------------------------------------------------------
def case_engine_verb_clears_inherited_pack() -> None:
    with tempfile.TemporaryDirectory() as t:
        td = Path(t)
        source = td / "src"
        shutil.copytree(REPO, source, symlinks=True,
                        ignore=shutil.ignore_patterns("__pycache__", ".git", "node_modules", ".venv",
                                                      ".index", ".logs", "site", "docs"))
        probe = source / "bin" / "v_engprobe"
        probe.mkdir()
        (probe / "cmd.json").write_text(json.dumps(
            {"verb": "v_engprobe", "summary": "probe", "usage": "plainkeep v_engprobe", "risk": "read"}),
            encoding="utf-8")
        (probe / "run.py").write_text(ENGINE_PROBE, encoding="utf-8")
        root = td / "engines"
        inst = subprocess.run([PY, str(ENGINETREE), "--install", str(source)], capture_output=True,
                              text=True, env={**os.environ, "PLAINKEEP_ENGINE_HOME": str(root)})
        if inst.returncode != 0:
            skip("engine verb clears an inherited pack marker", f"install failed: {inst.stderr[-300:]}")
            return
        launcher = root / "engine" / "current" / "plainkeep"
        vault = make_vault(td)
        r = pk(vault, td, "v_engprobe", "--yes", launcher=launcher,
               PLAINKEEP_PLUGIN_PACK="spoofed")
        try:
            d = json.loads(r.stdout.strip().splitlines()[-1])
        except Exception:
            d = {}
        check("an INHERITED PLAINKEEP_PLUGIN_PACK is REMOVED for an engine verb",
              d.get("pack") is None,
              f"rc={r.returncode} {r.stdout[:200]} {r.stderr[:200]}")

        # The consequence, which is what actually bites: with the marker present, `pluginenv.attach()`
        # arms the missing-dependency excepthook and an unrelated import failure is reported as the
        # named pack's fault. Asserted on the message, not on the variable.
        blamer = source / "bin" / "v_blame"
        blamer.mkdir()
        (blamer / "cmd.json").write_text(json.dumps(
            {"verb": "v_blame", "summary": "probe", "usage": "plainkeep v_blame", "risk": "read"}),
            encoding="utf-8")
        (blamer / "run.py").write_text(
            "import sys\nfrom pathlib import Path\n"
            "sys.path.insert(0, str(Path(__file__).resolve().parents[1]))\n"
            "from lib import api  # noqa: F401\n"
            "import zznotathing\n", encoding="utf-8")
        root2 = td / "engines2"
        inst2 = subprocess.run([PY, str(ENGINETREE), "--install", str(source)], capture_output=True,
                               text=True, env={**os.environ, "PLAINKEEP_ENGINE_HOME": str(root2)})
        if inst2.returncode != 0:
            skip("engine verb is not blamed on a spoofed pack", f"install failed: {inst2.stderr[-300:]}")
            return
        b = pk(vault, td, "v_blame", "--yes", launcher=root2 / "engine" / "current" / "plainkeep",
               PLAINKEEP_PLUGIN_PACK="spoofed")
        check("...so an engine verb's own import failure is never blamed on a pack",
              "spoofed" not in b.stderr, f"rc={b.returncode} {b.stderr[:300]}")


# --------------------------------------------------------------------------------------------------
# H. Doc and template agree on what a plugin imports
# --------------------------------------------------------------------------------------------------
def case_doc_template_agreement() -> None:
    tpl = (REPO / "templates" / "verb" / "run.py").read_text(encoding="utf-8")
    imported = sorted({line.split("import")[1].split("#")[0].strip().split(" ")[0]
                       for line in tpl.splitlines() if line.startswith("from lib import")})
    check("the scaffold imports exactly ONE lib module", len(imported) == 1, str(imported))
    check("...and it is `api`, the frozen SDK", imported == ["api"], str(imported))
    doc = (REPO / "docs" / "plugins.md").read_text(encoding="utf-8")
    check("docs/plugins.md still states the one-module rule",
          "A plugin imports **one** module: `lib.api`" in doc)
    check("docs/plugins.md documents the dependency contract",
          "dependencies" in doc and "plugin sync" in doc)


def main() -> int:
    case_unmodified_fixture()
    case_installed_engine()
    case_precedence()
    case_pythonpath_scope()
    case_engine_verb_untouched()
    case_dependency_resolution()
    case_sync_with_pip()
    case_sync_refuses_shadowing_overlay()
    case_dependency_grammar()
    case_new_dependency_revokes_trust()
    case_overlay_is_not_a_pack()
    case_overlay_end_to_end_with_pip()
    case_pip_argv_is_closed()
    case_sync_audit_trail()
    case_engine_verb_clears_inherited_pack()
    case_doc_template_agreement()

    print(f"{BOLD}SDK compatibility + the plugin dependency contract (Phase 2 Task 3) — "
          f"{len(results)} checks{RESET}\n")
    passed = sum(1 for _, ok, _ in results if ok)
    for name, ok, detail in results:
        mark = f"{GREEN}PASS{RESET}" if ok else f"{RED}FAIL{RESET}"
        print(f"  {mark} {name:<66}" + (f" {DIM}{str(detail).strip()[:90]}{RESET}" if (detail and not ok) else ""))
    for name, why in skips:
        print(f"  {YEL}SKIP{RESET} {name:<66} {DIM}{why}{RESET}")
    failed = len(results) - passed
    print(f"\n{BOLD}Result:{RESET} {GREEN}{passed} passed{RESET}, "
          f"{(RED if failed else DIM)}{failed} failed{RESET}, "
          f"{(YEL if skips else DIM)}{len(skips)} skipped{RESET}, {len(results)} checks")
    for name, why in skips:
        print(f"SUITE-NOTE: NOT RUN — {name}: {why}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
