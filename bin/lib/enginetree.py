"""
enginetree.py — WHERE THE CODE IS (ADR-014 D2/D6, Phase 2 Task 2).

`vaultroot.py` answers "which vault does this invocation act on". This module answers the other half
of the same question, and the two are deliberately separate modules because they must never be
allowed to answer each other: **the engine is code, the vault is data, and neither may be derived
from the other.** Phase 1 derived both from one directory; that is what this task ends.

**The engine root is CODE-RELATIVE, always, and that is a security property rather than a
convenience.** `ENGINE_ROOT` below is `Path(__file__).resolve().parents[2]` — the tree this very file
belongs to. It is not read from the environment, and nothing in this module will read
`PLAINKEEP_ENGINE` to decide where to load code from. ADR-014 D2 states the rule as "caller input
must not control it"; the way to make that true is not to validate the variable but to never consult
it. So `PLAINKEEP_ENGINE` is an **OUTPUT**: both dispatchers export the root they resolved from their
own location, REPLACING whatever the caller had set, and the consumers of the variable are the
processes that genuinely cannot self-locate — a PLUGIN verb under `<vault>/plugins/<pack>/<verb>/`,
a frontend script, a scheduled job. `templates/verb/run.py` (the scaffold every plugin starts from)
is the load-bearing one: it used to bootstrap `lib` through `$PLAINKEEP_HOME/bin`, which IS the
"engine lives in the vault" assumption, and it now bootstraps through `$PLAINKEEP_ENGINE/bin`.

**The installed layout** (the controller's decision for this task):

    ${XDG_DATA_HOME:-$HOME/.local/share}/plainkeep/engine/<version>/     one immutable tree per version
    ${XDG_DATA_HOME:-$HOME/.local/share}/plainkeep/engine/current -> <version>

`PLAINKEEP_ENGINE_HOME` relocates the INSTALL ROOT — where versions are written and where `current`
is looked for. It is the test/dev override the hermetic harness uses, and it is emphatically NOT a
second way to point a running dispatcher at code: a dispatch never consults it, because a dispatch
never asks where engines are installed. It only matters to `--install` / `--activate` / `--print`,
i.e. to the installer. That split is what lets the suite be hermetic without weakening the rule
above.

**Immutability is enforced, not asserted.** `install()` writes the tree and then makes it read-only
(dirs 0555, files 0444, executables 0555). A tree that cannot be written cannot be hot-patched, which
is the whole reason the engine leaves the vault: `skills/operate-plainkeep/SKILL.md` used to tell an
operator to edit `bin/<verb>/run.py` in place. The cost is stated rather than hidden — CPython cannot
write `__pycache__` into a read-only tree, so every invocation re-compiles the lib modules it
imports. Measured, and carried in the Task 2 report.

`verify()` then asks, on every `--verify` and every `doctor`, whether the tree is still complete and
still read-only. The seal check reads MODES, so it catches the seal being gone — an interrupted
install, a `chmod` someone forgot to undo — and on its own it cannot catch an edit whose author put
the mode back. **That third question — "is this the code that was installed" — is now answerable**,
by the digest manifest Task 4b needed and this module records at install time under
`<install-root>/engine/.digests/<version>.json`, i.e. OUTSIDE the tree it covers. `--verify
--digests` and `provision.sync()` ask it; `doctor`'s routine pass still does not, because re-reading
every owned file is the right cost for "prove this tree is intact" and the wrong one for a health
check. What it still does not prove is that the SOURCE was authentic: `install()` digests what it
was handed.

**An installed engine also has one writable directory**, `tools/` — where the pinned `uv` and the
Python environment it syncs are provisioned (`PROVISION_DIR` below, and `bin/lib/provision.py` for
why it is inside the versioned tree at all). It carries no engine code, and the seal is applied to
the whole tree and then lifted from that one path.

**What the engine OWNS** is enumerated in `OWNED_TREES`/`OWNED_FILES` below and verified, because a
manifest nothing executes is paperwork. `install()` refuses to leave behind a tree that fails
`verify()`, and `require_intact()` — which both dispatchers run on every invocation through
`vaultroot._select_cli` — refuses a tree whose dispatch-critical files have gone missing.

Stdlib only and import-light, for `vaultroot.py`'s reason: it is imported on the first act of every
invocation.
"""
from __future__ import annotations
import json          # the pair manifests + the activation state; `vaultreg` already pays for it
import os
import shutil
import stat
import sys
from pathlib import Path

try:
    from . import output, vaultreg  # type: ignore  # (namespace siblings)
except ImportError:      # loaded top-level / exec'd standalone with bin/lib on sys.path
    _LIB = os.path.dirname(os.path.abspath(__file__))
    if _LIB not in sys.path:
        sys.path.insert(0, _LIB)
    import output      # type: ignore
    import vaultreg    # type: ignore

VaultError = vaultreg.VaultError

# The variable the dispatchers EXPORT (never read — see the module header).
ENV_ENGINE = "PLAINKEEP_ENGINE"
# The variable that relocates the INSTALL ROOT, for the hermetic suite and for a dev who wants a
# second install. Consulted only by the installer surface below, never by a dispatch.
ENV_INSTALL_ROOT = "PLAINKEEP_ENGINE_HOME"

# THE ENGINE ROOT. `bin/lib/enginetree.py` -> parents[0]=lib, [1]=bin, [2]=the tree.
# `.resolve()` matters: the launcher is normally reached through `<install>/engine/current/plainkeep`
# (a symlink), and the floor, the core and this module must all name the same canonical tree or the
# disjointness check below compares two spellings of one directory and silently answers "no".
ENGINE_ROOT = Path(__file__).resolve().parents[2]

VERSIONS_DIRNAME = "engine"
CURRENT_NAME = "current"

# --- the ownership manifest ----------------------------------------------------------------------
# The engine-owned set, enumerated. The plan section lists it as prose and the advisor made the
# enumeration blocking, for the reason this file exists: an ownership table that nothing executes
# describes an intention rather than a disposition. These two tuples are what `install()` copies and
# what `verify()` checks, so the table IS the installer.
#
# ADDING AN ENGINE-OWNED PATH TOUCHES MORE THAN THESE TWO TUPLES, and the other places are worth
# naming here rather than leaving a reader to find them by reading to the end of `verify()`:
#   * `verify()`'s NAMED_LIB_MODULES and NAMED_CONTENT below — a directory that exists is not the
#     same as a directory that carries what makes it owned, so the load-bearing files are named;
#   * `DISPATCH_PROBE` above, IF the new path is one a dispatch cannot run without;
#   * `script/engine.txt`, the update boundary's own manifest (its header states how the two differ);
#   * `test/run_core_parity.py`'s EXPECTED_ENGINE_* count pins.
#
# `templates/` is a mixed directory — `templates/obsidian`, `templates/wiki`, `templates/project-repo`
# and `templates/tax-formula.md` are USER data that lives in the vault and that an update must never
# overwrite. Only `templates/verb` is a code scaffold, so only it is named here.
OWNED_TREES = (
    "bin",                              # every verb dir (run.py + cmd.json), bin/lib, bin/share/worker
    "templates/verb",                   # the scaffold `plainkeep new verb` renders
    "frontends/raycast",                # they shell to the installed launcher
    "skills/operate-plainkeep",         # the agent manual — engine-owned, rewritten in this task
)
OWNED_FILES = (
    "VERSION",                          # load-bearing: manifest.py reads it as <engine>/VERSION
    "plainkeep",                        # the launcher itself ships inside the tree it launches
    # THE LOCK TRAVELS WITH THE CODE IT LOCKS (Phase 2 Task 4). These two are ONE artifact with the
    # rest of the tree — extracted to the versioned directory, checksummed, and provisioned from with
    # `uv sync --frozen`. Shipping the engine and fetching its lock separately is how a machine ends
    # up resolving against a lock that was never paired with this code; making them owned paths means
    # the pairing is by construction rather than by procedure. `bin/lib/provision.py` reads them.
    "pyproject.toml",                   # the declared dependency matrix (base + [search] + [models])
    "uv.lock",                          # the exact transitive resolution, per platform
)

# The ONE writable directory in a sealed tree, and the whole of the exception (ADR-020 / Task 4a).
# `install()` creates it in staging, seals the tree with everything else, and then chmods THIS PATH
# alone back to 0755 — chmod needs ownership, not a writable parent, so no other path is unsealed
# even momentarily. What lands in it: the pinned `uv` binary (`tools/uv/<version>/uv`, itself sealed
# 0555 once its sha256 is verified), the environment `uv sync` manages (`tools/venv`, necessarily
# writable — uv owns it) and any uv-managed interpreter (`tools/python`).
#
# It carries no engine CODE, which is what keeps ADR-017 D4's claim intact: what must not be
# hot-patchable is the code, and everything here is reconstructible from the pin and the lock by
# deleting the directory. It is deliberately NOT in the ownership manifest — `verify()`'s seal check
# walks the manifest, so keeping `tools/` out of it means the exception never has to be special-cased
# inside the check. `verify()` asks about it separately and asks the INVERSE question (is it there
# and is it still writable), so the exception is in the model rather than in what the walk misses.
PROVISION_DIR = "tools"

# Directories under `bin/` that are NOT verbs, so `verify()` does not demand a run.py of them.
NON_VERB_BIN_DIRS = ("lib",)
# Excluded from the verb walk by NAME rather than by a `__`-prefix rule. The prefix rule was meant
# for this one directory and also swallowed `bin/__complete/`, which is a REAL verb carrying both a
# run.py and a cmd.json: it could vanish from an installed engine and `verify()`, `--verify` and
# `doctor` would all still call the tree complete. Excluding by name is the narrowest spelling of
# what was actually meant, and it makes `_verb_dirs` agree with the parity suite's count (which
# excludes `lib` and nothing else).
NON_VERB_BIN_NAMES = frozenset(NON_VERB_BIN_DIRS) | {"__pycache__"}

# The dispatch-critical files, probed on EVERY invocation by `require_intact()`. Deliberately the
# cheap half of `verify()` — and the number is MEASURED, by counting `Path.stat` on a real installed
# tree: 6 for `require_intact()` against 146 for the full walk. It used to read "four stats", and it
# was 40: `_verb_dirs()` built and sorted the whole list to answer "is there at least one", which
# `_has_verb_dir()` now short-circuits. The full walk runs at INSTALL time (where a broken tree must
# never be left behind) and in `plainkeep doctor`.
#
# WHICH four is the question, not how few. `bin/lib/vaultroot.py` used to head this list and was
# tautological: the only product caller is `vaultroot.require_engine()`, which runs INSIDE
# vaultroot.py, so the file cannot be missing while the check runs. Meanwhile the modules vaultroot
# imports at module scope — `output`, `wall`, `vaultreg`, `enginetree` — died with a raw traceback
# before the probe could speak, which is the failure mode ADR-014 D2's "absent/unverified → refuse"
# exists to prevent. `output.py` takes the tautological slot (it is what a refusal is PRINTED with,
# so its absence is the one that produces the worst output), and vaultroot's sibling-import block
# refuses in the same words when one of the others is gone.
DISPATCH_PROBE = (
    "bin/lib/output.py",                # the refusal itself is printed with this
    "bin/lib/vaultreg.py",              # ...VaultError and the canonical/compare pair live here
    "bin/lib/guardrail.py",             # ...the gate
    "bin/lib/resolver.py",              # ...how a verb is found
    "VERSION",                          # manifest.py's <engine>/VERSION
)


