#!/usr/bin/env python3
"""
migrate.py — move an EXISTING vault off its vault-local engine copy and onto the installed one.

A Phase 1 vault is TWO things in one directory: the notes, and a copy of the engine that acts on
them (`bin/`, `script/`, `VERSION`, `plainkeep`, `.plainkeep-engine-ref`, a `.venv`). Phase 2 moved
the engine out into its own versioned install root. Migration is what moves an EXISTING vault onto
that world, in this order and no other:

    provision the installed pair  ->  PROVE it operates THIS vault from an env-scrubbed shell
                                  ->  regenerate the schedules  ->  repoint the stale launcher
                                  ->  only then remove the vault's legacy engine copy

The ordering is the safety property. "Deleted but not working" is impossible not because the code is
careful but because nothing is deleted until the installed pair has already answered a real verb
against this vault, in a subprocess that inherited no `PLAINKEEP_HOME`.

THE DESIGN CONSTRAINT (Phase 2 panel, Codex). Protected-content modification is impossible WITHIN
THIS MODULE'S ACTION SPACE, and that is a stronger statement than "it does not happen":

  1. **The vault is never opened for writing.** No call in this file opens a path inside a vault for
     writing — not `open`, not `write_text`, not `mkdir`, not `shutil`. Exactly TWO functions remove
     anything from a vault and both are gated on `_VERIFIED`, the set `verify_candidate()` produces
     from a git tree diff: `_remove_engine_path` deletes a FILE and refuses any path not in that set,
     and `_remove_empty_dir` removes a DIRECTORY and refuses one that is not an ancestor of a
     verified deletion (after which `os.rmdir` itself refuses any directory that is not empty).
     `test/run_migrate.py`'s AST ratchet asserts this per function against the parse tree rather than
     the source text: it taints every local name derived from a function's `vault` argument and
     flags a write primitive applied to a tainted expression anywhere outside those two.

     The ratchet's honest limit is that `git` runs in a subprocess and no Python-level analysis can
     see what it writes. So there is a SECOND ratchet over the git argv this module constructs: no
     function may invoke a git subcommand that touches the WORKING TREE (`checkout`, `read-tree -u`,
     `reset`, `restore`, `clean`, `apply`, …). `rollback` is the one exception, it is declared there,
     and it is the only place a working-tree checkout is what the operator asked for.
  2. **The change is constructed as a git tree.** The removal is built in a TEMPORARY git index
     (`GIT_INDEX_FILE` under scratch), never the vault's own, and `git write-tree` yields a candidate
     tree object. Nothing in the working tree has moved at this point.
  3. **The tree is verified BEFORE checkout.** `git diff-tree HEAD <candidate>` must consist
     ENTIRELY of deletions, every one of whose paths is in the exact allowlist below. One `A`, `M`,
     `T` or `R` entry, or one path outside the allowlist, and the migration aborts having written
     nothing.
  4. **There is no force and no waiver.** No flag skips the divergence refusal, the allowlist subset
     check, the prove-before-remove step or the hash comparison. If someone needs an escape hatch
     they restore from git — which is why the vault must be a clean git repo to begin with.

WHAT IS NOT REMOVED. `.venv/` is RETAINED — the whole of the rollback soak depends on it and Phase 3
removes it with the rest of the scaffolding. A plugin may depend on a package somebody hand-installed
into it; it is the cheapest thing here to rebuild and the most expensive to be missing. `docs/`,
`AGENTS.md`, `CLAUDE.md`, `.gitignore`, `requirements*.txt` and the agent adapters are refreshed by
`script/update` but are not engine CODE and an installed engine does not provide them, so they stay.

WHY A MODULE CLI AND NOT A VERB. Same reason `enginetree.py` gives (see its `main()`), plus one that
is specific to this file: the vault being migrated IS an engine tree, so `<vault>/plainkeep <verb>`
is refused with exit 5 by the disjointness rule, and the INSTALLED launcher may not exist yet at the
moment migration starts — that is what step 1 is for. A verb would be unreachable at both ends of the
sequence it has to drive. The bootstrap shape is the same one `vaultroot.bootstrap_hint` hands an
operator and the same one `script/setup` calls.

Offline, stdlib only. Nothing here reaches the network.
"""
from __future__ import annotations
import hashlib
import json
import os
import plistlib
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lib import enginetree, output, vaultreg  # noqa: E402

VaultError = vaultreg.VaultError

SCHEMA = "plainkeep.migration/1"
TRAILER = "Plainkeep-Migration"
NULL_SHA = "0" * 40
SHA_RE = re.compile(r"^[0-9a-f]{40}$", re.IGNORECASE)


# --- the allowlist ---------------------------------------------------------------------------------
# THE EXACT, REVIEWED SET a migration may delete from a vault, and the only set. It is DERIVED from
# `enginetree`'s ownership manifest rather than restated: that tuple pair is what `install()` copies
# into the installed tree, so "what the installed engine now provides" and "what may leave the vault"
# are one list with one definition. A path added to the engine's ownership manifest becomes
# removable here automatically; a path added HERE has to be justified in the two tuples below.
#
# The legacy extras are the update machinery, and they are legacy in the precise sense that they
# exist only to refresh the vault-local copy that migration removes. `script/` carries `setup`,
# `update` and `engine.txt`; `.plainkeep-engine-ref` is the commit `script/update` 3-way-merges
# against. After migration the engine is updated with `enginetree.py --update` from a source
# checkout, which is a different mechanism in a different place.
#
# PHASE 3 OWNS REPOSITORY DELETION of `script/`, `engine.txt` and `.plainkeep-engine-ref` — that is,
# removing them from the plainkeep SOURCE. This module removes a MIGRATED VAULT's copy, after a
# proven migration, which ADR-014 keeps on this side of the boundary.
LEGACY_ONLY_TREES = ("script",)
LEGACY_ONLY_FILES = (".plainkeep-engine-ref",)
REMOVAL_TREES = (*enginetree.OWNED_TREES, *LEGACY_ONLY_TREES)
REMOVAL_FILES = (*enginetree.OWNED_FILES, *LEGACY_ONLY_FILES)

# WHAT THE DIVERGENCE CHECK COMPARES, and it is NOT the removal allowlist. `.plainkeep-engine-ref`
# records an UPSTREAM commit and is written into the vault by `script/update` itself (`script/update`
# line 108); `script/engine.txt` — the list of paths that come FROM upstream — does not list it,
# because it cannot: a file naming a commit can never be inside the tree of the commit it names.
#
# So `git diff <ref> HEAD -- .plainkeep-engine-ref` reports `A` in EVERY real vault, against every
# possible ref, and a divergence check that includes it refuses every migration it is ever asked to
# do. The file is still in the REMOVAL allowlist — migration deletes it — but it is a record, not
# engine code, and there is nothing to compare it against.
DIVERGENCE_TREES = REMOVAL_TREES
DIVERGENCE_FILES = tuple(f for f in REMOVAL_FILES if f not in LEGACY_ONLY_FILES)

# Paths excluded from the PROTECTED manifest. Every one of them is machine-generated output that the
# product rewrites as a matter of course, and each is gitignored for that same reason (see the
# vault's `.gitignore`), so none of them is user content and none is in the git tree the candidate is
# built from. Naming them here rather than "everything gitignored" is deliberate: `.venv/` is
# gitignored too and is PROTECTED — it is retained through the soak and its hashes are compared.
#
# `.git/` is excluded because migration writes a commit into it BY CONSTRUCTION. That is the one
# directory this module changes that is neither the allowlist nor regenerable output, and stating it
# here is the honest form of the claim: the protected-hash comparison is about the vault's content,
# and the repository's own object database is the mechanism the comparison relies on.
UNPROTECTED_PREFIXES = (".git", ".logs", ".index", "jobs/launchd")


def allowlist_trees() -> tuple[str, ...]:
    return REMOVAL_TREES


def allowlist_files() -> tuple[str, ...]:
    return REMOVAL_FILES


def is_allowlisted(rel: str) -> bool:
    """Is this repo-relative path one the migration may delete? The ONLY membership test."""
    rel = rel.strip("/")
    if rel in REMOVAL_FILES:
        return True
    return any(rel == t or rel.startswith(t + "/") for t in REMOVAL_TREES)


def _is_unprotected(rel: str) -> bool:
    return any(rel == p or rel.startswith(p + "/") for p in UNPROTECTED_PREFIXES)


# --- the failure-injection hook --------------------------------------------------------------------
# Same shape, same reasoning and the same signal as `enginetree.ENV_KILL_AT` (read that comment): the
# contract "an interruption at any boundary leaves either the old pair or the new pair runnable, and
# leaves the vault's content untouched" cannot be read off the code and cannot be raced from outside.
# The whole body is `os.kill(getpid(), SIGKILL)`; an unknown stage REFUSES rather than being ignored;
# there is no value of the variable that makes a migration do less checking and still succeed.
ENV_KILL_AT = "PLAINKEEP_MIGRATE_KILL_AT"
KILL_STAGES = (
    "preflight-done",       # everything read, nothing provisioned
    "provisioned",          # the engine pair is installed, `current` has NOT moved
    "activated",            # `current` points at the new pair; the vault still has its own copy
    "proved",               # the scrubbed-shell proof passed; nothing has been removed
    "schedules",            # plists regenerated and exercised
    "symlink",              # the provisional receipt is on disk and the stale launcher is repointed
    "tree-written",         # HEAD carries the migration commit; the working tree still has the files
    "worktree-pruned",      # the files are gone; the receipt is still PROVISIONAL (no after_commit)
    "receipt",              # the complete receipt exists; the post-verification has not run
)


def _kill_hook(stage: str) -> None:
    if os.environ.get(ENV_KILL_AT) == stage:
        os.kill(os.getpid(), signal.SIGKILL)


def _check_kill_stage() -> None:
    v = os.environ.get(ENV_KILL_AT)
    if v and v not in KILL_STAGES:
        raise VaultError(f"{ENV_KILL_AT}={v!r} names no boundary in this migration",
                         hint="one of: " + ", ".join(KILL_STAGES))


# --- git ------------------------------------------------------------------------------------------
def _git(root, *args, check: bool = True, env: dict | None = None,
         stdin: str | None = None) -> subprocess.CompletedProcess:
    r = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True,
                       env=env, input=stdin)
    if check and r.returncode != 0:
        raise VaultError(f"git {args[0]} failed in {root}: "
                         + (r.stderr.strip() or r.stdout.strip() or f"exit {r.returncode}"))
    return r


def _git_out(root, *args, env: dict | None = None) -> str:
    return _git(root, *args, env=env).stdout.strip()


def _toplevel(vault: Path) -> str:
    r = _git(vault, "rev-parse", "--show-toplevel", check=False)
    if r.returncode != 0:
        raise VaultError(f"{vault} is not a git repository",
                         hint="migration constructs its change as a git tree and verifies it before "
                              "checkout; a vault with no history has nothing to restore from")
    return r.stdout.strip()


def _head_branch(vault: Path) -> str:
    r = _git(vault, "symbolic-ref", "--quiet", "HEAD", check=False)
    if r.returncode != 0 or not r.stdout.strip():
        raise VaultError(f"{vault} has a DETACHED HEAD",
                         hint="check out the branch this vault lives on first:\n"
                              "    git -C %s checkout <branch>" % vault)
    return r.stdout.strip()


def _name_status_z(raw: str) -> list[tuple[str, str]]:
    """Parse `--name-status -z` output into `(status, path)` pairs.

    One parser for every caller — the forward gate, the rollback gate and the divergence check all
    ask git the same question, and three hand-rolled splits of a NUL-separated stream is three places
    for an off-by-one to turn a refusal into a pass. `-M`/`-C` are never passed, so a status is
    always one letter and every entry is exactly two fields."""
    fields = [f for f in raw.split("\0") if f]
    return [(fields[i], fields[i + 1]) for i in range(0, len(fields) - 1, 2)]


def _tracked_under_allowlist(vault: Path, ref: str = "HEAD") -> list[str]:
    """Every path in `ref`'s tree that the allowlist covers. The subject of the whole migration."""
    out = _git_out(vault, "ls-tree", "-r", "--name-only", "-z", ref)
    return sorted(p for p in out.split("\0") if p and is_allowlisted(p))


# --- the candidate tree ----------------------------------------------------------------------------
# Written by `verify_candidate()` and consumed by `_remove_engine_path()`. It is the seam that makes
# constraint 1 in the module docstring checkable rather than asserted: a removal of a path this
# process has not verified against a git tree diff raises, and there is no argument, flag or
# environment variable that populates it any other way.
_VERIFIED: set[str] | None = None


def build_candidate(vault: Path, scratch: Path) -> tuple[str, list[str]]:
    """Build the migrated tree in a TEMPORARY index and return `(tree_sha, removed_paths)`.

    The vault's own `.git/index` is not read and not written: `GIT_INDEX_FILE` points the whole
    sequence at a file under `scratch`. Nothing in the working tree moves."""
    tracked = _tracked_under_allowlist(vault)
    idx = scratch / "candidate.index"
    env = {**os.environ, "GIT_INDEX_FILE": str(idx)}
    _git(vault, "read-tree", "HEAD", env=env)
    if tracked:
        # The documented deletion form of `--index-info`: mode 0 with the null sha removes the path.
        # Tab-separated and unquoted, so a path containing a newline could not be expressed — which
        # is why `_refuse_unrepresentable` rejects one in preflight rather than silently skipping it.
        payload = "".join(f"0 {NULL_SHA}\t{p}\n" for p in tracked)
        _git(vault, "update-index", "--index-info", env=env, stdin=payload)
    return _git_out(vault, "write-tree", env=env), tracked


