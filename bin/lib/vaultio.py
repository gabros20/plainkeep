"""
vaultio.py — the ENFORCED write seam. Where the path-wall actually meets a write.

`guardrail.classify()` has always been documented as the Iron Law seam ("a write-verb calls this on
the path IT computes, so the wall holds where the path is actually known" — guardrail.py's own
docstring). It wasn't true. Before this module NO verb called it: the only callers were the test
harness and `lib/api.py`'s re-export for plugins. The dispatcher admitted a verb on its DECLARED
RISK CLASS alone and nothing ever looked at the path the verb then wrote to — `bin/capture/run.py`
computed `paths.INBOX / …` and went straight to `mkdir` / `write_text` / `append_journal`.

So: every guarded write goes through `guard()`, which classifies `{"kind": "write", "path": …,
"realpath": …}` and refuses a DENY verdict on the shared exit-code protocol (5 = `EXIT_DENY`). The
verb still owns placement — the Iron Law is unchanged. This only asks whether the placement is
allowed, at the one moment the answer is knowable.

`realpath` is always supplied: `classify()` re-runs the verdict on the resolved path and takes the
STRICTER of the two, which is what catches a symlink pointing out of the vault.

The seam is not destination-only. A move TAKES a file away as well as putting one down, and
`classify({"kind": "delete", …})` has always denied a path under `~/files/**/in/` — append-only cuts
both ways — with no caller anywhere on a verb's path. That was the same defect this module was
written to fix, one branch over: a validated rule with no seam. `_guard_delete()` is that seam, and
`move_create_only` runs it on its SOURCE before it creates anything.

One write SHAPE is distinguished besides its path: an ATOMIC CREATE (`move_create_only`, and
`mkdir(exist_ok=False)`). `~/files/**/in/` is append-only — an original may ARRIVE, and no existing
one may ever be overwritten, replaced or deleted — and that rule cannot be enforced by asking
whether the path exists, because between the question and the write another arrival can answer it
differently. So the primitives make the claim, the wall admits only that claim under `in/`, and the
create-only guarantee is `link(2)`/`O_EXCL`'s EEXIST rather than anything this module decided.

WHAT THIS DOES NOT DO, so nobody reads more into a green suite than is there:

  * It cannot police a MISRESOLVED data root. The wall is anchored to the same value it would have
    to doubt (`guardrail._vault_roots()`). Validating the root — marker, registry, no silent
    fallback — is ADR-014 / Phase 2 Task 1, and this seam is what makes that validation observable
    at all.
  * It does not cover writes a verb makes OUTSIDE the three roots — launchd plists under
    `~/Library/LaunchAgents`, the `~/.local/bin` launcher symlink, `plainkeep new repo` /
    `plainkeep repo` creating trees under `~/work`. The wall as written DENIES all of those, so
    wiring them here would break working verbs. They are enumerated with a reason each in
    `test/run_pathwall.py`'s exemption list, which is a pinned set: it can shrink, and nothing new
    joins it silently.
"""
from __future__ import annotations
import errno
import hashlib
import os
import shutil
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path

# Importable BOTH as `lib.vaultio` (every verb) and top-level as `vaultio` — indexlib/vectorstore
# are loaded top-level with bin/lib on sys.path (test/run_search_impl.py:27), and they write.
try:
    from . import guardrail  # type: ignore  # (namespace sibling)
    from . import output     # type: ignore
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import guardrail  # type: ignore
    import output     # type: ignore


def _verb() -> str | None:
    """The calling verb's name, for the error envelope: `bin/<verb>/run.py` → `<verb>`."""
    try:
        return Path(sys.argv[0]).resolve().parent.name or None
    except Exception:
        return None


# The action key by which a write DECLARES ITSELF an atomic creation — the only shape the
# append-only originals rule admits under `~/files/**/in/` (`guardrail._write_verdict`).
#
# It is a claim about the SYSCALL the caller is about to make — `link(2)`, `open(2)` with
# `O_CREAT|O_EXCL`, `mkdir(2)`, each of which fails EEXIST atomically — and NEVER about a prior
# `exists()` test, which is a different and much weaker thing (see `move_create_only`). So the public
# `guard()` STRIPS it: a verb that could pass `create_only=True` as a keyword could claim a guarantee
# it does not provide, and the wall would have no way to tell. Only the primitives below, which make
# the syscall themselves, may assert it.
CREATE_ONLY = "create_only"


