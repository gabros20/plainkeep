// The happy-path twin of async-reject-probe.ts: an interception that RESOLVES after a turn of the
// event loop. Same reason for being a separate entry point (main.ts exits the process at top level).
// It pins that main.ts genuinely awaits — an un-awaited promise would render as "[object Promise]"
// and exit 0 rather than carrying the interception's own stdout and exit code.
//
// Run directly with: cd cli && PLAINKEEP_HOME=<vault> bun run src/core/async-resolve-probe.ts help
// The verb intercepted is a REAL engine verb (`help`, read-class, and per run.md D6
// deliberately NOT intercepted in production). It has to be: this file reaches main.ts,
// which reaches runCore(), which ACTIVATES the engine from the binary's own location before
// anything else runs (Phase 2 Task 2) — so a synthetic verb planted in a temp tree would not
// be on the surface the gate consults, and the gate would answer not-found before the
// interception this probe exists to drive was ever reached.
import { INTERCEPTS } from "./dispatch.js";

INTERCEPTS.help = {
  comparable: false,
  run: async () => {
    await new Promise((resolve) => setTimeout(resolve, 5));
    // A non-zero, on-protocol code (confirm) so the assertion cannot be satisfied by the default 0.
    return { stdout: "answered after a turn of the event loop", code: 3 };
  },
};

await import("./main.js");
