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
import { EXIT_ANOMALOUS, runOwningStdio } from "./interception.js";
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
//   * SIGTERM after any action has run — BOTH sides SURVIVE that too. The same spinner() call
//     registers a SIGTERM listener beside the SIGINT one.
//
// The third row is why this had to be measured rather than reasoned about: before the absorption it
// was the one row where floor and core DISAGREED — the floor survived (the listener is in
// plainkeep-ui) while the core died -2 (plainkeep-core was a parent with no listener, and the signal
// goes to the whole foreground process group). Running the TUI in-process closes that divergence for
// free, because the listener is now registered in the process that receives the signal.
//
// WHAT "INSTALL NOTHING" ACTUALLY COSTS, stated plainly because it is easy to read this as harmless:
// once ANY action has run, plainkeep-core cannot be stopped by SIGINT or by SIGTERM for the rest of
// the session. A supervisor, a script, or a `kill` that waits on this process gets NOTHING until it
// escalates to SIGKILL — and there is no graceful shutdown on SIGTERM, because the handler that
// swallows it only redraws a spinner frame. Floor parity holds (the same clack runs inside
// plainkeep-ui), so this is a DISCLOSURE, not a regression introduced here — but it is a real
// operational property of `plainkeep ui` and it is not something anybody chose.
//
// AND THE MECHANISM IS A LEAK, NOT A DESIGN. @clack/prompts 0.7.0 contains zero removeListener calls,
// so every spinner() permanently adds five process listeners. The behavior above is downstream of a
// dependency forgetting to clean up, which is exactly the kind of thing a version bump fixes. Do NOT
// treat "both modes move together" as the safety net — an earlier version of this comment claimed
// that and it is false: the floor's TUI is a SEPARATELY BUILT, SEPARATELY INSTALLED artifact
// (bin/ui/run.py resolves $PLAINKEEP_UI_BIN, then .local/bin/plainkeep-ui, then PATH), so a clack
// bump changes the core at its next build while an installed standalone keeps the old disposition
// until someone reinstalls it. setuplib's staleness probe exists precisely because that copy goes
// stale. The modes move APART.
//
// Which is why the behavior is pinned by tests rather than by this comment: test/run_tui_pty.py
// asserts rows 1, 3 and 3b directly (SIGINT at the menu dies -2; SIGINT after an action survives;
// SIGTERM after an action survives). The day clack changes, those go red and this comment gets
// rewritten from evidence instead of quietly becoming a lie.

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

  // Everything from here runs under the seam's enforcement (interception.ts): process.exit is
  // guarded for the TUI's lifetime, the returned code is clamped onto the frozen protocol, and
  // stdout is drained before this resolves. All three used to be this file's own promises, and a
  // quality review measured all three being broken — most sharply by @clack/core's `block()`, which
  // calls process.exit(0) on Ctrl-C while a spinner is up, so an INTERRUPTED action reported success
  // and the drain never ran at all.
  //
  // WHY THE CLAMP IS NOT DECORATIVE, and why it is at THIS seam rather than in app.ts: cli/src/tui/
  // app.ts returns 1 when it cannot load the manifest (`plainkeep help --json` did not answer), which
  // is an ORDINARY failure — a vault that is not plainkeep.json/3, or a $PLAINKEEP_BIN that is not a
  // dispatcher — and 1 is off the protocol. Clamping here also covers any future TUI path that
  // invents a code, which fixing app.ts alone would not.
  return runOwningStdio("ui", async () => {
    try {
      return await main();
    } catch (e) {
      // The standalone entry prints the message and exits 1; losing the message would make a TUI
      // crash unreadable, so it is printed the same way. The CODE is the clamp's (5), not 1, and
      // deliberately the same code a RETURNED failure now gets — a thrown manifest failure and a
      // returned one are the same class of event and used to be classified differently (2 vs 1).
      console.error((e as Error)?.message ?? String(e));
      return EXIT_ANOMALOUS;
    }
  });
}
