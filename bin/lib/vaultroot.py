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

**A selected root no longer has to CARRY the engine** (Phase 2 Task 2). Task 1b's `require_engine()`
refused a vault with no `bin/lib/guardrail.py`, because Phase 1 ran the engine out of the vault it
acted on and an ordinary notes vault could not be dispatched for. This task moves the engine to its
own versioned tree, which removes the reason for that probe entirely — and leaving it in place would
refuse every data-only vault, i.e. every vault Task 5's `init` is meant to produce. It is INVERTED
rather than deleted: `enginetree.require_intact()` now runs in the same place, in the same shared
refusal, and asks whether the ENGINE tree is complete. Same seam, opposite subject.

**The engine root and the data root must be DISJOINT** (ADR-014 D3, and this is the task that turns
it on). Neither may be inside the other. Task 1b wrote the rule down and deliberately did not enforce
it: while the engine was `<vault>/bin` the rule was unsatisfiable, and a silent legacy exception would
have defeated the contract. It becomes true here, so it is enforced here — in `validate()`, which
means it holds for whichever of the four mechanisms selected the root.

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
    from . import enginetree, output, vaultreg, wall  # type: ignore  # (namespace siblings)
except ImportError:      # loaded top-level / exec'd standalone with bin/lib on sys.path
    _LIB = os.path.dirname(os.path.abspath(__file__))
    if _LIB not in sys.path:
        sys.path.insert(0, _LIB)
    try:
        import enginetree   # type: ignore
        import output      # type: ignore
        import vaultreg    # type: ignore
        import wall        # type: ignore
    except ImportError as _e:
        # THE PROBE CANNOT SPEAK IF THIS BLOCK DIES. `enginetree.require_intact()` is the refusal
        # ADR-014 D2 promises for an incomplete engine ("absent/unverified → refuse"), and it runs
        # from `require_engine()` BELOW these imports — so an engine missing `output.py`, `wall.py`
        # or `vaultreg.py` produced a raw Python traceback and exit 1 instead, from the one seam both
        # dispatchers share. There is no `output` to format with here, by construction, so the text
        # is written by hand and kept word-for-word identical to `require_intact()`'s.
        _r = os.path.dirname(_LIB)
        _r = os.path.dirname(_r)
        sys.stderr.write(
            f"plainkeep: the plainkeep engine at {_r} is incomplete ({_e})\n"
            f"  reinstall it:\n"
            f"    python3 {os.path.abspath(__file__).replace('vaultroot.py', 'enginetree.py')} "
            f"--install <source-checkout>\n")
        raise SystemExit(2)      # EXIT_USAGE, the code require_intact()'s VaultError carries — and
        #                          output.py is the module that would have told us so

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

