"""
resolver.py — multi-root verb resolution (proposal Part 2.1). One source of truth for turning a verb
name into the directory that holds its `run.py` + `cmd.json`, in STRICT precedence:

    1. bin/<verb>/            — the engine, RESERVED (always wins; a plugin can never shadow a
                                core or future-core verb)
    2. plugins/<pack>/<verb>/ — user packs inside the vault (PLAINKEEP_HOME), survive `script/update`
    3. $PLAINKEEP_PATH roots        — colon-separated extra pack roots, each a dir of <verb>/ folders

A plugin verb is the EXACT same shape as an engine verb (run.py + cmd.json) — zero new runtime. The
dispatcher asks this module for a run.py path; the guardrail asks it for the verb set + cmd.json
lookup; the manifest globs every cmd.json through it and tags each with its `source`. So every agent
discovers plugins through plainkeep.json with no other change.

Engine names are reserved: a plugin verb whose name collides with an engine verb is IGNORED (see
`shadowed()`), surfaced as a warning in `plainkeep help`. PLAINKEEP_HOME/PLAINKEEP_PATH are read per call so the running
process and the test harness (PLAINKEEP_HOME/PLAINKEEP_PATH env) see the same resolution.
"""
from __future__ import annotations
import os
import sys
from pathlib import Path

ENGINE_BIN = Path(__file__).resolve().parents[1]          # bin/ — ships with the CODE, reserved

sys.path.insert(0, str(Path(__file__).resolve().parent))  # importable as `lib.resolver` AND top-level
import vaultroot  # noqa: E402
import pluginenv  # noqa: E402


def _ops_home() -> Path:
    """The SELECTED data root — where PLUGIN packs live. `ENGINE_BIN.parent` used to be the fallback,
    which is the fifth copy of the engine-relative vault derivation ADR-014 D2 deletes (the plan
    section enumerates four Python sites plus `resolveHome()`; this one is the same class and is
    called out in the Task 1b report). Note the distinction the ADR draws and this file already got
    right: deriving the ENGINE from the code's own location is correct — `ENGINE_BIN` above — while
    deriving the VAULT from it is the assumption being deleted."""
    return vaultroot.active_root()


def _is_verb_dir(d: Path) -> bool:
    return (d / "run.py").exists() or (d / "cmd.json").exists()


def _plugin_packs() -> list[tuple[str, Path]]:
    """(pack_name, pack_dir) for each plugins/<pack>/ under PLAINKEEP_HOME, then each $PLAINKEEP_PATH root (the
    root itself is the pack). Order is the resolution order after the engine.

    A DOT-PREFIXED subdirectory is not a pack — `.git`, an OS turd, or a legacy `plugins/.deps/`
    dependency overlay left by a `plugin sync` from before the overlay moved to `<vault>/.plugin-deps/`
    (pluginenv.py). This filter used to be absent, and its absence is what let pip content be
    dispatched: a wheel that ships `<pkg>/run.py` unpacked into `plugins/.deps/` resolved as the verb
    `<pkg>` under a "pack" no lockfile recorded. The overlay's move out of `plugins/` is what actually
    fixes that; this line is what covers a vault that already has the old directory on disk. Same
    words, same reason as `pluginenv._pack_roots()` — the two enumerations agree by construction."""
    packs: list[tuple[str, Path]] = []
    pdir = _ops_home() / "plugins"
    if pdir.is_dir():
        for sub in sorted(pdir.iterdir()):
            if sub.is_dir() and not sub.name.startswith("."):
                packs.append((sub.name, sub))
    for root in os.environ.get("PLAINKEEP_PATH", "").split(":"):
        root = root.strip()
        if not root:
            continue
        rp = Path(os.path.expanduser(root))
        if rp.is_dir():
            packs.append((rp.name, rp))
    return packs


def _engine_names() -> set[str]:
    return ({p.parent.name for p in ENGINE_BIN.glob("*/cmd.json")}
            | {p.parent.name for p in ENGINE_BIN.glob("*/run.py")})


def resolve(verb: str) -> tuple[Path, str] | None:
    """(verb_dir, source) with source 'engine' | 'plugin:<pack>', strict precedence. None if unknown."""
    d = ENGINE_BIN / verb
    if _is_verb_dir(d):
        return d, "engine"
    for name, pack in _plugin_packs():
        d = pack / verb
        if _is_verb_dir(d):
            return d, f"plugin:{name}"
    return None


def resolve_verb(verb: str) -> Path | None:
    r = resolve(verb)
    return r[0] if r else None


