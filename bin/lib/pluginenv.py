"""
pluginenv.py — the PLUGIN SPAWN CONTRACT: what a dispatcher adds to a plugin verb's environment, and
nothing else. Stdlib-only, sibling-import-free (resolver.py loads it on the bare floor interpreter).

WHY THIS EXISTS. Phase 2 Task 2 moved the engine out of the vault. Every plugin ever scaffolded
bootstraps the SDK the way the pre-Task-2 template did:

    sys.path.insert(0, str(Path(os.environ["PLAINKEEP_HOME"]) / "bin"))
    from lib import api

`$PLAINKEEP_HOME/bin` is a directory a vault no longer has, so that line now prepends a path that
does not exist — which CPython SKIPS rather than fails on — and the import falls through to the rest
of `sys.path`. `PLAINKEEP_API_VERSION = "1.0"` is a public promise, so the fix has to require ZERO
plugin edits: the dispatcher puts the engine's own `bin/` on `PYTHONPATH` for the spawn, and the
stale insert becomes a harmless no-op. The engine's `bin/lib/` IS the compatibility layer — nothing
new is built, so nothing can drift from what it forwards to (ADR-018 D1).

THE TWO THINGS ADDED, and they are added ONLY for a verb the resolver answered `plugin:<pack>`:

  * `PYTHONPATH` — `<vault>/plugins/.deps` then `<engine>/bin`, prepended to whatever the caller had.
    The deps overlay comes FIRST so a pack's DECLARED dependency beats the engine tree's incidental
    top-level names (`bin/models/`, `bin/files/`, `bin/index/` are all importable namespace packages
    once `bin/` is on the path). The one name that inversion could cost is `lib` itself, which is why
    `plainkeep plugin sync` refuses an overlay that grew a top-level `lib` (ADR-018 D3).
  * `PLAINKEEP_PLUGIN_PACK` — WHICH pack is running. It is what lets `lib/api.py` name the pack in a
    missing-dependency refusal, and it is the flag that says "this process was spawned as a plugin",
    which nothing else in the environment says.

An ENGINE verb gets NEITHER. It self-locates through `__file__` and has never needed `PYTHONPATH`;
adding it there would put `<engine>/bin` in the environment of every `git`, every job, every child of
all 35 verbs, for no benefit at all. That is the per-spawn/per-process decision, made per-spawn.

THE LEAK, stated rather than implied. `PYTHONPATH` is inherited by everything a process spawns. So a
plugin verb's own children DO see these entries, and would keep seeing them to any depth. That is
narrowed at the earliest honest moment instead of being left open: `scrub_sdk_path()` below is called
by `lib/api.py` at import — i.e. by the very line the SDK contract makes every plugin execute — and
removes the ENGINE entry from `os.environ` once it has done its job. The interpreter's `sys.path` was
already built at startup, so the running plugin is unaffected; a child it spawns afterwards does not
inherit `<engine>/bin`. The overlay entry is deliberately KEPT: those are packages the pack declared
and a helper script of its own has the same claim on them. The window that remains — a child spawned
BEFORE the SDK import — is real, measured and documented (ADR-018 D2).
"""
from __future__ import annotations

import json
import os
from pathlib import Path

# The variable that carries the pack name into the verb. Read by lib/api.py (the missing-dependency
# refusal) and by the tests; written by BOTH dispatchers.
PACK_ENV = "PLAINKEEP_PLUGIN_PACK"

# The vault-local dependency overlay. Under `plugins/` rather than beside it because it is part of a
# vault's plugin state — the same tree `plugins.lock.json` describes and `script/update` never
# touches — and dot-prefixed because it is machinery, not a pack (the resolver skips it for the same
# reason `plugin_names()` never invents a pack called `.deps`).
DEPS_DIRNAME = ".deps"

SOURCE_PLUGIN_PREFIX = "plugin:"


def deps_dir(vault) -> Path:
    """The dependency overlay for a vault. May not exist — a missing `PYTHONPATH` entry is skipped by
    CPython, and both dispatchers add it unconditionally so neither has to stat anything to agree."""
    return Path(vault) / "plugins" / DEPS_DIRNAME


def engine_bin(engine) -> Path:
    return Path(engine) / "bin"


def pack_of(source: str | None) -> str | None:
    """The pack name behind a resolver source string, or None for an engine verb / no resolution."""
    if isinstance(source, str) and source.startswith(SOURCE_PLUGIN_PREFIX):
        return source[len(SOURCE_PLUGIN_PREFIX):] or None
    return None


