// Probe for interception.ts's process.exit guard, run as a CHILD by interception.test.ts.
//
// It has to be a child because the thing under test ends the process: the guard's whole job is to
// let a dependency's process.exit through to the real one, with a substituted code. An in-process
// assertion could only observe the substitution by preventing it, which would test something else.
//
// argv[2] selects the scenario:
//   dependency-exit-zero  — a "dependency" calls process.exit(0) from inside the window, the shape
//                           @clack/core's block() produces on Ctrl-C while a spinner is up.
//   dependency-exit-off   — the same, with an off-protocol code.
//   restored              — the guard must be UNINSTALLED after the body resolves, so an exit AFTER
//                           the window is the caller's own again. Exits 7, which is off-protocol on
//                           purpose: if the guard were still installed it would come out as 5.
//   returns-off-protocol  — the ordinary path: a body that RETURNS 1 (what cli/src/tui/app.ts does
//                           on a manifest failure) must be clamped, not passed through.
import { runOwningStdio } from "./interception.js";

const scenario = process.argv[2] ?? "";

if (scenario === "restored") {
  await runOwningStdio("probe", async () => 0);
  // Outside the window now. The real process.exit must be back.
  process.exit(7);
}

const r = await runOwningStdio("probe", async () => {
  if (scenario === "dependency-exit-zero") {
    process.stdout.write("before the dependency exits\n");
    process.exit(0);
  }
  if (scenario === "dependency-exit-off") {
    process.exit(1);
  }
  if (scenario === "returns-off-protocol") {
    return 1;
  }
  return 0;
});

// Only the non-exiting scenarios reach here. Print the CoreResult so the parent can assert the
// clamp, then exit with it exactly as main.ts would.
process.stdout.write(`RESULT ${JSON.stringify(r)}\n`);
process.exit(r.code);
