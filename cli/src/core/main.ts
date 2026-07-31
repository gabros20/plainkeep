#!/usr/bin/env bun
// plainkeep-core — the compiled TS core binary (Phase 1 of the hybrid-core refactor,
// docs/design/proposals/2026-07-29-hybrid-core-binary.md). This entry is intentionally thin: it
// hands argv to the pure runCore() and maps its result to stdio + exit code. The
// dispatcher/guardrail/resolver land in ./index.ts in later tasks.
import type { CoreResult } from "./cli.js";
import { runCore } from "./cli.js";
import { EXIT_DENY } from "./guardrail.js";

// Last-resort guard: this binary is an ENFORCEMENT tool, so an unexpected exception must never reach
// the shell as a stack trace and exit 1 — a code outside the frozen protocol (0/2/3/4/5) that the
// dispatcher has no meaning for. It maps to deny (5), the refusal an internal error deserves: never
// a silent success, never clearable with --yes. mainCli() already catches at the gate level so the
// audit line is written there; this covers everything else runCore reaches.
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
process.exit(r.code);