def verify_candidate(vault: Path, base: str, tree: str, expected: list[str]) -> list[str]:
    """THE GATE. Compare `base` with `tree` and refuse anything that is not an allowlisted deletion.
    Returns the verified deletion list and arms `_VERIFIED`.

    Both ends are parameters because the gate runs twice and in two situations: forward, with
    `base=HEAD` and a candidate tree that does not exist as a commit yet; and on RESUME, with the
    parent of a migration commit a killed run already made and `tree=HEAD`. A resumed run that
    trusted the commit trailer instead of re-deriving this answer would be finishing a mutation it
    never checked.

    Three separate refusals, because they fail for three different reasons and an operator reading
    one of them needs to know which:
      * a non-deletion entry — the candidate would ADD, MODIFY or TYPE-CHANGE something;
      * a deletion outside the allowlist — the candidate would remove content;
      * a mismatch with the expected set — the candidate removed a different set from the one
        preflight enumerated, which means the tree moved underneath the run.
    """
    global _VERIFIED
    entries = _name_status_z(
        _git_out(vault, "diff-tree", "-r", "--no-commit-id", "--name-status", "-z", base, tree))
    bad_kind = [(s, p) for s, p in entries if s != "D"]
    if bad_kind:
        raise VaultError(
            "refusing the candidate migration: it is not a pure deletion —\n  "
            + "\n  ".join(f"{s} {p}" for s, p in bad_kind[:20]),
            code=output.EXIT_DENY,
            hint="a migration may only REMOVE engine paths; there is no flag that permits this")
    outside = [p for _, p in entries if not is_allowlisted(p)]
    if outside:
        raise VaultError(
            "refusing the candidate migration: it would delete paths OUTSIDE the allowlist —\n  "
            + "\n  ".join(outside[:20]),
            code=output.EXIT_DENY,
            hint="the allowlist is derived from enginetree.OWNED_TREES/OWNED_FILES plus the legacy "
                 "update machinery (bin/lib/migrate.py); there is no --force")
    got = sorted(p for _, p in entries)
    if got != sorted(expected):
        raise VaultError(
            f"refusing the candidate migration: it removes {len(got)} path(s) where preflight "
            f"enumerated {len(expected)} — the tree moved underneath this run",
            code=output.EXIT_DENY, hint="re-run the migration")
    _VERIFIED = set(got)
    return got


def _remove_engine_path(vault: Path, rel: str) -> None:
    """THE ONLY FUNCTION IN THIS MODULE THAT REMOVES ANYTHING FROM A VAULT.

    It refuses a path that `verify_candidate()` did not put in `_VERIFIED`. That is not a second
    opinion about the same question — the first check is about a git TREE and this one is about the
    argument actually being passed to `os.remove`, and the gap between those two is precisely where
    a path outside the allowlist would have to enter."""
    if _VERIFIED is None or rel not in _VERIFIED:
        raise VaultError(f"refusing to remove {rel!r}: it is not in the verified deletion set",
                         code=output.EXIT_DENY)
    p = vault / rel
    if p.is_symlink() or p.is_file():
        os.remove(p)


def _remove_empty_dir(vault: Path, rel: str) -> bool:
    """THE ONLY OTHER FUNCTION THAT REMOVES ANYTHING FROM A VAULT, and it removes only directories.

    Gated on `_VERIFIED` like `_remove_engine_path`, but on a different question: a directory is not
    itself a verified deletion, so what is checked is that it is an ANCESTOR of one. A migration has
    no business removing a directory that held nothing it deleted.

    `os.rmdir` then refuses a non-empty directory, and that is the second half of the property: even
    an ancestor keeps its directory if anything of the operator's is still inside it. Both halves
    matter — the ancestor check is what makes the removal in-scope, and `rmdir`'s own refusal is what
    makes it non-destructive.

    This function exists because the module docstring used to claim there was exactly ONE removing
    function while `_prune_empty_dirs` called `os.rmdir` on a vault path with no gate at all. The
    AST ratchet in `test/run_migrate.py` reads that claim off this module and would have to be
    written around the exception; a rule with an exception carved for the code that breaks it is the
    shape ADR-019 catalogues. So the code changed, not the rule."""
    if _VERIFIED is None:
        raise VaultError(f"refusing to remove the directory {rel!r}: nothing has been verified",
                         code=output.EXIT_DENY)
    pre = rel.rstrip("/") + "/"
    if not any(v.startswith(pre) for v in _VERIFIED):
        raise VaultError(
            f"refusing to remove the directory {rel!r}: it is not an ancestor of any verified "
            f"deletion", code=output.EXIT_DENY)
    try:
        os.rmdir(vault / rel)
        return True
    except OSError:
        return False


def _prune_empty_dirs(vault: Path, removed: list[str]) -> list[str]:
    """Remove directories left empty by the deletions, bottom-up, stopping at the vault root.

    Only directories, only empty ones, only ancestors of a verified deletion. `os.rmdir` refuses a
    non-empty directory, so a user file anywhere under one of these trees keeps its directory.

    The candidate set is the full ANCESTOR CLOSURE of the deletions, tried deepest-first, each
    directory exactly once. An earlier version walked up from each deleted path and `break`ed on the
    first non-empty parent while marking it seen, which meant `bin/` was visited from `bin/lib/`
    (still non-empty at that moment, because `bin/verb/` had not been pruned yet), marked seen, and
    never revisited once it WAS empty. That left an empty `bin/` in a fully migrated vault — and
    `_worktree_residue` reports any surviving allowlisted DIRECTORY, so `state()` answered `resume`
    instead of `migrated` and the second run (acceptance item 12) was not a no-op. Deepest-first over
    the closure removes the ordering hazard rather than compensating for it."""
    gone: list[str] = []
    cands: set[str] = set()
    for r in removed:
        p = Path(r).parent
        while str(p) != ".":
            cands.add(str(p))
            p = p.parent
    for d in sorted(cands, key=lambda d: -d.count("/")):
        if _remove_empty_dir(vault, d):
            gone.append(d)
    return sorted(gone)


# --- the protected manifest -------------------------------------------------------------------------
def protected_manifest(vault: Path) -> dict:
    """sha256 of every protected path in the vault, plus its type and mode.

    "Protected" is everything that is not the removal allowlist and not machine-generated output —
    notes, tasks, journal, inbox, wiki, jobs/registry.json, plugins, user templates, `plainkeep.json`,
    the marker, the retained `.venv`, and every other user-owned file, tracked or not. It walks the
    filesystem rather than `git ls-files` on purpose: `.venv/` and `plainkeep.json` are the two things
    an operator would most notice going missing and one of them is gitignored.

    Symlinks are recorded by TARGET and never followed — following them would hash the same bytes
    twice and would leave the migration's strongest evidence dependent on what a link points at."""
    files = {rel: _digest_entry(vault / rel) for rel in _protected_paths(vault)}
    return {"schema": SCHEMA, "files": files, "count": len(files)}


def _protected_paths(vault: Path) -> list[str]:
    """Every protected path in the vault. ONE walk, and both callers read it.

    EVERY SYMLINK IS AN ENTRY, whatever it points at. `os.walk(followlinks=False)` decides which list
    a symlink lands in by `is_dir()`, which FOLLOWS it — so a symlink to a directory is enumerated in
    `dirnames` while its target resolves and in `filenames` once it does not. A manifest built from
    `filenames` alone therefore let the same path enter and leave it based on nothing but whether its
    target was currently reachable, which is wrong twice over:

      * an intact `.claude/skills -> ../skills` is protected content that was NEVER hashed, in either
        direction, so "no protected content changed" had a hole in it the shape of every committed
        agent adapter on a real vault;
      * breaking one reported it as `added` and repairing one as `removed`, which is how a correct
        rollback came to exit 5 AFTER restoring all 109 paths, skipping the launcher restore and the
        receipt deletion it had not got to yet.

    A symlink's content is its target string. That is what `_digest_entry` hashes, it is what git
    stores in a mode-120000 blob, and it does not change when the thing at the other end does."""
    out: list[str] = []
    for dirpath, dirnames, filenames in os.walk(vault, followlinks=False):
        rel_dir = os.path.relpath(dirpath, vault)
        rel_dir = "" if rel_dir == "." else rel_dir.replace(os.sep, "/")
        dirnames[:] = sorted(d for d in dirnames
                             if not _skip(f"{rel_dir}/{d}" if rel_dir else d))
        # An entry, not a directory to descend. Pruned by SLICE ASSIGNMENT rather than `.remove()`:
        # `dirnames` is derived from `os.walk(vault, …)` and therefore vault-tainted, and `remove` is
        # a write primitive — the AST ratchet would read `dirnames.remove(d)` as a removal inside a
        # vault and be right to, on the evidence available to it.
        links = [d for d in dirnames if os.path.islink(os.path.join(dirpath, d))]
        dirnames[:] = [d for d in dirnames if d not in links]
        out.extend(f"{rel_dir}/{d}" if rel_dir else d for d in links)
        for fn in sorted(filenames):
            rel = f"{rel_dir}/{fn}" if rel_dir else fn
            if not _skip(rel):
                out.append(rel)
    return out


def _skip(rel: str) -> bool:
    return _is_unprotected(rel) or is_allowlisted(rel)