def guard(path, **action) -> Path:
    """Classify a write to `path` and return it, or refuse and exit 5. Extra kwargs are merged into
    the action dict (`repo`/`task_repo` for the ~/work scoping rule, `flags` for --yes/--force)."""
    action.pop(CREATE_ONLY, None)   # not the caller's to claim — see CREATE_ONLY above
    return _guard(path, **action)


def _guard(path, **action) -> Path:
    p = Path(path)
    raw = str(p)
    act = {"kind": "write", "path": raw, "realpath": os.path.realpath(raw), **action}
    d = guardrail.classify(act)
    if d.verdict == guardrail.DENY:
        output.fail(output.EXIT_DENY, f"guardrail: {d}",
                    hint="the verb computed a path the wall refuses — nothing was written",
                    verb=_verb())
    return p


def _guard_delete(path) -> Path:
    """Classify a REMOVAL of `path` and return it, or refuse and exit 5.

    `guardrail.classify({"kind": "delete", …})` denies any path under `~/files/**/in/` on the path
    AND its realpath — an original is never deleted, which is the half of append-only that the
    destination wall cannot see. Until this helper that branch had no caller outside the test
    harness, so `move_create_only` could rename an original out of `in/` and report it filed.

    Only a DENY is enforced. `classify` answers CONFIRM for an ordinary delete, and whether a human
    confirms one is the verb's business, not this seam's; treating a non-ALLOW as a refusal here
    would break every legitimate move."""
    p = Path(path)
    raw = str(p)
    d = guardrail.classify({"kind": "delete", "path": raw, "realpath": os.path.realpath(raw)})
    if d.verdict == guardrail.DENY:
        output.fail(output.EXIT_DENY, f"guardrail: {d}",
                    hint="this file may not be taken away from where it is — nothing was moved",
                    verb=_verb())
    return p


def mkdir(path, parents: bool = True, exist_ok: bool = True, **action) -> Path:
    """`exist_ok=False` makes this an ATOMIC CREATE of the leaf directory — `mkdir(2)` fails EEXIST,
    so the directory this call returns is one this call made — and that is the only form the
    append-only rule admits under `~/files/**/in/`. The default tolerates an existing directory,
    which is a perfectly good `mkdir` everywhere else and a refusal there.

    `parents=True` still creates missing PARENTS tolerantly; only the leaf is create-only."""
    p = _guard(path, **{**action, CREATE_ONLY: not exist_ok})
    p.mkdir(parents=parents, exist_ok=exist_ok)
    return p


def write_text(path, text: str, encoding: str = "utf-8", **action) -> Path:
    p = guard(path, **action)
    p.write_text(text, encoding=encoding)
    return p


def write_bytes(path, data: bytes, **action) -> Path:
    p = guard(path, **action)
    p.write_bytes(data)
    return p


def append_text(path, text: str, encoding: str = "utf-8", **action) -> Path:
    p = guard(path, **action)
    with open(p, "a", encoding=encoding) as fh:
        fh.write(text)
    return p


@contextmanager
def open_append(path, encoding: str = "utf-8", **action):
    """Guarded `open(path, "a")` — for the callers that write several lines in one handle."""
    p = guard(path, **action)
    with open(p, "a", encoding=encoding) as fh:
        yield fh


def move(src, dst, **action) -> Path:
    """Guarded `shutil.move`. The DESTINATION is the write; the source leaving is the verb's own
    business (it is a rename inside the vault in every current caller)."""
    p = guard(dst, **action)
    shutil.move(str(src), str(p))
    return p


# Errnos on which `link(2)` means "this pair of paths cannot be hard-linked" rather than "the
# destination is taken": a different volume (EXDEV), a filesystem that has no hard links at all
# (EPERM / EOPNOTSUPP — exFAT, some FUSE mounts), or a source already at its link limit (EMLINK).
# EEXIST is deliberately NOT here: it is the refusal this primitive exists to produce.
_LINK_FALLBACK = {errno.EXDEV, errno.EPERM, errno.EOPNOTSUPP, errno.ENOTSUP, errno.EMLINK}

# The subset of the above that means the filesystem has NO hard links AT ALL (exFAT, some FUSE
# mounts), so a second name cannot be minted next to the destination either and the staged shape in
# `_create_only_copy` is unavailable. EXDEV and EMLINK are about the SOURCE's relationship to the
# destination and say nothing about the destination's own filesystem, so they are not here.
_NO_HARDLINKS = {errno.EPERM, errno.EOPNOTSUPP, errno.ENOTSUP}

