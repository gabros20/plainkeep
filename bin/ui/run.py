#!/usr/bin/env python3
"""
plainkeep ui [args…] — the entry shim for the plainkeep terminal UI (proposal: the plainkeep.json/3 TUI).

The TUI ships as a SEPARATE binary (`plainkeep-ui`) that lives OUTSIDE this repo and drives plainkeep
purely over the machine contract (`plainkeep <verb> --json`, `plainkeep complete --json`,
`plainkeep help --json`). This shim just hands the terminal to it: it locates `plainkeep-ui` on PATH
(or via $PLAINKEEP_UI_BIN) and `execv`s it — it never re-resolves the `plainkeep` dispatcher, so there
is no recursion. When the binary isn't installed it prints a blocked-style, exact-remediation hint
(mirroring the setup-layer `status: blocked` / `next` pattern) and exits cleanly — a missing optional
frontend is not a crash. Risk `read` (launching a UI has no side effects of its own; every mutation
the UI performs still goes back through a gated `plainkeep` verb).
"""
import os
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lib import output, paths  # noqa: E402

INSTALL_HINT = ("run `plainkeep setup ui --approved` to download the terminal UI binary into .local/bin, "
                "or set PLAINKEEP_UI_BIN=/path/to/plainkeep-ui; then re-run `plainkeep ui`")


def _executable(p: str | None) -> str | None:
    return p if (p and os.path.isfile(p) and os.access(p, os.X_OK)) else None


def _resolve() -> str | None:
    """Absolute path to an EXECUTABLE plainkeep-ui binary. Resolution order: explicit $PLAINKEEP_UI_BIN
    wins; then the vault-local install `plainkeep setup ui` provisions
    ($PLAINKEEP_HOME/.local/bin/plainkeep-ui); then PATH. `shutil.which` already guarantees an
    executable regular file; the other branches must check the same (isfile + X_OK) so a
    stale/dir/non-exec candidate degrades to the blocked hint rather than crashing the later execv."""
    override = os.environ.get("PLAINKEEP_UI_BIN")
    if override:
        p = override if os.path.isabs(override) else shutil.which(override)
        return _executable(p)
    local = _executable(str(paths.PLAINKEEP_HOME / ".local" / "bin" / "plainkeep-ui"))
    if local:
        return local
    return shutil.which("plainkeep-ui")


def main(argv):
    _, argv = output.parse_argv(argv)
    exe = _resolve()
    if exe:
        # hand off the terminal to the TUI — replaces this process, never returns (no recursion:
        # we exec plainkeep-ui directly, not the `plainkeep` dispatcher). Guard execv so a
        # race/permission error still falls through to the blocked hint instead of a traceback
        # (the "never a crash" contract).
        try:
            os.execv(exe, [os.path.basename(exe), *argv])
        except OSError:
            pass
    data = {"installed": False, "status": "blocked", "next": INSTALL_HINT}
    return output.emit(data, "ui", human=lambda _: f"plainkeep-ui is not installed.\n  {INSTALL_HINT}")


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
