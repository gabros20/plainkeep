"""
launchdfx.py — TEST-ONLY: a fake `launchctl` and a redirected LaunchAgents directory.

Read this before writing any test that touches activation (ADR-022).

`plainkeep job enable` is the first thing in this product that writes OUTSIDE the three roots and
changes the state of a running system daemon. Both of those are real on the developer's own machine:
a suite that forgot to redirect them would install plists into the human's `~/Library/LaunchAgents`
and bootstrap them into the human's live login session — and, being launchd, they would then keep
running after the suite finished, on a fixture vault in `/tmp` that no longer exists.

So the seam is not optional and it is not per-check. `install()` returns an env dict that a suite
merges into EVERY invocation:

    fake = launchdfx.install(tmpdir)
    env = {**os.environ, "PLAINKEEP_HOME": str(vault), **fake.env}

`PLAINKEEP_LAUNCHCTL` points at the script below and `PLAINKEEP_LAUNCH_AGENTS_DIR` at a temp
directory (both documented in `docs/machine-contract.md §9`). The script RECORDS its argv, so
ordering — bootout before bootstrap — and the exact service target are asserted from a log rather
than assumed, and it keeps a one-file-per-label "loaded" state so `print` can answer honestly.

It also models the failure that matters: `bootout` of a label that was never loaded exits nonzero,
which is precisely the case `enable` must ignore and `disable` must survive.
"""
from __future__ import annotations
import stat
from dataclasses import dataclass
from pathlib import Path

SCRIPT = """#!/bin/sh
printf '%s\\n' "$*" >> "$PK_FAKE_LOG"
mkdir -p "$PK_FAKE_STATE"
case "$1" in
  bootstrap)
    label=`basename "$3" .plist`
    touch "$PK_FAKE_STATE/$label"
    exit 0 ;;
  bootout)
    label=${2##*/}
    if [ -e "$PK_FAKE_STATE/$label" ]; then rm -f "$PK_FAKE_STATE/$label"; exit 0; fi
    exit 3 ;;
  print)
    label=${2##*/}
    if [ -e "$PK_FAKE_STATE/$label" ]; then exit 0; fi
    exit 1 ;;
esac
exit 1
"""


@dataclass
class Fake:
    exe: Path
    log: Path
    state: Path
    agents: Path

    @property
    def env(self) -> dict[str, str]:
        """What every invocation under test must carry."""
        return {"PLAINKEEP_LAUNCHCTL": str(self.exe),
                "PLAINKEEP_LAUNCH_AGENTS_DIR": str(self.agents),
                "PK_FAKE_LOG": str(self.log), "PK_FAKE_STATE": str(self.state)}

    def calls(self) -> list[str]:
        """Every launchctl invocation so far, one argv per line, in order."""
        try:
            return [ln for ln in self.log.read_text(encoding="utf-8").splitlines() if ln.strip()]
        except OSError:
            return []

    def clear(self) -> None:
        self.log.write_text("", encoding="utf-8")

    def mark_loaded(self, label: str) -> None:
        """Pretend launchd already knows a label, without going through `enable`."""
        self.state.mkdir(parents=True, exist_ok=True)
        (self.state / label).write_text("", encoding="utf-8")


def install(tmp) -> Fake:
    """Write the fake binary and its log/state/LaunchAgents directories under `tmp`."""
    tmp = Path(tmp)
    tmp.mkdir(parents=True, exist_ok=True)
    exe = tmp / "fake-launchctl"
    exe.write_text(SCRIPT, encoding="utf-8")
    exe.chmod(exe.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    fake = Fake(exe=exe, log=tmp / "launchctl.log", state=tmp / "launchctl-state",
                agents=tmp / "LaunchAgents")
    fake.log.write_text("", encoding="utf-8")
    fake.state.mkdir(exist_ok=True)
    fake.agents.mkdir(exist_ok=True)
    return fake