# Prefix for the staging leaf. Dot-prefixed and self-describing on purpose: on the one filesystem
# where it can be left behind (see `_create_only_copy`) a reader has to be able to tell it from an
# original at a glance.
ARRIVING_PREFIX = ".pk-arriving-"


def _sha256(path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _direct_create_only_copy(src: Path, dst: Path) -> None:
    """`O_CREAT|O_EXCL` straight onto `dst`, then fill it. The `open` is what fails EEXIST.

    Used ONLY where the destination's filesystem has no hard links, because it is the shape with a
    window: between the create and the last byte `dst` is SHORT, and a concurrent reader can see it
    that way. An exception unwinds into the cleanup below; a `SIGKILL` or a power loss does not, and
    under append-only a truncated leaf at that name can never afterwards be replaced. That residue
    is real, it is unavoidable without an atomic create-only rename, and it is why this is the
    fallback rather than the shape."""
    fd = os.open(dst, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    try:
        with os.fdopen(fd, "wb") as out, open(src, "rb") as inp:
            shutil.copyfileobj(inp, out)
            out.flush()
            os.fsync(out.fileno())
        if _sha256(dst) != _sha256(src):
            raise OSError(errno.EIO, "the copy does not match the source byte for byte", str(src))
        shutil.copystat(str(src), str(dst))
    except BaseException:
        # An unlink under an append-only tree, and what it removes is a leaf THIS call created and
        # did not finish filling — never an original, and never one any other process could have
        # read as such. Leaving it would be strictly worse: append-only means a truncated file at
        # that name could never afterwards be replaced by the real one.
        try:
            os.unlink(dst)
        except OSError:
            pass
        raise


def _create_only_copy(src: Path, dst: Path) -> None:
    """Give `dst` a PRIVATE inode holding `src`'s bytes — created, never replaced.

    "Private" is the point. `os.link` would give `dst` a second NAME for an inode somebody else
    already holds a name for, and append-only is a claim about the CONTENT of the leaf: an arrived
    original that another name can still be written through is not append-only, it is a file that
    happens to be listed under `in/`. A copy mints an inode with exactly one name.

    STAGED, so the destination never exists in a partial state: copy into a temporary leaf beside
    `dst`, verify the bytes, then `os.link(tmp, dst)` — the same directory, so that link cannot fail
    EXDEV — and drop the staging name. `dst` appears complete or not at all, and the create-only
    guarantee is still `link(2)`'s EEXIST rather than any prior test. Only a filesystem with no hard
    links at all falls back to `_direct_create_only_copy`, whose window is documented there.

    The bytes are VERIFIED against the source in both shapes before this returns, because the caller
    is about to unlink what may be the file's only other name."""
    fd, tmpname = tempfile.mkstemp(dir=str(dst.parent), prefix=ARRIVING_PREFIX)
    tmp = Path(tmpname)
    try:
        with os.fdopen(fd, "wb") as out, open(src, "rb") as inp:
            shutil.copyfileobj(inp, out)
            out.flush()
            os.fsync(out.fileno())
        if _sha256(tmp) != _sha256(src):
            raise OSError(errno.EIO, "the copy does not match the source byte for byte", str(src))
        shutil.copystat(str(src), str(tmp))
        try:
            os.link(tmp, dst)
        except FileExistsError:
            raise                   # the destination is taken — the refusal, not a fallback trigger
        except OSError as e:
            if e.errno not in _NO_HARDLINKS:
                raise
            _direct_create_only_copy(src, dst)
    finally:
        # The staging leaf is this call's own, was never an original and was never reachable under a
        # name that could be mistaken for one. Removing it is the whole reason it exists.
        try:
            os.unlink(tmp)
        except OSError:
            pass


def move_create_only(src, dst, **action) -> Path:
    """Move `src` onto `dst`, CREATING dst — never replacing it. Raises `FileExistsError` if anything
    already occupies `dst`: a file, a directory, or a symlink, live or dangling.

    The guarantee comes from the SYSCALL, not from a prior `dst.exists()` test, and that is the whole
    of the difference. An `exists()`-then-`move` pair has a window between the two in which another
    arrival takes the name, and the loser's bytes then replace the winner's with nothing left to see.
    Measured against the shape `bin/files/run.py` used at BASE 5436ec6 — 16 processes ingesting one
    filename into one directory — **217 of 320 originals were silently destroyed, in 20 rounds of
    20**. A uniquifying loop is not a guarantee; it is a description of the window.

      * a PRIVATE regular file: `os.link(src, dst)` then unlink the source. `link(2)` fails EEXIST
        atomically and copies no bytes, so `dst` is complete the instant it exists.
      * a SHARED or SYMLINKED source, or one that cannot be linked to `dst` at all (another device,
        a filesystem with no hard links): `_create_only_copy`, which mints a private inode under
        `O_EXCL`. Also atomic, also EEXIST.

    Neither falls back to a racy path when the atomic one fails. EEXIST is a REFUSAL, raised for the
    caller to answer — `files ingest` answers it by trying the next `-2`, `-3` name, a uniquifier
    whose every step is itself an atomic create.

    WHY A SHARED SOURCE IS COPIED RATHER THAN LINKED, and rather than refused. `link(2)` hands out a
    second NAME for one inode, and on macOS it follows a symlink, so linking a symlinked or
    already-hard-linked source files something the vault does not own: the other name stays outside
    `in/`, stays writable by anything, and every later write through it edits the "original" with no
    verb, no wall and no trace. Refusing instead would be sound but would break the ordinary case it
    hits — a symlink in a drop folder, a file a backup tool left a second link on — with no remedy
    the user could apply except copying it by hand, which is what this does for them. So the arrived
    leaf is always an inode this call created and nobody else has a handle to.

    That property is CHECKED, not assumed: pre-stat'ing the source would be a check-then-act on a
    path outside the vault. The link branch verifies afterwards that `dst` names the very file `src`
    named (not a symlink's target) and that the two are its ONLY names, and backs out if not.

    If the source cannot be unlinked after the destination exists, the destination STAYS and the
    error is raised: the original arrived, which is the half that append-only cares about, and
    removing it to tidy up would be the one thing the rule forbids."""
    s = Path(src)
    # The SOURCE is removed by this call, so the wall classifies that removal BEFORE anything is
    # created — a refusal has to leave the destination untouched rather than be rolled back.
    _guard_delete(s)
    p = _guard(dst, **{**action, CREATE_ONLY: True})
    if not s.is_file():
        # Kept ahead of `link`, because a directory source fails link(2) with EPERM and would
        # otherwise fall through to the copy branch and report something less true.
        raise OSError(errno.EINVAL, "only a regular file can be created as an original", str(s))
    if s.is_symlink() or s.stat().st_nlink > 1:
        _create_only_copy(s, p)
    else:
        try:
            os.link(s, p)
        except FileExistsError:
            raise                   # the destination is taken — the refusal, not a fallback trigger
        except OSError as e:
            if e.errno not in _LINK_FALLBACK:
                raise
            _create_only_copy(s, p)
        else:
            ls, lp = os.lstat(s), os.lstat(p)
            if (lp.st_dev, lp.st_ino) != (ls.st_dev, ls.st_ino) or lp.st_nlink != 2:
                # Only reachable if the source changed under the two statements above — it became a
                # symlink, or gained a second name — in which case `p` is a name for an inode the
                # vault does not own. `p` is a leaf THIS call created (link would have raised EEXIST
                # otherwise), so backing it out destroys nothing, and the source is still intact.
                os.unlink(p)
                raise OSError(errno.EAGAIN,
                              "the source gained another name while it was being filed — nothing "
                              "arrived", str(s))
    _unlink_arrived_source(s)
    return p


def _unlink_arrived_source(s: Path) -> None:
    """Drop the source's name, now that the vault holds the file under its own.

    Classified as a DELETE — the same wall the create side goes through, applied to what the move
    takes away. `move_create_only` already classified this path before creating anything; the check
    is repeated HERE because this is the syscall, and the invariant worth keeping is that the unlink
    is unreachable without a verdict rather than that one particular caller remembered to ask.

    For a symlinked source this removes the LINK and leaves its target where it was: the target was
    never ours to delete, and the vault holds its own copy of the bytes."""
    _guard_delete(s)
    os.unlink(s)


def copy2(src, dst, **action) -> Path:
    p = guard(dst, **action)
    shutil.copy2(str(src), str(p))
    return p


def copytree(src, dst, **kwargs) -> Path:
    action = {k: kwargs.pop(k) for k in ("repo", "task_repo", "flags") if k in kwargs}
    p = guard(dst, **action)
    shutil.copytree(str(src), str(p), **kwargs)
    return p


def replace(src, dst, **action) -> Path:
    p = guard(dst, **action)
    Path(src).replace(p)
    return p