def launcher(root: Path | None = None) -> Path:
    """The `plainkeep` launcher inside an engine tree — THIS engine tree, version and all.

    For spawning something NOW. For a path that gets written down, use `stable_launcher()`."""
    return (root or ENGINE_ROOT) / "plainkeep"


def stable_launcher() -> Path:
    """The launcher path to BAKE INTO an artefact that outlives this invocation — a launchd plist, an
    MCP client's server config, anything persisted.

    `ENGINE_ROOT` resolves THROUGH `current` on purpose (see above), so `launcher()` always spells the
    version: `…/engine/4.0.0-dev/plainkeep`. That is right for a spawn and wrong for a record. A plist
    written with a version-pinned path keeps running the OLD engine after the next `--activate`,
    silently, and becomes ENOENT the moment that version is pruned — at 2am, in a sanitized launchd
    environment, which is the failure `_plist`'s own comment says the absolute path exists to avoid.
    `current` is the name that survives an engine update, and it is already what `script/setup`
    symlinks onto PATH and what both refusal hints tell an operator to run.

    Falls back to the version-pinned path when there is no `current` to point at — a contributor
    running `./plainkeep` out of a checkout has no install root, and a persisted path that exists is
    worth more than a stable one that does not."""
    link = current_link()
    try:
        if link.is_symlink() and (link / "plainkeep").is_file():
            return link / "plainkeep"
    except OSError:
        pass
    return launcher()


def engine_bin(root: Path | None = None) -> Path:
    return (root or ENGINE_ROOT) / "bin"


# --- the install root (installer surface only) ----------------------------------------------------
def install_root() -> Path:
    """`${PLAINKEEP_ENGINE_HOME:-${XDG_DATA_HOME:-$HOME/.local/share}/plainkeep}`.

    XDG-correct and next to the vault registry's config dir, which is the controller's decision for
    this task. Read ONLY by the installer surface — see the module header for why a dispatch must
    never ask this question."""
    v = os.environ.get(ENV_INSTALL_ROOT)
    if v and v.strip():
        return Path(os.path.expanduser(v.strip()))
    xdg = os.environ.get("XDG_DATA_HOME")
    base = (Path(os.path.expanduser(xdg)) if xdg and xdg.strip()
            else Path(os.path.expanduser("~")) / ".local" / "share")
    return base / "plainkeep"


def versions_dir() -> Path:
    return install_root() / VERSIONS_DIRNAME


def current_link() -> Path:
    return versions_dir() / CURRENT_NAME


def installed_versions() -> list[str]:
    """Every INSTALLED version. Dot-prefixed names are excluded because they are the installer's own
    namespace, not versions: a `SIGKILL` mid-copy leaves `.incoming-<v>.<pid>` behind, and listing it
    as an installed version made `--print versions` report debris and `--activate` willing to point
    `current` at a half-copied tree."""
    try:
        return sorted(p.name for p in versions_dir().iterdir()
                      if p.is_dir() and not p.is_symlink() and not p.name.startswith("."))
    except OSError:
        return []


def active_engine() -> Path | None:
    """What `current` points at, resolved — or None when nothing is activated OR when the link
    dangles.

    `os.path.realpath` does not require the target to exist, so a `current` left pointing at a tree
    that is gone used to print a path and exit 0 — from the one diagnostic an operator reaches for
    when the launcher answers "no such file or directory". `is_dir()` is what makes the answer mean
    "there is an engine there"."""
    link = current_link()
    try:
        if not link.is_symlink():
            return None
        p = Path(os.path.realpath(link))
        return p if p.is_dir() else None
    except OSError:
        return None


# --- verification ---------------------------------------------------------------------------------
def _verb_dirs(root: Path) -> list[Path]:
    try:
        return sorted(d for d in (root / "bin").iterdir()
                      if d.is_dir() and d.name not in NON_VERB_BIN_NAMES)
    except OSError:
        return []


def _has_verb_dir(root: Path) -> bool:
    """Is there at least ONE verb directory? Short-circuits, so the per-invocation probe stops at the
    first hit instead of building and sorting the whole list (36 `is_dir()` calls for an answer that
    the first entry settles)."""
    try:
        return any(d.is_dir() for d in (root / "bin").iterdir()
                   if d.name not in NON_VERB_BIN_NAMES)
    except OSError:
        return False


# The `bin/lib` modules an installed tree has to carry, and the content paths that make an owned
# DIRECTORY owned — see the note above OWNED_TREES: these two lists are the other half of the
# manifest, and a new engine-owned path may belong in one of them.
NAMED_LIB_MODULES = ("vaultroot.py", "vaultreg.py", "enginetree.py", "guardrail.py", "resolver.py",
                     "manifest.py", "output.py", "paths.py", "wall.py", "vaultio.py",
                     # the plugin spawn contract (Task 3): resolver.py's `--dispatch` imports it, so a
                     # tree missing it dispatches every ENGINE verb fine and breaks every PLUGIN verb
                     "pluginenv.py")
NAMED_CONTENT = ("bin/ui/version.txt", "bin/share/worker/worker.js",
                 "templates/verb/run.py", "templates/verb/cmd.json",
                 "skills/operate-plainkeep/SKILL.md")

# The seal problem, as one exact string: `install()` matches on it to tell "this tree is complete but
# was never sealed" (repairable by re-sealing) from every other kind of incompleteness (not).
_UNSEALED = "engine tree is WRITABLE — it was not sealed, or the seal was removed"

# What the seal check stats when the caller has NOT already walked the tree. One path per owned tree
# plus the two roots — because `_chmod_tree` seals FILES first and then directories bottom-up, so a
# seal that stopped partway leaves something in this set writable — plus every named lib module,
# because the sample used to reach exactly one file under `bin/lib/` (this one) and every module a
# hot patch would actually go for (`guardrail.py`, `vaultroot.py`, `resolver.py`, `wall.py`) was
# unsampled. Ten extra `stat` calls. `verify()` does not use this list at all: it hands over the modes
# it has already paid for, which covers all 35 verb entry points as well — see `seal_problems`.
_SEAL_SAMPLE = ("VERSION", "plainkeep", "bin", "bin/lib",
                *(f"bin/lib/{m}" for m in NAMED_LIB_MODULES),
                *NAMED_CONTENT, "frontends/raycast")


def _looks_installed(root: Path) -> bool:
    """Is `root` a tree reachable under a VERSION NAME, i.e. one `install()` produced?

    The seal is a property of an INSTALLED engine and of nothing else: a contributor's checkout is
    writable by definition, and so is `install()`'s own `.incoming-*` staging before the seal is
    applied. Asked of the layout (`<anything>/engine/<version>/`) rather than of `versions_dir()`,
    because `versions_dir()` reads the environment and a doctor run with a different
    `PLAINKEEP_ENGINE_HOME` would then skip the check silently — the one outcome a seal check must
    not have."""
    return root.parent.name == VERSIONS_DIRNAME and not root.name.startswith(".")


def _mode_of(p: Path) -> int | None:
    """`p`'s `st_mode`, or None when it is not there. ONE `stat` answering both of the questions
    `verify()` asks about a path: is it present and of the right kind, and is it still sealed."""
    try:
        return p.stat().st_mode
    except OSError:
        return None


def seal_problems(root: Path, *, modes: list[int] | None = None) -> list[str]:
    """Is the tree still read-only? Empty means yes (or that it is not an installed tree at all).

    The module header claims immutability is ENFORCED rather than asserted. It was enforced at
    install time only: nothing on any later path asked whether the tree was still read-only, so a
    `SIGKILL` in the window between the rename and the seal — the window the rename-first order
    necessarily creates — left a fully writable engine that `--verify` called OK, `doctor` called
    complete, and every dispatch accepted. This is the question that was never asked.

    **What this proves, exactly: NOT that the tree is authentic.** A writable file is evidence that
    the seal is gone. A read-only one is not evidence that the content is the content that was
    installed — anyone who can `chmod u+w` a file to edit it can `chmod u-w` it afterwards, and
    nothing here would know, because nothing here reads content. `verify()` answers COMPLETE and this
    answers SEALED; neither answers AUTHENTIC. So this is an integrity check against ACCIDENT — an
    interrupted install, a seal that stopped partway, a `chmod -R u+w` someone ran to make one edit
    and never undid — and it is NOT a defence against a deliberate patch. What stands against that is
    that the tree is not writable in the first place, and reinstalling from a source of record.
    Content authentication would need digests recorded OUTSIDE the tree; this task does not ship them,
    and a check that samples modes must not be read as if it did.

    Within that limit the check is no longer a sample where it matters. `modes` is the list of
    `st_mode`s the caller has ALREADY collected while asking its own questions: `verify()` passes the
    one it built walking the manifest, which makes this exhaustive over every path `verify()` touched
    — the named lib modules, all 35 verb `run.py`/`cmd.json` pairs, the named content — at no extra
    syscall. Passing nothing falls back to `_SEAL_SAMPLE`, for a caller that has not walked the tree.
    """
    if not _looks_installed(root):
        return []
    if modes is None:
        modes = [m for m in (_mode_of(root / rel) for rel in _SEAL_SAMPLE) if m is not None]
    if any(m & stat.S_IWUSR for m in modes):
        return [_UNSEALED]
    # The root itself is in neither list: `verify()` asks about paths INSIDE the tree, and the sample
    # is written relative to it.
    m = _mode_of(root)
    return [_UNSEALED] if m is not None and m & stat.S_IWUSR else []


# --- the digest manifest ---------------------------------------------------------------------------
# WHAT WAS DELIVERED, recorded OUTSIDE the tree it covers (Phase 2 Task 4b).
#
# The module header used to end this subject with "authenticating content would need digests kept
# outside the tree, which is not shipped". This is that, shipped, and it exists because 4b's gate
# needs it: provisioning runs `uv sync --frozen` against a `pyproject.toml` and a `uv.lock` that ship
# INSIDE the engine, and "a tampered lock fails its checksum rather than installing" is only true if
# there is a checksum to fail, kept somewhere the same edit cannot rewrite.
#
# `<install-root>/engine/.digests/<version>.json`, beside the versioned trees rather than in one:
#   * a `chmod u+w && edit` inside the sealed tree does not reach it, which is the entire point;
#   * it is keyed by version, so it is written, replaced and removed exactly with its tree;
#   * `.`-prefixed, so `installed_versions()` and `_looks_installed()` skip it for free.
#
# WHAT IT DOES AND DOES NOT PROVE, in the same terms `seal_problems` uses. It proves the tree is
# byte-for-byte what `install()` copied. It does NOT prove the SOURCE was authentic — `install()`
# digests what it was handed — and it is not a defence against someone who can write the digest file
# too. It closes the gap between "sealed" (modes) and "complete" (presence): a hot patch whose author
# put the mode back is now visible, where before it was not.
DIGESTS_DIRNAME = ".digests"
DIGEST_SCHEMA = "plainkeep.engine-digests/1"


