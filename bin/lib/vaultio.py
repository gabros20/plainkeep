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
import os
import shutil
import sys
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


def _create_only_copy(src: Path, dst: Path) -> None:
    """The cross-device half: `O_CREAT|O_EXCL` then copy. The `open` is what fails EEXIST."""
    fd = os.open(dst, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    try:
        with os.fdopen(fd, "wb") as out, open(src, "rb") as inp:
            shutil.copyfileobj(inp, out)
        shutil.copystat(str(src), str(dst))
    except BaseException:
        # The one and only unlink this module performs under an append-only tree, and what it removes
        # is a leaf THIS call created and did not finish filling — never an original, and never one
        # any other process could have read as such. Leaving it would be strictly worse: append-only
        # means a truncated file at that name could never afterwards be replaced by the real one.
        try:
            os.unlink(dst)
        except OSError:
            pass
        raise


def move_create_only(src, dst, **action) -> Path:
    """Move `src` onto `dst`, CREATING dst — never replacing it. Raises `FileExistsError` if anything
    already occupies `dst`: a file, a directory, or a symlink, live or dangling.

    The guarantee comes from the SYSCALL, not from a prior `dst.exists()` test, and that is the whole
    of the difference. An `exists()`-then-`move` pair has a window between the two in which another
    arrival takes the name, and the loser's bytes then replace the winner's with nothing left to see.
    Measured against the shape `bin/files/run.py` used at BASE 5436ec6 — 16 processes ingesting one
    filename into one directory — **217 of 320 originals were silently destroyed, in 20 rounds of
    20**. A uniquifying loop is not a guarantee; it is a description of the window.

      * same device: `os.link(src, dst)` then `os.unlink(src)`. `link(2)` fails EEXIST atomically and
        copies no bytes, so `dst` is complete the instant it exists — there is no partial state.
      * cross device: `open(dst, O_CREAT|O_EXCL)` then copy, then unlink the source. `O_EXCL` fails
        EEXIST atomically too.

    Neither falls back to a racy path when the atomic one fails. EEXIST is a REFUSAL, raised for the
    caller to answer — `files ingest` answers it by trying the next `-2`, `-3` name, a uniquifier
    whose every step is itself an atomic create.

    If the source cannot be unlinked after the destination exists, the destination STAYS and the
    error is raised: the original arrived, which is the half that append-only cares about, and
    removing it to tidy up would be the one thing the rule forbids."""
    s = Path(src)
    p = _guard(dst, **{**action, CREATE_ONLY: True})
    if not s.is_file():
        # Kept ahead of `link`, because a directory source fails link(2) with EPERM and would
        # otherwise fall through to the copy branch and report something less true.
        raise OSError(errno.EINVAL, "only a regular file can be created as an original", str(s))
    try:
        os.link(s, p)
    except FileExistsError:
        raise                       # the destination is taken — the refusal, not a fallback trigger
    except OSError as e:
        if e.errno not in _LINK_FALLBACK:
            raise
        _create_only_copy(s, p)
    os.unlink(s)
    return p


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
