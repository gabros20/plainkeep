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
  r = runCore(process.argv.slice(2));
} catch (e) {
  r = { stderr: `plainkeep-core: internal error (${e instanceof Error ? e.name : "Error"})`, code: EXIT_DENY };
}
try {
  if (r.stdout) console.log(r.stdout);
  if (r.stderr) console.error(r.stderr);
} catch {
  // a broken stdout/stderr pipe must not change the exit code
}
// A verb killed by a signal must leave THIS process dead by the same signal, not merely exiting
// 128+N: the bash floor `exec`s the verb, so plainkeep IS the signalled process and every waitpid()
// caller sees WIFSIGNALED (Python's subprocess reports -N). Re-raising reproduces that wait status
// exactly. If the signal is blocked or ignored and we survive, fall through to the 128+N that
// dispatch() supplied — which is what a shell would have reported for the same death.
if (r.signal) {
  try {
    process.kill(process.pid, r.signal);
  } catch {
    // an unknown/undeliverable signal name falls through to the numeric exit below
  }
}
process.exit(r.code);