def digests_path(root: Path) -> Path:
    """Where `root`'s manifest lives. Derived from the TREE, never from `versions_dir()`: a `doctor`
    run under a different `PLAINKEEP_ENGINE_HOME` must not look up the digests of an engine it is not
    talking about — the same trap `_looks_installed` avoids for the seal."""
    return root.parent / DIGESTS_DIRNAME / f"{root.name}.json"


def _owned_paths(root: Path) -> list[str]:
    """Every owned FILE in `root`, as sorted relative posix paths. Symlinks are listed but not
    digested (see `compute_digests`) — `frontends/raycast` may legitimately carry one."""
    rels: list[str] = [r for r in OWNED_FILES if (root / r).is_file()]
    for tree in OWNED_TREES:
        base = root / tree
        if not base.is_dir():
            continue
        for p in base.rglob("*"):
            if p.is_file() and not p.is_symlink() and "__pycache__" not in p.parts:
                rels.append(p.relative_to(root).as_posix())
    return sorted(set(rels))


def compute_digests(root: Path) -> dict[str, str]:
    """sha256 of every owned file. `hashlib` is imported HERE rather than at module scope for the
    reason the header gives about import weight: this module is imported on the first act of every
    invocation, and no dispatch ever computes a digest."""
    import hashlib
    out: dict[str, str] = {}
    for rel in _owned_paths(root):
        h = hashlib.sha256()
        with open(root / rel, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        out[rel] = h.hexdigest()
    return out


def record_digests(root: Path, digests: dict[str, str] | None = None) -> Path:
    """Write `root`'s manifest. Called by `install()` from the STAGING tree, before the rename, so
    the digests describe what is about to land rather than what survived it."""
    import json
    p = digests_path(root)
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = {"schema": DIGEST_SCHEMA, "version": root.name,
               "files": digests if digests is not None else compute_digests(root)}
    tmp = p.with_name(f".{p.name}.incoming.{os.getpid()}")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, p)
    return p


