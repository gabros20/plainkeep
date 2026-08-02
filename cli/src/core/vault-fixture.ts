// vault-fixture.ts — TEST-ONLY: make a temp directory into a real vault.
//
// Since Phase 2 Task 1b a directory is not a vault because a variable points at it: `PLAINKEEP_HOME`
// is validated, and a root with no `.plainkeep/vault.json` marker refuses with exit 2 before the
// gate runs. Every bun test that builds a throwaway vault therefore has to build a REAL one — which
// is the point, not an inconvenience: the fixtures now have the same shape as the thing they stand
// in for.
//
// It is a separate module rather than a copy inside each test file so there is one spelling of the
// marker, and it is never imported by main.ts, so it is not in the compiled bundle.
import { existsSync, mkdirSync, symlinkSync, writeFileSync } from "node:fs";
import path from "node:path";

export const MARKER_SCHEMA = "plainkeep.vault/1";

// The engine tree these tests run out of: cli/src/core -> the repo root.
const REPO = path.resolve(import.meta.dir, "..", "..", "..");

// A fixed-shape uuid per fixture. `crypto.randomUUID()` is the real thing a `vault register` writes;
// the tests only need a marker that VALIDATES (schema + a well-formed uuid), and a random one keeps
// two fixtures from colliding if a test ever registers both.
export function markVault(home: string): string {
  const id = crypto.randomUUID();
  const d = path.join(home, ".plainkeep");
  mkdirSync(d, { recursive: true });
  writeFileSync(
    path.join(d, "vault.json"),
    `${JSON.stringify({ schema: MARKER_SCHEMA, id, created: new Date().toISOString() }, null, 2)}\n`,
    "utf-8",
  );
  // ...and the ENGINE. Phase 1 runs the engine from inside the vault it acts on, and since the r2
  // fix wave discovery says so out loud: a selected root that cannot be dispatched for is refused
  // with exit 2 rather than being left to fail at the far end of the dispatch (the floor's raw
  // CPython "can't open file", the core's false "unknown verb"). A fixture that could not pass that
  // probe was standing in for something that has to.
  //
  // `bin/lib` and not `bin`: several tests write their own `bin/<verb>/` into the fixture, and a
  // symlinked `bin` would put those inside the real checkout. `lib` carries no cmd.json, so the
  // resolver skips it exactly as it skips the engine's own.
  const bin = path.join(home, "bin");
  mkdirSync(bin, { recursive: true });
  if (!existsSync(path.join(bin, "lib"))) {
    symlinkSync(path.join(REPO, "bin", "lib"), path.join(bin, "lib"));
  }
  return id;
}

// A marker plus `bin/lib` is NOT a dispatchable root, and since the r3 fix wave it is refused as
// such: `require_engine` probes for a verb directory too. That second half exists because `bin/lib`
// alone split the two dispatchers — `resolver.py` finds verbs through its own `__file__`, which
// follows this symlink back into the real checkout, so the FLOOR saw every verb and captured notes
// into the fixture, while `resolver.ts` looks under the selected data root and saw none. Same argv:
// floor exit 0 WITH A WRITE, core exit 4.
//
// So a test that actually dispatches has to give its fixture a verb, and most already do (they write
// the `bin/<verb>/` whose behaviour they are asserting). This is for the ones that need a
// dispatchable root without caring what is in it. It is NOT folded into `markVault` on purpose: the
// completion tests assert the exact engine verb LIST, and a verb injected into every fixture would
// silently rewrite what they are checking.
export function addVerb(home: string, name: string): string {
  const d = path.join(home, "bin", name);
  mkdirSync(d, { recursive: true });
  writeFileSync(path.join(d, "run.py"), "raise SystemExit('fixture verb: never meant to run')\n", "utf-8");
  return d;
}
