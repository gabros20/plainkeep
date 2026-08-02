"""
vaultroot.py — WHICH vault this invocation acts on (ADR-014, Phase 2 Task 1b).

Task 1a gave a vault an identity (`vaultreg.py`: the marker and the registry). This module is the
one place that turns an invocation into a SELECTED, VALIDATED data root, and it is the only thing
allowed to answer the question. Everything downstream — the guardrail's path-wall, the audit log,
the resolver, every verb — consumes `PLAINKEEP_HOME` and never derives it.

**The precedence chain, and every step fails closed:**

  1. `--vault <name|id|absolute-path>` — supplied but unresolvable, unregistered or
     marker-mismatched → REFUSE. It never falls through to step 2. An explicit selection that
     cannot be honoured is an error, not a hint.
  2. `PLAINKEEP_HOME` — set but not a valid MARKED vault → REFUSE. The empty string is treated as
     *explicitly empty*, which is an error, not as unset.
  3. **Marker walk-up from `$PWD`.** The FIRST marker found going up decides. If that marker is
     malformed, unregistered, or registered to a different canonical path, it REFUSES THERE — it
     does not skip to an outer ancestor and does not fall through to the default. Only the complete
     ABSENCE of any marker up to `/` advances to step 4. (Walk-up is the one mechanism nobody typed,
     so it is also the one that must not be able to adopt an arbitrary checkout: a marker alone
     never establishes trust here, registration does.)
  4. The registry `default`.
  5. REFUSE, listing all four mechanisms and what each one saw.

**Why step 2 asks for a marker but NOT for registration**, while steps 1 and 3 demand both: the
registry answers "which vaults exist", and `--vault` and walk-up are lookups THROUGH it — a name has
no meaning without it, and a marker found by walking is not something the operator typed. An
absolute `PLAINKEEP_HOME` is typed, and requiring registration for it would make the one migration
ADR-014 calls mandatory evidence — a full clone of the real vault at a scratch path, deliberately
not registered — impossible to run against the real wall. The marker is what keeps it honest: a
plain clone of the public template carries none (`.plainkeep/` is gitignored), so pointing
`PLAINKEEP_HOME` at a checkout still refuses.

**What "fail before any I/O" actually means** (it is not "no reads" — validating a marker requires
reading it): no MUTATING I/O, no audit-log append, no index creation, no plugin scan and no verb
spawn happens before a root is validated. Validation reads are the only reads permitted. In
practice that is enforced by WHERE this runs: both dispatchers call `--select` as their first act,
before the gate (which is what appends the audit line) and before the resolver (which is what scans
plugins).

**A selected root still has to be DISPATCHABLE.** Phase 1 runs the engine from inside the vault it
acts on, so a valid, registered, ordinary notes vault — a directory with a marker and nothing else,
which is what a second vault looks like — cannot be dispatched for. `require_engine()` says that in
one shared refusal instead of letting each dispatcher fail its own way at the far end of the
dispatch. It does not lift the constraint; removing it is Phase 2 Task 2 (`PLAINKEEP_ENGINE`).

**Refusal codes.** Unset / invalid / unregistered / structurally-not-a-vault → 2 (`EXIT_USAGE`).
A policy-denied location — a vault inside a walled-off or cloud-sync tree, per `guardrail.py`'s
markers — → 5 (`EXIT_DENY`). Neither leaves a log, an index or a directory behind.

**There is deliberately NO engine-relative fallback anywhere.** `paths.py`, `guardrail.py`,
`indexlib.py` and `vectorstore.py` each carried `Path(__file__).resolve().parents[2]`, and
`resolver.py`/`dispatch.ts` each carried an executable-relative equivalent. That is the "the engine
lives in the vault" assumption in code, and because the write path does not consult the wall for
every write, its failure mode was a successful write to the WRONG root with exit 0. `active_root()`
replaces all of them: it reads `PLAINKEEP_HOME` and refuses when it is not there.

Stdlib only, and deliberately import-light: both dispatchers run this file as their FIRST act, so
its start-up cost is paid on every single invocation.
"""
from __future__ import annotations
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

