#!/usr/bin/env bun
// plainkeep-core — the compiled TS core binary (Phase 1 of the hybrid-core refactor,
// docs/design/proposals/2026-07-29-hybrid-core-binary.md). This entry is intentionally thin: it
// hands argv to runCore() and maps its result to stdio + exit status. Everything it uses comes
// through the ./index.ts barrel, which is the core's public surface (Task 4 closed the Task 1 seam
// where this file reached into ./cli.ts directly).
import type { CoreResult } from "./index.js";
import { EXIT_DENY, runCore } from "./index.js";

// Last-resort guard: this binary is an ENFORCEMENT tool, so an unexpected exception must never reach
// the shell as a stack trace and exit 1 — a code outside the frozen protocol (0/2/3/4/5) that the
// dispatcher has no meaning for. It maps to deny (5), the refusal an internal error deserves: never
// a silent success, never clearable with --yes. mainCli() already catches at the gate level so the
// audit line is written there; this covers everything else runCore reaches, dispatch included.
let r: CoreResult;
try {
  // The await is INSIDE the try, and that placement is the whole point of it. runCore() may now
  // return a promise (dispatch.ts's Intercept allows an async interception, so Tasks 6–7 can run a
  // TUI or an MCP session in-process). Awaiting it OUTSIDE this try would turn a rejection into an
  // unhandled rejection, which bun reports by exiting 1 — a code off the frozen protocol (0/2/3/4/5),
  // produced by the very guard whose job is to keep everything on it. Pinned by main.async.test.ts,
  // which drives a rejecting interception through this exact file.
  r = await runCore(process.argv.slice(2));
} catch (e) {
  r = { stderr: `plainkeep-core: internal error (${e instanceof Error ? e.name : "Error"})`, code: EXIT_DENY };
}
// PRESENCE, not truthiness. `if (r.stdout)` also skipped the EMPTY string, which made "print one
// empty line" inexpressible — and `__complete` can be asked for exactly that (a lone candidate whose
// value and description are both empty, which the Python verb prints as a bare newline). No existing
// producer sets stdout to "", so this changes nothing else: coreApi always emits JSON,
// `--core-resolve` sets stdout only for a non-empty path, and the gate/dispatch paths never set it.
try {
  if (r.stdout !== undefined) console.log(r.stdout);
  if (r.stderr) console.error(r.stderr);
} catch {
  // a broken stdout/stderr pipe must not change the exit code
}

// process.exit() TRUNCATES whatever is still queued for an async sink — stdout to a PIPE is exactly
// that, so `plainkeep <verb> | cat` can lose the tail of its own output. It has never bitten in
// practice (every buffered result this binary produces is far below the 64 KiB pipe buffer, so the
// write completes synchronously), but the async seam makes long-running interceptions possible and
// they will not be.
//
// Bounded on purpose. If the reader has stopped reading with a full pipe, the drain can never
// complete, and a hung command is a worse outcome than the truncation this prevents — so the wait
// gives up after DRAIN_TIMEOUT_MS and exits anyway, which is precisely today's behavior. The timer is
// unref'd so it can never be the thing keeping the process alive. Note what this can and cannot do:
// it drains what THIS file wrote. An stdio-owning interception that writes and does not wait must
// drain before it resolves; see the seam comment in dispatch.ts.
const DRAIN_TIMEOUT_MS = 2000;

async function drain(stream: NodeJS.WriteStream): Promise<void> {
  if (stream.writableLength === 0) return; // the common case: nothing queued, no waiting at all
  await Promise.race([
    new Promise<void>((resolve) => {
      stream.write("", () => resolve());
    }),
    new Promise<void>((resolve) => {
      setTimeout(resolve, DRAIN_TIMEOUT_MS).unref();
    }),
  ]);
}

try {
  await Promise.all([drain(process.stdout), drain(process.stderr)]);
} catch {
  // a drain that fails (EPIPE, a closed fd) must not change the exit code either
}
// A verb killed by a signal must leave THIS process dead by the same signal, not merely exiting
// 128+N: the bash floor `exec`s the verb, so plainkeep IS the signalled process and every waitpid()
// caller sees WIFSIGNALED (Python's subprocess reports -N). Re-raising reproduces that wait status.
//
// By NUMBER, never by bun's signal name — on macOS bun names a child's death signal with the LINUX
// name for that number, so re-raising the name kills us with a different signal than the one that
// killed the verb (dispatch.ts, signalNumberOf). dispatch() has already resolved the number; if it
// could not, it returns no signal at all and a distinct exit code instead of guessing.
//
// Two runtime behaviors keep this from being universal, and dispatch.ts carries the measured table of
// exactly which signals they affect (do not restate it here — it went stale twice): bun ignores some
// signals process-wide, so we survive and fall through to the 128+N that dispatch() supplied (what a
// shell would have reported anyway), and bun's crash handler turns a re-raised fault signal into
// SIGTRAP. Both are pinned per signal by the parity matrix.
if (r.signal) {
  try {
    process.kill(process.pid, r.signal);
  } catch {
    // an undeliverable signal number falls through to the numeric exit below
  }
}
process.exit(r.code);
