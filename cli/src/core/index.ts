// plainkeep core — dispatcher / guardrail / resolver export surface.
//
// This barrel is the public seam Tasks 2–4 of the hybrid-core refactor populate: the resolver
// (Task 2), guardrail (Task 3), and dispatcher (Task 4) are ported into this directory and
// re-exported here. Task 4 closed the seam Task 1 left — ./main.ts now consumes the core THROUGH
// this barrel rather than importing runCore from ./cli.ts directly.
export { runCore, CORE_IDENTITY, type CoreResult } from "./cli.js";
export { CORE_VERSION } from "./version.js";
export {
  resolve,
  resolveVerb,
  runPy,
  cmdJsonPath,
  sourceOf,
  isEngineVerb,
  knownVerbs,
  pluginNames,
  iterCmds,
  shadowed,
  type Source,
} from "./resolver.js";
// Task 4 — the dispatcher itself: PLAINKEEP_HOME resolution, venv liveness probe, gate, resolve,
// and ONE spawn with full exit/signal passthrough. runCore() routes every non-flag argv here.
export { dispatch, pickPython, resolveHome, verbFromArgv } from "./dispatch.js";
// Task 3 — the dispatcher-facing guardrail (gate + did-you-mean + audit log), ported from the
// gate-side subset of bin/lib/guardrail.py; dispatch() runs it before every verb.
export {
  gate,
  mainCli,
  riskOf,
  decisionStr,
  getCloseMatches,
  type Decision,
  EXIT_OK,
  EXIT_USAGE,
  EXIT_CONFIRM,
  EXIT_NOT_FOUND,
  EXIT_DENY,
} from "./guardrail.js";