def read_digests(root: Path) -> dict[str, str] | None:
    import json
    try:
        payload = json.loads(digests_path(root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    files = payload.get("files")
    return files if isinstance(files, dict) else None


def digest_problems(root: Path, *, only: tuple[str, ...] | None = None) -> list[str]:
    """Every owned file whose content is not what was installed. Empty means it matches (or that
    `root` is not an installed tree at all, which is the same convention `seal_problems` uses — a
    contributor's checkout has no manifest and is not claiming to).

    `only` narrows it to named paths. It is NOT what the provisioning gate uses any more: naming the
    subset by hand is what left `bin/lib/uvpin.json` — the file that decides which binary is
    downloaded and executed — outside a gate that was reported as covering provisioning. See
    `provision.require_delivered_intact`. The parameter stays because a caller that genuinely wants
    one path (a probe, a report) should not pay ~114 digests for it."""
    import hashlib
    if not _looks_installed(root):
        return []
    recorded = read_digests(root)
    if recorded is None:
        return [f"no recorded checksums for this engine ({digests_path(root)} is missing or unreadable)"]
    rels = [r for r in recorded if only is None or r in only]
    if only is not None:
        for r in only:
            if r not in recorded:
                return [f"{r} has no recorded checksum"]
    problems: list[str] = []
    for rel in sorted(rels):
        p = root / rel
        try:
            h = hashlib.sha256()
            with open(p, "rb") as f:
                for chunk in iter(lambda: f.read(1 << 20), b""):
                    h.update(chunk)
        except OSError:
            problems.append(f"{rel} is recorded but missing")
            continue
        if h.hexdigest() != recorded[rel]:
            problems.append(f"{rel} does not match its recorded checksum")
    if only is None:
        for rel in _owned_paths(root):
            if rel not in recorded:
                problems.append(f"{rel} is present but was never recorded")
    return problems


def verify(root: Path, *, check_seal: bool = True, check_digests: bool = False) -> list[str]:
    """Every way `root` fails to be a complete engine tree, as a list of one-line problems.

    Empty means the whole ownership manifest is present AND — for a tree installed under a version
    name — that the tree is still sealed. Called by `install()` (which refuses to leave an incomplete
    tree behind) and by `plainkeep doctor`. It walks the tree, so it is NOT what a dispatch runs —
    `require_intact()` is.

    `check_seal=False` is for the two callers that ask about COMPLETENESS only: `install()` verifying
    its own staging (not yet sealed, by construction) and `activate()` (an engine installed
    `--writable` is a deliberate dev shape and is still activatable — `--verify` and `doctor` are
    where that gets reported).

    Every presence question below goes through `_mode_of`, which is the same one `stat` the old
    `is_file()`/`is_dir()` calls each performed — the mode is now KEPT rather than thrown away, and
    handed to `seal_problems`. That is what makes the seal check exhaustive over this walk instead of
    a nine-path sample that reached one file under `bin/lib/`: the manifest and the seal are two
    questions about the same paths, so they cost one traversal, not two."""
    problems: list[str] = []
    modes: list[int] = []                 # every st_mode this walk has already paid for

    def kind_of(p: Path) -> str:
        m = _mode_of(p)
        if m is None:
            return "-"
        modes.append(m)
        return "f" if stat.S_ISREG(m) else "d" if stat.S_ISDIR(m) else "?"

    for rel in OWNED_FILES:
        if kind_of(root / rel) != "f":
            problems.append(f"missing engine file: {rel}")
    for rel in OWNED_TREES:
        if kind_of(root / rel) != "d":
            problems.append(f"missing engine tree: {rel}/")
    if kind_of(root / "bin" / "lib") != "d":
        problems.append("missing engine tree: bin/lib/")
    else:
        for mod in NAMED_LIB_MODULES:
            if kind_of(root / "bin" / "lib" / mod) != "f":
                problems.append(f"missing engine module: bin/lib/{mod}")
    verbs = _verb_dirs(root)
    if not verbs:
        problems.append("bin/ carries no verb directory at all")
    for d in verbs:
        for f in ("run.py", "cmd.json"):
            if kind_of(d / f) != "f":
                problems.append(f"verb {d.name!r} is missing bin/{d.name}/{f}")
    # The five paths the ownership table assigns to the engine that are neither bin/ nor VERSION, and
    # that no task moved before this one. Named individually because "the directory exists" is not
    # what makes them owned — their CONTENT is what an installed tree has to carry.
    for rel in NAMED_CONTENT:
        if kind_of(root / rel) != "f":
            problems.append(f"missing engine file: {rel}")
    scripts = sorted((root / "frontends" / "raycast").glob("*.sh"))
    if not scripts:
        problems.append("missing engine files: frontends/raycast/*.sh")
    for s in scripts:                     # they are executable engine code; the seal covers them too
        kind_of(s)
    # `tools/` is asked about SEPARATELY and in the INVERSE direction (Task 4a): it must be there and
    # it must still be writable, because it is where provisioning lands and a sealed one turns every
    # `plainkeep setup` into a permission error at the last step. Its mode is deliberately NOT added
    # to `modes` — that list feeds the seal check, and this is the one path exempt from it.
    #
    # Asked of an INSTALLED tree only, and that scope is load-bearing rather than cautious: `install()`
    # creates the directory, so its absence from an installed tree means the install is incomplete —
    # while a contributor's CHECKOUT has never been provisioned into and creates it on first use.
    # Demanding it there would have reddened `plainkeep doctor` on every fresh clone, which is the
    # opposite of what a completeness check is for.
    if _looks_installed(root):
        tools = root / PROVISION_DIR
        tm = _mode_of(tools)
        if tm is None or not stat.S_ISDIR(tm):
            problems.append(f"missing engine tree: {PROVISION_DIR}/")
        elif not tm & stat.S_IWUSR:
            problems.append(f"{PROVISION_DIR}/ is read-only — the engine cannot provision uv or its "
                            "Python environment into it")
    if check_seal:
        problems.extend(seal_problems(root, modes=modes))
    if check_digests:
        problems.extend(digest_problems(root))
    return problems


def require_intact(root: Path | None = None) -> None:
    """Refuse a broken engine tree, cheaply, on every invocation.

    ADR-014 D2 gives `PLAINKEEP_ENGINE` the failure mode "absent/unverified → refuse". This is that
    refusal, and it lives where BOTH dispatchers already run one shared decision (`vaultroot
    --select`) so the floor and the core refuse with byte-identical text.

    It replaces Task 1b's `require_engine(sel)`, which asked whether the selected VAULT carried the
    engine. That probe existed only because Phase 1 ran the engine out of the vault it acted on; this
    task removes the reason for it, and leaving it in place would refuse every data-only vault — which
    is exactly what Task 5's `init` must be able to produce."""
    r = root or ENGINE_ROOT
    for rel in DISPATCH_PROBE:
        if not (r / rel).is_file():
            raise VaultError(
                f"the plainkeep engine at {r} is incomplete (no {rel})",
                hint="reinstall it:\n"
                     f"    python3 {Path(__file__).resolve()} --install <source-checkout>")
    if not _has_verb_dir(r):
        raise VaultError(
            f"the plainkeep engine at {r} carries no verb directory under bin/",
            hint="reinstall it:\n"
                 f"    python3 {Path(__file__).resolve()} --install <source-checkout>")


# --- disjointness (ADR-014 D3, ACTIVATED in this task) --------------------------------------------
def _is_within(inner: str, outer: str) -> bool:
    """`inner` IS `outer` or lives under it, on a path boundary. Both must already be canonical.

    The comparison itself lives in `vaultreg` beside `canonical()`, deliberately: the reason a
    canonical path is not a comparison key (case folding, Unicode normalisation) is a fact about
    `canonical()`, so the rule belongs where `canonical()` is and every consumer of it inherits the
    same answer rather than each re-deriving one. This module keeps the name because the docstring
    below explains what the comparison is FOR."""
    return vaultreg.path_within(inner, outer)


# The paths whose presence makes a directory ENGINE rather than data. A subset of the ownership
# manifest, chosen for what actually discriminates: `templates/` and `frontends/` exist in a data
# vault for their own reasons, so naming those DIRECTORIES would call every vault an engine, while
# `templates/verb/run.py` is the scaffold and only an engine has one.
ENGINE_MARKERS = ("plainkeep", "VERSION", "bin/lib/vaultroot.py", "bin/lib/enginetree.py",
                  "bin/lib/guardrail.py", "templates/verb/run.py",
                  "skills/operate-plainkeep/SKILL.md")


def engine_paths_in(data_root) -> list[str]:
    """Engine-owned paths found INSIDE a data root — empty for the data-only vault `vault init`
    produces (Phase 2 Task 5).

    This is a weaker question than `disjointness_verdict` and a different one. Disjointness asks
    whether the data root and the RUNNING engine tree are the same or nested directories, and refuses
    with exit 5 when they are; that is what stops a vault being dispatched out of itself. This asks
    whether a vault carries a COPY of engine files, which is not the same thing and is not always
    wrong — the template checkout is legitimately both a source of the engine and a registered vault,
    which is exactly the Phase 1 shape migration exists to unwind.

    So the two callers treat it differently, on purpose. `vault init` REFUSES a target that already
    has any of these (that directory is a checkout, and `vault register` is the verb for adopting
    one), and asserts the list is empty for the vault it just created — the claim "init produces a
    data-only vault" enforced by the code that makes the claim rather than only stated beside it.
    `plainkeep doctor` REPORTS them, because a pre-Task-5 vault has them and telling that operator
    their vault is broken would be false."""
    root = Path(data_root)
    return [rel for rel in ENGINE_MARKERS if (root / rel).exists()]


def disjointness_verdict(data_root: str, engine_root: Path | None = None) -> str | None:
    """Why this data root and the engine tree overlap, or None when they are disjoint.

    ADR-014 D3 requires "not inside the engine root and the engine root not inside it". Task 1b
    DEFINED the rule and deliberately did not enforce it: while the engine was `<vault>/bin` the rule
    was unsatisfiable and would have refused every existing vault. It becomes true in this task, so it
    turns on in this task — a sequencing split rather than a silent legacy exception, which would have
    defeated the contract outright.

    Both sides are canonicalized before comparing, for `wall._anchored`'s reason: on macOS `/tmp/v`
    and `/private/tmp/v` are one directory under two names, and comparing spellings answers "disjoint"
    for a pair that is not. Canonicalizing is NECESSARY and not SUFFICIENT — it normalises symlinks
    and `..` but never case, and the default macOS volume folds case, so `/x/VAULTDIR` and
    `/x/vaultdir` survived it as two spellings and this function answered "disjoint" for one
    directory. Both comparisons below therefore go through `vaultreg`'s identity-aware pair rather
    than through `==` / `startswith`."""
    inside = inside_engine_verdict(data_root, engine_root)
    if inside is not None:
        return inside
    eng = vaultreg.canonical(str(engine_root or ENGINE_ROOT))
    data = vaultreg.canonical(data_root)
    if _is_within(eng, data):
        return f"the engine tree ({eng}) is inside it"
    return None


def inside_engine_verdict(data_root: str, engine_root: Path | None = None) -> str | None:
    """The two shapes of `disjointness_verdict` that a PARENT directory inherits DOWNWARD: this path
    is the engine tree, or it lives inside one.

    Split out for `vault init`, which has to ask the question about a directory that does not exist
    yet. `vaultreg.path_within` compares inodes and a missing path has none, so init asks the nearest
    existing ANCESTOR — and only these two shapes survive that substitution. The third ("the engine
    is inside it") does NOT: every ancestor of a target eventually reaches a directory that contains
    the engine tree somewhere below it (`/tmp`, `$HOME`, `/`), and asking it of an ancestor answers
    "overlap" for every path on the machine. Measured — it refused every `init` into a temp
    directory that also held the fixture engine. The full verdict is re-asked on the real path once
    it exists."""
    eng = vaultreg.canonical(str(engine_root or ENGINE_ROOT))
    data = vaultreg.canonical(data_root)
    if vaultreg.same_path(data, eng):
        return (f"it IS the engine tree ({eng}) — a vault is data and an engine is code, and one "
                f"directory cannot be both")
    if _is_within(data, eng):
        return f"it is inside the engine tree ({eng})"
    return None


# --- the failure-injection hook (Phase 2 Task 5) ---------------------------------------------------
# `PLAINKEEP_ENGINE_KILL_AT=<stage>` makes this process `SIGKILL` ITSELF the moment it reaches that
# boundary. It exists because the contract this module now carries — "after a kill at any boundary,
# either the old pair or the new pair is fully runnable, never neither" — cannot be checked by
# reading the code, and cannot be checked reliably from outside either: killing a subprocess "at the
# right moment" from a test is a race, and a race that usually lands somewhere harmless is a green
# test of nothing (ADR-015's standing rule, ADR-019 D2).
#
# It can only ABORT, never skip, weaken or reorder a step: the whole body is `os.kill(getpid(),
# SIGKILL)`, an unknown stage name is refused rather than ignored, and there is no value of the
# variable that makes an update do LESS checking and still succeed. `test/run_engineupdate.py`
# pins both halves — that every declared stage is actually reachable (a hook nothing reaches is the
# ADR-019 failure applied to the gate itself), and that the variable cannot produce a rc=0 run.
#
# SIGKILL rather than SIGABRT/SIGSEGV on purpose: it is the harshest interruption a filesystem
# sequence can face (no atexit, no `finally`, no flush) and it is the one signal macOS does NOT
# route through the crash reporter, so a full injection matrix costs the developer no crash dialogs.
ENV_KILL_AT = "PLAINKEEP_ENGINE_KILL_AT"
KILL_STAGES = (
    "provision-staged",             # the copy is complete and verified; nothing is in place yet
    "provision-replace-window",     # between remove_version() and rename — the measured residue
    "provision-renamed",            # the tree is under its version name and NOT yet sealed
    "checksum",                     # digests computed, before the pair manifest is recorded
    "selftest",                     # the new pair answered a real verb, before the pointer moves
    "activate",                     # the last instruction before os.replace of `current`
    "pointer",                      # `current` now names the new pair; state/cleanup not yet done
    "cleanup",                      # mid-prune of a retired version
)


def _kill_hook(stage: str) -> None:
    if os.environ.get(ENV_KILL_AT) != stage:
        return
    if stage not in KILL_STAGES:                     # unreachable via the guard above; kept explicit
        raise VaultError(f"unknown {ENV_KILL_AT} stage: {stage!r}")
    import signal
    sys.stderr.write(f"plainkeep: {ENV_KILL_AT}={stage} — killing this process here\n")
    sys.stderr.flush()
    os.kill(os.getpid(), signal.SIGKILL)


def _check_kill_stage() -> None:
    """A misspelled stage name is a test that injects NOTHING and passes. Refuse it at the door."""
    want = os.environ.get(ENV_KILL_AT)
    if want and want not in KILL_STAGES:
        raise VaultError(f"{ENV_KILL_AT}={want!r} names no boundary",
                         hint="stages: " + ", ".join(KILL_STAGES))


# --- installing ------------------------------------------------------------------------------------
def check_version_name(v: str, where: str) -> str:
    """A version is a SINGLE DIRECTORY NAME under `versions_dir()`, and this is what makes that true.

    It used to guard only the value read out of a source's `VERSION` file, while `--version` — a
    caller-supplied string that composes into the same destination — reached `versions_dir() / v`
    unchecked. That is path injection, and it was not theoretical: `--version ..` resolved
    `remove_version()` to `rmtree(<install root>)` and deleted everything the shared XDG data
    directory held besides `engine/`; `--version current` made `remove_version()` walk THROUGH the
    active symlink and strip the seal off the running engine.

    Validated rather than sanitised, on purpose: a rewrite ("strip the slashes and carry on") has to
    be right about every spelling that can reach a path, and being wrong about one is silent. A
    refusal only has to be right about being suspicious."""
    if not v:
        raise VaultError(f"{where} is empty — an engine tree is installed BY VERSION")
    if "/" in v or os.sep in v or v in (".", ".."):
        raise VaultError(f"{where} is not usable as a version directory name: {v!r}")
    if v.startswith("."):
        # Dot-prefixed names are the installer's own namespace (`.incoming-*`, `.current.incoming.*`)
        # and are excluded from `installed_versions()`; a version that hid there would be installable
        # and then invisible.
        raise VaultError(f"{where} may not begin with a dot — that is the installer's own "
                         f"namespace: {v!r}")
    if v == CURRENT_NAME:
        raise VaultError(f"{where} is the reserved name of the active-engine symlink: {v!r}",
                         hint=f"`{CURRENT_NAME}` names whichever version is active — install under a "
                              f"version name and point it there with --activate")
    return v


def read_version(src: Path) -> str:
    try:
        v = (src / "VERSION").read_text(encoding="utf-8").strip()
    except OSError as e:
        raise VaultError(f"cannot read {src / 'VERSION'} ({e})")
    return check_version_name(v, str(src / "VERSION"))


def _chmod_tree(root: Path, *, writable: bool) -> None:
    """Make an installed tree read-only (or writable again, to replace it).

    Bottom-up when locking, so a directory is not sealed before its children are; top-down when
    unlocking, so a sealed directory can be descended into at all.

    SEALING RAISES; unsealing does not. Both directions used to swallow every `OSError` with a bare
    `pass`, which meant a seal that half-failed reported success and left a tree that could be
    hot-patched — the one property this whole module exists to establish, off, with nothing said. A
    failed UNSEAL is different: it is only ever a prelude to `rmtree`, which will fail loudly on its
    own if the tree really is unremovable, and refusing there would turn a cleanup path into a dead
    end.

    **SEALING SKIPS `PROVISION_DIR`; UNSEALING DOES NOT.** The walk is `root.rglob("*")` — the whole
    tree — and on an ALREADY-PROVISIONED tree that swept up `tools/venv` and the uv-managed
    interpreter under it and left them read-only. `_seal_installed` re-opened `tools/` ITSELF only, so
    the damage was invisible: `verify()` asks whether that one directory is writable, which it is, and
    called a half-sealed tree OK — while the next `uv sync` fails on permissions inside a venv uv
    owns. Reproduced through the product CLI (`--install --writable` → provision → plain `--install`,
    which takes the repair branch). The subtree is pruned HERE rather than repaired afterwards,
    because a seal that has to be undone in the right places is a seal that will one day be undone in
    the wrong ones — and because the immutability claim is about ENGINE CODE, of which `tools/` holds
    none (see PROVISION_DIR). Unsealing still walks it: that direction is only ever a prelude to
    `rmtree`, which has to reach everything."""
    entries = list(root.rglob("*"))
    if not writable:
        provisioned = root / PROVISION_DIR
        entries = [p for p in entries if p != provisioned and provisioned not in p.parents]
    dirs = [p for p in entries if p.is_dir() and not p.is_symlink()]
    files = [p for p in entries if p.is_file() and not p.is_symlink()]
    if writable:
        for d in [root, *dirs]:
            try:
                d.chmod(0o755)
            except OSError:
                pass
        for f in files:
            try:
                f.chmod(0o644 if not (f.stat().st_mode & stat.S_IXUSR) else 0o755)
            except OSError:
                pass
        return
    failed: list[str] = []
    for f in files:
        try:
            f.chmod(0o555 if (f.stat().st_mode & stat.S_IXUSR) else 0o444)
        except OSError as e:
            failed.append(f"{f}: {e}")
    for d in sorted([root, *dirs], key=lambda p: len(p.parts), reverse=True):
        try:
            d.chmod(0o555)
        except OSError as e:
            failed.append(f"{d}: {e}")
    if failed:
        raise VaultError(
            f"could not seal the engine tree at {root} ({len(failed)} path(s) stayed writable):\n  "
            + "\n  ".join(failed[:5]),
            hint="an engine that can be written can be hot-patched, which is the one thing an "
                 "installed engine must not be — the tree was NOT left in place")


def _copy_owned(src: Path, dst: Path) -> None:
    ignore = shutil.ignore_patterns("__pycache__", "*.pyc")
    for rel in OWNED_TREES:
        s = src / rel
        if not s.is_dir():
            raise VaultError(f"source tree is missing {rel}/ — cannot install an engine from {src}")
        d = dst / rel
        d.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(s, d, ignore=ignore, symlinks=True)
    for rel in OWNED_FILES:
        s = src / rel
        if not s.is_file():
            raise VaultError(f"source tree is missing {rel} — cannot install an engine from {src}")
        d = dst / rel
        d.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(s, d)
    # The compiled core, when the source tree has one. OPTIONAL by design: the bash floor is the
    # zero-install path, and an engine tree without a core binary is a complete engine that dispatches
    # through the floor. It is not in the ownership manifest for that reason, and `verify()` does not
    # ask for it.
    core = src / ".local" / "bin" / "plainkeep-core"
    if core.is_file():
        d = dst / ".local" / "bin" / "plainkeep-core"
        d.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(core, d)
    # The provisioning directory is created EMPTY here rather than on first use, so that `verify()`
    # can ask for it and so that the seal-then-chmod dance in `install()` has exactly one path to
    # exempt. It is not copied from `src`: a contributor's checkout may have a populated `tools/`
    # holding a uv for a different engine version, and an install is a fresh tree.
    (dst / PROVISION_DIR).mkdir(parents=True, exist_ok=True)


# How old an abandoned staging tree must be before an unrelated install sweeps it. A `SIGKILL` mid
# copy leaves one behind and nothing else will ever remove it; a CONCURRENT install must not have its
# staging removed underneath it, which is the bug the pid suffix fixes and an eager sweep would undo.
# A day is far past any real copy and far short of "never".
STALE_STAGING_SECONDS = 24 * 60 * 60


def _sweep_stale_staging(root: Path, version: str) -> None:
    """Remove ABANDONED `.incoming-<version>.<pid>` trees — never a live one. Best effort: a staging
    directory this process cannot clean is not a reason to refuse to install."""
    import time
    cutoff = time.time() - STALE_STAGING_SECONDS
    for p in root.glob(f".incoming-{version}.*"):
        try:
            if not p.is_dir() or p.is_symlink() or p.stat().st_mtime > cutoff:
                continue
            _chmod_tree(p, writable=True)
            shutil.rmtree(p, ignore_errors=True)
        except (OSError, VaultError):
            continue


def remove_version(version: str) -> None:
    """Delete one installed version. Only ever called on a tree this module installed, and only to
    replace it: an installed engine is immutable, so `--install --force` removes and rewrites rather
    than patching in place.

    The name is re-validated here and not only at the caller, because this is the destructive end:
    `remove_version("..")` resolved to `rmtree(<install root>)` and `remove_version("current")` walked
    THROUGH the active symlink to unseal the running engine. A symlink is never a version and is
    never removed as one."""
    version = check_version_name(version, "version to remove")
    d = versions_dir() / version
    if d.is_symlink() or not d.is_dir():
        return
    _chmod_tree(d, writable=True)
    shutil.rmtree(d, ignore_errors=True)
    # The manifest is written and removed WITH its tree. A stale one outlives nothing useful: the
    # next install of the same version overwrites it anyway, and one left behind for a version that
    # is gone is a checksum nobody can check.
    try:
        digests_path(d).unlink()
    except OSError:
        pass


def _seal_installed(root: Path) -> None:
    """Seal the tree, then re-open the ONE writable exception (`PROVISION_DIR`).

    Both callers need both halves, and the pair is a unit: a seal without the re-open produces a tree
    that `verify()` calls broken and that no `plainkeep setup` can provision — which is exactly what
    the repair branch of `install()` used to produce, because it sealed and stopped there. `chmod`
    needs ownership rather than a writable parent, so this reaches into a 0555 root without unsealing
    it (see PROVISION_DIR).

    The re-open is now a REPAIR rather than the load-bearing half: `_chmod_tree` no longer walks into
    `PROVISION_DIR` when sealing, so a provisioned environment survives a re-seal intact instead of
    being sealed and then half-reopened. This line still runs, because a tree that arrived here with
    `tools/` already at 0555 (an older build, an interrupted seal) is exactly the state the repair
    branch exists to fix."""
    _chmod_tree(root, writable=False)
    (root / PROVISION_DIR).chmod(0o755)


def install(src: Path, *, version: str | None = None, force: bool = False,
            activate_it: bool = True, writable: bool = False) -> Path:
    """Install the engine-owned set from a source checkout into `<install root>/engine/<version>/`.

    Staged then renamed: the tree is built under a `.incoming-<version>.<pid>` sibling, VERIFIED
    there, and only moved into its final name once complete. A half-copied engine is never reachable
    under a version name, which matters because `current` may be pointed at it a line later.

    THE OLD TREE IS NOT REMOVED UNTIL A VERIFIED REPLACEMENT EXISTS. `--force` used to `rmtree` it on
    the way IN, before the copy that can fail — a source missing an owned path, `^C`, ENOSPC, a
    concurrent run — and the cleanup below then removed the debris and re-raised, leaving no engine at
    all and a dangling `current`. `script/setup` runs `--install --force` unconditionally and is also
    what an operator runs to REPAIR a broken install, so the failure window was on the repair path.
    The removal now happens one line before the rename: the exposure shrinks from the whole copy to
    an `rmtree` plus a `rename`.

    THAT WINDOW IS STILL OPEN, and the reason is a choice rather than an impossibility — said plainly
    here because an inaccurate impossibility claim guarding a known window is worse than the window.
    A kill between `remove_version()` and `os.rename` leaves no engine under the version name and a
    dangling `current`; a plain `--install` (not `--force`) recovers it. Closing it needs a
    swap-through-a-temporary-name: unseal `dst`, `rename(dst → .retiring-<v>.<pid>)`,
    `rename(staging → dst)`, `rmtree` the retired tree afterwards — and that sequence IS expressible,
    because `remove_version()` already runs the same unseal pass before its own `rmtree`. Measured:

        rename(SEALED dst → .retiring-*)                   EACCES     (the true constraint)
        _chmod_tree(dst, writable=True) THEN the rename     OK — the old tree survives the window
        one-syscall replace of a NON-EMPTY unsealed dst     ENOTEMPTY (errno 66)

    So what `os.rename` genuinely cannot do is replace a non-empty directory in one call, which is a
    narrower claim than "the dance cannot be expressed". It is not implemented here because it adds a
    third and fourth mutation to the destructive path to shrink a window that already recovers
    without `--force`; if it is ever implemented, the retired tree wants sweeping the way
    `.incoming-*` is."""
    src = Path(os.path.abspath(os.path.expanduser(str(src))))
    # `is not None`, not truthiness: `--version ""` is a SUPPLIED value and an invalid one, and a
    # falsy test quietly re-read the source's VERSION file instead — which made
    # `check_version_name`'s own "is empty" refusal unreachable from the flag that most needs it.
    version = check_version_name(version, "--version") if version is not None else read_version(src)
    root = versions_dir()
    dst = root / version
    if dst.exists() and not force:
        # An INTERRUPTED install leaves a tree that is complete but unsealed (the rename must precede
        # the seal — see below), and the only thing that used to move it forward was `--force`, i.e.
        # the destructive branch. Re-running the same install now REPAIRS it instead: the seal is the
        # one property that can be restored without touching content, so it is, and everything else
        # still refuses.
        problems = verify(dst)
        if problems == [_UNSEALED] and not writable:
            _seal_installed(dst)
            if activate_it:
                activate(version)
            return dst
        raise VaultError(f"engine {version} is already installed at {dst}",
                         hint="an installed engine is immutable — pass --force to replace it, "
                              "or bump VERSION")
    # PID-UNIQUE, like `activate()`'s `.current.incoming.<pid>`: two installs of one version used to
    # share a staging name, and the second `rmtree`'d the first one's tree mid-copy. Both then failed.
    staging = root / f".incoming-{version}.{os.getpid()}"
    root.mkdir(parents=True, exist_ok=True)
    _sweep_stale_staging(root, version)
    staging.mkdir()
    staged = True
    try:
        _copy_owned(src, staging)
        problems = verify(staging, check_seal=False)     # not sealed yet, by construction
        if problems:
            raise VaultError(
                f"refusing to install an incomplete engine from {src}:\n  "
                + "\n  ".join(problems),
                hint="the ownership manifest is enumerated in bin/lib/enginetree.py "
                     "(OWNED_TREES / OWNED_FILES)")
        # RENAME FIRST, SEAL SECOND, and the order is forced rather than chosen: renaming a
        # DIRECTORY needs write permission on the directory itself (the kernel updates its `..`
        # entry), so a tree sealed at 0555 cannot be moved into place at all — measured, EACCES on
        # macOS. The property the staging dance exists for is unaffected: what lands under the
        # version name is already complete and already verified, and only its permissions change
        # afterwards. The window it opens — a rename that lands and a seal that does not — is what
        # `verify()`'s mode check and the repair branch above exist to make visible and fixable.
        # Digested from the STAGING tree, before the rename: the manifest describes what is about to
        # land, and it is on disk before the tree it covers is reachable under a version name. It is
        # written under `dst`'s name (that is where the tree is going), so a failure after this point
        # leaves a manifest for a version that does not exist — which the `except` below removes
        # along with the tree, and which `digest_problems` would report as a missing tree anyway.
        digests = compute_digests(staging)
        _kill_hook("provision-staged")
        if dst.exists():
            remove_version(version)
            # THE MEASURED RESIDUE, and the one place a kill genuinely leaves no engine under
            # `version`. `update()` never reaches it with the ACTIVE version as its target (see
            # `_refuse_touching_active`), so the exposure is bounded to `--install --force` over the
            # running tree — which is `script/setup`'s path, not the update path. The injection cell
            # that proves the difference is `run_engineupdate.py::case_kill_matrix`.
            _kill_hook("provision-replace-window")
        os.rename(staging, dst)
        _kill_hook("provision-renamed")
        staged = False
        record_digests(dst, digests)
        if not writable:
            _seal_installed(dst)
    except BaseException:
        if staged:
            _chmod_tree(staging, writable=True)
            shutil.rmtree(staging, ignore_errors=True)
        else:
            remove_version(version)
        raise
    if activate_it:
        activate(version)
    return dst


def activate(version: str) -> Path:
    """Point `current` at `<version>`, atomically. A symlink cannot be rewritten in place, so a
    uniquely named one is created beside it and `os.replace`d over the old — an invocation that
    starts mid-switch sees one engine or the other, never nothing."""
    version = check_version_name(version, "--activate")
    dst = versions_dir() / version
    if not dst.is_dir():
        raise VaultError(f"engine {version} is not installed ({dst} does not exist)",
                         hint="install it first: --install <source-checkout>")
    # COMPLETENESS, not the seal: an engine installed `--writable` is a deliberate dev shape and is
    # still a complete engine. `--verify` and `doctor` are where a missing seal gets reported.
    problems = verify(dst, check_seal=False)
    if problems:
        raise VaultError(f"refusing to activate an incomplete engine at {dst}:\n  "
                         + "\n  ".join(problems))
    link = current_link()
    tmp = link.with_name(f".{CURRENT_NAME}.incoming.{os.getpid()}")
    if tmp.exists() or tmp.is_symlink():
        tmp.unlink()
    tmp.symlink_to(dst)
    os.replace(tmp, link)
    return link


# --- update: atomic activation with the previous pair retained (Phase 2 Task 5) --------------------
#
# `install()` above is the primitive: it stages, verifies, renames, seals and can activate. `update()`
# is the OPERATION an operator runs, and the difference between them is a contract rather than a
# convenience:
#
#   1. **The pair.** What gets activated is a core+engine PAIR — the versioned tree and, when the
#      source carries one, the compiled `plainkeep-core` that ships beside it. `install()` copies the
#      core as an optional extra that `verify()` never asks about. Here it is checksummed with the
#      rest and self-tested in the dispatcher mode it powers, because a tree whose core is truncated
#      dispatches fine on the floor and dies under `PLAINKEEP_CORE=require`.
#
#   2. **The running pair is never the target.** `update()` refuses a version that IS the active one
#      (as a no-op when that version is already healthy — see `update()`'s docstring for why that is
#      the correct answer rather than an error). Everything destructive it can reach therefore acts
#      on a directory the running engine does not live in, and THAT is what makes "the previous pair
#      is retained" true by construction instead of usually: retention is not a cleanup policy that
#      could be got wrong, it is the absence of any code path that could remove it.
#
#   3. **One pointer switch, last.** Checksums and the self-test both run against the tree at its
#      FINAL path, sealed, before `current` moves. A kill anywhere before `os.replace` leaves the old
#      pair active and complete; a kill after it leaves the new pair active and complete. There is no
#      third outcome, which is the property `KILL_STAGES` exists to let a test demand rather than
#      believe.
#
# What is deliberately NOT claimed: that the tree is AUTHENTIC. `pair_digests()` proves the installed
# pair is byte-for-byte the source it was copied from (and, with `--expect`, that the source matched
# a record made elsewhere). It does not prove the source was legitimate — see `seal_problems`, which
# states the same limit for the seal.
CORE_REL = os.path.join(".local", "bin", "plainkeep-core")
PAIRS_DIRNAME = ".pairs"
PAIR_SCHEMA = "plainkeep.pair/1"
STATE_SCHEMA = "plainkeep.pairstate/1"
UPDATE_LOCK_NAME = ".update.lock"
DEFAULT_KEEP = 2                   # the active pair and the one to roll back to — the whole contract


def pairs_dir() -> Path:
    """Where the pair manifests and the activation state live: BESIDE the version trees, never
    inside one. A digest recorded inside the tree it covers is rewritable by whatever rewrote the
    tree, and the sealed tree cannot be written by the updater afterwards anyway."""
    return versions_dir() / PAIRS_DIRNAME


def pair_manifest_path(version: str) -> Path:
    return pairs_dir() / f"{check_version_name(version, 'pair manifest version')}.json"


def state_path() -> Path:
    return pairs_dir() / "state.json"


def _read_json(p: Path) -> dict | None:
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return d if isinstance(d, dict) else None


def _write_json_atomic(p: Path, doc: dict) -> Path:
    """Write, fsync, `os.replace`. An interrupted write leaves the OLD file, never a truncated one —
    the same shape `vaultreg.write_registry` uses, for the same reason."""
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(f".{p.name}.incoming.{os.getpid()}")
    fd = os.open(str(tmp), os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
    try:
        os.write(fd, (json.dumps(doc, indent=2, sort_keys=True) + "\n").encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, p)
    return p


# --- the pair's checksums ---------------------------------------------------------------------
def pair_files(root: Path) -> list[str]:
    """Every REGULAR file the pair consists of, as sorted relative paths.

    Symlinks are skipped rather than followed: `_copy_owned` copies with `symlinks=True`, so a link
    in the source is a link in the installed tree, and hashing what it points at would record the
    target's bytes under the link's name — a manifest that disagrees with itself the moment the
    target changes."""
    rels: list[str] = []
    for rel in OWNED_FILES:
        if (root / rel).is_file() and not (root / rel).is_symlink():
            rels.append(rel)
    for rel in OWNED_TREES:
        base = root / rel
        if not base.is_dir():
            continue
        for p in base.rglob("*"):
            if p.is_symlink() or not p.is_file() or "__pycache__" in p.parts:
                continue
            rels.append(str(p.relative_to(root)))
    core = root / CORE_REL
    if core.is_file() and not core.is_symlink():
        rels.append(CORE_REL)
    return sorted(set(rels))


def pair_digests(root: Path) -> dict[str, str]:
    """sha256 per file. Chunked, because the core binary is tens of megabytes."""
    import hashlib
    out: dict[str, str] = {}
    for rel in pair_files(root):
        h = hashlib.sha256()
        with (root / rel).open("rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
        out[rel] = h.hexdigest()
    return out


def digest_problems(root: Path, expected: dict) -> list[str]:
    """Every way the tree at `root` disagrees with `expected`, as one-line problems.

    Missing, changed AND extra are all problems: a manifest that only checked the files it lists
    would pass a tree with an extra `bin/<verb>/run.py` copied in from somewhere else, and a verb
    directory is exactly what an attacker or a bad merge would add."""
    got = pair_digests(root)
    problems = []
    for rel in sorted(set(expected) | set(got)):
        want, have = expected.get(rel), got.get(rel)
        if want is None:
            problems.append(f"unexpected file in the pair: {rel}")
        elif have is None:
            problems.append(f"missing from the pair: {rel}")
        elif want != have:
            problems.append(f"checksum mismatch: {rel}")
    return problems


def read_expected(p: Path) -> dict:
    """A `--expect` manifest: this module's own `{"files": {...}}` shape, or a bare `{rel: sha}` map."""
    doc = _read_json(p)
    if doc is None:
        raise VaultError(f"cannot read a pair manifest from {p}")
    files = doc.get("files", doc)
    if not isinstance(files, dict) or not files or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in files.items()):
        raise VaultError(f"{p} carries no usable file->sha256 map")
    return files


# --- the pair's self-test ---------------------------------------------------------------------
SELFTEST_MODES = ("off", "require")


def selftest_pair(root: Path, *, modes: tuple[str, ...] | None = None) -> list[str]:
    """Drive a REAL verb through the REAL dispatcher of the pair at `root`. Empty means it works.

    ADR-019 D1: a rule is not enforced until a test drives the product's own entry point. The same
    standard applies to the product asking whether a build of itself works — "the files are all
    present" is what `verify()` already answered, and every one of this phase's six unwired rules
    passed a check of exactly that shape. So this spawns `<root>/plainkeep vault status --json`,
    which walks the whole chain the operator's next command will walk: discovery, the guardrail
    gate, the resolver, a verb process, the `--json` envelope.
    `vault status` is the choice because it is read-only and because it REPORTS the engine it ran
    out of, so the answer can be checked against the tree that was meant to be tested rather than
    assumed.

    HERMETIC BY CONSTRUCTION, and this is not optional politeness: an update runs on a machine with
    real notes on it. The child gets a throwaway marked vault as `PLAINKEEP_HOME` and an empty
    `PLAINKEEP_CONFIG_HOME`, so neither the operator's vault nor their registry is read or written —
    a self-test that touched either would make "we tested the new engine" mean "we ran the new
    engine against your notes".

    BOTH DISPATCHER MODES when the pair carries a core: the floor always, and
    `PLAINKEEP_CORE=require` when `.local/bin/plainkeep-core` is there. A pair whose core is
    truncated or built for another platform passes the floor and dies under `require`, which is the
    failure a per-tree completeness check structurally cannot see."""
    # Imported HERE, not at module scope: this module is imported on the first act of every
    # invocation (see the header), and `subprocess` is the heaviest import in the stdlib that a
    # dispatch would otherwise pay for to reach code only the updater runs.
    import subprocess
    import tempfile
    problems: list[str] = []
    want = list(modes) if modes is not None else ["off"] + (
        ["require"] if (root / CORE_REL).is_file() else [])
    launcher_path = launcher(root)
    if not launcher_path.is_file():
        return [f"the pair has no launcher at {launcher_path}"]
    td = tempfile.mkdtemp(prefix="pk-selftest-")
    try:
        vault = Path(td) / "vault"
        (vault / vaultreg.MARKER_DIR).mkdir(parents=True)
        vaultreg.marker_path(vault).write_text(
            vaultreg.marker_bytes(vaultreg.new_marker_doc()), encoding="utf-8")
        cfg = Path(td) / "config"
        cfg.mkdir()
        for mode in want:
            env = {k: v for k, v in os.environ.items()
                   # The kill hook must not travel into the child: a self-test that killed itself
                   # would report the pair broken for every injection run, i.e. the injection would
                   # be testing the injection.
                   if k not in (ENV_KILL_AT, "PLAINKEEP_VAULT_ID", "PLAINKEEP_VAULT_MECHANISM",
                                "PLAINKEEP_CORE_BIN", "PLAINKEEP_PLUGIN_PACK", "PYTHONPATH")}
            env.update({"PLAINKEEP_HOME": str(vault), "PLAINKEEP_CONFIG_HOME": str(cfg),
                        "PLAINKEEP_CORE": mode, "PLAINKEEP_JSON": "0"})
            try:
                r = subprocess.run([str(launcher_path), "vault", "status", "--json"],
                                   capture_output=True, text=True, timeout=120, env=env)
            except (OSError, subprocess.SubprocessError) as e:
                problems.append(f"self-test (PLAINKEEP_CORE={mode}) could not run the pair: {e}")
                continue
            if r.returncode != 0:
                problems.append(f"self-test (PLAINKEEP_CORE={mode}) exited {r.returncode}: "
                                + (r.stderr.strip().splitlines() or ["<no stderr>"])[-1])
                continue
            try:
                doc = json.loads(r.stdout)
                data = doc["data"]
            except (ValueError, KeyError, TypeError):
                problems.append(f"self-test (PLAINKEEP_CORE={mode}) produced no --json envelope")
                continue
            # `same_path`, not `==`: the child reports the path IT resolved, and two spellings of one
            # directory (case fold, a symlinked ancestor) are one tree — the comparison this repo has
            # already got wrong twice. vaultreg owns it.
            if not vaultreg.same_path(str(data.get("engine_root", "")), str(root)):
                problems.append(f"self-test (PLAINKEEP_CORE={mode}) ran out of "
                                f"{data.get('engine_root')!r}, not the pair under test")
            if not data.get("engine_intact"):
                problems.append(f"self-test (PLAINKEEP_CORE={mode}) reports the engine not intact")
    finally:
        shutil.rmtree(td, ignore_errors=True)
    return problems


# --- serialization ------------------------------------------------------------------------------
class _UpdateLock:
    """`flock` on `<versions>/.update.lock`, non-blocking. Held for the whole update.

    **`flock` and not an `O_EXCL` sentinel**, which is what `vaultreg._Lock` uses, and the difference
    is the one that matters here. A sentinel file outlives the process that made it: a `SIGKILL` mid
    update — the exact event this task's gate injects at eight boundaries — would leave a lock nobody
    holds, and every later update would refuse until a human deleted it. That turns "kill and re-run
    converges" into "kill and re-run wedges". `flock` is released by the kernel when the process
    dies, however it dies, so convergence needs no cleanup path and no stale-lock heuristic (which is
    the other way to get this wrong: a heuristic that force-breaks a lock is how two writers both
    think they won — `vaultreg._Lock` says so, and it is right for a registry write measured in
    milliseconds).

    Non-blocking: the loser refuses immediately and touches nothing, rather than queueing behind an
    update whose outcome may make its own source obsolete."""

    def __init__(self, path: Path):
        self.path, self.fd = path, None

    def __enter__(self):
        import fcntl
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.fd = os.open(str(self.path), os.O_CREAT | os.O_RDWR, 0o644)
        try:
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(self.fd)
            self.fd = None
            raise VaultError(
                f"another plainkeep update is running (lock held on {self.path})",
                code=output.EXIT_CONFIRM,
                hint="updates are serialized — wait for it to finish and re-run")
        os.ftruncate(self.fd, 0)
        os.write(self.fd, f"{os.getpid()}\n".encode())
        return self

    def __exit__(self, *exc):
        if self.fd is not None:
            os.close(self.fd)           # closing releases the flock
        return False


# --- the guard that makes retention structural ---------------------------------------------------
def _active_conflict(dst: Path) -> str | None:
    """Why `dst` must not be written or removed, or None. `dst` is the version directory an update
    is about to replace; the answer is about the RUNNING pair.

    Every comparison goes through `vaultreg`'s identity-aware pair. Nothing here compares strings:
    `current` is a symlink and the active tree is reached THROUGH it, the install root can sit under
    a symlinked or case-folded ancestor, and this repo has paid for a hand-rolled path comparison
    twice already (`vaultreg.path_within`'s docstring carries both post-mortems)."""
    active = active_engine()
    if active is None:
        return None
    a, d = vaultreg.canonical(str(active)), vaultreg.canonical(str(dst))
    if vaultreg.same_path(a, d):
        return f"it is the RUNNING engine ({active})"
    if vaultreg.path_within(a, d):
        return f"the running engine ({active}) is inside it"
    if vaultreg.path_within(d, a):
        return f"it is inside the running engine ({active})"
    return None


# --- update / rollback / prune --------------------------------------------------------------------
def read_state() -> dict:
    doc = _read_json(state_path()) or {}
    return {"activated": doc.get("activated"), "rollback_to": doc.get("rollback_to"),
            "at": doc.get("at")}


def rollback_target() -> str | None:
    """The version `--rollback` would activate: the pair that was active before the last completed
    activation, IF it is still installed and still complete. None when there is nothing to go back to.

    Read from the state file rather than inferred from directory mtimes, because "the other one" is
    not a well-defined answer once three versions are installed, and a rollback that picks the wrong
    one is worse than a rollback that refuses."""
    v = read_state().get("rollback_to")
    if not isinstance(v, str) or not v:
        return None
    try:
        check_version_name(v, "recorded rollback target")
    except VaultError:
        return None
    d = versions_dir() / v
    if not d.is_dir() or verify(d, check_seal=False):
        return None
    return v


def _active_version() -> str | None:
    a = active_engine()
    return a.name if a is not None else None


def update(src: Path, *, version: str | None = None, expect: Path | None = None,
           keep: int = DEFAULT_KEEP) -> dict:
    """Stage a checksum-verified core+engine pair, self-test it, and switch ONE pointer at it.

    Returns a result dict (`{"result": ..., "version": ..., "previous": ..., ...}`) rather than
    printing, so the CLI can render it and a caller can assert on it.

    **A re-run is a no-op**, and that is the same code path rather than a special case: the target
    version is already active and already healthy, so there is nothing to stage, nothing to test and
    no pointer to move. This is what makes "kill and re-run converges" true for every boundary at
    once — after any interruption, running the same command again either finishes the work or finds
    it done, and a THIRD run finds it done too.

    **The previous pair is retained by construction.** The only destructive operations an update can
    reach are `remove_version(target)` (guarded by `_active_conflict`, which refuses the running
    tree) and `prune()` (which excludes both the active version and the rollback target). There is no
    branch that removes the pair you are running, so retention does not depend on getting a cleanup
    policy right.

    `keep` bounds how many versions survive the prune AFTER activation; below 2 it is raised to 2,
    because retaining the previous pair is the contract and a flag must not be able to opt out of it.
    """
    _check_kill_stage()
    src = Path(os.path.abspath(os.path.expanduser(str(src))))
    version = check_version_name(version, "--version") if version is not None else read_version(src)
    keep = max(int(keep), DEFAULT_KEEP)
    root = versions_dir()
    dst = root / version
    expected_from_record = read_expected(expect) if expect is not None else None

    with _UpdateLock(root / UPDATE_LOCK_NAME):
        was_active = _active_version()
        conflict = _active_conflict(dst)
        if conflict is not None:
            problems = verify(dst)
            if not problems:
                # THE NO-OP. Not an error: an operator (or a retry loop, or a second run after a
                # kill) asking for the version that is already running and healthy has asked for a
                # state the system is already in. Reporting failure there is what turns a converging
                # sequence into one that needs a human to read it.
                return {"result": "already-active", "version": version, "previous": was_active,
                        "activated": False, "engine": str(dst),
                        # Reported on the NO-OP too: "which pair am I one command away from" is the
                        # question an operator brings to a re-run after an interruption, and a
                        # convergent run that answers nothing sends them looking for it elsewhere.
                        "rollback_to": rollback_target(),
                        "core": (dst / CORE_REL).is_file()}
            raise VaultError(
                f"refusing to update INTO the running engine {version}, and {conflict} — "
                f"it is also not healthy:\n  " + "\n  ".join(problems),
                code=output.EXIT_DENY,
                hint="roll back to the retained pair first, then update:\n"
                     f"    python3 {Path(__file__).resolve()} --rollback\n"
                     "or repair this one in place (a plain re-install re-seals a complete tree):\n"
                     f"    python3 {Path(__file__).resolve()} --install <source-checkout>")

        # --- checksum, part 1: the SOURCE against a record made elsewhere ------------------------
        # Asked BEFORE anything is copied, deliberately: a source that disagrees with its manifest
        # must not be staged at all, so the refusal costs no copy and leaves nothing to remove. The
        # second half of the checksum gate — the STAGED TREE against the source — necessarily comes
        # after the copy, and catches a different failure (a copy that did not survive).
        source_digests = pair_digests(src)
        if expected_from_record is not None and source_digests != expected_from_record:
            raise VaultError(f"the source pair at {src} does not match {expect}:\n  "
                             + "\n  ".join(digest_problems(src, expected_from_record)[:8]),
                             code=output.EXIT_DENY,
                             hint="the recorded manifest and the source disagree — do not install "
                                  "either until you know which one is wrong")

        # --- provision -------------------------------------------------------------------------
        # `install()` is reused rather than re-implemented: it stages under `.incoming-<v>.<pid>`,
        # runs the whole ownership manifest against the staging tree, renames only a COMPLETE tree
        # into place and seals it. `--force` is safe here in the way it is not on the install path,
        # because `_active_conflict` has already refused the running tree above.
        reused = dst.is_dir() and not verify(dst)
        if not reused:
            install(src, version=version, force=True, activate_it=False)

        # --- checksum, part 2: the STAGED TREE against the source it was copied from -------------
        # Over the tree at its FINAL path, sealed — so what is checked is what will be activated,
        # not a staging copy that a rename could still have truncated.
        problems = digest_problems(dst, source_digests)
        _kill_hook("checksum")
        if problems:
            remove_version(version)
            raise VaultError(f"the staged pair at {dst} does not match its source:\n  "
                             + "\n  ".join(problems[:8]), code=output.EXIT_DENY,
                             hint="the copy is corrupt — nothing was activated and the running "
                                  "engine is untouched")
        has_core = CORE_REL in source_digests
        _write_json_atomic(pair_manifest_path(version), {
            "schema": PAIR_SCHEMA, "version": version, "core": has_core,
            "installed_at": _now(), "source": str(src), "files": source_digests})

        # --- self-test -------------------------------------------------------------------------
        st = selftest_pair(dst)
        _kill_hook("selftest")
        if st:
            # The new pair does not work. Removing it is safe for exactly the reason the whole
            # design rests on: it is not the running one.
            remove_version(version)
            pair_manifest_path(version).unlink(missing_ok=True)
            raise VaultError(f"the new pair {version} failed its self-test:\n  " + "\n  ".join(st),
                             code=output.EXIT_DENY,
                             hint="nothing was activated — the running engine is untouched")

        # --- activation: ONE pointer -----------------------------------------------------------
        # The rollback target is recorded BEFORE the switch, naming the pair that is active RIGHT
        # NOW. A kill between this write and `os.replace` leaves a state file that says "roll back
        # to X" while X is still active — which is a no-op, i.e. correct. Writing it afterwards
        # would leave the opposite: a switch that happened with no record of what to go back to.
        if was_active and was_active != version:
            _write_json_atomic(state_path(), {"schema": STATE_SCHEMA, "activated": version,
                                              "rollback_to": was_active, "at": _now()})
        _kill_hook("activate")
        activate(version)
        _kill_hook("pointer")

        # --- cleanup ---------------------------------------------------------------------------
        pruned = prune(keep=keep)
        return {"result": "activated", "version": version, "previous": was_active,
                "activated": True, "engine": str(dst), "core": has_core,
                "reused_staged_tree": reused, "pruned": pruned,
                "rollback_to": rollback_target()}


def rollback() -> dict:
    """Re-activate the retained previous pair. THE ROLLBACK IS A POINTER SWITCH — the same one
    `update` performs, in the other direction — which is why the previous pair being retained is the
    whole feature and not a nicety."""
    _check_kill_stage()
    with _UpdateLock(versions_dir() / UPDATE_LOCK_NAME):
        target = rollback_target()
        if target is None:
            recorded = read_state().get("rollback_to")
            raise VaultError(
                "there is no retained pair to roll back to"
                + (f" (the state file names {recorded!r}, which is not installed or not complete)"
                   if recorded else ""),
                code=output.EXIT_NOT_FOUND,
                hint="installed versions: " + (", ".join(installed_versions()) or "none"))
        was = _active_version()
        if was == target:
            return {"result": "already-active", "version": target, "previous": was,
                    "activated": False}
        _write_json_atomic(state_path(), {"schema": STATE_SCHEMA, "activated": target,
                                          "rollback_to": was, "at": _now()})
        activate(target)
        return {"result": "rolled-back", "version": target, "previous": was, "activated": True,
                "rollback_to": rollback_target()}


def prune(keep: int = DEFAULT_KEEP) -> list[str]:
    """Remove old versions, NEVER the active one and NEVER the rollback target. Returns what went.

    Oldest first by the `installed_at` in the pair manifest, falling back to directory mtime for a
    version installed before this task existed. Best effort: a version that will not delete is not a
    reason to fail an update that has already succeeded."""
    keep = max(int(keep), DEFAULT_KEEP)
    protected = {v for v in (_active_version(), rollback_target()) if v}
    candidates = [v for v in installed_versions() if v not in protected]
    if len(candidates) + len(protected) <= keep:
        return []

    def when(v: str) -> float:
        # Every read here is best-effort by design. `prune()` runs AFTER a successful activation, so
        # anything it raises turns an update that WORKED into one that reports failure — and the two
        # readable sources of a raise are a version directory whose name `check_version_name`
        # refuses (something else put it in `engine/`) and a manifest that will not parse.
        try:
            doc = _read_json(pair_manifest_path(v)) or {}
        except VaultError:
            doc = {}
        at = doc.get("installed_at")
        if isinstance(at, str):
            try:
                from datetime import datetime
                return datetime.fromisoformat(at).timestamp()
            except ValueError:
                pass
        try:
            return (versions_dir() / v).stat().st_mtime
        except OSError:
            return 0.0

    candidates.sort(key=when)
    drop = candidates[:max(0, len(candidates) + len(protected) - keep)]
    gone = []
    for v in drop:
        _kill_hook("cleanup")
        if _active_conflict(versions_dir() / v) is not None:   # belt and braces; see _active_conflict
            continue
        try:
            remove_version(v)
            pair_manifest_path(v).unlink(missing_ok=True)
            gone.append(v)
        except (OSError, VaultError):
            continue
    return gone


def pairs_report() -> dict:
    """What is installed, what is active, what a rollback would do — the diagnostic an operator
    reaches for after an interrupted update.

    `current` is the AUTHORITY on what is active; the state file only records intent. They can
    disagree exactly once — a kill between the state write and the pointer switch — and this reports
    the disagreement rather than picking one, because an operator who cannot see it will not believe
    the rollback target."""
    state = read_state()
    active = _active_version()
    rows = []
    for v in installed_versions():
        d = versions_dir() / v
        # Guarded for `prune`'s reason and one more: `plainkeep doctor` calls this on every run, and
        # a diagnostic that dies on the thing it is diagnosing reports the crash instead of the
        # damage (ADR-019 D3).
        try:
            manifest = _read_json(pair_manifest_path(v)) or {}
        except VaultError:
            manifest = {}
        rows.append({"version": v, "active": v == active,
                     "rollback_target": v == rollback_target(),
                     "complete": not verify(d, check_seal=False),
                     "sealed": not seal_problems(d),
                     "core": bool(manifest.get("core", (d / CORE_REL).is_file())),
                     "checksums": bool(manifest.get("files")),
                     "installed_at": manifest.get("installed_at")})
    return {"active": active, "rollback_to": rollback_target(),
            "state_says_activated": state.get("activated"),
            "state_agrees_with_current": state.get("activated") in (None, active),
            "versions": rows}


def _now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --- CLI -------------------------------------------------------------------------------------------
_USAGE = ("usage: enginetree.py --print [root|install-root|current|versions|pairs]\n"
          "       enginetree.py --install <source-checkout> [--version V] [--force] "
          "[--no-activate] [--writable]\n"
          "       enginetree.py --activate <version>\n"
          "       enginetree.py --verify [<engine-root>] [--digests]\n"
          "       enginetree.py --update <source-checkout> [--version V] [--expect <manifest.json>] "
          "[--keep N] [--json]\n"
          "       enginetree.py --rollback [--json]\n"
          "       enginetree.py --digests <engine-root|source-checkout>   (the pair manifest, on "
          "stdout)")


def _flag(opts: list[str], name: str) -> str | None:
    if name not in opts:
        return None
    i = opts.index(name)
    if i + 1 >= len(opts):
        raise VaultError(f"{name} needs a value")
    return opts[i + 1]


def _emit(doc: dict, opts: list[str], human) -> int:
    if "--json" in opts:
        print(json.dumps(doc, indent=2, sort_keys=True))
    else:
        human(doc)
    return output.EXIT_OK


def _render_update(d: dict) -> None:
    v, prev = d.get("version"), d.get("previous")
    if d.get("result") == "already-active":
        print(f"engine {v} is already active — nothing to do")
    elif d.get("result") == "rolled-back":
        print(f"rolled back to engine {v}" + (f" (was {prev})" if prev else ""))
    else:
        print(f"activated engine {v}" + (f" (was {prev})" if prev else "")
              + ("  [core+engine pair]" if d.get("core") else "  [engine only, no core binary]"))
    if d.get("pruned"):
        print(f"  pruned: {', '.join(d['pruned'])}")
    # ALWAYS printed, including on the no-op: "which pair am I one command away from" is the whole
    # reason the previous one is retained, and an operator who has to run a second command to learn
    # it will not learn it at 2am.
    rb = d.get("rollback_to") or rollback_target()
    print(f"  roll back with: python3 {Path(__file__).resolve()} --rollback"
          + (f"   -> {rb}" if rb else "   (nothing retained yet)"))


def _render_pairs(rep: dict) -> None:
    print(f"active        {rep.get('active') or 'none'}")
    print(f"roll back to  {rep.get('rollback_to') or 'nothing retained'}")
    if not rep.get("state_agrees_with_current"):
        print(f"  NOTE: the state file records an activation of {rep.get('state_says_activated')!r} "
              f"that `current` does not show — an update was interrupted between the two. "
              f"`current` is what runs.")
    for r in rep.get("versions", []):
        flags = "".join([" *" if r["active"] else "  ", "<-" if r["rollback_target"] else "  "])
        print(f" {flags} {r['version']:<16} "
              f"{'complete' if r['complete'] else 'INCOMPLETE':<11} "
              f"{'sealed' if r['sealed'] else 'UNSEALED':<9} "
              f"{'core+engine' if r['core'] else 'engine-only':<12} "
              f"{'checksummed' if r['checksums'] else 'no manifest'}")
    print("  * = active · <- = rollback target")


def main(argv: list[str]) -> int:
    """The installer surface. Deliberately a MODULE CLI and not a `plainkeep engine` verb: a new verb
    would change the verb surface, plainkeep.json, the completion catalogs and the help output, none
    of which this task is scoped to move. `script/setup` and the test harness call this."""
    if not argv:
        print(_USAGE, file=sys.stderr)
        return output.EXIT_USAGE
    cmd, rest = argv[0], argv[1:]
    try:
        if cmd == "--print":
            what = rest[0] if rest else "root"
            if what == "root":
                print(ENGINE_ROOT)
            elif what == "install-root":
                print(install_root())
            elif what == "current":
                a = active_engine()
                if a is None:
                    print(f"plainkeep: no engine activated ({current_link()} is not a symlink)",
                          file=sys.stderr)
                    return output.EXIT_NOT_FOUND
                print(a)
            elif what == "versions":
                for v in installed_versions():
                    print(v)
            elif what == "pairs":
                rep = pairs_report()
                if "--json" in rest:
                    print(json.dumps(rep, indent=2, sort_keys=True))
                else:
                    _render_pairs(rep)
            else:
                print(_USAGE, file=sys.stderr)
                return output.EXIT_USAGE
            return output.EXIT_OK
        if cmd == "--install":
            if not rest:
                print("plainkeep: --install needs a source checkout", file=sys.stderr)
                return output.EXIT_USAGE
            src = Path(rest[0])
            opts = rest[1:]
            ver = None
            if "--version" in opts:
                i = opts.index("--version")
                if i + 1 >= len(opts):
                    print("plainkeep: --version needs a value", file=sys.stderr)
                    return output.EXIT_USAGE
                ver = opts[i + 1]
            d = install(src, version=ver, force="--force" in opts,
                        activate_it="--no-activate" not in opts, writable="--writable" in opts)
            print(d)
            return output.EXIT_OK
        if cmd == "--activate":
            if not rest:
                print("plainkeep: --activate needs a version", file=sys.stderr)
                return output.EXIT_USAGE
            print(activate(rest[0]))
            return output.EXIT_OK
        if cmd == "--verify":
            # `--digests` is OPT-IN rather than the default because it re-reads every owned file
            # (~150 of them, ~5 MB) where the mode walk stats them: the right cost for an operator
            # asking "is this the code that was installed", the wrong one for `doctor`'s routine
            # pass. Every provisioning entry point asks for the WHOLE tree (see
            # `provision.require_delivered_intact`) — it is about to download and run a binary the
            # tree names, and a hand-written subset of that tree is what left the uv pin ungated.
            args = [a for a in rest if a != "--digests"]
            root = Path(args[0]) if args else ENGINE_ROOT
            problems = verify(root, check_digests="--digests" in rest)
            for p in problems:
                print(p, file=sys.stderr)
            print(f"{root}: {'OK' if not problems else f'{len(problems)} problem(s)'}")
            return output.EXIT_OK if not problems else output.EXIT_DENY
        if cmd == "--update":
            if not rest:
                print("plainkeep: --update needs a source checkout", file=sys.stderr)
                return output.EXIT_USAGE
            opts = rest[1:]
            keep = _flag(opts, "--keep")
            exp = _flag(opts, "--expect")
            res = update(Path(rest[0]), version=_flag(opts, "--version"),
                         expect=Path(exp) if exp else None,
                         keep=int(keep) if keep else DEFAULT_KEEP)
            return _emit(res, opts, _render_update)
        if cmd == "--rollback":
            return _emit(rollback(), rest, _render_update)
        if cmd == "--digests":
            if not rest:
                print("plainkeep: --digests needs a tree", file=sys.stderr)
                return output.EXIT_USAGE
            root = Path(os.path.abspath(os.path.expanduser(rest[0])))
            files = pair_digests(root)
            print(json.dumps({"schema": PAIR_SCHEMA, "version": None, "files": files},
                             indent=2, sort_keys=True))
            return output.EXIT_OK
    except VaultError as e:
        sys.stderr.write("plainkeep: " + e.message + (f"\n  {e.hint}" if e.hint else "") + "\n")
        return e.code
    except OSError as e:
        # ENOSPC, EACCES, a source that vanished mid-copy, a destination on a full volume. The
        # protocol reserves 1 for EXIT_UNEXPECTED and these genuinely are — but a Python traceback is
        # not the refusal shape any other surface in this codebase produces, and this one is reached
        # by `script/setup`, where the operator's next move depends on reading the error.
        sys.stderr.write(f"plainkeep: {cmd} failed: {e}\n")
        return output.EXIT_UNEXPECTED
    print(_USAGE, file=sys.stderr)
    return output.EXIT_USAGE


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