def _digest_entry(p: Path) -> str:
    if p.is_symlink():
        return "symlink:" + hashlib.sha256(os.readlink(p).encode("utf-8", "surrogateescape")).hexdigest()
    try:
        h = hashlib.sha256()
        with open(p, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
        return f"{h.hexdigest()}:{oct(p.stat().st_mode & 0o7777)}"
    except OSError as e:
        return f"unreadable:{e.errno}"


def manifest_diff(before: dict, after: dict) -> dict:
    b, a = before["files"], after["files"]
    return {"modified": sorted(k for k in b if k in a and a[k] != b[k]),
            "removed": sorted(k for k in b if k not in a),
            "added": sorted(k for k in a if k not in b)}


# --- provisioning ------------------------------------------------------------------------------------
# The modules a PHASE 2 engine has to carry. A tree without them installs as an engine that can
# neither update itself nor migrate anything, and both are module CLIs the operator is told to run by
# name, so their absence is not something the next command discovers gracefully.
PHASE2_MODULES = ("bin/lib/enginetree.py", "bin/lib/migrate.py")


def _check_engine_source(src: Path, *, defaulted: bool) -> None:
    """Refuse a source tree that is not a Phase 2 engine.

    `engine_source` DEFAULTS TO THE VAULT, which is right — a Phase 1 vault's own copy is the engine
    that has been running it — and is a live hazard exactly once: when that copy predates Phase 2.
    The clone canary hit the benign half of it. `provision()` found the vault's `4.0.0-dev` already
    installed, verified it and REUSED it, so the vault's pre-Phase-2 code was never installed. That
    is the right outcome reached by a version-string collision: on a machine where `4.0.0-dev` is not
    already present, the same default would have installed the engine FROM the vault's own copy, and
    that copy carries neither `enginetree.py` nor `migrate.py`.

    Checked on the CONTENT of the resolved source, never on whether it happens to be the vault: a
    vault whose engine copy IS Phase 2 is a perfectly good source and is what every fixture uses. The
    refusal names `--engine-source` because pointing at a real checkout is the fix, and it is also
    what the canary report tells the live run to do regardless."""
    missing = [m for m in PHASE2_MODULES if not (src / m).is_file()]
    if not missing:
        return
    raise VaultError(
        f"the engine source {src} is not a Phase 2 engine — it does not carry "
        + ", ".join(missing) + ("\n  (no --engine-source was given, so the source DEFAULTS to the "
                                "vault, whose engine copy predates Phase 2)" if defaulted else ""),
        code=output.EXIT_DENY,
        hint="installing it would produce an engine that can neither update itself nor migrate a "
             "vault. Point the migration at a real checkout:\n"
             "    --engine-source <path to a plainkeep checkout>")


def provision(source: Path) -> dict:
    """Install and activate the engine pair from `source`, retaining whatever pair is active now.

    NEVER `--force`. `enginetree.install()`'s own docstring measures the window `--force` opens
    (a kill between `remove_version()` and the rename leaves no tree under that version name), and
    Task 5's report names migration as the caller that would be exposed to it. So an already-present
    version is VERIFIED and reused, an unsealed one is repaired by `install()`'s own repair branch,
    and a broken one REFUSES with the two commands that fix it. A migration that silently reinstalls
    over the running engine would be trading the property this whole module is built around."""
    source = Path(os.path.abspath(os.path.expanduser(str(source))))
    version = enginetree.read_version(source)
    dst = enginetree.versions_dir() / version
    previous = None
    active = enginetree.active_engine()
    if active is not None:
        previous = active.name
    reused = False
    if dst.is_dir():
        problems = enginetree.verify(dst)
        if not problems:
            reused = True
        elif problems == [enginetree._UNSEALED]:
            enginetree.install(source, version=version, activate_it=False)   # the repair branch
        else:
            raise VaultError(
                f"engine {version} is already installed at {dst} and is not intact:\n  "
                + "\n  ".join(problems),
                code=output.EXIT_DENY,
                hint="repair it before migrating — migration never passes --force:\n"
                     f"    python3 {enginetree.__file__} --install <source-checkout> --force\n"
                     f"    python3 {enginetree.__file__} --update <source-checkout>")
    else:
        enginetree.install(source, version=version, activate_it=False)
    _kill_hook("provisioned")
    enginetree.activate(version)
    _kill_hook("activated")
    return {"version": version, "previous": previous, "reused": reused, "path": str(dst)}


# --- prove-before-remove ------------------------------------------------------------------------------
# The variables a scrubbed shell must NOT carry, because carrying them would answer the question the
# proof is asking. `PLAINKEEP_HOME` is the whole point: with it set, discovery step 2 wins and the
# proof shows only that a path we just typed is a vault. Scrubbed, the installed binary has to reach
# THIS vault by the marker walk-up from `$PWD` — which is what an operator's shell, and a `cd` in a
# script, actually do.
SCRUB = ("PLAINKEEP_HOME", "PLAINKEEP_VAULT_ID", "PLAINKEEP_VAULT_MECHANISM", "PLAINKEEP_ENGINE",
         "PLAINKEEP_PATH", "PLAINKEEP_PLUGIN_PACK", "PLAINKEEP_JSON", "PYTHONPATH",
         ENV_KILL_AT, enginetree.ENV_KILL_AT)

# DELIBERATELY KEPT, and this is the honest limit of the word "scrubbed": `PLAINKEEP_CONFIG_HOME` and
# `PLAINKEEP_ENGINE_HOME` name WHERE the registry and the install root live. They are the machine's
# identity, not a selection — and unsetting them in a test would point the proof at the developer's
# real `~/.config/plainkeep` and real `~/.local/share/plainkeep`, which is the one thing this whole
# task may not do. On a real machine neither is set and the defaults apply, so the scrubbed shell is
# literally an operator's shell.
KEEP = ("PLAINKEEP_CONFIG_HOME", enginetree.ENV_INSTALL_ROOT)


def scrubbed_env(mode: str) -> dict:
    env = {k: v for k, v in os.environ.items() if k not in SCRUB}
    env["PLAINKEEP_CORE"] = mode
    return env


def dispatcher_modes(engine: Path) -> tuple[str, ...]:
    """The dispatcher modes the INSTALLED PAIR can actually be proved in — derived from the pair, not
    assumed. `require` refuses to degrade without a compiled core, and a core is a build artifact
    that a fresh clone does not have, so demanding it unconditionally would be asking about the
    developer's machine rather than about the migration."""
    return ("off", "require") if (engine / enginetree.CORE_REL).is_file() else ("off",)


def _run_probe(launcher: Path, vault: Path, args: list[str], mode: str,
               timeout: int = 300) -> subprocess.CompletedProcess:
    return subprocess.run([str(launcher), *args], cwd=str(vault), env=scrubbed_env(mode),
                          capture_output=True, text=True, timeout=timeout)


def _clip(text: str, limit: int = 1600) -> str:
    """Clip a probe's output KEEPING BOTH ENDS.

    A head-only clip is not neutral about what it drops, and this one dropped the answer. `doctor`
    prints its `FAIL` lines LAST, so 1200 characters of its output is sixteen `ok` lines and not one
    failing line — the migration reported a doctor failure and then hid which one, to the person who
    had to act on it. The head carries the command's framing, the tail carries its verdict, and the
    elision says how much is missing rather than trailing off."""
    text = (text or "").strip()
    if len(text) <= limit:
        return text or "(no output)"
    head = limit // 3
    tail = limit - head
    return (text[:head] + f"\n  … {len(text) - head - tail} character(s) elided …\n" + text[-tail:])


def prove(vault: Path, vault_id: str, launcher: Path, engine: Path, *, write: bool = True,
          done: dict | None = None) -> dict:
    """PROVE-BEFORE-REMOVE. The installed binary must, from a subprocess carrying no `PLAINKEEP_HOME`
    and with `$PWD` inside the vault: discover THIS vault by id, pass `doctor`, answer a read verb,
    and complete one guardrail-gated write. In every dispatcher mode the pair supports.

    Every one of these is a DISPATCH through the real launcher, which is the only form ADR-019 D1
    accepts: five of the seven unwired rules that ADR catalogues would have passed a check that
    merely asked a library function whether it would answer correctly.

    `write=False` drops ONLY the capture canary, and there is exactly one caller that passes it: the
    second run of a migration that is already finished. Acceptance item 12 asks that run to be a
    no-op, and a "no-op" that appends a note to the operator's inbox every time they re-run it is not
    one. The write is never skipped on the path the acceptance bar is about — prove-before-remove and
    the post-removal re-proof both perform it, because a dry run proves the argument parser works and
    nothing about whether a byte can land.

    `done` IS WHAT MAKES THE REFUSAL TRUE. This function runs at three moments — before the removal,
    after it, and on the second run of a finished migration — and every one of them used to fail with
    the FIRST one's sentence: "prove-before-remove FAILED … nothing was removed", hinted with "the
    vault still carries its own engine copy". On the two later calls both halves are false, and they
    were measured being told to an operator whose 109 engine paths were already gone and whose
    receipt already said `complete`. So `done` carries the true state and the true recovery down from
    the caller that knows them; `None` means this really is prove-before-remove.

    Raises on the first failure, with the probe's own output, having removed nothing."""
    modes = dispatcher_modes(engine)
    report: dict = {"modes": list(modes), "probes": [], "wrote": []}
    for mode in modes:
        r = _run_probe(launcher, vault, ["vault", "status", "--json"], mode)
        _probe_ok(r, f"vault status ({mode})", vault, done)
        doc = _payload(r.stdout, f"vault status --json ({mode})")
        selected = doc.get("id") or doc.get("vault_id")

        # WHICH MECHANISM CHOSE IT, not merely which vault got chosen. The proof is about discovery
        # reaching THIS vault with no `PLAINKEEP_HOME` in the environment; a probe that only compared
        # ids would still pass if the scrub silently stopped working, because the inherited variable
        # would select the same correct vault for the wrong reason. The dispatcher reports what chose
        # (`selected_by`), so the claim is checked rather than assumed.
        how = str(doc.get("selected_by") or "")
        if "PLAINKEEP_HOME" in how:
            raise VaultError(
                f"the prove-before-remove shell was not scrubbed: the installed engine selected the "
                f"vault by {how!r}, not by discovery", code=output.EXIT_DENY,
                hint=f"{'/'.join(SCRUB)} must not be set when `prove()` runs")

        if selected != vault_id:
            raise VaultError(
                f"the installed engine, run from inside {vault} with no PLAINKEEP_HOME, selected "
                f"vault id {selected!r} — not this vault ({vault_id})", code=output.EXIT_DENY,
                hint="register this vault before migrating:\n    "
                     f"PLAINKEEP_HOME={vault} python3 {enginetree.engine_bin(engine) / 'vault' / 'run.py'}"
                     f" register {vault} --yes")
        report["probes"].append({"mode": mode, "probe": "vault status", "selected": selected,
                                 "selected_by": how})

        r = _run_probe(launcher, vault, ["doctor"], mode)
        _probe_ok(r, f"doctor ({mode})", vault, done)
        report["probes"].append({"mode": mode, "probe": "doctor", "rc": r.returncode})

        r = _run_probe(launcher, vault, ["status", "--json"], mode)          # the read verb
        _probe_ok(r, f"status ({mode})", vault, done)
        report["probes"].append({"mode": mode, "probe": "status", "rc": r.returncode})

        # The guardrail-gated write. It is a REAL write through the product's own safe-write path,
        # because a dry run proves the argument parser works and nothing about whether the wall, the
        # gate and the vault's permissions let a byte land. Its footprint is declared: the capture
        # verb writes one note into `inbox/` and appends one line to today's journal. Those two paths
        # are reported, attributed and printed for the operator — a migration that wrote into a vault
        # without saying exactly what it wrote would be the wrong kind of quiet.
        if not write:
            continue
        stamp = f"plainkeep migration canary {time.strftime('%Y-%m-%dT%H:%M:%S')} ({mode})"
        r = _run_probe(launcher, vault, ["capture", stamp, "--json"], mode)
        _probe_ok(r, f"capture ({mode})", vault, done)
        wrote = _payload(r.stdout, f"capture --json ({mode})")
        path = wrote.get("path") or wrote.get("wrote") or ""
        if path and not (vault / path).exists() and not Path(path).exists():
            raise VaultError(f"the capture canary reported {path!r} but nothing is there",
                             code=output.EXIT_DENY)
        report["wrote"].append(path or "(path not reported)")
        report["probes"].append({"mode": mode, "probe": "capture", "path": path})
    return report


def _payload(raw: str, what: str) -> dict:
    """The `data` object out of a `--json` envelope.

    Every verb's machine output is `{"ops_json": 1, "ok": …, "verb": …, "data": {…}}` (ADR the
    `plainkeep.json/3` contract, `test/run_json.py`). Reading the payload's keys off the TOP level
    finds none of them and yields `None` for every field — which is how the vault-identity probe came
    to compare `None` against a real vault id and refuse every migration it was asked to run. The
    envelope is unwrapped once, here, rather than at each of the four probes."""
    try:
        doc = json.loads(raw)
    except json.JSONDecodeError:
        raise VaultError(f"the installed engine's `{what}` did not emit JSON:\n{raw[:400]}",
                         code=output.EXIT_DENY)
    data = doc.get("data")
    return data if isinstance(data, dict) else doc


def _probe_ok(r: subprocess.CompletedProcess, what: str, vault: Path,
              done: dict | None = None) -> None:
    """Refuse on a failed probe, SAYING WHICH OF THE THREE PROOFS THIS IS.

    `done` is `{"summary", "hint"}` from a caller that knows the vault is already migrated (see
    `prove`). Its absence is not a default so much as the pre-removal case: nothing has been removed,
    the vault still has its engine copy, and re-running after a fix is exactly the right advice."""
    if r.returncode == 0:
        return
    body = _clip(r.stderr or r.stdout)
    if done is None:
        raise VaultError(
            f"prove-before-remove FAILED at `{what}` (exit {r.returncode}) — nothing was removed:\n"
            + body,
            code=output.EXIT_DENY,
            hint=f"the vault at {vault} still carries its own engine copy; fix the failure above "
                 "and re-run the migration")
    raise VaultError(
        f"{done['summary']} — and the RE-PROOF then failed at `{what}` (exit {r.returncode}):\n"
        + body,
        code=output.EXIT_DENY, hint=done["hint"])


# --- schedules --------------------------------------------------------------------------------------
# A launchd agent wakes up with almost no environment and no PATH worth the name. The failure this
# guards is Codex's most-expected one: a plist whose ProgramArguments still name the vault-local shim
# is ENOENT at 2am, silently, with nothing on screen to explain it.
#
# REGENERATED, never edited in place. `plainkeep job apply` re-renders every plist from
# `jobs/registry.json` through the INSTALLED launcher, and `bin/job/run.py:_plist` bakes
# `enginetree.stable_launcher()` — `…/engine/current/plainkeep`, the name that survives the next
# activation — plus the validated vault as an absolute `PLAINKEEP_HOME`. So a regenerated schedule
# does not depend on discovery at all, which is what makes the sanitized exercise below meaningful.
LAUNCHD_BASE = ("HOME", "USER", "LOGNAME", "TMPDIR", "LANG", "SHELL")
LAUNCHD_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"


def _sanitized_env(plist_env: dict) -> dict:
    """`env -i` plus what launchd genuinely provides plus the plist's own EnvironmentVariables.

    `KEEP` rides along for the reason stated at its definition: on a real machine neither variable is
    set, and in a test unsetting them would aim the exercise at the developer's real registry. It
    changes nothing about what is being proved — the plist bakes `PLAINKEEP_HOME` absolutely, so
    discovery step 2 wins and the registry is never consulted."""
    env = {"PATH": LAUNCHD_PATH}
    for k in (*LAUNCHD_BASE, *KEEP):
        if k in os.environ:
            env[k] = os.environ[k]
    env.update({str(k): str(v) for k, v in plist_env.items()})
    return env


EXEC_FAILED = (126, 127)     # the shell's "found but not executable" / "not found"


def _sanitized_run(argv: list[str], penv: dict, plist_name: str,
                   timeout: int = 300) -> subprocess.CompletedProcess:
    """Run `argv` exactly as launchd would, and turn an EXEC failure into a refusal.

    `subprocess.run` raises `OSError` for a program that is missing or not executable, while a
    wrapper script that fails to exec its own target reports 127/126 instead. Both mean the same
    thing — this schedule cannot run at 2am — and both are the failure the whole step exists to
    catch, so they are one refusal rather than an exception and a number."""
    try:
        r = subprocess.run(argv, env=_sanitized_env(penv), cwd="/", capture_output=True, text=True,
                           timeout=timeout)
    except OSError as e:
        raise VaultError(
            f"the regenerated schedule {plist_name} names a program that COULD NOT BE RUN: "
            f"{argv[0]} ({e.strerror}) — nothing has been removed", code=output.EXIT_DENY,
            hint="a launchd agent wakes with no PATH worth the name; the plist must name an "
                 "absolute, executable launcher")
    if r.returncode in EXEC_FAILED:
        raise VaultError(
            f"the regenerated schedule {plist_name} names a program that COULD NOT BE RUN: "
            f"{argv[0]} (exit {r.returncode}) — nothing has been removed:\n"
            + _clip(r.stderr or r.stdout), code=output.EXIT_DENY)
    if r.returncode < 0:
        # Killed by a signal. The one non-zero exit that no verb can mean as a verdict about the
        # vault, so it is the one this step is still entitled to read as a broken schedule.
        raise VaultError(
            f"the regenerated schedule {plist_name} DIED ON SIGNAL {-r.returncode} in a sanitized "
            "launchd environment — nothing has been removed:\n" + _clip(r.stderr or r.stdout),
            code=output.EXIT_DENY)
    return r


def _routing_probe(vault: Path, plist_name: str, program: Path, penv: dict,
                   launcher: Path) -> dict:
    """THE GATE. Does this schedule run the INSTALLED launcher, and does it operate THIS vault?

    It replaced "the rendered argv must exit 0", which conflated two unrelated things. A schedule's
    verb reports a VERDICT ABOUT THE VAULT in its exit code — the real vault schedules
    `backup_check` -> `plainkeep backup`, whose bare form is a commit/push nag that exits 1 whenever
    the tree is dirty or unpushed. `prove()` runs BEFORE this step by design and its guardrail-gated
    canary writes a note, so the tree is GUARANTEED dirty here and that job is GUARANTEED to exit 1.
    The migration refused, every time, and each retry added another canary note: it did not converge,
    structurally.

    And the exit code cannot be rescued by classifying it, because the product's exit space does not
    separate a verdict from a failure: `output.EXIT_UNEXPECTED` IS 1, so "restic failed" and "your
    notes are not pushed" are the same number. Interpreting it would be guessing.

    So the two questions the step exists for are asked DIRECTLY, and mostly of the ARTEFACT rather
    than of a subprocess. A plist bakes both facts absolutely — `bin/job/run.py:_plist` renders
    `enginetree.stable_launcher()` as the program and the validated vault as `PLAINKEEP_HOME`,
    precisely so that a scheduled job depends on neither discovery nor `PATH` — so reading them back
    off the rendered file is not a weaker check than running something, it is the check:

      1. the program IS the stable launcher of the pair this migration installed (`current`, not a
         pinned version, and not anything else);
      2. `PLAINKEEP_HOME` is baked, and it is THIS vault;
      3. the launcher is on disk and executable, so the 2am ENOENT cannot happen.

    A VERB PROBE WAS TRIED AND REJECTED, and the reason belongs here because it is a property of the
    environment rather than of this module: in a sanitized launchd environment `PATH` is
    `/usr/bin:/bin:/usr/sbin:/sbin`, so the launcher resolves `python3` to macOS's system 3.9 — and
    `plainkeep vault status` does not parse under it. Gating the migration on a verb-neutral probe
    would have replaced F1 with a stricter version of F1.

    Returns the routing facts for the receipt."""
    if not vaultreg.same_path(str(program), str(launcher)):
        raise VaultError(
            f"the regenerated schedule {plist_name} does not name THE INSTALLED LAUNCHER — it names "
            f"{program} — nothing has been removed", code=output.EXIT_DENY,
            hint=f"a schedule must run {launcher}, the `current` name that survives the next engine "
                 "activation. Re-render the schedules (`plainkeep job apply`) and delete any plist "
                 "that is not this vault's")
    home = penv.get("PLAINKEEP_HOME")
    if not home:
        raise VaultError(
            f"the regenerated schedule {plist_name} bakes NO PLAINKEEP_HOME — nothing has been "
            "removed", code=output.EXIT_DENY,
            hint="a launchd agent wakes with almost no environment and `cwd=/`, so a schedule that "
                 "does not name its vault absolutely is relying on a discovery that cannot succeed")
    if not vaultreg.same_path(str(home), str(vault)):
        raise VaultError(
            f"the regenerated schedule {plist_name} runs the right program against the WRONG VAULT: "
            f"it bakes PLAINKEEP_HOME={home}, not {vault} — nothing has been removed",
            code=output.EXIT_DENY,
            hint="re-render the schedules from this vault (`plainkeep job apply`) and delete any "
                 "plist that belongs to another one")
    if not (os.path.isfile(program) and os.access(program, os.X_OK)):
        raise VaultError(
            f"the regenerated schedule {plist_name} names a program that COULD NOT BE RUN: {program} "
            "is missing or not executable — nothing has been removed", code=output.EXIT_DENY)
    return {"routed": True, "program": str(program), "home": str(home)}


def regenerate_schedules(vault: Path, launcher: Path, engine: Path) -> dict:
    """Re-render every plist, prove each one ROUTES, and exercise each one's own argv.

    Two questions per plist, and keeping them apart is the whole of F1 (see `_routing_probe`):

      * **Does it route?** REFUSED on a program that is not the installed launcher, on a missing or
        absent `PLAINKEEP_HOME`, on one naming another vault, and on a launcher that is not there or
        not executable. This is the gate.
      * **Does its own argv run?** The rendered `ProgramArguments` are still executed with
        `--dry-run` appended in a sanitized launchd environment, because that is the only thing here
        that exercises the ARTEFACT AS WRITTEN. Its exit code is RECORDED, not gated on: it is the
        verb's verdict about the vault, and this module is not entitled to an opinion about it. What
        IS still refused is a failure to EXEC (126/127, `OSError`) or a death by signal — neither of
        which any verb can mean as a verdict.

    `--dry-run` is appended for the same reason it always was: actually running a user's
    `consolidate` in the middle of a migration would write into the vault at the moment the migration
    is about to hash it. If a verb ever stopped honouring it the manifest comparison would catch it,
    because the hashes are taken AFTER this step and must be identical across the removal.

    THE HONEST LIMIT: a verb that CRASHES with exit 1 is indistinguishable here from a verb that
    reports bad news with exit 1, and this step no longer guesses. What stands behind it is
    `prove()`, which dispatched four real verbs through this same launcher against this same vault
    two steps earlier and refused if any of them failed."""
    out_dir = vault / "jobs" / "launchd"
    modes = dispatcher_modes(engine)
    r = _run_probe(launcher, vault, ["job", "apply"], modes[0])
    if r.returncode != 0:
        raise VaultError("regenerating the schedules failed — nothing has been removed:\n"
                         + _clip(r.stderr or r.stdout), code=output.EXIT_DENY)
    rendered, exercised, stale = [], [], []
    for plist in sorted(out_dir.glob("*.plist")) if out_dir.is_dir() else []:
        with open(plist, "rb") as fh:
            doc = plistlib.load(fh)
        args = [str(a) for a in doc.get("ProgramArguments", [])]
        penv = doc.get("EnvironmentVariables", {}) or {}
        rendered.append(plist.name)
        if not args:
            raise VaultError(f"{plist.name} renders no ProgramArguments", code=output.EXIT_DENY)
        program = Path(args[0])
        # THE STALE-ROUTING CHECK, asked of the artifact rather than of the renderer. A plist that
        # still names anything inside the vault is the failure being migrated away from, and it must
        # be caught here — before the copy it names is removed — not after.
        if vaultreg.path_within(vaultreg.canonical(str(program)), vaultreg.canonical(str(vault))):
            stale.append(f"{plist.name} -> {program}")
            continue
        routing = _routing_probe(vault, plist.name, program, penv, launcher)
        rr = _sanitized_run([*args, "--dry-run"], penv, plist.name)
        # `nonzero`, not `verdict`. It was `verdict`, which read as "this schedule is fine" while
        # meaning `rc != 0` — a field name that inverts its own sense in the one record an operator
        # reads to find out what happened.
        exercised.append({"plist": plist.name, "rc": rr.returncode, "nonzero": rr.returncode != 0,
                          **routing})
    if stale:
        raise VaultError("regenerated schedules still point INSIDE the vault:\n  " + "\n  ".join(stale),
                         code=output.EXIT_DENY,
                         hint="bin/job/run.py must render enginetree.stable_launcher()")
    return {"rendered": rendered, "exercised": exercised, "dir": str(out_dir)}


# --- stale executable routing -------------------------------------------------------------------------
def bin_dir() -> Path:
    """Where `script/setup` puts the `plainkeep` symlink. Same variable, same default, so a machine
    set up with a relocated bin dir is migrated at the same place it was installed."""
    v = os.environ.get("PLAINKEEP_BIN_DIR")
    return Path(os.path.expanduser(v)) if v and v.strip() else Path.home() / ".local" / "bin"


def launcher_route(vault: Path) -> dict:
    """What `<bin>/plainkeep` is, and whether it routes into the vault about to lose its engine."""
    link = bin_dir() / "plainkeep"
    if link.is_symlink():
        target = os.readlink(link)
        abs_target = str((link.parent / target).resolve()) if not os.path.isabs(target) else target
        into = vaultreg.path_within(vaultreg.canonical(abs_target), vaultreg.canonical(str(vault)))
        return {"path": str(link), "kind": "symlink", "target": target, "into_vault": into}
    if link.exists():
        return {"path": str(link), "kind": "file", "target": None, "into_vault": False}
    return {"path": str(link), "kind": "absent", "target": None, "into_vault": False}


def repoint_launcher(vault: Path, route: dict) -> dict:
    """Repoint a symlink that routes into the vault at `engine/current/plainkeep`, atomically.

    ONLY a symlink, and only one that points into THIS vault. A regular file at that path is somebody
    else's `plainkeep` and is reported, never replaced — the migration's job is to stop a stale link
    becoming ENOENT, not to take ownership of a name.

    The old target is returned so the receipt can carry it: this is the one irreversible-looking step
    the migration takes, and it is sequenced AFTER prove-before-remove precisely so the thing it
    points at is already known to work."""
    if route["kind"] != "symlink" or not route["into_vault"]:
        return {**route, "repointed": False}
    link = Path(route["path"])
    want = enginetree.current_link() / "plainkeep"
    tmp = link.with_name(f".plainkeep.migrate.{os.getpid()}")
    if tmp.exists() or tmp.is_symlink():
        tmp.unlink()
    tmp.symlink_to(want)
    os.replace(tmp, link)
    return {**route, "repointed": True, "new_target": str(want), "old_target": route["target"]}


# --- the receipt ---------------------------------------------------------------------------------------
# OUTSIDE THE VAULT, keyed by vault id. A receipt written into `<vault>/.plainkeep/` would be the one
# write this module makes into a directory it promises never to open for writing, and "except for the
# receipt" is exactly the shape of the waiver the design constraint forbids. The install root is
# engine-side, already writable, and the id is the vault's identity (ADR-014: the id is the identity,
# the path is not), so a moved vault still finds its own receipt.
def receipts_dir() -> Path:
    return enginetree.install_root() / "migrations"


def receipt_path(vault_id: str) -> Path:
    return receipts_dir() / f"{vault_id}.json"


def read_receipt(vault_id: str) -> dict | None:
    p = receipt_path(vault_id)
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def write_receipt(doc: dict) -> Path:
    d = receipts_dir()
    d.mkdir(parents=True, exist_ok=True)
    p = receipt_path(doc["vault_id"])
    tmp = p.with_name(p.name + f".incoming.{os.getpid()}")
    tmp.write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, p)
    return p