try:
    from . import output, vaultreg, wall  # type: ignore  # (namespace siblings)
except ImportError:      # loaded top-level / exec'd standalone with bin/lib on sys.path
    _LIB = os.path.dirname(os.path.abspath(__file__))
    if _LIB not in sys.path:
        sys.path.insert(0, _LIB)
    import output      # type: ignore
    import vaultreg    # type: ignore
    import wall        # type: ignore

VaultError = vaultreg.VaultError

# The env vars this module owns end to end. PLAINKEEP_VAULT_ID travels with PLAINKEEP_HOME so a verb,
# a plugin or a scheduled job can assert WHICH vault it woke up in without re-reading the marker —
# and so a moved vault is detectable from the inside (ADR-014: the id is the identity, the path is
# not).
ENV_HOME = "PLAINKEEP_HOME"
ENV_ID = "PLAINKEEP_VAULT_ID"
# WHICH of the four mechanisms chose the root, carried to the spawned verb because it cannot be
# recovered there: the dispatcher exports PLAINKEEP_HOME before spawning, so a verb re-running the
# chain always sees step 2 win and can only ever answer "PLAINKEEP_HOME". `vault status` is the
# surface every refusal in ADR-014 Task 1b depends on for diagnosability, and without this it
# reported a constant.
ENV_MECHANISM = "PLAINKEEP_VAULT_MECHANISM"

MECHANISMS = ("--vault", "PLAINKEEP_HOME", "marker walk-up from $PWD", "registry default")

# The engine's own tree — where the CODE is, never where the data is. `resolver.py` draws the same
# distinction on the same line number for the same reason.
ENGINE_BIN = Path(__file__).resolve().parents[1]

# The file whose presence makes a selected root DISPATCHABLE, relative to that root. Phase 1 still
# runs the engine from INSIDE the vault it acts on — the floor spawns `$PLAINKEEP_HOME/bin/lib/
# guardrail.py` (plainkeep:98) and the core's resolver looks under the same root (resolver.ts's
# `engineBin()`) — so an ordinary notes vault, which is a directory with a marker and nothing else,
# cannot be dispatched for. Relocating the engine is Phase 2 Task 2's job (`PLAINKEEP_ENGINE`); this
# constant does not lift the constraint, it makes the constraint SAY SO.
ENGINE_PROBE = ("bin", "lib", "guardrail.py")


def bootstrap_hint(path) -> str:
    """The exact command an UNREGISTERED vault needs, spelled out absolutely.

    It invokes `bin/vault/run.py` DIRECTLY instead of saying `plainkeep vault register`, and that is
    not a stylistic choice — `plainkeep <anything>` goes through the dispatcher, the dispatcher
    validates a root first, and an unmarked vault has none. Telling the operator to run a command
    that refuses for the same reason would be a remediation that cannot be followed. This is the
    bootstrap path, and it is the one `script/setup` uses."""
    p = os.path.abspath(os.path.expanduser(str(path)))
    return f"PLAINKEEP_HOME={p} python3 {ENGINE_BIN / 'vault' / 'run.py'} register {p} --yes"


@dataclass
class Selection:
    """A validated root and the full account of how it was chosen. `saw` carries one line per
    mechanism — including the ones that did not win — because a refusal you cannot explain is a
    refusal you will work around."""
    root: str
    id: str
    mechanism: str
    saw: dict = field(default_factory=dict)


