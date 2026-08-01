// interception.ts — what makes dispatch.ts's STDIO-OWNING interception contract ENFORCEABLE rather
// than merely written down.
//
// The seam says an stdio-owning interception "writes to process.stdout/stderr DIRECTLY for the call's
// lifetime and returns only `{ code }`". Task 6 shipped that as a promise the seam could not keep, in
// two ways a quality review measured end to end:
//
//   1. The returned code was passed through UNCLAMPED, so a TUI returning 1 (which cli/src/tui/app.ts
//      does on a manifest-load failure) reached the shell as exit 1 — off the frozen protocol
//      (0/2/3/4/5), from the very layer whose job is to keep everything on it.
//   2. A DEPENDENCY can end the process from inside the window. @clack/core's `block()` — installed
//      while a spinner is up — calls `process.exit(0)` on Ctrl-C, which is the only `process.exit` in
//      that package. When it fires, the interception never resumes: the drain never runs, `{ code }`
//      is never returned, and an INTERRUPTED action reported success.
//
// This module owns both, in one place, because Task 7's MCP session needs exactly the same guard for
// a much higher-stakes stdout: there, an unguarded `process.exit` truncates a JSON-RPC frame
// mid-write and the peer sees a protocol error rather than an exit code.
import fs from "node:fs";
import type { CoreResult } from "./cli.js";

// The frozen exit protocol (Part 0.3): 0 ok · 2 usage · 3 confirm · 4 not-found · 5 deny. Note what
// is NOT here: 1. That is the whole point — 1 is what bun produces for an unhandled rejection and
// what a careless `return 1` produces, and it carries no meaning in this system.
const PROTOCOL = new Set([0, 2, 3, 4, 5]);

// Where anything off-protocol, and anything anomalous, lands.
//
// 5 rather than 2, and the reasoning is main.ts's, reused deliberately rather than re-invented: its
// top-level guard maps an internal error to EXIT_DENY because that is "the refusal an internal error
// deserves: never a silent success, never clearable with --yes". Every case this module redirects is
// that same class — the interception did not complete the thing the user asked for, for a reason the
// user cannot fix by retyping the command. 2 (usage) would tell them to fix their command line, which
// is wrong and actively misleading when the real cause is a vault that cannot produce a manifest.
export const EXIT_ANOMALOUS = 5;

export function onProtocol(code: number): boolean {
  return Number.isInteger(code) && PROTOCOL.has(code);
}

// The clamp. Applied to whatever an interception RETURNS, so a TUI (or an MCP handler) that invents a
// code cannot put it on the wire. On-protocol codes pass through untouched, which is what preserves
// the TUI's deliberate 2 for "no terminal" and its 0 for a clean quit.
export function clampToProtocol(code: number): number {
  return onProtocol(code) ? code : EXIT_ANOMALOUS;
}

const DRAIN_TIMEOUT_MS = 2000;

// Bounded for the same reason main.ts's drain is: a reader that has stopped reading with a full pipe
// can never let the drain complete, and a hung command is worse than a lost byte.
export async function drainStream(stream: NodeJS.WriteStream): Promise<void> {
  if (stream.writableLength === 0) return;
  await Promise.race([
    new Promise<void>((resolve) => {
      stream.write("", () => resolve());
    }),
    new Promise<void>((resolve) => {
      setTimeout(resolve, DRAIN_TIMEOUT_MS).unref();
    }),
  ]);
}

// The honest half of the exit guard. `process.exit` is SYNCHRONOUS and cannot await, so when a
// dependency calls it there is no way to run the real (async) drain — that is a property of the
// platform, not a gap in this code, and pretending otherwise is how the previous version of this
// comment went wrong.
//
// What is possible is to refuse to hide it. Measured on bun 1.3.14 / macOS arm64: stdout writes
// complete synchronously on a TTY and on a pipe alike, so `writableLength` is 0 here in every case
// this system produces and nothing is lost. If that ever stops being true, this writes a diagnostic
// with fs.writeSync — which bypasses the stream buffer entirely and therefore still works when the
// stream itself is the thing that is backed up — so a truncation becomes a visible line rather than
// silently missing output.
function reportUndrainedBytes(): void {
  const pending = process.stdout.writableLength + process.stderr.writableLength;
  if (pending === 0) return;
  try {
    fs.writeSync(
      2,
      // NOTE the wording avoids spelling the exit call literally. bundle-exit-sites.test.ts audits
      // the built bundle for `.exit(` and asserts exactly two call sites; a string literal
      // containing that text is a decoy the audit would have to carve out, which weakens it.
      `plainkeep-core: ${pending} byte(s) of output may be lost — a dependency ended the process ` +
        `while output was still queued, and a synchronous exit cannot wait for it\n`,
    );
  } catch {
    // a diagnostic that cannot be written must not become the failure it is reporting
  }
}

