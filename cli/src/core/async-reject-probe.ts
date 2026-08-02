// The rejecting-interception probe for main.ts's last-resort refusal path (fix wave r1, seam widened
// to async). NOT named *.test.ts and not a test at all: it is an ENTRY POINT that registers a
// promise-rejecting interception and then imports ./main.js, so the real main.ts runs its real
// sequence — gate, interception, await, catch, render, exit — in this process.
//
// It has to be a separate process because main.ts calls process.exit() at top level: importing it
// from inside a test would take the test runner down with it. `main.async.test.ts` spawns this file
// and asserts the exit code and the audit line it left behind.
//
// Run directly with: cd cli && PLAINKEEP_HOME=<vault> bun run src/core/async-reject-probe.ts help
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
    // An async interception that fails the way a real one would: not by returning an error result,
    // but by rejecting. Before the seam was widened this could not be expressed at all; the risk it
    // introduces is that an unhandled rejection exits 1, OFF the frozen protocol (0/2/3/4/5).
    await Promise.resolve();
    throw new TypeError("injected async interception failure");
  },
};

await import("./main.js");
