// ui.ts — `plainkeep ui` (and bare `plainkeep` on a terminal) answered IN-PROCESS: the clack TUI
// that used to ship as a separate `plainkeep-ui` binary now runs inside this one.
//
// This is a RELOCATION, not a redesign. cli/src/tui/ is imported unchanged; the loop, the prompts
// and the one-door rule (every action re-enters `plainkeep <verb> --json` as a child process) are
// exactly what the standalone binary did. The floor path is untouched: `PLAINKEEP_CORE=off` still
// runs bin/ui/run.py, which still execs the separately-built plainkeep-ui.
//
// It is the first STDIO-OWNING interception (the shape dispatch.ts's seam comment describes): the
// TUI writes to the real stdout for its whole lifetime and this returns only `{ code }`. Buffering a
// TUI into `CoreResult.stdout` is not merely impractical, it is wrong — the output is a terminal
// being painted, and it has to appear WHILE the user is answering prompts, not after.
import type { CoreResult } from "./cli.js";
import { VERSION } from "../tui/version.js";

// `plainkeep ui --version` / `-v`, answered HEADLESSLY and before anything touches the terminal —
// the same order the standalone binary uses (cli/src/tui/index.ts checks these before its TTY
// guard, because a headless prober must get an answer rather than the "run me in a real terminal"
// refusal).
//
// WHY IN-PROCESS RATHER THAN FALLING THROUGH TO bin/ui/run.py: under the core, the TUI that `ui`
// launches IS this binary, compiled from cli/src/tui/. Falling through would answer with the
// version of a DIFFERENT artifact — the standalone plainkeep-ui — which may be absent (the fall
// through then prints an install hint instead of a version) or a stale copy someone installed
// months ago. Reporting the version of the code that will actually run is the only honest answer,
// and VERSION is read from the same cli/src/tui/version.ts the standalone compiles in, pinned
// against bin/ui/version.txt by tui/version.test.ts.
//
// setuplib's staleness probe is unaffected either way, which is the fact that made this decision
// free rather than a trade: `_status_ui` calls `_ui_installed_version(exe)` where `exe` comes from
// `_ui_installed()` — the plainkeep-ui BINARY path ($PLAINKEEP_UI_BIN, then
// $PLAINKEEP_HOME/.local/bin/plainkeep-ui, then PATH). It never goes through the `plainkeep ui`
// verb, so no dispatcher mode can change what it sees.
const VERSION_FLAGS = new Set(["--version", "-v"]);

export function isVersionProbe(args: string[]): boolean {
  return args.some((a) => VERSION_FLAGS.has(a));
}

// What makes bare `plainkeep` a TUI launch instead of the default verb.
//
// BOTH streams, and the predicate is the TUI's own guard (cli/src/tui/app.ts: `!process.stdin.isTTY
// || !process.stdout.isTTY`) rather than a second, subtly different rule written here. The TUI needs
// stdin to read keys and stdout to paint; either one being a pipe means there is no human at the
// other end. Reusing the same condition means bare `plainkeep` can never route to a TUI that would
// then refuse itself with exit 2.
//
// WHY THIS MUST NOT WIDEN: non-TTY bare `plainkeep` prints help, and scripts and agents depend on
// that. `plainkeep | cat`, `plainkeep > out`, a CI job, a subprocess with piped stdio — all keep the
// default verb. It also keeps the core-parity dispatcher matrix honest by construction: the harness
// runs both sides with captured (piped) stdio, so the predicate is false there and bare argv
// compares against the bash floor exactly as it did before this existed.
export function isInteractiveTerminal(): boolean {
  return Boolean(process.stdin.isTTY) && Boolean(process.stdout.isTTY);
}

// ONLY a completely empty argv. `plainkeep ""` is NOT bare — bash's `${1:-help}` substitutes on
// unset OR empty, so both spell `help`, but the empty string is a real argument the user typed and
// silently turning it into a TUI would be a different command than the one they ran. The brief's
// rule is "bare argv on a TTY resolves to the TUI; everything else keeps today's default-verb
// behavior", and this is the narrowest reading of "bare".
export function bareTtyLaunchesUi(argv: string[]): boolean {
  return argv.length === 0 && isInteractiveTerminal();
}

// SIGNAL DISPOSITION — the decision is to INSTALL NOTHING, and it is a decision, not an omission.
//
// The tempting move is to install a SIGINT handler here that restores and re-raises, mirroring what
// main.ts does for a signalled CHILD (dispatch.ts's spawnVerb + the re-raise). That would be wrong,
// because the thing that made the child's re-raise necessary is gone: with the TUI in-process there
// IS no child, so the process the terminal signals is already the process whose wait status the
// caller reads. Installing a handler could only ADD divergence from the floor.
//
// What the floor's disposition actually is — measured end to end on a real pty, floor
// (PLAINKEEP_CORE=off, bin/ui/run.py → plainkeep-ui) vs core, over three stimuli
// (.orchestrate/raw/task6-signal-baseline.log and task6-signal-after.log):
//
//   * SIGINT delivered at the menu, before any spinner exists — BOTH sides die by SIGINT
//     (WIFSIGNALED, subprocess returncode -2). Nothing in plainkeep set that; it is the default.
//   * Ctrl-C typed at a prompt — NOT a signal at all on either side. @clack/core puts the tty in raw
//     mode for a prompt (ISIG off), so the terminal delivers the BYTE 0x03, which clack turns into a
//     prompt cancel; the loop breaks and both sides exit 0.
//   * SIGINT after any action has run — BOTH sides SURVIVE it. @clack/prompts' spinner() registers
//     `process.on("SIGINT", …)` (it stops the spinner frame), and registering any SIGINT listener
//     removes node/bun's default terminating disposition for the rest of the process's life.
//
// The third row is why this had to be measured rather than reasoned about: before the absorption it
// was the one row where floor and core DISAGREED — the floor survived (the listener is in
// plainkeep-ui) while the core died -2 (plainkeep-core was a parent with no listener, and the signal
// goes to the whole foreground process group). Running the TUI in-process closes that divergence for
// free, because the listener is now registered in the process that receives the signal. All three
// rows agree after this change.
//
// So the disposition is whatever @clack/prompts makes it, identically in both modes, which is the
// definition of matching the floor. If a future clack stops registering that handler, both modes
// move together and stay equal — which is the property worth having.

