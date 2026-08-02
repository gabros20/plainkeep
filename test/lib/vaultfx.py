"""
vaultfx.py — TEST-ONLY: turn a throwaway directory into a real vault.

Since ADR-014 Phase 2 Task 1b a directory is not a data root because a variable points at it.
`PLAINKEEP_HOME` is VALIDATED: it must exist, must not sit inside a walled-off or cloud-sync tree,
and must carry `.plainkeep/vault.json`. A root without one refuses with exit 2 before the gate runs,
before the resolver scans plugins, and before any verb is spawned.

So every suite that builds a fixture vault has to build a REAL one. That is the point rather than
the inconvenience: a fixture that could not pass validation was standing in for something that has
to. One spelling of the marker lives here so the suites cannot drift from `bin/lib/vaultreg.py`.

The marker is written DIRECTLY rather than by shelling out to `plainkeep vault register`, and the
difference matters: registering also writes a REGISTRY, and the registry lives outside every vault —
in the developer's real `~/.config` unless `PLAINKEEP_CONFIG_HOME` is set. A fixture must never
touch it. Registration is only needed for the mechanisms that go THROUGH the registry (`--vault` and
the marker walk-up); `PLAINKEEP_HOME` needs the marker alone, which is what every fixture here uses.
"""
from __future__ import annotations
import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

MARKER_SCHEMA = "plainkeep.vault/1"


def mark_vault(root) -> str:
    """Write `<root>/.plainkeep/vault.json` and return the new vault id. Idempotent per call site in
    the sense that it always writes a FRESH id — two fixtures are two vaults, never accidentally one.
    """
    d = Path(root) / ".plainkeep"
    d.mkdir(parents=True, exist_ok=True)
    vid = str(uuid.uuid4())
    (d / "vault.json").write_text(
        json.dumps({"schema": MARKER_SCHEMA, "id": vid,
                    "created": datetime.now(timezone.utc).isoformat(timespec="seconds")},
                   indent=2) + "\n",
        encoding="utf-8")
    return vid


def mark_engine_vault(root, repo) -> str:
    """A marked fixture vault that also CARRIES THE ENGINE, by symlink. Returns the vault id.

    For the handful of checks that run the REAL dispatcher (`REPO/plainkeep …`) rather than a verb's
    `run.py`. Both dispatchers still look for the engine UNDER the selected root — the floor's
    `$PK/bin/lib/guardrail.py`, the core's `opsHome()` — so those checks cannot point at a bare
    marked directory; they used to point at the checkout instead, which is the developer's own
    registered vault and which they appended an audit line to on every green run.

    Symlinking `bin/` and the `plainkeep` shim satisfies the resolver without copying the tree, and
    every path the dispatch WRITES (the audit log, an index, a note) is under `root` and therefore
    thrown away with it. The engine-in-vault assumption on the resolver side is out of Task 1b's
    scope (report §6.3); this is how a test lives with it hermetically until it is not."""
    src = Path(repo)
    dst = Path(root)
    (dst / "bin").symlink_to(src / "bin")
    (dst / "plainkeep").symlink_to(src / "plainkeep")
    return mark_vault(dst)


def hermetic_config(tmp) -> str:
    """A `PLAINKEEP_CONFIG_HOME` that is guaranteed to hold no registry.

    Needed by any test that invokes plainkeep WITHOUT `PLAINKEEP_HOME`: discovery then falls through
    to the marker walk-up and the registry DEFAULT, and the default on a developer's machine is a
    real vault full of real notes. Setting this makes "nothing selected a root" mean what the test
    says it means."""
    p = Path(tmp) / "no-registry"
    os.makedirs(p, exist_ok=True)
    return str(p)