# The engine's own tree — where the CODE is, never where the data is. Owned by `enginetree.py`,
# re-exported here because this module's refusals name it. `resolver.py` draws the same distinction
# for the same reason.
ENGINE_ROOT = enginetree.ENGINE_ROOT
ENGINE_BIN = enginetree.engine_bin()


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
    a bare substring: under those, `~/notes/my.sync-notes` and `~/notes/not-iCloudy` were denied as
    "inside a cloud-sync tree", which is untrue of both. It fails closed either way, so it was never
    a safety hole — but this is exit 5, the strictest code in the protocol, aimed at the root itself,
    and unlike a refused WRITE there is nothing the operator can re-path.

    The component matcher does NOT require equality, and three names that once selected are refused
    again because of it (`~/notes/dropbox-export`, `~/notes/OneDrive-old`, `~/notes/icloud-archive`).
    That is a deliberate trade, not an oversight: the real macOS sync mount points are spelled
    `OneDrive-Personal`, `GoogleDrive-<account>`, `Dropbox (Team)` and `Dropbox.nosync`, and no rule
    can accept those as sync trees while still accepting `dropbox-export`. Where the two cannot be
    separated by spelling, the refusal wins — it is visible and carries a `vault rebind` hint, while
    the miss is silent and leaves a `.git` inside a sync client. See wall.py's header for the full
    statement of the trade and for why the write path keeps the older semantics."""
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

    # DISJOINTNESS (ADR-014 D3), enforced from this task on. Asked BEFORE the marker for the same
    # reason the sync-tree policy is: it is a fact about the LOCATION, true or false whether or not
    # the directory is a well-formed vault, and answering it first means the operator is told the
    # thing that actually blocks them rather than a downstream consequence of it.
    #
    # It is exit 5 (`EXIT_DENY`), the same code as the walled-off/cloud-sync verdict and for the same
    # reason: this is a refusal about WHERE, not about a missing or stale selection (which is 2). The
    # remediation differs, so it is spelled out rather than shared — the fix for "your vault is your
    # engine" is to install the engine somewhere else, not to rebind the vault.
    overlap = enginetree.disjointness_verdict(root)
    if overlap is not None:
        raise VaultError(
            f"{how} names {root}, and {overlap}", code=output.EXIT_DENY,
            hint="the engine is installed separately from the data it acts on:\n"
                 f"    python3 {ENGINE_BIN / 'lib' / 'enginetree.py'} --install <source-checkout>\n"
                 "then dispatch through the installed launcher "
                 f"({enginetree.current_link() / 'plainkeep'})")

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
        # `same_path`, not `==`: canonical is not a comparison key (see vaultreg), and on the default
        # macOS volume a vault registered as `/x/vault` and reached as `/x/Vault` is ONE directory.
        # `!=` called that a moved vault and pushed the operator at `vault rebind` for a vault that
        # had not moved.
        if not vaultreg.same_path(entry["path"], root):
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
def require_engine() -> None:
    """Refuse a broken ENGINE tree, before either dispatcher tries to run out of it.

    Task 1b's `require_engine(sel)` asked the opposite question — whether the selected VAULT carried
    a copy of the engine — and it existed only because Phase 1 ran the engine out of the vault it
    acted on. Task 2 removes that reason, so the probe is INVERTED rather than deleted: the seam is
    worth keeping (it is the one function both dispatchers run, which is what makes their refusals
    byte-identical instead of two spellings that drift), and the subject moves from the data to the
    code. Leaving the old question in place would have refused every data-only vault — the shape
    Task 5's `init` exists to produce.

    Thin on purpose: `enginetree.require_intact()` owns the probe, this owns WHERE it is asked."""
    enginetree.require_intact()


def _select_cli(argv: list[str]) -> int:
    """`vaultroot.py --select [--vault X]` — run the chain and print the answer for a DISPATCHER to
    export. THREE lines on stdout: the canonical data root, the vault id, then the mechanism that
    chose it. Any refusal goes to stderr with the frozen exit code.

    The third line exists because the mechanism is the one part of the answer that cannot be
    recovered downstream: exporting PLAINKEEP_HOME (which the dispatcher must do) destroys the
    evidence of which step won, so a verb re-running the chain can only ever answer "PLAINKEEP_HOME".

    The ENGINE root is deliberately NOT a fourth line. Each dispatcher already self-located to FIND
    this module — the floor through a `$0` symlink chain ending in `cd -P`, the core through
    `realpath(execPath)` — and both of those are canonical, so a value carried from here would be a
    third spelling of a directory two other derivations already agree on. What keeps them honest is
    an assertion rather than a channel: the parity suite's `v_engine` check compares the exported
    `PLAINKEEP_ENGINE` against the running verb's OWN `Path(__file__).resolve().parents[2]`, so all
    three derivations are pinned equal by a test that fails if any one of them drifts.

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
        # The ENGINE is checked FIRST, before discovery: a broken engine cannot be diagnosed by
        # anything downstream of it, and asking about the vault first would report a vault problem
        # for a code problem.
        require_engine()
        sel = discover(selector)
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