# --- state ------------------------------------------------------------------------------------------
def _is_migration_commit(vault: Path, rev: str = "HEAD") -> bool:
    return f"{TRAILER}:" in _git_out(vault, "log", "-1", "--format=%B", rev)


def _worktree_residue(vault: Path) -> list[str]:
    """Allowlisted paths still present in the WORKING TREE. Non-empty after a kill between the ref
    update and the prune, which is the one interruption that leaves a vault half-migrated."""
    found: list[str] = []
    for f in REMOVAL_FILES:
        if (vault / f).exists() or (vault / f).is_symlink():
            found.append(f)
    for t in REMOVAL_TREES:
        if (vault / t).is_dir():
            found.append(t + "/")
    return sorted(found)


def state(vault: Path) -> str:
    """`pristine` | `resume` | `migrated`. Asked BEFORE the clean-tree check, because a killed
    migration leaves exactly the dirt it was in the middle of removing and refusing it for being
    dirty would make kill-and-re-run diverge instead of converge."""
    tracked = _tracked_under_allowlist(vault)
    residue = _worktree_residue(vault)
    if tracked:
        return "pristine"
    if residue and _is_migration_commit(vault):
        return "resume"
    if residue:
        # The vault carries engine files that HEAD does not track and no migration commit explains.
        # That is somebody's untracked engine copy, not a half-finished migration, and guessing which
        # is the guess ADR-014 forbids.
        raise VaultError(
            "this vault carries UNTRACKED engine paths that no migration commit explains:\n  "
            + "\n  ".join(residue),
            code=output.EXIT_DENY,
            hint="commit or remove them yourself, then re-run; migration only removes what git "
                 "tracks, so it cannot construct a verified tree for these")
    return "migrated"


# --- preflight ------------------------------------------------------------------------------------------
def _refuse_unrepresentable(paths_: list[str]) -> None:
    bad = [p for p in paths_ if "\n" in p]
    if bad:
        raise VaultError("refusing: an engine path contains a newline and cannot be expressed in the "
                         "index-info deletion form", code=output.EXIT_DENY)


def _would_prune_dirs(vault: Path, removed: list[str]) -> list[str]:
    """Which directories `_prune_empty_dirs` WOULD remove, predicted without removing anything.

    Read-only, and deliberately the same shape as the real prune: the ANCESTOR CLOSURE of the
    deletions, deepest-first, a directory counting as emptied when every entry it currently holds is
    either a deletion or a directory this walk has already emptied. Deepest-first is what makes the
    second clause work — `bin/` is only empty after `bin/lib/` and `bin/verb/` have gone — and it is
    the same ordering, for the same reason, that `_prune_empty_dirs`' docstring records paying for."""
    gone = set(removed)
    pruned: list[str] = []
    cands: set[str] = set()
    for r in removed:
        p = Path(r).parent
        while str(p) != ".":
            cands.add(str(p))
            p = p.parent
    for d in sorted(cands, key=lambda d: -d.count("/")):
        p = vault / d
        if p.is_symlink() or not p.is_dir():
            continue
        try:
            entries = {f"{d}/{e}" for e in os.listdir(p)}
        except OSError:
            continue
        if entries <= gone:
            gone.add(d)
            pruned.append(d)
    return sorted(pruned)


def _refuse_dangling_symlinks(vault: Path, removed: list[str]) -> list[str]:
    """REFUSE, BEFORE ANYTHING IS REMOVED, a protected symlink the migration would break.

    The real vault carries two committed, protected, user-owned symlinks — `.claude/skills` and
    `.codex/skills`, both `-> ../skills` — into a `skills/` whose only entry is the allowlisted
    `skills/operate-plainkeep`. The removal empties it, the prune takes the directory, and both links
    dangle. Nothing in the allowlist logic accounted for protected paths that point INTO removed
    ones, so the only thing that noticed was the protected-hash comparison, which runs AFTER `_apply`
    — 109 paths gone, the migration commit made — and which described the outcome as
    `added: ['.claude/skills', '.codex/skills']`: the manifest artefact of F2(a), not the cause and
    not the fix.

    WHY THIS REFUSES RATHER THAN REPAIRS. Those links are the operator's. They are committed, they
    are protected content, and a migration that silently repointed one would be doing the exact thing
    this module's design constraint says is impossible within its action space — and would do it to a
    file it cannot even see is wrong, since a link pointing somewhere else may be entirely deliberate.
    So the migration stops and hands over the two end-states that ARE legitimate, both of which
    `doctor` accepts: point the adapter at the installed engine's skills tree (which is where
    `operate-plainkeep` lives once the engine is out of the vault), or drop the adapter.

    Scoped to a PRISTINE run. On a resume the removal has already happened and the links may already
    be broken; refusing there would stop the one command that finishes the job, and `rollback` is
    what puts the target back.

    Returns the empty list when nothing would break, so a caller can state that it asked."""
    gone = set(removed) | set(_would_prune_dirs(vault, removed))
    broken: list[str] = []
    for rel in _protected_paths(vault):
        link = vault / rel
        if not link.is_symlink():
            continue
        target = os.readlink(link)
        # LEXICAL resolution, not `Path.resolve()`: what git records is the target STRING, and the
        # question is which vault path it names — not what that path happens to resolve to right now,
        # which is the thing about to change. Built from `vault`, which `preflight` has already
        # canonicalised, so `path_within`'s "both sides canonical" precondition holds by construction
        # for the in-vault case its string comparison answers.
        abs_target = os.path.normpath(os.path.join(os.path.dirname(str(link)), target))
        if not vaultreg.path_within(abs_target, str(vault)) or abs_target == str(vault):
            continue                      # outside the vault; this removal cannot reach it
        t = os.path.relpath(abs_target, str(vault)).replace(os.sep, "/")
        parts = t.split("/")
        if any("/".join(parts[:i]) in gone for i in range(1, len(parts) + 1)):
            broken.append(f"{rel} -> {target}   (resolves to {t}, which this migration removes)")
    if broken:
        raise VaultError(
            f"refusing to migrate {vault}: {len(broken)} protected symlink(s) point INTO paths this "
            "migration removes, and it would leave them DANGLING — nothing has been removed:\n  "
            + "\n  ".join(broken[:20]),
            code=output.EXIT_DENY,
            hint="these links are yours — migration never rewrites protected content, so repair "
                 "them and re-run. Either point the adapter at the installed engine, which is where "
                 "the skill lives once the engine is out of the vault:\n"
                 f"    ln -sfn {enginetree.current_link() / 'skills'} <vault>/<the link above>\n"
                 "or drop the adapter (`git rm --cached <link> && rm <link>`). `plainkeep doctor` "
                 "accepts both.")
    return broken


