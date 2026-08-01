// plainkeep core — dispatcher / guardrail / resolver export surface.
//
// This barrel is the public seam Tasks 2–4 of the hybrid-core refactor populate: the resolver
// (Task 2), guardrail (Task 3), and dispatcher (Task 4) are ported into this directory and
// re-exported here. Task 4 closed the seam Task 1 left — ./main.ts now consumes the core THROUGH
// this barrel rather than importing runCore from ./cli.ts directly.
export {
  runCore,
  CORE_IDENTITY,
  INTERCEPTED_FLAGS_ALWAYS,
  INTERCEPTED_FLAGS_BARE,
  type CoreResult,
} from "./cli.js";
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
// INTERCEPTS is the seam Tasks 5–7 fill: a verb registered there is answered in-process, after the
// gate and after argv normalization (see the comment on it — putting it anywhere earlier loses the
// audit line and four of the six `help` spellings).
export {
  dispatch,
  classifySpawnOutcome,
  INTERCEPTS,
  interceptionFor,
  pickPython,
  resolveHome,
  signalNumberOf,
  spawnPythonVerb,
  verbFromArgv,
  type Intercept,
  type Interception,
  type SpawnOutcome,
} from "./dispatch.js";
// Phase 2 Task 1b — WHICH vault an invocation acts on: the pre-verb `--vault` selector and the
// discovery call the dispatcher makes before it does anything else. The decision itself lives in
// bin/lib/vaultroot.py and is shared with the bash floor rather than ported (see the module header).
export {
  discoverRoot,
  engineRoot,
  requireHome,
  takeVaultSelector,
  VaultRefusal,
  type Root,
} from "./vaultroot.js";
// Task 5 — the first interception: `__complete` answered in-process from the live cmd.json surface,
// falling through to the Python verb for every live-vault provider.
export { completeIntercept } from "./complete.js";
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
