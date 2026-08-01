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

`guardrail.py` re-exports every name below, so its own callers and the validated spec model
(`test/lib/guardrail.py`) are unchanged.
"""
from __future__ import annotations
import os

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
    doctor's sync-wall (Part 0.4) — a .git tree must never live under one — and by vault selection,
    which refuses a data root inside one."""
    pl = (path or "").lower()
    return any(m.lower() in pl for m in SYNC_DIR_MARKERS)
