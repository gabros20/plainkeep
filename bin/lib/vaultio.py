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


def guard(path, **action) -> Path:
    """Classify a write to `path` and return it, or refuse and exit 5. Extra kwargs are merged into
    the action dict (`repo`/`task_repo` for the ~/work scoping rule, `flags` for --yes/--force)."""
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
    p = guard(path, **action)
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
