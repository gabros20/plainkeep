"""
wall.py — the LOCATION policy: which trees are off-limits no matter what.

Split out of `guardrail.py` in Phase 2 Task 1b, and the split is a layering fix rather than tidying.
Two things need these markers and they sit on opposite sides of vault selection:

  * `guardrail.classify()` asks "may this WRITE land here?" — after a root has been selected.
  * `vaultroot.discover()` asks "may a VAULT LIVE here?" — before one has been, which is the whole
    point of it. A vault synced into iCloud is refused (exit 5) before its marker is read.

Leaving them in `guardrail.py` made that second question impossible to ask: `guardrail.py` resolves
`PLAINKEEP_HOME` at import (no fallback, Task 1b), so importing it from the module whose job is to
PRODUCE `PLAINKEEP_HOME` is a cycle. Nothing here depends on which vault is selected, which is
exactly why it can be asked first.

`guardrail.py` re-exports `HOME`, `WALLED_OFF_MARKERS`, `SYNC_DIR_MARKERS` and `under_sync_dir`
under their own names; `is_walled` it imports as `wall_is_walled` and wraps, so `guardrail.is_walled`
does NOT exist. Its callers and the validated spec model (`test/lib/guardrail.py`) are unchanged.

**Two matchers over one marker list, and the split is deliberate.** `is_walled` / `under_sync_dir`
match a marker as a bare SUBSTRING anywhere in the path — the semantics the guardrail's 51 validated
write cases were recorded against. `vault_is_walled` / `vault_under_sync_dir` match a path COMPONENT
for the un-anchored markers and a proper path PREFIX for the `$HOME`-anchored ones, and vault
SELECTION uses those. The reason is asymmetry of cost, not taste: under substring matching
`~/notes/dropbox-export`, `~/notes/my.sync-notes` and `~/notes/OneDrive-old` are all "inside a
cloud-sync tree", which is false. On a write that is a recoverable annoyance — re-path the write. On
a vault it is exit 5, the strictest code in the protocol, against the root itself, and there is
nothing to re-path. Both matchers read the SAME lists, so a marker added below is honoured by each;
converging the two (and re-recording the guardrail cases) is a follow-up this fix deliberately does
not take on.
"""
from __future__ import annotations
import os
from pathlib import Path

# The SIBLING-ROOTS anchor (~/work, ~/files, ~/dotfiles) — ADR-015 D4 converged two variables that
# were relocating the same conceptual thing. PLAINKEEP_TEST_HOME first, so the validated spec model
# (test/lib/guardrail.py) and its 51 parity cases keep resolving exactly as before.
HOME = (os.environ.get("PLAINKEEP_TEST_HOME")
        or os.environ.get("PLAINKEEP_ROOTS_HOME")
        or os.environ.get("HOME")
        or "/Users/tamas")

# Paths walled off by LOCATION (§2, §5) — matched case-insensitively, as a SUBSTRING, so a nested
# spelling ("…/Mobile Documents/…") is caught wherever it appears in the path.
WALLED_OFF_MARKERS = [
    f"{HOME}/Library/Mobile Documents", f"{HOME}/iCloud Drive",
    "Mobile Documents", "iCloud", f"{HOME}/Pictures/Photos Library.photoslibrary", f"{HOME}/Pictures",
]
# Cloud-sync trees a .git must never live inside (doctor sync-wall, Part 0.4) — extends the location
# wall with the sync clients; Syncthing's own maintainers warn never to sync a .git tree.
SYNC_DIR_MARKERS = [
    "Mobile Documents", "iCloud", "Dropbox", "Syncthing", ".sync",
    "OneDrive", "Google Drive", "GoogleDrive",
]


def is_walled(path: str) -> bool:
    pl = (path or "").lower()
    return any(m.lower() in pl for m in WALLED_OFF_MARKERS)


def under_sync_dir(path: str) -> bool:
    """True if `path` resolves inside a known cloud-sync tree (iCloud/Dropbox/Syncthing/…). Used by
    doctor's sync-wall (Part 0.4) — a .git tree must never live under one."""
    pl = (path or "").lower()
    return any(m.lower() in pl for m in SYNC_DIR_MARKERS)


# --- the same lists, asked as a question about a LOCATION rather than about a string ---------------
# A marker spelled from $HOME is an ANCHOR: it names one real directory, so it is matched as a path
# prefix. Anything else is a NAME: it names a directory wherever it appears, so it is matched as a
# path component. Both are derived from the lists above rather than restated, so the two matchers
# cannot drift apart on which markers exist — only on how they are compared.
def _anchored(markers: list[str]) -> list[str]:
    return [m for m in markers if m.startswith(HOME)]


def _bare(markers: list[str]) -> list[str]:
    return [m for m in markers if not m.startswith(HOME)]


def _under_prefix(path: str, prefixes: list[str]) -> bool:
    """`path` IS one of `prefixes` or lives under it — on a path boundary, so `$HOME/Pictures` does
    not swallow `$HOME/Pictures-notes`."""
    pl = (path or "").lower()
    return any(pl == p.lower() or pl.startswith(p.lower() + "/") for p in prefixes)


def _has_component(path: str, markers: list[str]) -> bool:
    """A whole path SEGMENT equals a marker. `…/Dropbox/notes` matches; `…/dropbox-export` does not,
    and neither does `…/my.sync-notes` or `…/OneDrive-old`."""
    parts = {p.lower() for p in Path(path or "").parts}
    return any(m.lower() in parts for m in markers)


def vault_is_walled(path: str) -> bool:
    """May a VAULT live here? — the walled-off half. See the module header for why this is not
    `is_walled`."""
    return (_under_prefix(path, _anchored(WALLED_OFF_MARKERS))
            or _has_component(path, _bare(WALLED_OFF_MARKERS)))


def vault_under_sync_dir(path: str) -> bool:
    """May a VAULT live here? — the cloud-sync half."""
    return (_under_prefix(path, _anchored(SYNC_DIR_MARKERS))
            or _has_component(path, _bare(SYNC_DIR_MARKERS)))
