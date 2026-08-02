#!/usr/bin/env python3
"""
plainkeep vault init|register|rebind|deregister|default|list|status — the vault marker + registry
surface (ADR-014, Phase 2 Tasks 1a + 1b + 5).

A vault is identified by an immutable `id` in `<vault>/.plainkeep/vault.json` and named by an entry
in `$XDG_CONFIG_HOME/plainkeep/registry.json`. This verb is the ONLY thing that writes either.

`init` (Task 5) CREATES a vault: content dirs, configuration, `plugins/`, a generated
`plainkeep.json`, the marker, a registry entry — and **no engine code**, because since Task 2 the
engine is a versioned read-only tree outside every vault. `register` ADOPTS a directory that is
already there, and stays the bootstrap for a vault that predates `init` (including the template
checkout, which is both a source of the engine and a vault).

**Where `init` lives, and why it is not a top-level `plainkeep init`.** Both dispatchers discover and
validate a data root BEFORE any verb runs, so a hypothetical `plainkeep init` would refuse on a
machine with no vault yet — the exact machine it exists for. That is the same wall
`vaultroot.bootstrap_hint` already answers for `register`, and the answer is the same one: on a
machine that has a vault, `plainkeep vault init <path>` dispatches normally; on a machine that has
none, this file is invoked directly, which is what `script/setup` does. Making it a pre-verb
intercept instead would have meant one in the bash floor and a second in the compiled core — two
implementations of a safety-relevant path, which is the drift this repo has already paid for.

init/register/rebind/deregister/default mutate state outside the current vault, so each refuses
without an explicit `--yes` (exit 3, with the exact re-run line). list/status are read-only.

`vault status` (Task 1b) is the DEBUGGING SURFACE for discovery: it re-runs the real chain
(lib/vaultroot.discover) and prints which of the four mechanisms won and what each of the others saw
— including when the chain refuses, which is exactly when an operator needs it.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lib import enginetree, output, paths, vaultreg, vaultroot  # noqa: E402
from lib.setuplib import REQUIRED_DIRS  # noqa: E402  (the ONE list of what a data vault must contain)

GREEN, RED, YEL, DIM, CYAN, RESET = "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[36m", "\033[0m"
MUTATING = ("init", "register", "rebind", "deregister", "default")


def _refuse(e: vaultreg.VaultError):
    output.fail(e.code, e.message, hint=e.hint, verb="vault")


def _need_yes(action: str, argv: list[str]) -> None:
    if "--yes" not in argv and "-y" not in argv:
        output.fail(output.EXIT_CONFIRM,
                    f"'vault {action}' changes the vault registry — re-run with --yes",
                    hint="re-run: plainkeep vault " + " ".join([action, *argv, "--yes"]),
                    verb="vault")


def _flag_value(argv: list[str], name: str) -> tuple[str | None, list[str]]:
    """Pull `--name X` out of argv, returning (value, remaining)."""
    out, val, i = [], None, 0
    while i < len(argv):
        if argv[i] == name and i + 1 < len(argv):
            val = argv[i + 1]
            i += 2
            continue
        out.append(argv[i])
        i += 1
    return val, out


# --- init: a DATA-ONLY vault (Phase 2 Task 5) ----------------------------------------------------
# What a fresh vault gets, and nothing else. Every entry here is DATA — there is no `bin/`, no
# launcher, no `VERSION`, no `skills/`, because the engine is a versioned tree outside every vault
# since Task 2 and a vault that carried a copy of it would be the Phase 1 shape this phase exists to
# end. `plainkeep vault init` asserts that (see `enginetree.engine_paths_in`).
#
# `REQUIRED_DIRS` is IMPORTED rather than restated: it is what `plainkeep doctor` gates a vault's
# readiness on, so a second list here would be a vault that init calls finished and doctor calls
# incomplete. That drift is not hypothetical — `setuplib`'s own comment records `bin` and `skills`
# sitting in that list until Task 2, which made `doctor --init` create an empty `bin/` in every vault.
INIT_GITIGNORE = """\
# The vault MARKER holds this vault's immutable id (bin/lib/vaultreg.py). It is deliberately NOT
# committed: an id names ONE vault, and a clone that carried it would be a second directory claiming
# to be the same vault — which is how a verb writes into the wrong notes. A clone becomes a vault of
# its own with `plainkeep vault register`.
.plainkeep/

