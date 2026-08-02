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
`~/notes/my.sync-notes` and `~/notes/not-iCloudy` are "inside a cloud-sync tree", which is false. On
a write that is a recoverable annoyance — re-path the write. On a vault it is exit 5, the strictest
code in the protocol, against the root itself, and there is nothing to re-path. Both matchers read
the SAME lists, so a marker added below is honoured by each; converging the two (and re-recording the
guardrail cases) is a follow-up, registered in `docs/followups.md`.

**A component may also START WITH a marker, and that costs us three false positives on purpose.**
The first cut of the component matcher required a component to EQUAL a marker, and that was
fail-OPEN against the spellings the sync clients actually use: since Ventura the macOS mount point
for OneDrive and Google Drive is `~/Library/CloudStorage/<Provider>-<Account>`
(`OneDrive-Personal`, `GoogleDrive-me@gmail.com`), and Dropbox names its own folders
`Dropbox (Acme Inc)`, `Dropbox Personal` and `Dropbox.nosync`. Not one of those is a component equal
to a marker, so every one of them SELECTED — a git tree inside a live sync client, which is the exact
corruption this wall exists to prevent. So a component now matches when it equals a marker *or*
begins with one followed by a separator (`-`, ` `, `.`, `_`), and `$HOME/Library/CloudStorage` is
carried as an anchored marker besides, which catches both CloudStorage providers precisely.

The trade, stated rather than discovered later: `~/notes/dropbox-export`, `~/notes/OneDrive-old` and
`~/notes/icloud-archive` ARE whole components beginning with a marker plus a separator, so they are
refused again. They cannot be told apart from the real mount points by spelling alone. **Where the
two cannot be separated, prefer the refusal** — a false positive costs the operator one exit 5
carrying a `vault rebind` hint they can act on, while a false negative is silent and puts a `.git`
inside a sync client. Fail-closed and visible beats fail-open and quiet. (`~/notes/my.sync-notes`,
`~/notes/not-iCloudy`, `~/Pictures-notes` and `~/Picturesque/notes` are unaffected: none of them
begins with a marker plus a separator.)
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
#
# `$HOME/Library/CloudStorage` is the ANCHOR and it is not redundant with the provider names below:
# since macOS Ventura it is THE mount point for OneDrive, Google Drive, Box and Egnyte, and the
# per-account directory under it is spelled `<Provider>-<Account>`. As an anchored prefix it catches
# every provider — including ones not named below — with no false positives at all, because it names
# one real directory rather than a word.
SYNC_DIR_MARKERS = [
    f"{HOME}/Library/CloudStorage",
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
    """The `$HOME`-spelled markers, CANONICALIZED.

    `_under_prefix` compares these against `vaultreg.canonical(root)`, so they have to live in the
    same space or the comparison is between two spellings of one directory and silently answers
    "no". A symlinked or network-mounted `$HOME` is the live case: with `$HOME=/var/…` whose realpath
    is `/private/var/…`, every anchored marker missed and `~/iCloud Drive` and `~/Pictures` became
    selectable. The substring matchers above are deliberately NOT canonicalized — they compare
    against a raw path, not a canonical one, and their 51 recorded verdicts were taken that way."""
    return [os.path.realpath(m) for m in markers if m.startswith(HOME)]


def _bare(markers: list[str]) -> list[str]:
    return [m for m in markers if not m.startswith(HOME)]


def _under_prefix(path: str, prefixes: list[str]) -> bool:
    """`path` IS one of `prefixes` or lives under it — on a path boundary, so `$HOME/Pictures` does
    not swallow `$HOME/Pictures-notes`."""
    pl = (path or "").lower()
    return any(pl == p.lower() or pl.startswith(p.lower() + "/") for p in prefixes)


# The characters a sync client puts between its own name and the account/team it holds:
# `OneDrive-Personal`, `Dropbox Personal`, `Dropbox.nosync`, `GoogleDrive-me@gmail.com`. A marker
# followed by one of these still names THAT client's tree.
_MARKER_SEPARATORS = ("-", " ", ".", "_")


def _has_component(path: str, markers: list[str]) -> bool:
    """A whole path SEGMENT equals a marker, or begins with one plus a separator.

    `…/Dropbox/notes`, `…/Dropbox (Acme Inc)/notes`, `…/OneDrive-Personal/notes` and
    `…/Dropbox.nosync/notes` all match — the last three are the real mount points, and requiring
    equality made them invisible. `…/my.sync-notes` and `…/not-iCloudy` do NOT match: neither begins
    with a marker.

    `…/dropbox-export`, `…/OneDrive-old` and `…/icloud-archive` DO match, and that is the accepted
    cost — see the module header. They are indistinguishable from `OneDrive-Personal` by spelling,
    and the refusal they get is exit 5 carrying a `vault rebind` hint, which the operator can act on;
    the alternative was a silent yes to a vault living inside a sync client."""
    parts = [p.lower() for p in Path(path or "").parts]
    prefixes = tuple(m.lower() + sep for m in markers for sep in _MARKER_SEPARATORS)
    exact = {m.lower() for m in markers}
    return any(p in exact or p.startswith(prefixes) for p in parts)


def vault_is_walled(path: str) -> bool:
    """May a VAULT live here? — the walled-off half. See the module header for why this is not
    `is_walled`."""
    return (_under_prefix(path, _anchored(WALLED_OFF_MARKERS))
            or _has_component(path, _bare(WALLED_OFF_MARKERS)))


def vault_under_sync_dir(path: str) -> bool:
    """May a VAULT live here? — the cloud-sync half."""
    return (_under_prefix(path, _anchored(SYNC_DIR_MARKERS))
            or _has_component(path, _bare(SYNC_DIR_MARKERS)))