def sdk_path_entries(engine, vault) -> list[str]:
    """The `PYTHONPATH` entries a plugin spawn gets, in order. ORDER IS THE CONTRACT — see the module
    docstring; both dispatchers and the TS port produce exactly this list."""
    return [str(deps_dir(vault)), str(engine_bin(engine))]


def prepend_path(entries: list[str], existing: str | None) -> str:
    """`entries` ahead of whatever the caller had. Duplicates are NOT filtered: a filter is a second
    behavior the TS port would have to reproduce exactly, and a repeated `sys.path` entry costs one
    failed stat. An empty/absent inherited value yields just the entries."""
    tail = existing or ""
    return os.pathsep.join(entries) + (os.pathsep + tail if tail else "")


def spawn_env(engine, vault, source: str | None, environ=None) -> dict[str, str]:
    """The environment ADDITIONS for one verb spawn — `{}` for an engine verb. The dispatcher merges
    this into the child's environment and changes nothing else."""
    pack = pack_of(source)
    if pack is None:
        return {}
    env = os.environ if environ is None else environ
    return {
        "PYTHONPATH": prepend_path(sdk_path_entries(engine, vault), env.get("PYTHONPATH")),
        PACK_ENV: pack,
    }


# --------------------------------------------------------------------------------------------------
# THE PRECEDENCE INVERSION, and the preflight that finds it.
#
# `sys.path[0]` is the SCRIPT'S OWN DIRECTORY and it precedes every PYTHONPATH entry. The pre-Task-2
# scaffold put the SDK at position 0 with `sys.path.insert`, AHEAD of the plugin's own directory, so
# a plugin that happened to ship a `lib.py` or `lib/` beside its `run.py` still got the engine's. On
# PYTHONPATH the order is the other way round and that plugin now imports ITS OWN `lib` — silently,
# and possibly with an `api` of its own that answers every call differently.
#
# This is not fixable from the dispatcher: `sys.path[0]` belongs to CPython, and prepending the SDK
# from outside the process is the whole mechanism. What IS possible is to make the case VISIBLE
# before it bites, which is what this function is for — `plainkeep doctor` reports it and `plugin
# add` says it at install time, when the pack can still be looked at.
# --------------------------------------------------------------------------------------------------
SDK_PACKAGE = "lib"


def _pack_roots(vault, environ=None) -> list[tuple[str, Path]]:
    """(pack_name, pack_dir) for the vault's own packs and each $PLAINKEEP_PATH root — the same set
    the resolver treats as packs, minus the dot-prefixed machinery (`.deps` is not a pack)."""
    env = os.environ if environ is None else environ
    roots: list[tuple[str, Path]] = []
    pdir = Path(vault) / "plugins"
    if pdir.is_dir():
        roots += [(d.name, d) for d in sorted(pdir.iterdir())
                  if d.is_dir() and not d.name.startswith(".")]
    for raw in (env.get("PLAINKEEP_PATH") or "").split(os.pathsep):
        root = raw.strip()
        if root:
            rp = Path(os.path.expanduser(root))
            if rp.is_dir():
                roots.append((rp.name, rp))
    return roots


def sdk_shadows(vault, environ=None) -> list[tuple[str, str]]:
    """(pack, verb) for every plugin verb directory that ships a top-level `lib` beside its run.py.

    Cheap by construction — two stats per verb directory — so it can sit in `doctor` without turning
    a health check into a tree walk.
    """
    out: list[tuple[str, str]] = []
    for pack, root in _pack_roots(vault, environ):
        for d in sorted(root.iterdir()):
            if not d.is_dir() or d.name.startswith("."):
                continue
            if (d / SDK_PACKAGE).is_dir() or (d / f"{SDK_PACKAGE}.py").is_file():
                out.append((pack, d.name))
    return out


# --------------------------------------------------------------------------------------------------
# In-process side: what a PLUGIN VERB's own interpreter does with all this. Both functions below are
# no-ops unless PACK_ENV is set, i.e. unless this process really was spawned as a plugin verb — an
# engine verb, a test importing `lib.api` directly, and anything else that reaches this module are
# all left exactly as they were.
# --------------------------------------------------------------------------------------------------
def running_pack(environ=None) -> str | None:
    env = os.environ if environ is None else environ
    return env.get(PACK_ENV) or None