# Machine-local, rebuildable caches — never source of truth.
.index/
/.venv/
/plugins/.deps/
.logs/
.cache/
"""

# The agent adapters. `plainkeep doctor` FAILS a vault without `AGENTS.md` and without a `CLAUDE.md`
# that bridges to it, so a vault that init left out of these is a vault whose very first health check
# is red — which is what "usable immediately" has to mean if it is to mean anything. They are
# VAULT-owned (doctor's own comment says so: they are that vault's instructions to its agents), so
# they are generated here rather than installed from the engine, and the operating manual they point
# at is the engine-owned SKILL.
#
# The engine is named through `current`, never through the version that happens to be active — see
# `enginetree.stable_launcher()`. A path written into a file that outlives the invocation and spells
# `…/engine/4.0.0/…` keeps pointing at the old pair after the next update and becomes ENOENT when
# that version is pruned, which is precisely what this task's retention/prune policy makes possible.
INIT_AGENTS_MD = """\
# {name} — a plainkeep vault

This directory is DATA. The plainkeep engine is installed separately, as a versioned read-only tree
outside every vault; nothing here is code, and nothing here should be edited to change how plainkeep
behaves.

## How to act on this vault

Go through the dispatcher — `plainkeep <verb>` — and never write into `tasks/`, `journal/`, `wiki/`
or `inbox/` by hand. The verbs enforce the guardrail, the path-wall and the audit log; a direct edit
enforces none of them.

    plainkeep --vault {name} help        the verb surface
    plainkeep --vault {name} status      where you are
    plainkeep --vault {name} doctor      whether this vault is healthy

`plainkeep.json` in this directory is the machine contract (schema, verbs, capabilities). It is
GENERATED — regenerate it with `plainkeep help` rather than editing it.

## The operating manual

Engine-owned, and it travels with the engine rather than with these notes:

    {skill}

That path goes through `current`, so it keeps naming the engine that is actually active after an
update or a rollback.
"""

INIT_CLAUDE_MD = """\
@AGENTS.md

