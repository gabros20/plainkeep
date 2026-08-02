#!/usr/bin/env python3
"""
plainkeep vault register|rebind|deregister|default|list|status — the vault marker + registry surface
(ADR-014, Phase 2 Tasks 1a + 1b).

A vault is identified by an immutable `id` in `<vault>/.plainkeep/vault.json` and named by an entry
in `$XDG_CONFIG_HOME/plainkeep/registry.json`. This verb is the ONLY thing that writes either, and it
is also the bootstrap: an existing vault predates `init` and migration, so `plainkeep vault register`
is how it acquires a marker at all.

register/rebind/deregister/default mutate state outside the current vault, so each refuses without an
explicit `--yes` (exit 3, with the exact re-run line). list/status are read-only.

`vault status` (Task 1b) is the DEBUGGING SURFACE for discovery: it re-runs the real chain
(lib/vaultroot.discover) and prints which of the four mechanisms won and what each of the others saw
— including when the chain refuses, which is exactly when an operator needs it.
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lib import enginetree, output, paths, vaultreg, vaultroot  # noqa: E402

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
