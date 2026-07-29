#!/usr/bin/env bun
// plainkeep-core — the compiled TS core binary (Phase 1 of the hybrid-core refactor,
// docs/design/proposals/2026-07-29-hybrid-core-binary.md). This entry is intentionally thin: it
// hands argv to the pure runCore() and maps its result to stdio + exit code. The
// dispatcher/guardrail/resolver land in ./index.ts in later tasks.
import { runCore } from "./cli.js";

const r = runCore(process.argv.slice(2));
if (r.stdout) console.log(r.stdout);
if (r.stderr) console.error(r.stderr);
process.exit(r.code);