The vault's instructions live in AGENTS.md, which every agent adapter reads. This file only bridges
to it, so there is one set of instructions rather than two that drift.
"""

INIT_JOBS_REGISTRY = {
    "description": "The jobs registry — scheduler-neutral job definitions. `plainkeep job apply` "
                   "renders launchd plists; `plainkeep job run <name>` runs any job manually. Jobs "
                   "call ONE verb; only read/safe_write may be scheduled.",
    "external_allowlist": [],
    "jobs": {},
}


def _nearest_existing(p: Path) -> Path:
    """The closest ancestor of `p` that is actually there — `p` itself when it exists.

    The location questions below are IDENTITY questions: `vaultreg.path_within` walks parents
    comparing `(st_dev, st_ino)`, and a path with no inode falls back to comparing strings, which is
    exactly the comparison that module's docstring documents two post-mortems for (case folding on
    APFS, macOS firmlinks). Asking them of an ancestor is sound in the direction that matters —
    containment is inherited downwards, so a parent inside the engine tree means the child would be
    too — and it means the refusal happens BEFORE anything is created."""
    q = Path(os.path.abspath(os.path.expanduser(str(p))))
    while not q.exists() and q.parent != q:
        q = q.parent
    return q


def _init_refusals(target: Path) -> None:
    """Every reason `target` may not become a new vault. Each one exits; there is no partial init.

    The order is the order an operator can act on: what the LOCATION is (which no amount of fixing
    the directory changes), then what is already there.

    Called TWICE by `cmd_init`: once on the path as given (which may not exist yet, so the location
    questions fall back to the nearest existing ancestor and only the two downward-inherited shapes
    are asked — see `enginetree.inside_engine_verdict`), and once on the canonical path after it has
    been created, where the full verdict and the real inode comparisons apply."""
    exists = target.exists()
    probe = target if exists else _nearest_existing(target)
    # DISJOINTNESS and the location policy, asked with the same functions `vaultroot.validate()` asks
    # them with on every dispatch. Creating a vault the dispatcher would then refuse with exit 5 is
    # the worst outcome available here: it succeeds, and every command afterwards fails.
    overlap = (enginetree.disjointness_verdict(str(probe)) if exists
               else enginetree.inside_engine_verdict(str(probe)))
    if overlap is not None:
        output.fail(output.EXIT_DENY, f"cannot init a vault at {target}, and {overlap}",
                    hint="a vault is data and an engine is code — pick a path outside "
                         f"{enginetree.ENGINE_ROOT}",
                    verb="vault")
    denied = vaultroot._policy_verdict(str(probe))
    if denied is not None:
        output.fail(output.EXIT_DENY, f"cannot init a vault at {target}, and {denied}",
                    hint="pick a local path outside that tree", verb="vault")
    engine_here = enginetree.engine_paths_in(target)
    if engine_here:
        output.fail(output.EXIT_USAGE,
                    f"{target} already carries engine code ({', '.join(engine_here[:3])}) — "
                    f"`init` creates a DATA-ONLY vault and will not adopt a checkout",
                    hint=f"to make this checkout a vault instead:\n    "
                         f"plainkeep vault register {target} --yes",
                    verb="vault")
    if vaultreg.read_marker(target) is not None:
        output.fail(output.EXIT_USAGE, f"{target} is already a plainkeep vault "
                                       f"({vaultreg.marker_path(target)})",
                    hint=f"register it: plainkeep vault register {target} --yes", verb="vault")


def _generate_manifest(target: Path) -> tuple[bool, str]:
    """Write `<vault>/plainkeep.json` by DISPATCHING into the new vault. Returns (ok, detail).

    Generated by running the product rather than by importing `manifest.write_manifest()` here, and
    the reason is the whole shape of this task. `manifest` binds `PLAINKEEP_HOME` at import — to the
    vault THIS process was pointed at, which is the operator's current vault and not the one being
    created — so an in-process call would write the manifest into the wrong directory. Spawning the
    installed launcher with the new root selected is the only spelling that puts it in the right one.

    It is also the proof, not a side effect: this is a real verb going through the real dispatcher —
    discovery, the marker, the registry entry, the guardrail, the resolver — against the vault that
    was just created. If a freshly `init`-ed vault were not immediately usable, this is the line that
    would fail, in the product, on the operator's own machine, and not only in a suite.

    `launcher()`, not `stable_launcher()`: this is a spawn happening NOW, and the version-pinned path
    is the correct one for a spawn (see enginetree)."""
    exe = enginetree.launcher()
    if not exe.is_file():
        return False, f"no installed launcher at {exe}"
    env = {k: v for k, v in os.environ.items()
           if k not in ("PLAINKEEP_VAULT_ID", "PLAINKEEP_VAULT_MECHANISM", "PLAINKEEP_PLUGIN_PACK")}
    env["PLAINKEEP_HOME"] = str(target)
    try:
        r = subprocess.run([str(exe), "help"], capture_output=True, text=True, timeout=120, env=env)
    except (OSError, subprocess.SubprocessError) as e:
        return False, str(e)
    if r.returncode != 0:
        return False, (r.stderr.strip().splitlines() or ["<no stderr>"])[-1]
    return (target / "plainkeep.json").is_file(), "written"


def cmd_init(argv):
    """Create a DATA-ONLY vault: content dirs, configuration, plugins/, a generated plainkeep.json,
    the marker, a registry entry — and no engine code."""
    _need_yes("init", argv)
    name, argv = _flag_value(argv, "--name")
    as_default = "--default" in argv
    rest = [a for a in argv if not a.startswith("-")]
    if not rest:
        output.fail(output.EXIT_USAGE, "usage: plainkeep vault init <path> [--name <slug>] "
                                       "[--default] --yes", verb="vault")
    raw = Path(os.path.abspath(os.path.expanduser(rest[0])))
    if raw.exists() and not raw.is_dir():
        output.fail(output.EXIT_USAGE, f"not a directory: {raw}", verb="vault")

    # THE LOCATION CHECKS RUN FIRST, against the nearest existing ancestor when the target is not
    # there yet (see `_nearest_existing`). Creating the directory first would let init `mkdir` inside
    # a sealed engine tree — which fails with a raw EACCES traceback rather than the disjointness
    # refusal that is the true and actionable answer — and would leave a directory behind on every
    # refusal after it.
    _init_refusals(raw)
    created_dir = not raw.exists()
    if created_dir:
        try:
            raw.mkdir(parents=True)
        except OSError as e:
            output.fail(output.EXIT_UNEXPECTED, f"cannot create {raw}: {e}", verb="vault")
    target = Path(vaultreg.canonical(raw))
    # Asked AGAIN on the canonical path now that it exists, so the identity comparisons run with
    # real inodes rather than on an ancestor. Cheap, and it is the one that is authoritative.
    try:
        _init_refusals(target)
    except SystemExit:
        if created_dir:
            try:
                # Only ever the empty directory this call made moments ago: `rmdir` refuses a
                # non-empty one, so anything that appeared in between survives.
                target.rmdir()
            except OSError:
                pass
        raise

    reg = vaultreg.read_registry()
    existing = vaultreg.entry_for_path(reg, target)
    if existing:
        output.fail(output.EXIT_USAGE, f"already registered as '{existing['name']}': {target}",
                    verb="vault")
    name = name or vaultreg.suggest_name(target)
    if not vaultreg.NAME_RE.match(name):
        output.fail(output.EXIT_USAGE,
                    f"invalid vault name {name!r} — lowercase letters, digits, '-' and '_', "
                    f"starting with a letter", verb="vault")
    if any(v["name"] == name for v in reg["vaults"]):
        output.fail(output.EXIT_USAGE, f"vault name {name!r} is already taken",
                    hint="pass a different --name", verb="vault")

    # --- the skeleton. Not behind the path-wall, for `cmd_register`'s reason (test/run_pathwall.py
    # EXEMPT): the wall classifies against the ACTIVE data root, and the target of an init is by
    # definition not it — classifying there would refuse every init but one into the current vault.
    made: list[str] = []
    for rel in [*REQUIRED_DIRS, "plugins"]:
        d = target / rel
        if not d.is_dir():
            d.mkdir(parents=True, exist_ok=True)
            made.append(rel + "/")
        # Git does not track an empty directory, so a vault cloned to a second machine would arrive
        # missing exactly the structure `doctor` gates on.
        keep = d / ".gitkeep"
        if not any(d.iterdir()):
            keep.touch()
    skill = enginetree.stable_launcher().parent / "skills" / "operate-plainkeep" / "SKILL.md"
    for rel, text in ((".gitignore", INIT_GITIGNORE),
                      ("jobs/registry.json", json.dumps(INIT_JOBS_REGISTRY, indent=2) + "\n"),
                      ("AGENTS.md", INIT_AGENTS_MD.format(name=name, skill=skill)),
                      ("CLAUDE.md", INIT_CLAUDE_MD)):
        f = target / rel
        if not f.exists():
            f.write_text(text, encoding="utf-8")
            made.append(rel)

    # --- identity: the marker, then the registry entry. Same two writes `register` makes, in the
    # same order and through the same module, so there is one spelling of what a vault IS.
    marker = vaultreg.new_marker_doc()
    vaultreg.marker_path(target).parent.mkdir(parents=True, exist_ok=True)
    vaultreg.marker_path(target).write_text(vaultreg.marker_bytes(marker), encoding="utf-8")
    reg["vaults"].append({"id": marker["id"], "name": name, "path": str(target)})
    if as_default or reg["default"] is None:
        reg["default"] = marker["id"]
    rp = vaultreg.write_registry(reg)

    manifest_ok, manifest_detail = _generate_manifest(target)

    # --- and the assertion the whole action is named for. `init` claims to produce a data-only
    # vault; this is the claim being checked against the filesystem by the code that made it, on
    # every real run, rather than only in a suite (ADR-019 D1).
    leaked = enginetree.engine_paths_in(target)

    data = {"id": marker["id"], "name": name, "path": str(target), "created": made,
            "registry": str(rp), "default": reg["default"] == marker["id"],
            "manifest": manifest_ok, "manifest_detail": manifest_detail,
            "data_only": not leaked, "engine_paths": leaked,
            "engine": str(enginetree.ENGINE_ROOT)}

    def render(_):
        print(f"{GREEN}initialized{RESET} '{name}' -> {target}")
        print(f"  id:       {marker['id']}")
        print(f"  created:  {len(made)} paths ({', '.join(made[:6])}"
              + (" …" if len(made) > 6 else "") + ")")
        print(f"  marker:   {vaultreg.marker_path(target)}")
        print(f"  registry: {rp}")
        if manifest_ok:
            print(f"  manifest: {target / 'plainkeep.json'}  {DIM}(generated by a real dispatch){RESET}")
        else:
            print(f"  manifest: {YEL}not generated{RESET} ({manifest_detail})")
            print(f"    {CYAN}plainkeep --vault {name} help{RESET}  regenerates it")
        if leaked:
            print(f"  {RED}NOT data-only{RESET}: {', '.join(leaked)}")
        else:
            print(f"  {DIM}data-only: no engine code in the vault (the engine stays at "
                  f"{enginetree.ENGINE_ROOT}){RESET}")
        if data["default"]:
            print(f"  {CYAN}this is now the default vault{RESET}")
        print(f"  use it:   {CYAN}plainkeep --vault {name} status{RESET}")
    return output.emit(data, "vault", human=render)


# --- actions -------------------------------------------------------------------------------------
def cmd_register(argv):
    _need_yes("register", argv)
    name, argv = _flag_value(argv, "--name")
    as_default = "--default" in argv
    rest = [a for a in argv if not a.startswith("-")]
    target = Path(vaultreg.canonical(rest[0] if rest else os.getcwd()))
    if not target.is_dir():
        output.fail(output.EXIT_NOT_FOUND, f"not a directory: {target}", verb="vault")

    reg = vaultreg.read_registry()
    existing_path = vaultreg.entry_for_path(reg, target)
    if existing_path:
        output.fail(output.EXIT_USAGE,
                    f"already registered as '{existing_path['name']}': {target}",
                    hint="rename with: plainkeep vault deregister <name> --yes, then register again",
                    verb="vault")

    marker = vaultreg.read_marker(target)
    created_marker = marker is None
    if created_marker:
        marker = vaultreg.new_marker_doc()

    if any(v["id"] == marker["id"] for v in reg["vaults"]):
        other = next(v for v in reg["vaults"] if v["id"] == marker["id"])
        output.fail(output.EXIT_USAGE,
                    f"this vault (id {marker['id']}) is already registered as '{other['name']}' "
                    f"at {other['path']}",
                    hint=f"it MOVED? re-point it with: plainkeep vault rebind {other['name']} {target} --yes",
                    verb="vault")

    name = name or vaultreg.suggest_name(target)
    if not vaultreg.NAME_RE.match(name):
        output.fail(output.EXIT_USAGE,
                    f"invalid vault name {name!r} — lowercase letters, digits, '-' and '_', "
                    f"starting with a letter", verb="vault")
    if any(v["name"] == name for v in reg["vaults"]):
        output.fail(output.EXIT_USAGE, f"vault name {name!r} is already taken",
                    hint="pass a different --name", verb="vault")

    if created_marker:
        # NOT behind the path-wall, deliberately — see test/run_pathwall.py EXEMPT. This is the one
        # write that ESTABLISHES where the wall goes: the target vault is by definition not yet the
        # active data root, so classifying against the active root would refuse every registration
        # but the current vault's. It writes exactly one file, and only with --yes.
        d = vaultreg.marker_path(target).parent
        d.mkdir(parents=True, exist_ok=True)
        vaultreg.marker_path(target).write_text(vaultreg.marker_bytes(marker), encoding="utf-8")

    reg["vaults"].append({"id": marker["id"], "name": name, "path": str(target)})
    if as_default or reg["default"] is None:
        reg["default"] = marker["id"]
    rp = vaultreg.write_registry(reg)

    data = {"id": marker["id"], "name": name, "path": str(target),
            "marker_created": created_marker, "default": reg["default"] == marker["id"],
            "registry": str(rp)}

    def render(_):
        what = "registered" if not created_marker else "marked + registered"
        print(f"{GREEN}{what}{RESET} '{name}' -> {target}")
        print(f"  id:       {marker['id']}")
        print(f"  marker:   {vaultreg.marker_path(target)}" + ("" if created_marker else f" {DIM}(already present){RESET}"))
        print(f"  registry: {rp}")
        if data["default"]:
            print(f"  {CYAN}this is now the default vault{RESET}")
    return output.emit(data, "vault", human=render)


def cmd_rebind(argv):
    _need_yes("rebind", argv)
    rest = [a for a in argv if not a.startswith("-")]
    if len(rest) < 2:
        output.fail(output.EXIT_USAGE, "usage: plainkeep vault rebind <name|id> <new-path> --yes",
                    verb="vault")
    reg = vaultreg.read_registry(required=True)
    entry = vaultreg.find(reg, rest[0])
    if not entry:
        output.fail(output.EXIT_NOT_FOUND, f"no registered vault {rest[0]!r}", verb="vault")
    target = Path(vaultreg.canonical(rest[1]))
    if not target.is_dir():
        output.fail(output.EXIT_NOT_FOUND, f"not a directory: {target}", verb="vault")

    marker = vaultreg.read_marker(target)
    if marker is None:
        output.fail(output.EXIT_USAGE, f"no vault marker at {target}",
                    hint="rebind re-points an EXISTING vault; a new location needs: "
                         "plainkeep vault register <path> --yes",
                    verb="vault")
    # The id is the identity. A different id means this is a DIFFERENT vault, and silently accepting
    # it would repoint a name at someone else's notes.
    if marker["id"] != entry["id"]:
        output.fail(output.EXIT_USAGE,
                    f"{target} is a different vault (id {marker['id']}, expected {entry['id']})",
                    verb="vault")
    clash = vaultreg.entry_for_path(reg, target)
    if clash and clash["id"] != entry["id"]:
        output.fail(output.EXIT_USAGE, f"{target} is already registered as '{clash['name']}'",
                    verb="vault")

    was, entry["path"] = entry["path"], str(target)
    vaultreg.write_registry(reg)
    data = {"id": entry["id"], "name": entry["name"], "path": str(target), "previous_path": was}
    return output.emit(data, "vault", human=lambda _:
                       f"{GREEN}rebound{RESET} '{entry['name']}': {was} -> {target}")


def cmd_deregister(argv):
    _need_yes("deregister", argv)
    rest = [a for a in argv if not a.startswith("-")]
    if not rest:
        output.fail(output.EXIT_USAGE, "usage: plainkeep vault deregister <name|id> --yes", verb="vault")
    reg = vaultreg.read_registry(required=True)
    entry = vaultreg.find(reg, rest[0])
    if not entry:
        output.fail(output.EXIT_NOT_FOUND, f"no registered vault {rest[0]!r}", verb="vault")
    reg["vaults"] = [v for v in reg["vaults"] if v["id"] != entry["id"]]
    cleared = reg["default"] == entry["id"]
    if cleared:
        # Never auto-promote another vault: a silently-changed default is exactly how a verb writes
        # into the wrong notes.
        reg["default"] = None
    vaultreg.write_registry(reg)
    data = {"id": entry["id"], "name": entry["name"], "path": entry["path"],
            "default_cleared": cleared, "marker_kept": True}

    def render(_):
        print(f"{GREEN}deregistered{RESET} '{entry['name']}' ({entry['path']})")
        print(f"  {DIM}the vault and its marker are untouched — only the registry entry is gone{RESET}")
        if cleared:
            print(f"  {YEL}that was the default; there is now no default vault{RESET}")
    return output.emit(data, "vault", human=render)


def cmd_default(argv):
    _need_yes("default", argv)
    rest = [a for a in argv if not a.startswith("-")]
    if not rest:
        output.fail(output.EXIT_USAGE, "usage: plainkeep vault default <name|id> --yes", verb="vault")
    reg = vaultreg.read_registry(required=True)
    entry = vaultreg.find(reg, rest[0])
    if not entry:
        output.fail(output.EXIT_NOT_FOUND, f"no registered vault {rest[0]!r}", verb="vault")
    reg["default"] = entry["id"]
    vaultreg.write_registry(reg)
    return output.emit({"id": entry["id"], "name": entry["name"], "path": entry["path"]}, "vault",
                       human=lambda _: f"{GREEN}default vault{RESET} is now '{entry['name']}' ({entry['path']})")


def cmd_list(_argv):
    reg = vaultreg.read_registry()
    rows = [{"name": v["name"], "id": v["id"], "path": v["path"],
             "default": v["id"] == reg["default"],
             "marker": vaultreg.marker_path(v["path"]).is_file()}
            for v in reg["vaults"]]

    def render(rs):
        if not rs:
            print(f"no vaults registered ({vaultreg.registry_path()})")
            print(f"  register this one: {CYAN}plainkeep vault register --yes{RESET}")
            return
        for r in rs:
            mark = f"{CYAN}*{RESET}" if r["default"] else " "
            miss = "" if r["marker"] else f"  {RED}MARKER MISSING{RESET}"
            print(f" {mark} {r['name']:<16} {r['path']}{miss}")
        print(f"{DIM}  * = default · registry: {vaultreg.registry_path()}{RESET}")
    return output.emit_rows(rows, "vault", human=render,
                            header={"registry": str(vaultreg.registry_path()),
                                    "default": reg["default"]})


def cmd_status(_argv):
    """Which root this invocation is acting on, WHICH OF THE FOUR MECHANISMS chose it, and what each
    of the others saw (ADR-014 Task 1b).

    The `saw` map is the point, not decoration. Every refusal this task adds is "the chain came up
    empty" or "step N refused" — and an operator who cannot see what step N looked at will work
    around the refusal instead of fixing it. `status` is the one place that answers it, so it re-runs
    the REAL chain (lib/vaultroot.discover) rather than describing what it thinks the chain would do.

    The two answers are deliberately separate: `active_root` is what THIS process was handed
    (PLAINKEEP_HOME, exported by the dispatcher that spawned it), while `would_select` is what the
    chain picks in this cwd WITH PLAINKEEP_HOME OUT OF THE WAY. They differ exactly when the answer
    is interesting — inside a vault while pointed at another one, say — and collapsing them into one
    field would hide it.

    **Why the chain is re-run without PLAINKEEP_HOME, and why `selected_by` does not come from that
    re-run.** This verb runs in a process the dispatcher already exported PLAINKEEP_HOME into, so a
    plain `discover()` here has step 2 pre-satisfied: it returns "PLAINKEEP_HOME" for EVERY
    invocation that can exist, steps 3 and 4 are never reached, `saw` can never carry their lines,
    and `selection_error` can never be a chain refusal. Measured, and it made VaultError.saw a field
    with no reachable reader in production. So the two questions are separated: `selected_by` is
    what the dispatcher recorded (PLAINKEEP_VAULT_MECHANISM), and the chain is re-run with
    PLAINKEEP_HOME removed so the mechanisms BELOW it can be asked at all."""
    active = Path(vaultreg.canonical(paths.PLAINKEEP_HOME))
    reg = vaultreg.read_registry()
    try:
        marker = vaultreg.read_marker(active)
        marker_err = None
    except vaultreg.VaultError as e:
        marker, marker_err = None, e.message
    entry = vaultreg.entry_for_path(reg, active)

    mech = vaultroot.active_mechanism()
    home_raw = os.environ.get(vaultroot.ENV_HOME)

    # Run the chain with step 2 taken out of the way. A refusal is an ANSWER here, not an error:
    # `vault status` is the verb you reach for precisely when discovery is refusing, so it reports
    # the refusal and still exits 0. Restored in a `finally` — this process still has to be able to
    # find its own root afterwards.
    try:
        os.environ.pop(vaultroot.ENV_HOME, None)
        try:
            sel = vaultroot.discover()
            sel_err, saw = None, dict(sel.saw)
        except vaultreg.VaultError as e:
            sel, sel_err, saw = None, e.message, dict(e.saw)
    finally:
        if home_raw is not None:
            os.environ[vaultroot.ENV_HOME] = home_raw

    # The re-run cannot see the two mechanisms that were already decided for THIS process — the
    # selector is gone from argv by the time a verb is spawned, and PLAINKEEP_HOME was popped four
    # lines up — so those two lines are restored from what actually happened. Everything below them
    # is the re-run's own honest account, and it is labelled as such in `saw_is`.
    if mech == vaultroot.MECHANISMS[0]:
        saw[vaultroot.MECHANISMS[0]] = f"SELECTED this invocation -> {active}"
    if home_raw is not None:
        if mech is None:
            why = "no dispatcher exported a mechanism — this verb was invoked directly"
        elif mech == vaultroot.MECHANISMS[1]:
            why = "SELECTED this invocation"
        else:
            why = f"exported by the dispatcher after {mech} chose — not what chose"
        saw[vaultroot.MECHANISMS[1]] = f"{home_raw!r} — {why}"

    data = {
        "active_root": str(active),
        # The RAW value this process was handed, uncanonicalized. It is reported separately from
        # `active_root` (which is canonical) because the two being equal is the assertion Task 1b
        # cares about: the dispatcher exports the canonical realpath, so a verb that sees the
        # caller's spelling means the export regressed. Canonicalizing before reporting would hide
        # exactly that — measured: a mutation returning the caller's spelling passed until this
        # field existed.
        "home_env": os.environ.get("PLAINKEEP_HOME"),
        # What ACTUALLY chose this process's root. From the dispatcher, never recomputed here.
        # A directly-invoked verb has no dispatcher: PLAINKEEP_HOME is then the only thing that
        # pointed it anywhere, which is a true answer rather than a missing one — `selected_by_source`
        # is what distinguishes the two, so a caller is never left guessing which it got.
        "selected_by": mech or (vaultroot.MECHANISMS[1] if home_raw else None),
        "selected_by_source": "dispatcher" if mech else (
            "PLAINKEEP_HOME (no dispatcher — this verb was invoked directly)" if home_raw else None),
        "would_select": sel.root if sel else None,
        "would_select_id": sel.id if sel else None,
        "selection_error": sel_err,
        # One line per mechanism, in precedence order, INCLUDING the ones that did not win.
        "saw": saw,
        "saw_is": "the chain re-run in this cwd with PLAINKEEP_HOME removed, so the mechanisms "
                  "below it can be asked at all; the --vault and PLAINKEEP_HOME lines report what "
                  "this invocation actually did",
        "marker": str(vaultreg.marker_path(active)) if marker else None,
        "marker_error": marker_err,
        "id": marker["id"] if marker else None,
        "vault_id_env": vaultroot.active_id(),
        "registered_as": entry["name"] if entry else None,
        "registry": str(vaultreg.registry_path()),
        "registry_exists": vaultreg.registry_path().is_file(),
        "default": reg["default"],
        "discovery": "task-1b",
        # THE ENGINE, reported beside the vault (Phase 2 Task 2) — the two roots this invocation
        # actually used, so "code and data are separate trees" is something an operator can SEE
        # rather than a claim in a doc. Three fields, and the third is the point:
        #
        #   engine_root  — where this process's code IS, derived from `__file__`. Authoritative.
        #   engine_env   — what PLAINKEEP_ENGINE says. The dispatcher REPLACES any inherited value,
        #                  so in a real invocation the two agree; when they do not, the variable is
        #                  wrong and the code is right, which is what `engine_env_matches` says.
        #
        # ADR-014 D2 requires that caller input not control where code is loaded from. That is true
        # here by construction (nothing in bin/lib reads PLAINKEEP_ENGINE), and this is where it
        # becomes OBSERVABLE: `PLAINKEEP_ENGINE=/evil plainkeep vault status --json` reports the real
        # tree and `engine_env_matches: true`, because the dispatcher overwrote /evil on the way in.
        "engine_root": str(paths.ENGINE),
        "engine_env": os.environ.get(enginetree.ENV_ENGINE),
        "engine_env_matches": os.environ.get(enginetree.ENV_ENGINE) == str(paths.ENGINE),
        "engine_intact": not enginetree.verify(paths.ENGINE),
    }

    def render(_):
        print(f"active root   {active}")
        if marker_err:
            print(f"  marker      {RED}{marker_err}{RESET}")
        elif marker:
            print(f"  marker      {vaultreg.marker_path(active)}  (id {marker['id']})")
        else:
            print(f"  marker      {YEL}none{RESET} — mark + register it:")
            print(f"    {CYAN}{vaultroot.bootstrap_hint(active)}{RESET}")
        print(f"  registered  {entry['name'] if entry else YEL + 'no' + RESET}")
        print(f"  selected by {data['selected_by'] or YEL + 'nothing — no root is selected' + RESET}"
              + (f"  {DIM}({data['selected_by_source']}){RESET}"
                 if data["selected_by_source"] and not mech else ""))
        print()
        # The arrow marks what REALLY chose, which is `selected_by` — not the winner of the re-run
        # below it. Marking the re-run's winner is what made this display say "PLAINKEEP_HOME" on
        # every invocation that can exist.
        print("discovery     (the chain, in precedence order)")
        for m in vaultroot.MECHANISMS:
            mark = f"{GREEN}->{RESET}" if data["selected_by"] == m else "  "
            print(f"  {mark} {m:<26} {data['saw'].get(m, DIM + 'not reached' + RESET)}")
        if sel:
            print(f"  {DIM}would select{RESET}  {sel.root}  (id {sel.id})"
                  f"  {DIM}(ignoring PLAINKEEP_HOME){RESET}")
        else:
            print(f"  {DIM}would select{RESET}  {RED}REFUSED{RESET} {sel_err}"
                  f"  {DIM}(ignoring PLAINKEEP_HOME){RESET}")
        print()
        # The OTHER root. Printed after discovery because that is the reading order of the question
        # an operator brings here — "which notes am I on, and which code is on them".
        print(f"engine        {data['engine_root']}"
              + ("" if data["engine_intact"] else f"  {RED}INCOMPLETE{RESET}"))
        env_eng = data["engine_env"]
        if env_eng is None:
            print(f"  exported    {YEL}PLAINKEEP_ENGINE unset{RESET}  "
                  f"{DIM}(no dispatcher — this verb was invoked directly){RESET}")
        elif data["engine_env_matches"]:
            print(f"  exported    PLAINKEEP_ENGINE agrees  "
                  f"{DIM}(the dispatcher replaces any inherited value){RESET}")
        else:
            print(f"  exported    {RED}PLAINKEEP_ENGINE={env_eng}{RESET} — disagrees with the code "
                  f"actually running")
        print()
        print(f"registry      {vaultreg.registry_path()}"
              + ("" if data["registry_exists"] else f"  {DIM}(does not exist yet){RESET}"))
        dflt = next((v for v in reg["vaults"] if v["id"] == reg["default"]), None)
        print(f"  default     {dflt['name'] if dflt else DIM + 'none' + RESET}")
    return output.emit(data, "vault", human=render)


ACTIONS = {"init": cmd_init, "register": cmd_register, "rebind": cmd_rebind,
           "deregister": cmd_deregister, "default": cmd_default, "list": cmd_list,
           "status": cmd_status}


def main(argv):
    _, argv = output.parse_argv(argv)
    action = argv[0] if argv and not argv[0].startswith("-") else "list"
    rest = argv[1:] if (argv and not argv[0].startswith("-")) else argv
    fn = ACTIONS.get(action)
    if not fn:
        output.fail(output.EXIT_USAGE,
                    f"unknown action {action!r} — one of: {', '.join(ACTIONS)}", verb="vault")
    try:
        return fn(rest)
    except vaultreg.VaultError as e:
        _refuse(e)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