/**
 * The `ui` interception, registered into dispatch.ts's INTERCEPTS post-gate and post-normalization —
 * so the audit line is appended for `ui` exactly as when bin/ui/run.py served it, and every spelling
 * that resolves to this verb (including the bare-TTY route, which rewrites argv to ["ui"] BEFORE the
 * gate) is recorded.
 *
 * Returns only `{ code }`: the TUI owned stdout for its lifetime.
 */
export async function uiIntercept(args: string[]): Promise<CoreResult> {
  if (isVersionProbe(args)) return { stdout: VERSION, code: 0 };

  // DYNAMIC import, and it is load-bearing rather than stylistic. The TUI graph is @clack/prompts +
  // execa + picocolors: 162 modules against the dispatcher's 8 (measured with `bun build --compile`
  // on both entries). A static import would EVALUATE all of it on every single invocation of this
  // binary — including `__complete`, the TAB path whose whole reason for being intercepted is
  // latency. Behind a dynamic import the modules are still bundled into the binary but are only
  // evaluated when someone actually opens the TUI.
  const { useSelfExec } = await import("../tui/plainkeep.js");
  const { main } = await import("../tui/app.js");

  // run.md D8: TUI re-entry self-execs THIS binary's own path, never a PATH lookup for "plainkeep".
  // process.execPath is the compiled core binary, which is a dispatcher, so every action the TUI
  // runs goes through the gate and the audit log — one door — and does so even when no `plainkeep`
  // is on PATH at all. An explicit $PLAINKEEP_BIN still wins inside resolvePlainkeepBin().
  useSelfExec(process.execPath);

  // A throw from the TUI must not escape as an unhandled rejection: main.ts awaits this inside its
  // try, so a rejection already maps to the deterministic deny/5 — but the standalone entry prints
  // the message and exits 1, and losing that message would make a TUI crash unreadable. Print it the
  // same way, then hand back a code on the frozen protocol instead of the standalone's off-protocol 1.
  let code: number;
  try {
    code = await main();
  } catch (e) {
    console.error((e as Error)?.message ?? String(e));
    code = EXIT_USAGE;
  }

  // DECISION: stdout is drained HERE, before resolving, rather than relying on main.ts.
  //
  // WHAT ACTUALLY GUARANTEES THE FINAL FRAME TODAY — stated first, because it is not this call.
  // Measured on bun 1.3.14 / macOS arm64: after a single 200 KB `process.stdout.write`,
  // `writableLength` is 0 immediately, on a TTY *and* on a pipe alike
  // (.orchestrate/raw/task6-drain-measure.log). Bun's stdout writes complete synchronously, so there
  // is nothing queued at this point and this drain returns at its `writableLength === 0` early exit
  // without awaiting anything. The end-to-end proof that the last frame survives is empirical rather
  // than architectural: test/run_tui_pty.py reads the outro line OFF THE TERMINAL and only then
  // reaps the process, so a truncating exit would redden that check.
  //
  // WHY IT IS HERE ANYWAY, honestly framed (Task 5's review, N2, flagged the previous version of
  // this argument in main.ts for claiming a truncation nobody had reproduced — this is not a second
  // helping of that): it is a CHEAP PRECONDITION, not a fix for a demonstrated bug. dispatch.ts's
  // seam contract says an stdio-owning interception "must have drained before it resolves", and the
  // only way that contract can be honoured by construction rather than by the runtime happening to
  // flush synchronously is for the owner to check. If a future bun buffers stdout asynchronously —
  // or someone runs the TUI with stdout redirected to a slow sink — this is already in the right
  // place. It costs one property read in the common case.
  await drainStdout();
  return { code };
}

// The gate protocol's usage code, which is what a TUI that died of an internal error is: it did not
// complete the thing you asked for. Never 1 — that is off the frozen protocol (0/2/3/4/5) and is
// exactly the code main.ts's guard exists to keep this binary from ever producing.
const EXIT_USAGE = 2;

const DRAIN_TIMEOUT_MS = 2000;

// Bounded for the same reason main.ts's is: a reader that has stopped reading with a full pipe can
// never let the drain complete, and a hung TUI on exit is worse than a lost byte.
async function drainStdout(): Promise<void> {
  const stream = process.stdout;
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