def scrub_sdk_path(engine_bin_dir, environ=None) -> str | None:
    """Remove the ENGINE entry from `PYTHONPATH` in this process's environment, so children do not
    inherit it. Returns the new value (or None when the variable was dropped entirely).

    `sys.path` is NOT touched: it was built at interpreter startup and the plugin is mid-import.
    """
    env = os.environ if environ is None else environ
    raw = env.get("PYTHONPATH")
    if not raw:
        return raw
    target = str(Path(engine_bin_dir))
    kept = [p for p in raw.split(os.pathsep) if p != target]
    if len(kept) == len(raw.split(os.pathsep)):
        return raw
    value = os.pathsep.join(kept)
    if value:
        env["PYTHONPATH"] = value
        return value
    env.pop("PYTHONPATH", None)
    return None


# --------------------------------------------------------------------------------------------------
# The missing-dependency refusal. A plugin that imports something the environment does not have gets
# a CPython traceback naming the module and nothing else — not the pack, not where its dependencies
# are declared, not how to install them. The pack is what the operator can act on, so the pack is
# what the message names.
# --------------------------------------------------------------------------------------------------
def declared_dependencies(vault, pack: str) -> list[str]:
    """What `plugins.lock.json` records the pack as declaring. Never raises: this runs on the error
    path, and a lockfile problem must not replace the error the operator is actually looking at."""
    try:
        lock = json.loads((Path(vault) / "plugins" / "plugins.lock.json").read_text(encoding="utf-8"))
        deps = lock.get("plugins", {}).get(pack, {}).get("dependencies", [])
        return [str(d) for d in deps] if isinstance(deps, list) else []
    except Exception:
        return []


def requirement_name(req: str) -> str:
    """The distribution name at the head of a requirement string (`httpx>=0.27` -> `httpx`)."""
    head = str(req).strip()
    for sep in ("[", "=", "<", ">", "!", "~", " ", ";"):
        head = head.split(sep, 1)[0]
    return head.strip().lower().replace("-", "_")


def missing_dependency_message(module: str, pack: str, vault) -> str:
    """The refusal text for an uncaught ModuleNotFoundError inside a plugin verb.

    Two shapes, because the operator's next move is different: a DECLARED dependency that is missing
    means the overlay was never built (or was built for another interpreter) and `plugin sync` fixes
    it; an UNDECLARED one means the pack never said it needed the module, and the manifest is what
    has to change first. Guessing one message for both is how a user runs `sync` four times against a
    pack that declares nothing.
    """
    top = str(module).split(".", 1)[0]
    declared = declared_dependencies(vault, pack)
    if any(requirement_name(d) == top.lower().replace("-", "_") for d in declared):
        return (f"plainkeep: pack '{pack}' declares '{top}' as a dependency but it is not installed "
                f"for this interpreter\n"
                f"  fix: plainkeep plugin sync {pack} --yes")
    return (f"plainkeep: pack '{pack}' imported '{top}', which is not installed and which the pack "
            f"does not declare\n"
            f'  fix: add "dependencies": ["{top}"] to the pack\'s plugin.json, then '
            f"plainkeep plugin sync {pack} --yes")


def install_missing_dependency_hook(vault, environ=None) -> bool:
    """Install a `sys.excepthook` that turns an uncaught ModuleNotFoundError into the refusal above.
    Returns whether it was installed (it is not, outside a plugin spawn).

    EVERY OTHER EXCEPTION IS CHAINED to the hook that was there — this replaces one message, it does
    not take over the process's error reporting. And the exit code stays 1 (EXIT_UNEXPECTED): a
    missing module is neither a usage error (2) nor a policy refusal (5), it is the environment not
    being what the pack needs.
    """
    pack = running_pack(environ)
    if not pack:
        return False
    import sys

    previous = sys.excepthook

    def _hook(exc_type, exc, tb):
        if issubclass(exc_type, ModuleNotFoundError) and getattr(exc, "name", None):
            sys.stderr.write(missing_dependency_message(exc.name, pack, vault) + "\n")
            return
        previous(exc_type, exc, tb)

    sys.excepthook = _hook
    return True


def attach(vault, engine_bin_dir, environ=None) -> bool:
    """Everything a PLUGIN process does to itself, in the one call `lib/api.py` makes at import.

    Gated on PACK_ENV — outside a plugin spawn this is a no-op, so importing the SDK from a test, an
    engine verb or a REPL changes nothing about that process. Returns whether it ran.

    The order matters in one direction only: the excepthook is installed BEFORE the scrub, so a
    failure inside the scrub could not leave a process with neither.
    """
    if not running_pack(environ):
        return False
    install_missing_dependency_hook(vault, environ)
    scrub_sdk_path(engine_bin_dir, environ)
    return True