# --- validation ----------------------------------------------------------------------------------
def _policy_verdict(root: str) -> str | None:
    """The LOCATION policy for a candidate root, or None if it is acceptable.

    The markers live in `wall.py` rather than `guardrail.py` precisely so this question can be asked
    HERE: guardrail resolves `PLAINKEEP_HOME` at import and has no fallback left, so the module whose
    job is to PRODUCE that value cannot import it.

    It asks `wall.vault_*` and not `wall.is_walled` / `wall.under_sync_dir`, which match a marker as
    a bare substring: under those, `~/notes/dropbox-export`, `~/notes/my.sync-notes` and
    `~/notes/OneDrive-old` were all denied as "inside a cloud-sync tree", which is untrue of every
    one of them. It fails closed either way, so it was never a safety hole — but this is exit 5, the
    strictest code in the protocol, aimed at the root itself, and unlike a refused WRITE there is
    nothing the operator can re-path. See wall.py's header for why the write path keeps the older
    semantics."""
    if wall.vault_is_walled(root):
        return "it is inside a walled-off tree (iCloud/Photos) — propose, never write"
    if wall.vault_under_sync_dir(root):
        return "it is inside a cloud-sync tree — a vault's .git must never live under one"
    return None


def validate(candidate, *, how: str, require_registered: bool = False,
             reg: dict | None = None) -> tuple[str, str]:
    """Validate one candidate root and return `(canonical_path, vault_id)`.

    Raises VaultError on anything less than a complete answer. `how` names the mechanism, so every
    refusal says which of the four produced it rather than leaving the operator to guess."""
    raw = str(candidate)
    if not raw.strip():
        raise VaultError(f"{how} is set but EMPTY — that is an explicit selection of nothing",
                         hint="unset it to fall through to the next mechanism, or give it a path")
    root = vaultreg.canonical(raw)
    if not os.path.isdir(root):
        raise VaultError(f"{how} names {root}, which is not a directory")

    # A policy-denied LOCATION is a different refusal from a structural one, and it is the stricter
    # of the two: exit 5, before the marker is even read, because a vault inside iCloud is refused
    # whether or not it is a well-formed vault.
    denied = _policy_verdict(root)
    if denied is not None:
        # A DENY with no remediation is a refusal an operator can only work around. This one carried
        # none at all, which on the strictest code in the protocol is the worst place to omit it.
        raise VaultError(f"{how} names {root}, and {denied}", code=output.EXIT_DENY,
                         hint="move the vault to a local path outside that tree, then point "
                              "plainkeep at where it went:\n"
                              "    plainkeep vault rebind <name> <new-path> --yes")

    marker = vaultreg.read_marker(root)          # raises on a present-but-unusable marker
    if marker is None:
        raise VaultError(f"{how} names {root}, which is not a plainkeep vault "
                         f"(no {vaultreg.MARKER_DIR}/{vaultreg.MARKER_NAME})",
                         hint="if it should be one, mark and register it:\n    " + bootstrap_hint(root))
    vid = marker["id"]

    if require_registered:
        reg = vaultreg.read_registry() if reg is None else reg
        entry = next((v for v in reg["vaults"] if v["id"] == vid), None)
        if entry is None:
            raise VaultError(f"{how} names {root}, which carries a vault marker (id {vid}) that is "
                             f"not in the registry",
                             hint="register it:\n    " + bootstrap_hint(root))
        # A registry entry pointing somewhere else means the vault MOVED (or this is a copy of it).
        # Substituting the registered path would act on the wrong notes and rescanning the disk to
        # find the "real" one is exactly the guess ADR-014 forbids, so this is loud.
        if entry["path"] != root:
            raise VaultError(f"{how} names {root}, but vault '{entry['name']}' (id {vid}) is "
                             f"registered at {entry['path']}",
                             hint=f"if it moved: plainkeep vault rebind {entry['name']} {root} --yes")
    return root, vid