# Every fsck complaint that names something under .git/objects. Concurrent git housekeeping shows up
# in more than one shape: a temp packfile it is still writing, and loose objects it has just packed
# and unlinked.
#   packfile .git/objects/pack/.tmp-<pid>-pack-<sha>.pack cannot be accessed
#   unable to mmap .git/objects/ab/cdef…: No such file or directory
#   <sha>: object corrupt or missing: .git/objects/ab/cdef…
_OBJ_PATH_RE = re.compile(r"(\S*\.git/objects/\S+?)(?::|\s|$)")


def _only_vanished_object_errors(vault: Path, fsck) -> bool:
    """True when every fsck complaint names a path that no longer exists.

    That is the signature of racing git's own housekeeping rather than of a damaged object store:
    `git gc` packs loose objects and unlinks them, and a concurrent fsck that already enumerated one
    then fails to read it. A REAL problem names a file that is still sitting there — so a single
    still-present path, or a single complaint that names no path at all, returns False and the
    caller raises.
    """
    lines = [ln.strip() for ln in (fsck.stderr + "\n" + fsck.stdout).splitlines() if ln.strip()]
    if not lines:
        return False
    for ln in lines:
        named = _OBJ_PATH_RE.findall(ln)
        if not named:
            return False                              # a complaint about something else → real
        for raw in named:
            # git quotes the path in some messages ("unable to load rev-index for pack '<p>'").
            # Strip them: a still-quoted string never exists as a path, which would make EVERY
            # quoted complaint look like a vanished artifact — including a real one.
            raw = raw.strip("'\"")
            p = Path(raw)
            if not p.is_absolute():
                p = vault / raw
            if p.exists():
                return False                          # still there → not a vanishing artifact
    return True


def _clean_tree_gate(vault: Path, *, op: str = "migration") -> list[str]:
    """ACCEPTANCE ITEM 2's clean-tree requirement, scoped to what a migration can actually harm.

    `op` names the direction for the message only. Both hazards are symmetric — `rollback()` runs the
    same index rewrite and then a `checkout-index -f` over the same allowlist — so it is the same
    gate, called twice, rather than two gates that could drift.

    TWO refusals, because "the tree is dirty" conflates two different hazards:

      * **Any STAGED change, anywhere in the vault.** `_apply` runs `git read-tree <commit>`, which
        rewrites `.git/index` wholesale, so a staged edit to a note is silently discarded. What is at
        risk is not the file — `read-tree` without `-u` never touches the working tree — it is the
        operator's staging, and losing that without saying so is exactly the quiet damage this module
        exists to make impossible.
      * **Unstaged or untracked changes to an ALLOWLISTED path.** Those are the paths migration
        deletes. An uncommitted edit to one is destroyed with no record and no patch, which is the
        same hazard `_divergence` covers for COMMITTED edits. An untracked file inside `bin/` is the
        specific case the panel named.

    And deliberately NOT a refusal, which is a NARROWING of item 2's literal "clean working tree" and
    is recorded as a deviation rather than smuggled in: uncommitted changes to NOTES. A migration is
    a pure deletion of engine paths, verified as such before checkout; an unstaged note edit is
    outside the removal set and outside the index rewrite, so there is no mechanism by which it can
    be harmed. Two reasons the strict rule is worse than this one:

      1. It refuses most real vaults most of the time — a person who keeps notes has uncommitted
         notes — and the pressure that creates is a blind `git add -A` before migrating, which is
         strictly more dangerous for them than migrating with a dirty journal.
      2. It makes a KILLED RUN UNRECOVERABLE. Prove-before-remove writes a real note and appends to
         today's journal, by design; a SIGKILL after that boundary therefore leaves a vault the
         strict rule refuses to finish migrating, with the engine copy still in place and no way
         forward but manual git surgery. A gate that a correct partial run cannot get past is not a
         safety property, it is a trap. `test/run_migrate.py`'s kill matrix is what found this.

    Returns the harmless dirt, so preflight can report it rather than pretend the vault was clean."""
    staged = _git_out(vault, "diff", "--cached", "--name-only", "HEAD")
    if staged:
        names = staged.splitlines()
        raise VaultError(
            f"{vault} has {len(names)} STAGED change(s), and {op} rewrites the git index:\n  "
            + "\n  ".join(names[:20]),
            code=output.EXIT_DENY,
            hint="commit them or unstage them (`git -C %s restore --staged .`) — a staged edit "
                 "would be discarded by the index rewrite, silently" % vault)

    entries = [e for e in _git_out(vault, "status", "--porcelain", "--untracked-files=all",
                                   "-z").split("\0") if e and len(e) > 3]
    engine_dirt = [e for e in entries if is_allowlisted(e[3:])]
    if engine_dirt:
        raise VaultError(
            f"{vault} has uncommitted changes to {len(engine_dirt)} ENGINE path(s) — the paths "
            f"migration deletes and rollback restores over:\n  " + "\n  ".join(engine_dirt[:20]),
            code=output.EXIT_DENY,
            hint=f"commit or remove them first. They are inside the removal allowlist, so {op} "
                 "would destroy them with no patch and no record; there is no --force")
    return [e for e in entries if not is_allowlisted(e[3:])]


def preflight(vault, *, engine_source=None, scratch: Path | None = None) -> dict:
    """READ-ONLY and CONFIRM-FREE. Everything a migration would refuse on, asked before anything is
    provisioned and before `--yes` is even looked at. It builds and VERIFIES the candidate tree —
    the expensive, load-bearing half — because a preflight that only lists what it would do is a
    preflight an operator cannot trust.

    Nothing here writes inside the vault. The temporary index lives in `scratch`."""
    _check_kill_stage()
    vault = Path(vaultreg.canonical(str(vault)))
    if not vault.is_dir():
        raise VaultError(f"{vault} is not a directory")
    top = _toplevel(vault)
    if not vaultreg.same_path(top, str(vault)):
        raise VaultError(f"{vault} is inside a git repository rooted at {top}, not the root of one",
                         hint="migrate the repository root")
    branch = _head_branch(vault)

    marker = vaultreg.read_marker(vault)
    if marker is None:
        raise VaultError(f"{vault} is not a plainkeep vault "
                         f"(no {vaultreg.MARKER_DIR}/{vaultreg.MARKER_NAME})",
                         hint="mark and register it first:\n    "
                              f"PLAINKEEP_HOME={vault} python3 "
                              f"{enginetree.engine_bin() / 'vault' / 'run.py'} register {vault} --yes")
    vault_id = marker["id"]
    reg = vaultreg.read_registry()
    entry = next((v for v in reg["vaults"] if v["id"] == vault_id), None)
    if entry is None:
        raise VaultError(f"{vault} carries a vault marker (id {vault_id}) that is not in the registry",
                         hint="a migrated vault is reached by DISCOVERY, and discovery goes through "
                              "the registry; register it first")

    st = state(vault)
    doc: dict = {"schema": SCHEMA, "vault": str(vault), "vault_id": vault_id, "branch": branch,
                 "state": st, "head": _git_out(vault, "rev-parse", "HEAD"),
                 "engine_paths_in_vault": enginetree.engine_paths_in(vault)}

    # Resolved BEFORE the `migrated` early return, not after it. A migrated vault no longer carries
    # a `VERSION` file — it is in the removal allowlist — so `engine_version` is legitimately None
    # there, and the caller that re-runs a finished migration still needs to be told which source it
    # would use. Computing it after the return left `engine_source` absent from exactly the one doc
    # that the second-run path reads it out of, which is a `KeyError`, not a no-op.
    src = Path(engine_source) if engine_source else vault
    doc["engine_source"] = str(src)
    doc["engine_version"] = enginetree.read_version(src) if (src / "VERSION").is_file() else None

    if st == "migrated":
        doc.update({"would_remove": [], "protected_files": None, "candidate_tree": None,
                    "verdict": "already migrated — nothing to remove"})
        return doc
    if st == "pristine" or engine_source is not None:
        # THE NARROWING IS ABOUT THE DEFAULTED SOURCE, NOT ABOUT THE STATE. A `resume` vault is
        # mid-removal, so its own copy — which is what `engine_source` DEFAULTS to — may already be
        # missing `bin/lib/migrate.py` through nothing but the interruption. Refusing on that would
        # make a half-pruned vault permanently unfinishable: "a gate that a correct partial run
        # cannot get past is not a safety property, it is a trap" (`_clean_tree_gate`, which paid for
        # the same lesson). An EXPLICITLY supplied source cannot create that trap — dropping the flag
        # is always available — so it is checked in every state.
        #
        # This condition used to be `st == "pristine"` alone, justified by the claim that a resume
        # could not be describing a real hazard because `provision()` would reuse the already-active
        # pair rather than install from this source. Review r1 measured that false: reuse is keyed on
        # the VERSION STRING — the exact collision this check exists to stop trusting — so a resume
        # vault plus `--engine-source <tree with a different VERSION and no migrate.py>` installed
        # and ACTIVATED it at exit 0. The claim is gone rather than reworded.
        _check_engine_source(src, defaulted=engine_source is None)

    if st == "pristine":
        doc["dirty_but_harmless"] = _clean_tree_gate(vault)

    # OBJECT INTEGRITY (acceptance item 2). Full `fsck`, not `--connectivity-only`: the migration is
    # about to make a commit the operator's only recovery path depends on, and connectivity says
    # nothing about whether a blob still hashes to its name. That argument is about REACHABLE
    # objects, and `--no-reflogs` does not weaken it — every reachable object is still walked and
    # still hash-verified.
    #
    # What it drops is fsck's default traversal of the REFLOG, which reports objects an earlier
    # `git gc` pruned but that a stale reflog entry still names:
    #
    #     error: HEAD: invalid reflog entry <sha>
    #     error: <sha>: object corrupt or missing: .git/objects/ab/cdef…
    #
    # That is the normal state of any repository whose history was rewritten and then packed — it
    # says nothing about the integrity of the history itself, and no retry can clear it because the
    # objects are genuinely gone. Refusing there fails a healthy vault for a condition git created
    # on purpose.
    #
    # This is safe for recovery specifically because rollback does NOT read the reflog: it restores
    # `before_commit` from the receipt (see `_before_commit()` and the parent check in `rollback()`),
    # which is a reachable commit this fsck still verifies. If recovery ever starts depending on the
    # reflog, this flag has to come back off with it.
    # A concurrent `git gc` makes fsck fail for reasons that are not corruption: it writes
    # `.tmp-<pid>-pack-<sha>.pack` while packing, packs loose objects and unlinks them, and builds
    # a rev-index that is briefly unreadable. All of it surfaces as a non-zero fsck naming a path
    # that has ALREADY VANISHED by the time we look — which is exactly what distinguishes the race
    # from a damaged object store, where the named file is still sitting there.
    #
    # Re-VERIFY rather than filter the messages out: each retry is a full fsck that still checks
    # every object, so corruption co-occurring with packing is still caught. Only a clean run is
    # accepted. Bounded, with a short pause, because packing takes longer than one immediate retry
    # on a loaded machine — this gate guards the commit the operator's rollback depends on, so it
    # must neither pass on unverified objects nor abort a healthy vault over git's housekeeping.
    t0 = time.time()
    for attempt in range(4):
        fsck = _git(vault, "-c", "gc.auto=0", "fsck", "--no-progress", "--no-dangling",
                    "--no-reflogs", check=False)
        if fsck.returncode == 0 or not _only_vanished_object_errors(vault, fsck):
            break
        doc["fsck_retries"] = attempt + 1
        time.sleep(0.5 * (attempt + 1))
    doc["fsck_seconds"] = round(time.time() - t0, 2)
    if fsck.returncode != 0:
        raise VaultError(f"git object integrity check FAILED in {vault}:\n"
                         + (fsck.stderr.strip() or fsck.stdout.strip())[:1200],
                         code=output.EXIT_DENY)

    ref = _engine_ref(vault) if st == "pristine" else None
    doc["engine_ref"] = ref
    if st == "pristine":
        doc["divergence"] = _divergence(vault, ref)

    tmp_owned = scratch is None
    scratch = Path(tempfile.mkdtemp(prefix="pk-migrate-")) if tmp_owned else scratch
    try:
        tracked = _tracked_under_allowlist(vault)
        _refuse_unrepresentable(tracked)
        if st == "pristine":
            tree, expected = build_candidate(vault, scratch)
            verify_candidate(vault, doc["head"], tree, expected)
            doc.update({"candidate_tree": tree, "would_remove": expected,
                        "would_prune_dirs": _would_prune_dirs(vault, expected)})
            _refuse_dangling_symlinks(vault, expected)
        else:
            doc.update({"candidate_tree": None, "would_remove": [],
                        "would_prune_worktree": _worktree_residue(vault)})
    finally:
        if tmp_owned:
            shutil.rmtree(scratch, ignore_errors=True)

    doc["verdict"] = ("ready to migrate" if st == "pristine"
                      else "a previous migration was interrupted — re-run to finish it")
    return doc


