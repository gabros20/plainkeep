// plainkeep core — dispatcher / guardrail / resolver seam.
//
// Placeholder for Phase 1, Tasks 2–4 of the hybrid-core refactor: the resolver, guardrail, and
// dispatcher are ported into this directory and wired into ./main.ts here. Task 1 ships only the
// skeleton binary (./main.ts + ./cli.ts), so this barrel intentionally re-exports just the identity
// probe for now — the clean seam later tasks build on.
export { runCore, CORE_IDENTITY, type CoreResult } from "./cli.js";
export { CORE_VERSION } from "./version.js";
