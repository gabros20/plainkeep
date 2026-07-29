// plainkeep core — dispatcher / guardrail / resolver export surface.
//
// This barrel is the public seam Tasks 2–4 of the hybrid-core refactor populate: the resolver,
// guardrail, and dispatcher are ported into this directory and re-exported here. It is NOT yet
// consumed by ./main.ts — Task 1 ships only the skeleton, whose entry imports runCore from ./cli.ts
// directly; main.ts is re-pointed through this barrel when the dispatcher lands (Task 4). For now it
// re-exports just the identity probe plus the ported resolver (Task 2) that Task 3's guardrail
// consumes (known_verbs / cmd_json_path / source_of) exactly as the Python guardrail imports it.
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
// Task 3 — the dispatcher-facing guardrail (gate + did-you-mean + audit log), ported from the
// gate-side subset of bin/lib/guardrail.py. Task 4 wires gate()/mainCli() into real verb dispatch.
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