/**
 * Run an stdio-owning interception body with the seam's contract ENFORCED.
 *
 * `body` owns the real stdout/stderr for its lifetime and returns an exit code. Around it:
 *
 *  * `process.exit` is REPLACED for the body's lifetime. A dependency that calls it no longer decides
 *    this process's exit status: the guard records what it asked for, reports anything it is about to
 *    truncate, maps the outcome onto the frozen protocol, and only then calls the real exit. Measured
 *    in bun: wrapping `process.exit` works, where a `process.on("exit")` hook that reassigns
 *    `process.exitCode` does NOT (bun exits with the original code regardless).
 *  * the returned code is CLAMPED, so nothing off-protocol escapes even on the normal path.
 *  * stdout is drained before resolving, which is the seam's stated precondition.
 *  * `process.exit` is restored on the way out, including when the body throws — the guard must not
 *    outlive the window it belongs to, or a later caller's exit would be silently rewritten.
 *
 * A dependency-initiated exit has its INTENT MAPPED onto the protocol — it is not blanket-treated as
 * a failure, and the distinction matters:
 *
 *   * `process.exit(0)` is a DELIBERATE QUIT. The one call site that reaches this in the shipped
 *     graph is @clack/core's `block()` keypress handler on Ctrl-C, i.e. the user chose to stop.
 *     Quitting an interactive program you chose to quit is not an error — it exits 0, exactly as the
 *     bash floor does, and no diagnostic is printed.
 *   * anything OFF the protocol (1, 127, …) becomes EXIT_ANOMALOUS, because those codes mean nothing
 *     in this system and must never reach the shell.
 *
 * An earlier version reported 5 for EVERY dependency exit including 0. That was wrong three ways at
 * once: it told the shell an anomaly had occurred when the user had simply quit, it made clack print
 * "Something went wrong" (its own exit hook renders that for any code > 1) after a normal cancel, and
 * it diverged from the floor, where Ctrl-C exits 0 and says "Canceled".
 *
 * WHETHER 0-AS-CANCEL IS DISTINGUISHABLE FROM 0-AS-ERROR — measured, not assumed, because the answer
 * decides whether honouring 0 is safe. `bun build` of src/core/main.ts contains exactly TWO real
 * `process.exit` call sites: clack's `block()` Ctrl-C handler (`process.exit(0)`) and main.ts's own
 * final `process.exit(r.code)`, which is outside this window. No error path in the graph exits 0, so
 * within the shipped dependency set a dependency-initiated 0 unambiguously means "the user quit".
 *
 * That is a property of a DEPENDENCY SET, so it is ENFORCED, not documented: bundle-exit-sites.test.ts
 * rebuilds the bundle and fails if a third call site ever appears, naming it. Without that, a `bun
 * add` or a clack bump introducing an error path that exits 0 would leave every other test green
 * while this function silently reported that failure as success — and the tests that look like they
 * cover this (the pty cancel row, the dependency-exit-zero probe) do not: they assert Ctrl-C still
 * exits 0, which stays true in exactly that scenario.
 */
export async function runOwningStdio(verb: string, body: () => Promise<number>): Promise<CoreResult> {
  // The ORIGINAL reference, not `process.exit.bind(process)`. Restoring a bound copy would put a
  // different function back than the one that was taken, so nesting or simply calling this twice
  // would stack a new wrapper each time (measured: "bound bound bound exit") and the guard would
  // never truly be uninstalled. Invoked below with .call(process) to keep the receiver right.
  const realExit = process.exit;
  const guarded = ((code?: number): never => {
    // `process.exit()` with no argument means 0, same as node/bun.
    const requested = typeof code === "number" ? code : 0;
    const final = clampToProtocol(requested);
    reportUndrainedBytes();
    // ONLY when the code was actually changed. A deliberate Ctrl-C quit is the routine path through
    // here and it must be silent — printing a line every time someone quits is noise, and the
    // previous version of this guard did exactly that.
    if (final !== requested) {
      // Addressed to the OPERATOR, not to the next maintainer: "a dependency ended the process from
      // inside an stdio-owning interception" is implementation detail in the face of someone whose
      // command just stopped. It has to say something, though — a bare exit 5 with no line at all is
      // the "refusals teach" rule broken in the other direction.
      try {
        fs.writeSync(
          2,
          `plainkeep: '${verb}' ended early with a status this system has no meaning for ` +
            `(${requested}); reporting ${final}\n`,
        );
      } catch {
        // never let the diagnostic be the failure
      }
    }
    return realExit.call(process, final);
  }) as typeof process.exit;

  process.exit = guarded;
  try {
    const returned = await body();
    await drainStream(process.stdout);
    return { code: clampToProtocol(returned) };
  } finally {
    process.exit = realExit;
  }
}
