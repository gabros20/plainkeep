// The plainkeep-ui version, compiled into the binary and served by `plainkeep-ui --version` — how
// the engine's `ui` setup layer detects a stale install offline (it compares this against
// bin/ui/version.txt). Keep in sync with ui/package.json "version" and bin/ui/version.txt — the
// release workflow fails on drift.
export const VERSION = "0.2.0";
