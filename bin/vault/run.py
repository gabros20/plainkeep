#!/usr/bin/env python3
"""
plainkeep vault register|rebind|deregister|default|list|status — the vault marker + registry surface
(ADR-014, Phase 2 Task 1a).

A vault is identified by an immutable `id` in `<vault>/.plainkeep/vault.json` and named by an entry
in `$XDG_CONFIG_HOME/plainkeep/registry.json`. This verb is the ONLY thing that writes either, and it
is also the bootstrap: an existing vault predates `init` and migration, so `plainkeep vault register`
is how it acquires a marker at all.

register/rebind/deregister/default mutate state outside the current vault, so each refuses without an
explicit `--yes` (exit 3, with the exact re-run line). list/status are read-only.

**Task 1a changes no discovery behaviour.** `PLAINKEEP_HOME` still resolves exactly as it did before;
`vault status` reports what the CURRENT rules select and says so. The `--vault` selector, marker
walk-up and the registry default arrive in Task 1b, on top of this.
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lib import output, paths, vaultreg  # noqa: E402

GREEN, RED, YEL, DIM, CYAN, RESET = "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[36m", "\033[0m"
MUTATING = ("register", "rebind", "deregister", "default")


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
    """What root would this environment select, and why. In Task 1a the answer is still 'whatever
    PLAINKEEP_HOME resolves to' — discovery is Task 1b — so this reports the current mechanism
    honestly rather than describing one that does not exist yet."""
    active = Path(vaultreg.canonical(paths.PLAINKEEP_HOME))
    env_set = bool(os.environ.get("PLAINKEEP_HOME"))
    reg = vaultreg.read_registry()
    try:
        marker = vaultreg.read_marker(active)
        marker_err = None
    except vaultreg.VaultError as e:
        marker, marker_err = None, e.message
    entry = vaultreg.entry_for_path(reg, active)
    data = {
        "active_root": str(active),
        "selected_by": "PLAINKEEP_HOME" if env_set else "engine-relative fallback (parents[2])",
        "marker": str(vaultreg.marker_path(active)) if marker else None,
        "marker_error": marker_err,
        "id": marker["id"] if marker else None,
        "registered_as": entry["name"] if entry else None,
        "registry": str(vaultreg.registry_path()),
        "registry_exists": vaultreg.registry_path().is_file(),
        "default": reg["default"],
        "discovery": "task-1a",
    }

    def render(_):
        print(f"active root   {active}")
        print(f"  selected by {data['selected_by']}")
        if marker_err:
            print(f"  marker      {RED}{marker_err}{RESET}")
        elif marker:
            print(f"  marker      {vaultreg.marker_path(active)}  (id {marker['id']})")
        else:
            print(f"  marker      {YEL}none{RESET} — register it: {CYAN}plainkeep vault register --yes{RESET}")
        print(f"  registered  {entry['name'] if entry else YEL + 'no' + RESET}")
        print(f"registry      {vaultreg.registry_path()}"
              + ("" if data["registry_exists"] else f"  {DIM}(does not exist yet){RESET}"))
        dflt = next((v for v in reg["vaults"] if v["id"] == reg["default"]), None)
        print(f"  default     {dflt['name'] if dflt else DIM + 'none' + RESET}")
        print(f"{DIM}discovery: the --vault selector, marker walk-up and the registry default are "
              f"Task 1b — today the active root is still whatever PLAINKEEP_HOME resolves to.{RESET}")
    return output.emit(data, "vault", human=render)


ACTIONS = {"register": cmd_register, "rebind": cmd_rebind, "deregister": cmd_deregister,
           "default": cmd_default, "list": cmd_list, "status": cmd_status}


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