def run_py(verb: str) -> Path | None:
    d = resolve_verb(verb)
    return d / "run.py" if d and (d / "run.py").exists() else None


def cmd_json_path(verb: str) -> Path | None:
    d = resolve_verb(verb)
    return d / "cmd.json" if d and (d / "cmd.json").exists() else None


def source_of(verb: str) -> str | None:
    r = resolve(verb)
    return r[1] if r else None


def is_engine_verb(verb: str) -> bool:
    return _is_verb_dir(ENGINE_BIN / verb)


def known_verbs() -> set[str]:
    """Every resolvable verb name across engine + plugins + $PLAINKEEP_PATH (engine names are reserved,
    but a shadowing plugin doesn't change the NAME set — it only loses the resolution)."""
    names = _engine_names()
    for _, pack in _plugin_packs():
        names |= ({p.parent.name for p in pack.glob("*/cmd.json")}
                  | {p.parent.name for p in pack.glob("*/run.py")})
    return names


def plugin_names() -> list[str]:
    """Sorted, de-duplicated pack names that contribute at least one (non-shadowed) verb — for
    plainkeep.json `capabilities.plugins`."""
    engine = _engine_names()
    seen: set[str] = set()
    out: list[str] = []
    for name, pack in _plugin_packs():
        if name in seen:
            continue
        if any(_is_verb_dir(d) and d.name not in engine for d in pack.iterdir() if d.is_dir()):
            seen.add(name)
            out.append(name)
    return sorted(out)


def iter_cmds() -> list[tuple[Path, str]]:
    """All (cmd.json path, source) in resolution order — engine first (reserved). A plugin verb whose
    name is already claimed (by the engine or an earlier pack) is SKIPPED, so plainkeep.json never lists a
    shadowed verb; `shadowed()` reports the engine collisions as warnings."""
    seen: set[str] = set()
    out: list[tuple[Path, str]] = []
    for p in sorted(ENGINE_BIN.glob("*/cmd.json")):
        seen.add(p.parent.name)
        out.append((p, "engine"))
    for name, pack in _plugin_packs():
        for p in sorted(pack.glob("*/cmd.json")):
            if p.parent.name in seen:
                continue
            seen.add(p.parent.name)
            out.append((p, f"plugin:{name}"))
    return out


def shadowed() -> list[tuple[str, str]]:
    """(verb, pack) for every plugin verb IGNORED because it collides with a reserved engine verb."""
    engine = _engine_names()
    out: list[tuple[str, str]] = []
    for name, pack in _plugin_packs():
        for d in sorted(pack.glob("*/")):
            if _is_verb_dir(d) and d.name in engine:
                out.append((d.name, name))
    return out


def _dispatch_lines(verb: str) -> list[str] | None:
    """The bash floor's whole answer, in ONE spawn: the run.py to exec, WHICH PACK it belongs to
    (empty for an engine verb), and the `PYTHONPATH` the child must be given (likewise empty).

    The third line is computed HERE, not in the shell, because it is policy: which entries, in which
    order, merged with the caller's own value (pluginenv.spawn_env). This process inherits the
    floor's environment, so the merge it does is the merge the floor would have done — and the floor
    is left with `export PYTHONPATH="$LINE3"`, which cannot drift from the rule. The compiled core
    ports the same two functions (cli/src/core/dispatch.ts) and the parity oracle compares the child
    env both dispatchers actually produce.

    A separate flag rather than extra lines on the bare `resolver.py <verb>` form: that form is the
    resolver differential's own subject (`--core-resolve` prints exactly one path), and widening it
    would change what the oracle compares in order to serve a caller that is not it.
    """
    r = resolve(verb)
    if r is None:
        return None
    d, source = r
    p = d / "run.py"
    if not p.exists():
        return None
    env = pluginenv.spawn_env(ENGINE_BIN.parent, _ops_home(), source)
    return [str(p), pluginenv.pack_of(source) or "", env.get("PYTHONPATH", "")]


if __name__ == "__main__":
    # Dispatcher helper: print the resolved run.py path (empty + exit 4 if the verb has none).
    args = sys.argv[1:]
    if args and args[0] == "--dispatch":
        lines = _dispatch_lines(args[1] if len(args) > 1 else "")
        if lines is None:
            raise SystemExit(4)
        print("\n".join(lines))
        raise SystemExit(0)
    v = args[0] if args else ""
    p = run_py(v)
    if p:
        print(p)
        raise SystemExit(0)
    raise SystemExit(4)