# --- the chain -----------------------------------------------------------------------------------
def _walk_up(cwd: str | None, reg: dict, saw: dict) -> tuple[str, str] | None:
    """The nearest ancestor of `cwd` carrying a marker, validated. None only when there is NO marker
    anywhere up to `/` — or when there is no `cwd` left to walk from at all.

    The subtlety this function exists for: the first marker found DECIDES. A malformed or
    unregistered marker at the nearest ancestor refuses THERE. Skipping it to try an outer ancestor
    would mean a broken inner vault silently hands your keystrokes to the outer one — which is the
    single worst outcome this whole task exists to prevent."""
    if cwd is None:                     # $PWD was unlinked underneath the shell — see discover()
        saw[MECHANISMS[2]] = "$PWD no longer exists — the directory was deleted underneath this shell"
        return None
    d = Path(vaultreg.canonical(cwd))
    for cand in (d, *d.parents):
        # PRESENCE, not usability, and deliberately the same predicate `read_marker` uses (see the
        # note there): a marker that is a directory or a dangling symlink must DECIDE and then
        # refuse in validate(), not be invisible here. `is_file()` made it invisible, so the walk
        # skipped a broken inner vault and selected the outer ancestor with exit 0 — the outcome
        # this function's docstring above says it exists to prevent.
        m = vaultreg.marker_path(cand)
        if m.exists() or m.is_symlink():
            saw[MECHANISMS[2]] = f"marker at {cand}"
            return validate(cand, how=f"the vault marker at {cand}", require_registered=True, reg=reg)
    saw[MECHANISMS[2]] = f"no marker in {d} or any ancestor"
    return None


def discover(selector: str | None = None, cwd: str | None = None) -> Selection:
    """Run the whole chain and return the validated Selection, or raise VaultError naming every
    mechanism and what it saw.

    A raised VaultError carries `.saw` — the same per-mechanism account the Selection would have —
    so a refusal can be EXPLAINED and not merely reported. `vault status` renders it, which is the
    difference between an operator fixing a refusal and working around it."""
    saw: dict = {}
    if cwd is None:
        try:
            cwd = os.getcwd()
        except OSError:
            # $PWD was unlinked underneath this shell — a worktree removed, a `git clean`, a temp dir
            # the agent that made it deleted. That is a mechanism SEEING NOTHING, not an unexpected
            # condition: the frozen protocol has no "1 = something went wrong" excuse for a
            # foreseeable one, and reading the cwd before the chain runs meant even an explicit
            # --vault or PLAINKEEP_HOME died here without being asked. Steps 1, 2 and 4 still answer;
            # only step 3 is unavailable, and it says so.
            cwd = None
    try:
        return _discover(selector, cwd, saw)
    except VaultError as e:
        e.saw = saw          # type: ignore[attr-defined]
        raise


