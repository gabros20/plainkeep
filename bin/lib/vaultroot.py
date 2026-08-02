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

MECHANISMS = ("--vault", "PLAINKEEP_HOME", "marker walk-up from $PWD", "registry default")

# The engine's own tree — where the CODE is, never where the data is. `resolver.py` draws the same
# distinction on the same line number for the same reason.
ENGINE_BIN = Path(__file__).resolve().parents[1]


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
    job is to PRODUCE that value cannot import it."""
    if wall.is_walled(root):
        return "it is inside a walled-off tree (iCloud/Photos) — propose, never write"
    if wall.under_sync_dir(root):
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
        raise VaultError(f"{how} names {root}, and {denied}", code=output.EXIT_DENY)

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
def _walk_up(cwd: str, reg: dict, saw: dict) -> tuple[str, str] | None:
    """The nearest ancestor of `cwd` carrying a marker, validated. None only when there is NO marker
    anywhere up to `/`.

    The subtlety this function exists for: the first marker found DECIDES. A malformed or
    unregistered marker at the nearest ancestor refuses THERE. Skipping it to try an outer ancestor
    would mean a broken inner vault silently hands your keystrokes to the outer one — which is the
    single worst outcome this whole task exists to prevent."""
    d = Path(vaultreg.canonical(cwd))
    for cand in (d, *d.parents):
        if vaultreg.marker_path(cand).is_file():
            saw[MECHANISMS[2]] = f"marker at {cand}"
            return validate(cand, how=f"the vault marker at {cand}", require_registered=True, reg=reg)
    saw[MECHANISMS[2]] = f"no marker in {d} or any ancestor"
    return None


def discover(selector: str | None = None, cwd: str | None = None) -> Selection:
    """Run the whole chain and return the validated Selection, or raise VaultError naming every
    mechanism and what it saw."""
    saw: dict = {}
    cwd = cwd or os.getcwd()

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
    raise VaultError("no vault selected — every discovery mechanism came up empty:\n"
                     + "\n".join(f"  {m:<26} {saw.get(m, '?')}" for m in MECHANISMS),
                     hint="pick a registered vault (plainkeep --vault <name> <verb>), point "
                          "PLAINKEEP_HOME at one, or make THIS directory a vault:\n    "
                          + bootstrap_hint(cwd))


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


# --- the dispatcher entry point -------------------------------------------------------------------
def _select_cli(argv: list[str]) -> int:
    """`vaultroot.py --select [--vault X]` — run the chain and print the answer for a DISPATCHER to
    export. Two lines on stdout: the canonical root, then the vault id. Any refusal goes to stderr
    with the frozen exit code.

    Both dispatchers call exactly this, which is what makes them agree: the bash floor and the
    compiled core share ONE implementation of the safety-critical decision rather than a port and a
    differential. It costs the core one extra process (measured ~23 ms) — the same trade the
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
    except VaultError as e:
        sys.stderr.write("plainkeep: " + e.message + (f"\n  {e.hint}" if e.hint else "") + "\n")
        return e.code
    sys.stdout.write(sel.root + "\n" + sel.id + "\n")
    return output.EXIT_OK


def main(argv: list[str]) -> int:
    if argv and argv[0] == "--select":
        return _select_cli(argv[1:])
    print("usage: vaultroot.py --select [--vault <name|id|absolute-path>]", file=sys.stderr)
    return output.EXIT_USAGE


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