def _engine_ref(vault: Path) -> str:
    """`.plainkeep-engine-ref` — one 40-hex commit that must be PRESENT in this repository.

    Same three refusals `script/update` already makes about this file (missing, malformed, unknown),
    and they are refusals here rather than warnings for the reason the divergence check exists: with
    no usable base, "unmodified since the last sync" cannot be answered at all, and a migration that
    could not answer it would remove local engine edits it never saw."""
    p = vault / ".plainkeep-engine-ref"
    if not p.is_file():
        raise VaultError(f"{vault} has no .plainkeep-engine-ref",
                         code=output.EXIT_DENY,
                         hint="it records the upstream commit this vault's engine was last synced "
                              "to; without it, local engine edits cannot be detected. Run "
                              "`script/update` (which records it) or `script/setup --engine-ref <sha>`")
    raw = p.read_text(encoding="utf-8", errors="replace").strip()
    if not SHA_RE.match(raw):
        raise VaultError(f".plainkeep-engine-ref is not a single 40-char SHA ({len(raw)} chars)",
                         code=output.EXIT_DENY)
    if _git(vault, "rev-parse", "--verify", "--quiet", raw + "^{commit}", check=False).returncode != 0:
        raise VaultError(f".plainkeep-engine-ref names {raw[:8]}, which is not a commit in this "
                         f"repository", code=output.EXIT_DENY,
                         hint="fetch it so the divergence check has a base:\n"
                              f"    git -C {vault} fetch upstream")
    return raw


def _synced_by_ref(vault: Path, ref: str) -> tuple[list[str], list[str]]:
    """Split the divergence allowlist into what `script/update` SYNCED from `ref`, and what it did not.

    `_divergence` refuses on any difference between the recorded ref and HEAD over the engine
    allowlist. That is only sound for the paths `script/update` actually checked out of that ref, and
    what it checks out is `script/engine.txt` AS IT STOOD IN THAT COMMIT — a list that has
    demonstrably drifted from the removal allowlist. `templates/verb` is the recorded case: owned by
    `enginetree` and absent from `engine.txt` until Phase 2 Task 2, so a vault whose last sync
    predates that carries a copy laid down by `script/setup` which will not match the ref's, reports
    `M`, and refuses the migration by NAMING A PATH THE OPERATOR NEVER TOUCHED. Same class as the
    `.plainkeep-engine-ref` bug `b373028` fixed, one layer out: comparing against a base that was
    never the source of the thing being compared.

    So the pathspec is intersected with the ref's OWN manifest. The remainder is REPORTED rather than
    silently dropped — an unsynced engine path can still carry a local edit, and an operator whose
    migration is about to delete it is owed the sentence "this was not compared" rather than a
    check that quietly narrowed itself."""
    all_ = [*DIVERGENCE_TREES, *DIVERGENCE_FILES]
    r = _git(vault, "show", f"{ref}:script/engine.txt", check=False)
    if r.returncode != 0:
        # The ref carries no manifest at all (a vault synced before `engine.txt` existed). Compare
        # everything: that is the pre-existing behaviour and it is the conservative direction — a
        # false refusal hands the operator a patch they can read, a skipped comparison hands them
        # nothing.
        return all_, []
    listed = [ln.strip() for ln in r.stdout.splitlines()]
    listed = [ln for ln in listed if ln and not ln.startswith("#")]

    def covered(p: str) -> bool:
        # Either direction of containment: `engine.txt` says `frontends` where the ownership manifest
        # says `frontends/raycast`, and both spellings mean that path was synced from this ref.
        return any(p == e or p.startswith(e + "/") or e.startswith(p + "/") for e in listed)

    compared = [p for p in all_ if covered(p)]
    if not compared:
        raise VaultError(
            f".plainkeep-engine-ref names {ref[:8]}, whose script/engine.txt lists none of the "
            f"engine paths this vault carries — it cannot be the commit this engine was synced from",
            code=output.EXIT_DENY,
            hint="record the ref this vault actually synced from (`script/update` writes it), or "
                 "fetch the commit it names")
    return compared, [p for p in all_ if not covered(p)]


def _divergence(vault: Path, ref: str) -> dict:
    """ACCEPTANCE ITEM 3. Every added, modified, deleted or type-changed engine path between the
    recorded ref and HEAD. Any divergence REFUSES and emits a recoverable patch.

    The hazard is specific and it is the one the panel named: an agent's local edits to `bin/**`
    inside a real vault. After migration they are dead code — the installed engine is what runs — and
    after removal they are invisible. So they are not merged, not warned about and not carried: the
    migration stops, writes the patch OUTSIDE the vault, and names it.

    Scoped to the paths that ref actually synced — see `_synced_by_ref` for why comparing the rest is
    a refusal the operator cannot act on. Returns both halves so preflight can report what was left
    out of the comparison."""
    compared, unsynced = _synced_by_ref(vault, ref)
    raw = _git_out(vault, "diff", "--name-status", "-z", ref, "HEAD", "--", *compared)
    fields = [f for f in raw.split("\0") if f]
    entries = [f"{fields[i]} {fields[i + 1]}" for i in range(0, len(fields) - 1, 2)]
    if not entries:
        return {"compared": compared, "unsynced": unsynced, "entries": []}
    d = receipts_dir()
    d.mkdir(parents=True, exist_ok=True)
    patch = d / f"{vaultreg.read_marker(vault)['id']}.divergence.patch"
    body = _git(vault, "diff", ref, "HEAD", "--", *compared).stdout
    patch.write_text(body, encoding="utf-8")
    raise VaultError(
        f"this vault's engine copy has DIVERGED from the recorded sync ref {ref[:8]} in "
        f"{len(entries)} path(s):\n  " + "\n  ".join(entries[:30])
        + (f"\n  … and {len(entries) - 30} more" if len(entries) > 30 else ""),
        code=output.EXIT_DENY,
        hint=f"a recoverable patch of every difference is at:\n    {patch}\n"
             "port what you want into the engine source, commit the vault back to the recorded ref "
             "for these paths, and re-run. There is no --force: after migration these edits are "
             "dead code, and after removal they are invisible.")


# --- migrate --------------------------------------------------------------------------------------------
def _before_commit(vault: Path, pre: dict) -> str:
    """The commit a ROLLBACK has to return this vault to, which is not always `pre["head"]`.

    On a pristine run it is exactly HEAD. On a RESUME it is not, and getting that wrong was silent:
    the killed run had already moved HEAD onto the migration commit, so `preflight()` recorded that
    commit as `head` and the receipt claimed `before_commit == after_commit`. `rollback()` then
    diffed a commit against itself, got an empty path set, passed every gate vacuously, restored
    NOTHING, printed success, and deleted the receipt — the only record of the pre-migration commit
    and of where the launcher used to point. After that there was no rollback at all, on any
    interrupted migration.

    The true answer is the migration commit's parent, and it is safe to ask for: `state()` returns
    `resume` only when HEAD carries this module's trailer, so HEAD is a migration commit and it has
    one."""
    if pre["state"] != "resume":
        return pre["head"]
    return _git_out(vault, "rev-parse", "HEAD^")


def _launcher_route_for_receipt(fresh: dict, prior: dict | None) -> dict:
    """The launcher route a rollback needs, which is the one the FIRST run of this migration saw.

    A resumed run re-reads `<bin>/plainkeep` and finds it already pointing at the installed pair, so
    its own `launcher_route()` reports `into_vault: False` and carries no old target. Overwriting the
    receipt with that erases the one fact in it that git cannot reconstruct — where the operator's
    launcher used to point — and `git revert` does not put a symlink back."""
    old = (prior or {}).get("launcher_route") or {}
    if fresh.get("old_target") or fresh.get("into_vault"):
        return fresh
    if old.get("old_target") or old.get("into_vault"):
        return old
    return fresh


def _finalize_interrupted_receipt(vault: Path, vault_id: str) -> dict | None:
    """Complete a provisional receipt whose run was killed AFTER the removal and before it finished.

    A kill at `worktree-pruned` leaves a vault that is fully and correctly migrated, so `state()`
    answers `migrated` and the re-run takes the CLI's no-op branch — which does not run `_apply` and
    never wrote a receipt. Before the provisional receipt existed that combination left an operator
    with a migrated vault, no rollback at all, and no record of where their launcher used to point.
    The record is now already on disk from before the removal; this fills in the one field only the
    completed removal can supply.

    It REFUSES to complete a receipt it cannot tie to this HEAD: HEAD must be a migration commit and
    its parent must be the `before_commit` the provisional recorded. A receipt that aimed a rollback
    at the wrong commit would be worse than the absent one it replaces.

    It also refuses while the working tree STILL CARRIES engine paths, which is the `tree-written`
    boundary: HEAD is the migration commit and its parent is right, but the removal itself never ran,
    so `removed` derived from that commit would describe deletions the operator can still see on
    disk. Completing there would write the same class of untruth this function is called from
    `rollback()` to stop telling. That state is a `resume`, and re-running is what finishes it.

    `removed` is DERIVED FROM THE COMMIT rather than left empty, and that is not cosmetic. It is the
    number `rollback()`'s empty-restore refusal compares against — the check that turns "restored 0
    paths" into a refusal instead of a success — so a recovered receipt claiming zero removals would
    hand that check a vacuous comparison of 0 against 0 on the very path B1 was about. The commit's
    own deletions are the exact set `_prune_worktree` removed, because the prune is driven from the
    verified diff of this commit.

    `pruned_dirs` stays empty: emptied directories leave no record in git, so a recovered receipt
    cannot reconstruct them. Nothing reads the field — `checkout-index` recreates directories as it
    restores the files — so an empty list is the honest answer rather than a guessed one."""
    rec = read_receipt(vault_id)
    if rec is None or rec.get("after_commit"):
        return rec
    head = _git_out(vault, "rev-parse", "HEAD")
    if not _is_migration_commit(vault, head) or _worktree_residue(vault):
        return rec
    parent = _git(vault, "rev-parse", "HEAD^", check=False)
    if parent.returncode != 0 or parent.stdout.strip() != rec.get("before_commit"):
        return rec
    raw = _git_out(vault, "diff-tree", "-r", "--no-commit-id", "--name-status", "-z",
                   rec["before_commit"], head)
    fields = [f for f in raw.split("\0") if f]
    removed = [fields[i + 1] for i in range(0, len(fields) - 1, 2) if fields[i] == "D"]
    rec.update({"after_commit": head, "status": "recovered", "removed": removed,
                "pruned_dirs": rec.get("pruned_dirs") or [],
                "recovered_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")})
    write_receipt(rec)
    return rec


def _migration_commit_in(vault: Path, before: str | None, head: str) -> str | None:
    """The migration commit in `before..head` when HEAD itself is not one.

    The one question that separates "this receipt describes a migration that never started" from
    "…that finished and then had work committed on top of it". Both leave a PROVISIONAL receipt, and
    telling them apart is what stops `rollback()` asserting that nothing was removed to an operator
    whose engine copy is gone. Matched on the commit trailer, the same evidence `_is_migration_commit`
    uses, so a rewritten or amended history answers honestly rather than by hash."""
    if not before:
        return None
    r = _git(vault, "log", "--format=%H", "-F", f"--grep={TRAILER}:", f"{before}..{head}",
             check=False)
    hits = r.stdout.split() if r.returncode == 0 else []
    return hits[-1] if hits else None       # oldest match: the migration, not what came after it


