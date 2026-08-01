// The happy-path twin of async-reject-probe.ts: an interception that RESOLVES after a turn of the
// event loop. Same reason for being a separate entry point (main.ts exits the process at top level).
// It pins that main.ts genuinely awaits — an un-awaited promise would render as "[object Promise]"
// and exit 0 rather than carrying the interception's own stdout and exit code.
//
// Run directly with: cd cli && PLAINKEEP_HOME=<vault> bun run src/core/async-resolve-probe.ts v_reject
import { INTERCEPTS } from "./dispatch.js";

INTERCEPTS.v_reject = {
  comparable: false,
  run: async () => {
    await new Promise((resolve) => setTimeout(resolve, 5));
    // A non-zero, on-protocol code (confirm) so the assertion cannot be satisfied by the default 0.
    return { stdout: "answered after a turn of the event loop", code: 3 };
  },
};

await import("./main.js");