def _discover(selector: str | None, cwd: str | None, saw: dict) -> Selection:
    # 1. --vault. Resolved THROUGH the registry (a name/id/path all mean "a vault I know about"), so
    #    an unregistered spelling refuses here rather than being validated as a bare path.
    if selector is not None:
        reg = vaultreg.read_registry()
        entry = vaultreg.find(reg, selector)
        if entry is None:
            saw[MECHANISMS[0]] = f"{selector!r} matches no registered vault"
            raise VaultError(f"--vault {selector!r} matches no registered vault "
                             f"({vaultreg.registry_path()})",
                             hint="see them with: plainkeep vault list")
        saw[MECHANISMS[0]] = f"{selector!r} -> '{entry['name']}' at {entry['path']}"
        root, vid = validate(entry["path"], how=f"--vault {selector!r}")
        if vid != entry["id"]:
            raise VaultError(f"--vault {selector!r} resolves to {root}, which carries a DIFFERENT "
                             f"vault (id {vid}, registry says {entry['id']})",
                             hint=f"if it moved: plainkeep vault rebind {entry['name']} <path> --yes")
        return Selection(root, vid, MECHANISMS[0], saw)
    saw[MECHANISMS[0]] = "not supplied"

    # 2. PLAINKEEP_HOME. `is not None` rather than truthiness: the empty string is an explicit
    #    selection of nothing and must refuse, not fall through (a `PLAINKEEP_HOME=` in a shell
    #    profile is otherwise indistinguishable from never having set it).
    env_home = os.environ.get(ENV_HOME)
    if env_home is not None:
        saw[MECHANISMS[1]] = f"{env_home!r}"
        root, vid = validate(env_home, how=ENV_HOME)
        return Selection(root, vid, MECHANISMS[1], saw)
    saw[MECHANISMS[1]] = "unset"

    # 3. Marker walk-up from $PWD.
    reg = vaultreg.read_registry()
    found = _walk_up(cwd, reg, saw)
    if found is not None:
        return Selection(found[0], found[1], MECHANISMS[2], saw)

    # 4. The registry default.
    if reg["default"]:
        entry = next((v for v in reg["vaults"] if v["id"] == reg["default"]), None)
        # read_registry() already refuses a default naming no registered vault, so `entry` is never
        # None here; the guard is kept because this module must not depend on that staying true.
        if entry is not None:
            saw[MECHANISMS[3]] = f"'{entry['name']}' at {entry['path']}"
            root, vid = validate(entry["path"], how=f"the registry default '{entry['name']}'")
            if vid != entry["id"]:
                raise VaultError(f"the registry default '{entry['name']}' points at {root}, which "
                                 f"carries a DIFFERENT vault (id {vid}, registry says {entry['id']})",
                                 hint=f"if it moved: plainkeep vault rebind {entry['name']} <path> --yes")
            return Selection(root, vid, MECHANISMS[3], saw)
    saw[MECHANISMS[3]] = f"none set ({vaultreg.registry_path()})"

    # 5. Refuse, showing the whole chain. This is the message an operator meets on a fresh install,
    #    in a cron job with a sanitized environment, and in an agent shell started outside any vault,
    #    so it carries the remediation rather than only the diagnosis.
    # The "make THIS directory a vault" half of the remediation is only offered when there IS a
    # this-directory: with `$PWD` deleted, a bootstrap command naming it would be one the operator
    # cannot run, which is the failure mode `bootstrap_hint`'s own docstring exists to avoid.
    hint = ("pick a registered vault (plainkeep --vault <name> <verb>), or point PLAINKEEP_HOME "
            "at one" + (f":\n    cd <somewhere that still exists> first — {MECHANISMS[2]} "
                        f"could not run" if cwd is None
                        else ", or make THIS directory a vault:\n    " + bootstrap_hint(cwd)))
    raise VaultError("no vault selected — every discovery mechanism came up empty:\n"
                     + "\n".join(f"  {m:<26} {saw.get(m, '?')}" for m in MECHANISMS),
                     hint=hint)


# --- the consumer side ----------------------------------------------------------------------------
def active_root() -> Path:
    """The data root this process was pointed at, from `PLAINKEEP_HOME`. **NO FALLBACK.**

    This replaces four copies of `Path(__file__).resolve().parents[2]`. It does NOT re-validate the
    marker: validation belongs to the dispatcher, which does it once per invocation and exports the
    canonical path (`discover()` above). Re-validating here would make every module import pay a
    registry read, and would refuse the legitimate case of a verb invoked directly by a test harness
    against a scratch root."""
    v = os.environ.get(ENV_HOME)
    if not v:
        output.fail(output.EXIT_USAGE,
                    f"no vault selected — {ENV_HOME} is unset "
                    f"(plainkeep no longer guesses one from where the engine is installed)",
                    hint="run this through `plainkeep <verb>`, which selects and validates one, "
                         "or set PLAINKEEP_HOME to a marked vault")
    return Path(v)


def active_id() -> str | None:
    """The selected vault's id, when the dispatcher exported one. None for a directly-invoked verb."""
    return os.environ.get(ENV_ID) or None


def active_mechanism() -> str | None:
    """Which of the four mechanisms chose this process's root, as the dispatcher recorded it.

    None means no dispatcher was involved — a verb invoked directly as `python3 bin/<verb>/run.py`.
    That is a real answer, not a missing one: such a process was pointed at its root by
    PLAINKEEP_HOME and by nothing else, and `vault status` says so rather than guessing."""
    v = os.environ.get(ENV_MECHANISM)
    return v if v in MECHANISMS else None