def migrate(vault, *, yes: bool = False, engine_source=None, pre: dict | None = None) -> dict:
    """The whole sequence. Read the module docstring for the ordering and why it is the ordering.

    `pre` lets the CLI hand in the preflight it already ran. Not a shortcut around the checks — every
    one of them is re-derived below where it is load-bearing (`_apply` rebuilds and re-verifies the
    candidate tree, and `update-ref` is a compare-and-swap on the HEAD this doc recorded) — but a
    preflight runs a FULL `git fsck`, and running it twice per migration doubles the one cost that
    scales with the size of the vault's history.

    That sentence used to be true of the allowlist gate and the CAS and FALSE of the two refusals
    below, which were reached only from `preflight()`. A caller handing in a hand-built `pre` — this
    is a public function, and `pre` is a keyword argument, not a flag — therefore migrated a vault
    carrying committed local engine edits and destroyed them with no patch and no record. There is no
    `--force` in this module and a keyword argument is not allowed to be one, so both are re-derived
    here — and the `state` they are keyed on is VALIDATED first, because re-deriving a refusal under
    `state == "pristine"` only moves the waiver into every other spelling of the state."""
    _check_kill_stage()
    if not yes:
        raise VaultError("refusing to migrate without --yes", code=output.EXIT_CONFIRM,
                         hint="run the read-only preflight first:\n"
                              f"    python3 {Path(__file__).resolve()} --preflight {vault}")
    scratch = Path(tempfile.mkdtemp(prefix="pk-migrate-"))
    try:
        if pre is None:
            pre = preflight(vault, engine_source=engine_source, scratch=scratch)
        # THE STATE IS THE KEY BOTH GATES BELOW ARE HUNG ON, so it is validated before either of them
        # is consulted. Closing the `pre` waiver by re-deriving the two refusals under
        # `state == "pristine"` left the hole one narrowing smaller rather than shut: `_apply`'s
        # resume branch is keyed on `== "resume"`, so ANY THIRD VALUE fell through both and the
        # pristine path built, committed and removed normally. Review r2 measured seven of them —
        # `"bogus-state"`, `"migrated"`, `""`, `null`, `"PRISTINE"`, `"pristine "`, `"clean"` — each
        # completing a 122-path removal on a vault carrying a committed engine edit, with no
        # divergence patch and the operator's staging discarded. An exact-string gate on
        # caller-supplied data has to refuse what it does not recognise, not fall through it.
        declared = pre.get("state")
        if declared not in ("pristine", "resume"):
            raise VaultError(
                f"refusing to migrate {vault}: the preflight handed in declares state "
                f"{declared!r}, which is not a state a migration can start from",
                code=output.EXIT_DENY,
                hint=("this vault is already migrated — there is nothing to remove. `--rollback` is "
                      "the command that undoes a migration"
                      if declared == "migrated" else
                      "`state()` answers only 'pristine', 'resume' or 'migrated', so a `pre` "
                      "carrying anything else did not come from `preflight()`. It is not a waiver: "
                      "the divergence and clean-tree refusals are keyed on this value, and an "
                      "unrecognised one would skip both. There is no --force"))
        vault = Path(pre["vault"])
        vault_id = pre["vault_id"]
        if pre["state"] == "pristine":
            _clean_tree_gate(vault)
            _divergence(vault, _engine_ref(vault))
            # RE-DERIVED, like the two above and for the same reason: `pre` is a keyword argument on
            # a public function and is not allowed to be a `--force`. The removal set is read from
            # git here rather than out of `pre["would_remove"]`, so a hand-built doc cannot shrink it.
            _refuse_dangling_symlinks(vault, _tracked_under_allowlist(vault))
        before_commit = _before_commit(vault, pre)
        _kill_hook("preflight-done")

        src = Path(pre["engine_source"])
        prov = provision(src)
        engine = enginetree.versions_dir() / prov["version"]
        launcher = enginetree.current_link() / "plainkeep"

        proof = prove(vault, vault_id, launcher, engine)
        _kill_hook("proved")

        sched = regenerate_schedules(vault, launcher, engine)
        _kill_hook("schedules")

        # THE RECOVERY RECORD, WRITTEN BEFORE THE STEP IT DESCRIBES. Two facts a rollback needs
        # cannot be recovered from the repository once this run stops answering: the pre-migration
        # commit, and where `<bin>/plainkeep` pointed before it was repointed. Writing the receipt
        # only at the end meant a SIGKILL at `worktree-pruned` — one boundary the kill matrix calls
        # safe — left a fully migrated vault with no receipt, and the re-run took the no-op branch
        # and never wrote one either. Rollback was then permanently unavailable and the old symlink
        # target was gone.
        #
        # So a provisional receipt lands here, BEFORE `repoint_launcher`, carrying the route as it
        # stands. `os.replace` makes the repoint itself atomic, so there is no instant at which the
        # launcher has moved and the receipt does not say where from.
        planned = launcher_route(vault)
        prior = read_receipt(vault_id)
        write_receipt({"schema": SCHEMA, "vault": str(vault), "vault_id": vault_id,
                       "branch": pre["branch"], "before_commit": before_commit,
                       "after_commit": None, "status": "in-progress",
                       "engine": prov,
                       "launcher_route": _launcher_route_for_receipt(planned, prior),
                       "resumed": pre["state"] == "resume",
                       "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")})
        route = _launcher_route_for_receipt(repoint_launcher(vault, planned), prior)
        _kill_hook("symlink")

        # THE HASHES THE CONTRACT IS ABOUT (acceptance item 13), taken here rather than at the top:
        # everything above this line is the product doing its normal job through its own verbs, and
        # everything below is the removal. Comparing across the removal alone is the strong form of
        # the claim — no exception list, no attributable-difference clause, nothing tolerated.
        before = protected_manifest(vault)

        result = _apply(vault, pre, scratch, before_commit)

        after = protected_manifest(vault)
        delta = manifest_diff(before, after)
        if delta["modified"] or delta["removed"] or delta["added"]:
            raise VaultError(
                "THE REMOVAL CHANGED PROTECTED CONTENT — this must be impossible and it happened:\n"
                f"  modified: {delta['modified'][:10]}\n  removed:  {delta['removed'][:10]}\n"
                f"  added:    {delta['added'][:10]}\n"
                f"restore with: git -C {vault} reset --hard {before_commit}",
                code=output.EXIT_DENY)

        left = enginetree.engine_paths_in(vault)
        if left:
            raise VaultError(f"the vault still carries engine paths after migration: {left}",
                             code=output.EXIT_DENY)

        doc = {"schema": SCHEMA, "vault": str(vault), "vault_id": vault_id,
               "branch": pre["branch"], "before_commit": before_commit, "status": "complete",
               "after_commit": result.get("commit"), "removed": result.get("removed", []),
               "pruned_dirs": result.get("pruned_dirs", []), "engine": prov,
               "launcher_route": route, "schedules": sched, "proof": proof,
               "protected_files": before["count"], "canary_writes": proof["wrote"],
               "resumed": pre["state"] == "resume",
               "migrated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
        rec_path = write_receipt(doc)
        _kill_hook("receipt")

        # PROVE AGAIN, after the removal. The first proof said the installed pair can operate this
        # vault; this one says it still can with the vault's own copy gone — which is the sentence an
        # operator actually cares about and is not implied by the first.
        #
        # AND IF IT FAILS, THE REFUSAL DESCRIBES THE VAULT AS IT NOW IS. Everything above this line
        # has happened: the paths are gone, the commit is made, the receipt on disk says `complete`.
        # A failure here is a failure of the re-proof, not of the migration, and the pre-removal
        # text — "nothing was removed", "the vault still carries its own engine copy" — is false in
        # both halves at this point. Re-running is not the recovery either: `state()` answers
        # `migrated`, so a re-run is a no-op that cannot change anything the operator needs changed.
        doc["proof_after"] = prove(
            vault, vault_id, launcher, engine,
            done={"summary": (f"the migration of {vault} COMPLETED — {len(doc['removed'])} engine "
                              f"path(s) removed in commit {str(doc['after_commit'])[:12]}, receipt "
                              f"at {rec_path}"),
                  "hint": ("this vault IS migrated and its receipt is complete, so there is nothing "
                           "left to remove and starting the migration again cannot change that. Fix "
                           "the failure above and re-check with `plainkeep doctor`. To put this "
                           "vault's engine copy back instead:\n"
                           f"    python3 {Path(__file__).resolve()} --rollback {vault} --yes")})
        # Every note this migration wrote, in one field. The post-removal proof performs the same
        # guardrail-gated capture the pre-removal one does, and a `canary_writes` that listed only
        # the first half would under-report the migration's own footprint in the receipt an operator
        # reads to find out exactly what it put in their vault.
        doc["canary_writes"] = proof["wrote"] + doc["proof_after"]["wrote"]
        doc["clean_after"] = _git_out(vault, "status", "--porcelain", "--untracked-files=all") == ""
        write_receipt(doc)
        return doc
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def _apply(vault: Path, pre: dict, scratch: Path, before: str) -> dict:
    """The mutation, and the only part of a migration that touches the vault's own directory.

    Four steps, in this order, each of which leaves a state the next run can finish from:
      1. build + verify the candidate tree            (nothing has moved)
      2. commit-tree, then update-ref with a CAS      (HEAD moves; the worktree still has the files)
      3. read-tree into the REAL index                (the index agrees with HEAD; still no worktree write)
      4. delete the verified paths from the worktree  (the only removal, path by path)

    No git command in this sequence writes to the working tree. `read-tree` without `-u` rewrites
    `.git/index` only; the deletions are `os.remove` calls driven from the VERIFIED diff, not from
    the allowlist — the allowlist is what the diff was checked against, one step further back.

    `before` is the pre-migration commit as `_before_commit` derived it, passed in rather than read
    off `pre["head"]`, so the commit this verifies against and the commit the receipt aims a rollback
    at are one value with one derivation. They used to be two, and on a resumed run they disagreed."""
    if pre["state"] == "resume":
        # A killed run already moved HEAD. Re-verify what it did rather than trusting the trailer:
        # the commit's own diff against its parent must still be an allowlisted pure deletion.
        head = _git_out(vault, "rev-parse", "HEAD")
        verified = verify_candidate(vault, before, head,
                                    _tracked_under_allowlist(vault, before))
        _git(vault, "read-tree", head)
        removed = _prune_worktree(vault, verified)
        return {"commit": head, "removed": removed,
                "pruned_dirs": _prune_empty_dirs(vault, verified)}

    tree, expected = build_candidate(vault, scratch)
    verified = verify_candidate(vault, before, tree, expected)
    msg = (f"plainkeep: migrate off the vault-local engine\n\n"
           f"Removes {len(verified)} engine path(s) the installed engine now provides. Verified as a\n"
           f"pure deletion against an exact allowlist before checkout; no protected path is touched.\n\n"
           f"{TRAILER}: {SCHEMA}\n")
    commit = _git_out(vault, "-c", "commit.gpgsign=false", "commit-tree", tree, "-p", before,
                      "-m", msg, env=_commit_env(vault))
    # CAS: the old value is passed, so a HEAD that moved underneath this run makes the update FAIL
    # rather than silently discarding whatever moved it.
    _git(vault, "update-ref", pre["branch"], commit, before)
    _kill_hook("tree-written")
    _git(vault, "read-tree", commit)
    removed = _prune_worktree(vault, verified)
    pruned = _prune_empty_dirs(vault, verified)
    _kill_hook("worktree-pruned")
    return {"commit": commit, "removed": removed, "pruned_dirs": pruned}


def _prune_worktree(vault: Path, verified: list[str]) -> list[str]:
    done = []
    for rel in verified:
        _remove_engine_path(vault, rel)
        done.append(rel)
    return done


def _commit_env(vault: Path) -> dict:
    """An identity for the migration commit that never PROMPTS and never invents one silently.

    A vault set up by `script/setup` has `user.name`/`user.email`; a bare fixture or a sanitized
    environment may not, and `commit-tree` then fails with a message about `git config` that is
    correct and unhelpful in the middle of a migration."""
    env = dict(os.environ)
    have_name = _git(vault, "config", "user.name", check=False).returncode == 0
    have_mail = _git(vault, "config", "user.email", check=False).returncode == 0
    if not have_name:
        env.setdefault("GIT_AUTHOR_NAME", "plainkeep migrate")
        env.setdefault("GIT_COMMITTER_NAME", "plainkeep migrate")
    if not have_mail:
        env.setdefault("GIT_AUTHOR_EMAIL", "migrate@plainkeep.invalid")
        env.setdefault("GIT_COMMITTER_EMAIL", "migrate@plainkeep.invalid")
    return env


# --- rollback ---------------------------------------------------------------------------------------------
def rollback(vault, *, yes: bool = False) -> dict:
    """ACCEPTANCE ITEM 12: a tested command sequence, not prose.

    Restores the vault's engine copy from git and puts the launcher symlink back where it pointed.
    It does NOT roll the ENGINE back — the installed pair is retained and `enginetree.py --rollback`
    is the command for that, deliberately separate: rolling a vault back is about this vault, and
    rolling the engine back is about every vault on the machine.

    Refuses when HEAD is not the migration commit. A commit made since means the operator has work on
    top of it, and rewriting their branch to undo one commit underneath it is the destructive
    recovery the whole design exists to avoid — `git revert` is theirs to run.

    A PROVISIONAL receipt is completed here before anything else, so an interrupted migration that
    actually finished its removal can be undone without being re-run first. What cannot be completed
    is refused, and the refusal says which of the three interruptions this vault is in rather than
    telling all of them the story of the earliest one."""
    if not yes:
        raise VaultError("refusing to roll back without --yes", code=output.EXIT_CONFIRM)
    vault = Path(vaultreg.canonical(str(vault)))
    marker = vaultreg.read_marker(vault)
    if marker is None:
        raise VaultError(f"{vault} is not a plainkeep vault")
    rec = read_receipt(marker["id"])
    if rec is None:
        raise VaultError(f"no migration receipt for this vault ({receipt_path(marker['id'])})",
                         code=output.EXIT_NOT_FOUND,
                         hint="if the migration commit is in the history, revert it yourself:\n"
                              f"    git -C {vault} revert <commit>")
    if not rec.get("after_commit"):
        # A PROVISIONAL receipt describes THREE different vaults, and the refusal used to describe
        # only the first one — to all three. Review r2 (NEW-1) measured it telling an operator whose
        # entire engine copy had been removed that the run "was interrupted before it removed
        # anything": the one sentence a module built on "no quiet or misleading damage" cannot say.
        #
        # So the completable case is COMPLETED FIRST — `_finalize_interrupted_receipt` already owns
        # the HEAD-is-a-migration-commit-whose-parent-is-`before_commit` test — and a kill at
        # `worktree-pruned` then rolls back here and now, without the re-run step the old hint made
        # mandatory. What is left is genuinely un-completable, and the two shapes of it get their own
        # answers below.
        rec = _finalize_interrupted_receipt(vault, marker["id"]) or rec
    if not rec.get("after_commit"):
        rt = rec.get("launcher_route") or {}
        launcher = (f"\nIf the launcher was already repointed, it belongs at:\n"
                    f"    {rt.get('path')} -> {rt.get('old_target') or rt.get('target')}")
        head = _git_out(vault, "rev-parse", "HEAD")
        residue = _worktree_residue(vault)
        if residue and _is_migration_commit(vault, head):
            # `tree-written`: the commit is made, the removal is not. Rolling back is not the way
            # out — re-running is, and it converges (the kill matrix proves it at this boundary).
            raise VaultError(
                "this vault's migration receipt is PROVISIONAL and the migration is HALF APPLIED — "
                f"the migration commit {head[:12]} is in place but the removal was interrupted "
                f"part-way, so {len(residue)} engine path(s) are still in the working tree",
                code=output.EXIT_DENY,
                hint="re-run the migration to finish it; it converges, and it can be rolled back "
                     "afterwards. This command will not undo a removal that has not happened."
                     + launcher)
        done = _migration_commit_in(vault, rec.get("before_commit"), head)
        if done:
            # The dead end review r2 named: a kill at `worktree-pruned` followed by a commit. The
            # removal DID happen, so re-running is a permanent no-op that cannot finish this
            # receipt, and rewriting the branch under the operator's own commit is the destructive
            # recovery this module refuses. `git revert` is the route, and naming the commit is the
            # difference between advice and a dead end.
            raise VaultError(
                "this vault's migration receipt is PROVISIONAL and can no longer be completed: the "
                f"migration commit {done[:12]} IS in this history — the engine copy was removed — "
                "but there is work committed on top of it, so HEAD is no longer that commit",
                code=output.EXIT_DENY,
                hint="undo it with a commit of your own; re-running the migration will NOT finish "
                     "this receipt, because the vault is already migrated:\n"
                     f"    git -C {vault} revert {done[:12]}" + launcher)
        # And the case the old message was written for, where it was always true: the run stopped
        # before it committed anything. HEAD never moved and there is nothing to undo.
        raise VaultError(
            "this vault's migration receipt is PROVISIONAL — the run that wrote it was interrupted "
            "before it removed anything, so there is no migration commit to roll back",
            code=output.EXIT_DENY,
            hint="re-run the migration to finish it." + launcher)
    head = _git_out(vault, "rev-parse", "HEAD")
    if head != rec.get("after_commit"):
        raise VaultError(
            f"HEAD is {head[:8]}, not the migration commit {str(rec.get('after_commit'))[:8]}",
            code=output.EXIT_DENY,
            hint="there is work on top of the migration; undo it with a commit of your own:\n"
                 f"    git -C {vault} revert {str(rec.get('after_commit'))[:12]}")
    branch = rec["branch"]
    before = rec["before_commit"]
    if not before or before == rec.get("after_commit"):
        # A RECEIPT THAT CANNOT DESCRIBE A MIGRATION, and the reason this refusal exists rather than
        # a comment: when `before_commit == after_commit` every gate below passes VACUOUSLY. The
        # diff is empty, so nothing is "not an allowlisted addition"; `update-ref` sets the branch to
        # itself; zero paths are checked out; the protected manifest is trivially unchanged. The
        # command then reported success, restored nothing, and deleted the receipt.
        raise VaultError(
            f"refusing to roll back: the receipt names the same commit before and after "
            f"({str(before)[:12]}), which cannot describe a migration — it gives no state to return "
            "to, and rolling 'back' to it would restore nothing while deleting the receipt",
            code=output.EXIT_DENY,
            hint="the migration commit is still in the history; undo it yourself:\n"
                 f"    git -C {vault} revert {head[:12]}")

    # THE STAGED-CHANGE REFUSAL, IN THE DIRECTION IT WAS MISSING. `_clean_tree_gate` refuses any
    # staged change on the forward path because `read-tree` rewrites `.git/index` wholesale and
    # losing an operator's staging without saying so is exactly the quiet damage this module exists
    # to make impossible. `rollback()` runs the same `read-tree`, and then a `checkout-index -f` that
    # would overwrite an untracked file sitting at an allowlisted path — so it needs the same gate,
    # not a weaker one.
    _clean_tree_gate(vault, op="rollback")

    # Same gate as the forward direction, in reverse: the restoration must be a pure ADDITION of
    # allowlisted paths. Anything else and the rollback refuses rather than "restoring".
    raw = _git_out(vault, "diff-tree", "-r", "--no-commit-id", "--name-status", "-z", head, before)
    fields = [f for f in raw.split("\0") if f]
    entries = [(fields[i], fields[i + 1]) for i in range(0, len(fields) - 1, 2)]
    bad = [(s, p) for s, p in entries if s != "A" or not is_allowlisted(p)]
    if bad:
        raise VaultError("refusing to roll back: restoring would change more than the allowlist —\n  "
                         + "\n  ".join(f"{s} {p}" for s, p in bad[:20]), code=output.EXIT_DENY)
    paths_ = [p for _, p in entries]
    if not paths_:
        # "Restored 0 paths" is never a correct rollback of a migration that removed 122. An empty
        # set means the receipt does not describe the state on disk, and the only safe thing to do
        # with a receipt that cannot be trusted is to refuse and KEEP it.
        raise VaultError(
            f"refusing to roll back: restoring {head[:12]} -> {before[:12]} would put back 0 engine "
            f"path(s), but this receipt records a migration that removed "
            f"{len(rec.get('removed') or [])}",
            code=output.EXIT_DENY,
            hint="the receipt does not match this repository's history. It is left in place at\n"
                 f"    {receipt_path(marker['id'])}\n"
                 "so you can read the pre-migration commit and the old launcher target out of it.")

    protected_before = protected_manifest(vault)
    _git(vault, "update-ref", branch, before, head)
    _git(vault, "read-tree", before)
    if paths_:
        _git(vault, "checkout-index", "-f", "-u", "--", *paths_)
    delta = manifest_diff(protected_before, protected_manifest(vault))
    if delta["modified"] or delta["removed"] or delta["added"]:
        raise VaultError("the rollback changed protected content:\n" + json.dumps(delta, indent=2),
                         code=output.EXIT_DENY)

    route = rec.get("launcher_route") or {}
    restored = False
    # `old_target` is what a COMPLETED repoint recorded. `target` + `into_vault` is what the
    # provisional receipt recorded BEFORE the repoint, and it is the same fact — the launcher pointed
    # into this vault and the migration is what moved it. Accepting both is what makes the record
    # useful after an interruption, which is the only time it matters.
    old_target = route.get("old_target") or (route.get("target") if route.get("into_vault") else None)
    if old_target:
        link = Path(route["path"])
        if link.is_symlink() and os.readlink(link) != old_target:
            tmp = link.with_name(f".plainkeep.rollback.{os.getpid()}")
            if tmp.exists() or tmp.is_symlink():
                tmp.unlink()
            tmp.symlink_to(old_target)
            os.replace(tmp, link)
            restored = True
    receipt_path(marker["id"]).unlink(missing_ok=True)
    return {"schema": SCHEMA, "vault": str(vault), "result": "rolled-back",
            "restored": sorted(paths_), "head": before, "launcher_restored": restored,
            "clean": _git_out(vault, "status", "--porcelain", "--untracked-files=all") == ""}


# --- CLI --------------------------------------------------------------------------------------------------
_USAGE = ("usage: migrate.py --preflight <vault> [--json]            (READ-ONLY, no confirmation)\n"
          "       migrate.py --migrate   <vault> --yes [--engine-source <checkout>] [--json]\n"
          "       migrate.py --rollback  <vault> --yes [--json]\n"
          "       migrate.py --print allowlist|receipt <vault>")


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


def _render_preflight(d: dict) -> None:
    print(f"vault      {d['vault']}  ({d['vault_id']})")
    print(f"branch     {d['branch']}  @ {d['head'][:12]}")
    print(f"state      {d['state']}")
    if d.get("engine_ref"):
        print(f"sync ref   {d['engine_ref'][:12]}  (no divergence)")
        unsynced = (d.get("divergence") or {}).get("unsynced") or []
        if unsynced:
            print(f"           {len(unsynced)} engine path(s) that ref never synced, NOT compared: "
                  + ", ".join(unsynced))
    if d.get("engine_version"):
        print(f"engine     {d['engine_version']}  from {d['engine_source']}")
    rm = d.get("would_remove") or []
    print(f"would remove {len(rm)} tracked engine path(s)"
          + (f", first: {', '.join(rm[:4])}" if rm else ""))
    if d.get("would_prune_worktree"):
        print(f"would finish pruning: {', '.join(d['would_prune_worktree'])}")
    print(f"\n{d['verdict']}")
    if d["state"] != "migrated":
        print(f"  migrate with: python3 {Path(__file__).resolve()} --migrate {d['vault']} --yes")


def _render_migrate(d: dict) -> None:
    if d.get("result") == "no-op":
        print(f"{d['vault']} is already migrated — nothing to do")
        if d.get("receipt_completed"):
            print("  a migration receipt left provisional by an interrupted run was completed; "
                  "rollback is available again")
        return
    print(f"migrated {d['vault']}")
    print(f"  engine        {d['engine']['version']}"
          + (f" (reused)" if d["engine"]["reused"] else " (installed)")
          + (f", previous {d['engine']['previous']} retained" if d["engine"]["previous"] else ""))
    print(f"  removed       {len(d['removed'])} engine path(s) in commit {str(d['after_commit'])[:12]}")
    print(f"  protected     {d['protected_files']} file(s), byte-identical across the removal")
    # THE NON-ZERO EXERCISES ARE SAID OUT LOUD. `regenerate_schedules` deliberately stopped gating on
    # a job verb's exit code (F1: the code is a verdict about the vault and the exit space cannot
    # separate one from a failure) — but not gating on it is not a reason to stop REPORTING it. A
    # schedule naming a verb that does not exist exits 4, is correctly not refused, and used to leave
    # the operator reading "2 plist(s) regenerated and exercised" with nothing to act on. The signal
    # belongs here; the blocker does not come back.
    sched = d["schedules"]
    hot = [e for e in sched.get("exercised") or [] if e.get("nonzero")]
    print(f"  schedules     {len(sched['rendered'])} plist(s) regenerated and exercised"
          + (f", {len(hot)} exited NON-ZERO — recorded, not gated: "
             + ", ".join(f"{e['plist']} rc {e['rc']}" for e in hot[:3])
             + (f" and {len(hot) - 3} more" if len(hot) > 3 else "") if hot else ""))
    r = d["launcher_route"]
    print(f"  launcher      {r['path']} -> "
          + (f"{r.get('new_target')} (repointed from {r.get('old_target')})" if r.get("repointed")
             else f"{r.get('target') or r['kind']} (unchanged)"))
    for w in d.get("canary_writes", []):
        print(f"  canary wrote  {w}")
    print(f"\n  roll back with: python3 {Path(__file__).resolve()} --rollback {d['vault']} --yes")


def main(argv: list[str]) -> int:
    """The migration surface. A MODULE CLI for the reason the header states — the vault being
    migrated cannot dispatch its own engine, and the installed one may not exist yet."""
    if not argv:
        print(_USAGE, file=sys.stderr)
        return output.EXIT_USAGE
    cmd, rest = argv[0], argv[1:]
    try:
        if cmd == "--print":
            what = rest[0] if rest else "allowlist"
            if what == "allowlist":
                print(json.dumps({"trees": list(REMOVAL_TREES), "files": list(REMOVAL_FILES),
                                  "unprotected_prefixes": list(UNPROTECTED_PREFIXES)},
                                 indent=2, sort_keys=True))
                return output.EXIT_OK
            if what == "receipt":
                if len(rest) < 2:
                    print("plainkeep: --print receipt needs a vault", file=sys.stderr)
                    return output.EXIT_USAGE
                m = vaultreg.read_marker(Path(vaultreg.canonical(rest[1])))
                rec = read_receipt(m["id"]) if m else None
                if rec is None:
                    print("plainkeep: no migration receipt for that vault", file=sys.stderr)
                    return output.EXIT_NOT_FOUND
                print(json.dumps(rec, indent=2, sort_keys=True))
                return output.EXIT_OK
            print(_USAGE, file=sys.stderr)
            return output.EXIT_USAGE
        if cmd == "--preflight":
            if not rest:
                print("plainkeep: --preflight needs a vault", file=sys.stderr)
                return output.EXIT_USAGE
            return _emit(preflight(rest[0], engine_source=_flag(rest[1:], "--engine-source")),
                         rest[1:], _render_preflight)
        if cmd == "--migrate":
            if not rest:
                print("plainkeep: --migrate needs a vault", file=sys.stderr)
                return output.EXIT_USAGE
            opts = rest[1:]
            pre = preflight(rest[0], engine_source=_flag(opts, "--engine-source"))
            if pre["state"] == "migrated" and "--yes" in opts:
                # THE SECOND RUN (acceptance item 12). Converging rather than refusing: a re-run
                # after a kill at any stage finishes the job, and once it IS finished the tree half
                # is a no-op that still RE-PROVES the property the migration exists to establish.
                #
                # It does not re-provision from the vault. A migrated vault has no `VERSION` — that
                # file is in the removal allowlist — so `--engine-source` is the only source there
                # can be, and without one the honest answer is to check the pair that is already
                # active rather than to invent a source and fail inside `read_version`.
                src = _flag(opts, "--engine-source")
                if src:
                    _check_engine_source(Path(src), defaulted=False)
                    prov = provision(Path(src))
                else:
                    act = enginetree.active_engine()
                    if act is None:
                        raise VaultError(
                            f"{pre['vault']} is migrated but no engine is active — it has no engine "
                            "of its own to fall back on", code=output.EXIT_DENY,
                            hint="activate one, or re-run with --engine-source <checkout>:\n"
                                 f"    python3 {enginetree.__file__} --install <source-checkout>")
                    prov = {"version": act.name, "previous": None, "reused": True, "path": str(act)}
                engine = enginetree.versions_dir() / prov["version"]
                # The same truth requirement as the post-removal re-proof, on the other path that
                # reached prove-before-remove's sentence with it false: this vault was migrated,
                # possibly minutes ago, and telling its owner that "nothing was removed" and that it
                # "still carries its own engine copy" is the class of message this module's own
                # design notes forbid.
                proof = prove(Path(pre["vault"]), pre["vault_id"],
                              enginetree.current_link() / "plainkeep", engine, write=False,
                              done={"summary": f"{pre['vault']} is already migrated — this run is "
                                               "the second-run no-op, which RE-PROVES that the "
                                               "installed engine still operates the vault",
                                    "hint": ("nothing is left to remove, so this is a failed health "
                                             "check and not a failed migration. Fix the failure "
                                             "above and re-check with `plainkeep doctor`; the "
                                             "vault's engine copy can be restored with "
                                             "`--rollback` while its receipt exists.")})
                # A kill at `worktree-pruned` reaches this branch with a PROVISIONAL receipt: the
                # vault is fully migrated but the run that did it never finished. Complete the
                # record here or the operator has a migrated vault and no rollback at all.
                rec = _finalize_interrupted_receipt(Path(pre["vault"]), pre["vault_id"])
                return _emit({"schema": SCHEMA, "vault": pre["vault"], "result": "no-op",
                              "engine": prov, "proof": proof,
                              "receipt_completed": bool(rec and rec.get("status") == "recovered")},
                             opts, _render_migrate)
            return _emit(migrate(rest[0], yes="--yes" in opts,
                                 engine_source=_flag(opts, "--engine-source"),
                                 pre=pre if "--yes" in opts else None),
                         opts, _render_migrate)
        if cmd == "--rollback":
            if not rest:
                print("plainkeep: --rollback needs a vault", file=sys.stderr)
                return output.EXIT_USAGE
            opts = rest[1:]
            return _emit(rollback(rest[0], yes="--yes" in opts), opts,
                         lambda d: print(f"rolled back {d['vault']} to {d['head'][:12]} — "
                                         f"{len(d['restored'])} engine path(s) restored"
                                         + (", launcher symlink restored" if d["launcher_restored"]
                                            else "")))
    except VaultError as e:
        sys.stderr.write("plainkeep: " + e.message + (f"\n  {e.hint}" if e.hint else "") + "\n")
        return e.code
    except subprocess.TimeoutExpired as e:
        sys.stderr.write(f"plainkeep: a migration probe timed out after {e.timeout}s: {e.cmd}\n")
        return output.EXIT_UNEXPECTED
    except OSError as e:
        sys.stderr.write(f"plainkeep: {cmd} failed: {e}\n")
        return output.EXIT_UNEXPECTED
    print(_USAGE, file=sys.stderr)
    return output.EXIT_USAGE


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
