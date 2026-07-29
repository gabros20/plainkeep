// The core binary's argv handler — pure so it can be unit-tested without spawning a process.
// Phase 1 is a skeleton: only the identity probes (`--version`, `--core-selftest`) are live; every
// other argv exits 2 with a one-line "not yet wired" note, so nothing silently pretends the
// dispatcher/guardrail/resolver (Tasks 2–4, ./index.ts) exist yet.
import { CORE_VERSION } from "./version.js";

export interface CoreResult {
  stdout?: string;
  stderr?: string;
  code: number;
}

// The identity line both probes emit. It unambiguously names the core binary and carries a version,
// so later differential/acceptance tests can tell this artifact apart from the legacy plainkeep-ui.
export const CORE_IDENTITY = `plainkeep-core ${CORE_VERSION}`;

export function runCore(argv: string[]): CoreResult {
  if (argv.length === 1 && (argv[0] === "--version" || argv[0] === "-v")) {
    return { stdout: CORE_IDENTITY, code: 0 };
  }
  if (argv.length === 1 && argv[0] === "--core-selftest") {
    return { stdout: `${CORE_IDENTITY} selftest ok`, code: 0 };
  }
  return { stderr: "plainkeep-core: not yet wired (skeleton binary — no verbs are dispatched yet)", code: 2 };
}
