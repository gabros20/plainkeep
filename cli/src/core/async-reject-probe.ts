// The rejecting-interception probe for main.ts's last-resort refusal path (fix wave r1, seam widened
// to async). NOT named *.test.ts and not a test at all: it is an ENTRY POINT that registers a
// promise-rejecting interception and then imports ./main.js, so the real main.ts runs its real
// sequence — gate, interception, await, catch, render, exit — in this process.
//
// It has to be a separate process because main.ts calls process.exit() at top level: importing it
// from inside a test would take the test runner down with it. `main.async.test.ts` spawns this file
// and asserts the exit code and the audit line it left behind.
//
// Run directly with: cd cli && PLAINKEEP_HOME=<vault> bun run src/core/async-reject-probe.ts v_reject
import { INTERCEPTS } from "./dispatch.js";

INTERCEPTS.v_reject = {
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
