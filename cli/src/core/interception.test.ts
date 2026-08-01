// The seam's enforcement, tested as behavior rather than as shape. Every assertion here corresponds
// to a way the contract was measurably broken at 5fda304 (quality review r1, Q1 and Q2): a returned
// code reaching the shell unclamped, and a dependency's process.exit bypassing the drain, the clamp
// and the return value entirely.
import { test, expect } from "bun:test";
import path from "node:path";
import { clampToProtocol, EXIT_ANOMALOUS, onProtocol, runOwningStdio } from "./interception.js";

const PROBE = path.join(import.meta.dir, "exit-guard-probe.ts");
const CWD = path.join(import.meta.dir, "..", "..");

function runProbe(scenario: string) {
  const r = Bun.spawnSync(["bun", "run", PROBE, scenario], { cwd: CWD, env: { ...process.env } });
  return { code: r.exitCode, stdout: r.stdout.toString(), stderr: r.stderr.toString() };
}

// --------------------------------------------------------------------------------------------------
// The clamp (Q1)
// --------------------------------------------------------------------------------------------------

test("the frozen protocol is exactly 0/2/3/4/5 — 1 is not on it", () => {
  for (const c of [0, 2, 3, 4, 5]) expect(onProtocol(c)).toBe(true);
  // 1 is the code bun produces for an unhandled rejection and the one app.ts returns on a manifest
  // failure. It carries no meaning in this system, which is why it is the headline exclusion.
  for (const c of [1, 6, 7, 127, 200, -1, 128]) expect(onProtocol(c)).toBe(false);
  // Non-integers cannot be an exit status; NaN in particular would exit 0 if passed through.
  expect(onProtocol(NaN)).toBe(false);
  expect(onProtocol(2.5)).toBe(false);
});

test("clampToProtocol passes protocol codes through and redirects everything else to 5", () => {
  for (const c of [0, 2, 3, 4, 5]) expect(clampToProtocol(c)).toBe(c);
  expect(clampToProtocol(1)).toBe(EXIT_ANOMALOUS);
  expect(clampToProtocol(127)).toBe(EXIT_ANOMALOUS);
  expect(clampToProtocol(NaN)).toBe(EXIT_ANOMALOUS);
});

test("runOwningStdio clamps a body that RETURNS an off-protocol code", async () => {
  // This is app.ts's real manifest-failure path: `return 1`.
  expect(await runOwningStdio("t", async () => 1)).toEqual({ code: EXIT_ANOMALOUS });
  // ...and does not disturb a body that returns a legitimate one. 2 is the TUI's own "no terminal"
  // refusal and 0 its clean quit; clamping either would be a regression, not a fix.
  expect(await runOwningStdio("t", async () => 0)).toEqual({ code: 0 });
  expect(await runOwningStdio("t", async () => 2)).toEqual({ code: 2 });
});

test("runOwningStdio restores process.exit even when the body throws", async () => {
  const before = process.exit;
  await expect(
    runOwningStdio("t", async () => {
      throw new Error("boom");
    }),
  ).rejects.toThrow("boom");
  // A guard that outlived its window would silently rewrite a later caller's exit status.
  expect(process.exit).toBe(before);
});

test("runOwningStdio installs the guard only for the body's lifetime", async () => {
  const before = process.exit;
  let inside: typeof process.exit | null = null;
  await runOwningStdio("t", async () => {
    inside = process.exit;
    return 0;
  });
  expect(inside).not.toBe(before);
  expect(process.exit).toBe(before);
});

// --------------------------------------------------------------------------------------------------
// The process.exit guard (Q2) — driven in a child, because the thing under test ends the process.
// --------------------------------------------------------------------------------------------------

test("a dependency's process.exit(0) inside the window does NOT report success", () => {
  // The measured shape: @clack/core's block() calls process.exit(0) on Ctrl-C while a spinner is up,
  // so an INTERRUPTED action used to exit 0 — a silent success for a run that did not complete.
  const r = runProbe("dependency-exit-zero");
  expect(r.code).toBe(EXIT_ANOMALOUS);
  // The output written before the exit still made it out (the guard reports rather than truncates).
  expect(r.stdout).toContain("before the dependency exits");
  // ...and it says so, so the operator can tell this from an ordinary refusal.
  expect(r.stderr).toContain("'probe' ended early — the run did not complete, so its status is 5 rather than 0");
});

test("a dependency's off-protocol process.exit is redirected onto the protocol", () => {
  const r = runProbe("dependency-exit-off");
  expect(r.code).toBe(EXIT_ANOMALOUS);
  expect(r.stderr).toContain("'probe' ended early — the run did not complete, so its status is 5 rather than 1");
});

test("the guard is uninstalled after the window, so a later exit is the caller's own", () => {
  // Exits 7 AFTER runOwningStdio resolved. 7 is off-protocol on purpose: a still-installed guard
  // would rewrite it to 5, and this assertion is what distinguishes "restored" from "always 5".
  expect(runProbe("restored").code).toBe(7);
});

test("the ordinary return path is clamped end-to-end, through a real process exit", () => {
  const r = runProbe("returns-off-protocol");
  expect(r.code).toBe(EXIT_ANOMALOUS);
  // The CoreResult itself carried the clamped code — not merely the process's exit status.
  expect(r.stdout).toContain(`RESULT {"code":${EXIT_ANOMALOUS}}`);
});