# --- the dispatcher entry point -------------------------------------------------------------------
def require_engine(sel: Selection) -> None:
    """Refuse a SELECTED root that carries no copy of the engine, before either dispatcher tries to
    run one out of it.

    This is a diagnosis, not a policy: selection genuinely succeeded, the vault is genuinely valid,
    and the invocation genuinely cannot proceed. Without it the failure landed at the far end of the
    dispatch, in two different places and untruthfully in both — the floor reached
    `"$PY" "$PK/bin/lib/guardrail.py"` and let CPython answer ("can't open file '<vault>/bin/lib/
    guardrail.py'", exit 2, with no plainkeep in the message), while the core got as far as the
    resolver, found no verb directory under the root and said `unknown verb 'capture'` (exit 4) —
    a FALSE reason, since `capture` exists, with a remediation (`plainkeep help`) that fails the
    same way and so loops.

    It lives HERE, in the one function both dispatchers run, rather than as a check in each of them:
    two spellings of one refusal is exactly the drift `--select` exists to prevent, and the brief
    requires the two to agree byte-for-byte."""
    p = Path(sel.root).joinpath(*ENGINE_PROBE)
    if p.is_file():
        return
    name = None
    try:
        entry = vaultreg.entry_for_path(vaultreg.read_registry(), sel.root)
        name = entry["name"] if entry else None
    except VaultError:
        pass          # a registry we cannot read must not turn THIS refusal into a different one
    who = f"vault '{name}'" if name else "the selected vault"
    raise VaultError(
        f"{who} at {sel.root} does not carry the plainkeep engine (no {p}) — selection itself "
        f"SUCCEEDED, via {sel.mechanism}; Phase 1 still runs the engine from inside the vault it "
        f"acts on, so a vault holding only notes cannot be dispatched for",
        hint="put the engine in that vault (script/setup), or select one that already carries it:"
             "\n    plainkeep vault list")


def _select_cli(argv: list[str]) -> int:
    """`vaultroot.py --select [--vault X]` — run the chain and print the answer for a DISPATCHER to
    export. THREE lines on stdout: the canonical root, the vault id, then the mechanism that chose
    it. Any refusal goes to stderr with the frozen exit code.

    The third line exists because the mechanism is the one part of the answer that cannot be
    recovered downstream: exporting PLAINKEEP_HOME (which the dispatcher must do) destroys the
    evidence of which step won, so a verb re-running the chain can only ever answer "PLAINKEEP_HOME".

    Both dispatchers call exactly this, which is what makes them agree: the bash floor and the
    compiled core share ONE implementation of the safety-critical decision rather than a port and a
    differential. It costs the core one extra process — measured +28.8 ms median on a `vault list
    --json` dispatch, 70.2 -> 99.0 ms, over 25 interleaved runs on bun 1.3.14 / macOS arm64 /
    CPython 3.12 against a build with this call stubbed out. That is the same trade the
    O_NONBLOCK helper takes, and for a stronger reason: a ported registry validator whose refusal
    text must stay byte-identical is precisely the drift this repo has already paid for once."""
    selector = None
    if "--vault" in argv:
        i = argv.index("--vault")
        if i + 1 >= len(argv):
            print("plainkeep: --vault needs a value (<name|id|absolute-path>)", file=sys.stderr)
            return output.EXIT_USAGE
        selector = argv[i + 1]
    try:
        sel = discover(selector)
        require_engine(sel)
    except VaultError as e:
        sys.stderr.write("plainkeep: " + e.message + (f"\n  {e.hint}" if e.hint else "") + "\n")
        return e.code
    sys.stdout.write(sel.root + "\n" + sel.id + "\n" + sel.mechanism + "\n")
    return output.EXIT_OK


def main(argv: list[str]) -> int:
    if argv and argv[0] == "--select":
        return _select_cli(argv[1:])
    print("usage: vaultroot.py --select [--vault <name|id|absolute-path>]", file=sys.stderr)
    return output.EXIT_USAGE


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
