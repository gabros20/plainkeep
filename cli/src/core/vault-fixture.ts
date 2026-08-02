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
//
// PHASE 2 TASK 2: a marked vault is a marked vault and NOTHING ELSE. It used to symlink `bin/lib`
// into the fixture, because Phase 1 refused a selected root that carried no copy of the engine. The
// engine has left the vault, that probe is inverted (it now asks whether the ENGINE tree is intact),
// and a fixture that installed a `bin/` would be modelling a shape the product no longer has.
export function markVault(home: string): string {
  const id = crypto.randomUUID();
  const d = path.join(home, ".plainkeep");
  mkdirSync(d, { recursive: true });
  writeFileSync(
    path.join(d, "vault.json"),
    `${JSON.stringify({ schema: MARKER_SCHEMA, id, created: new Date().toISOString() }, null, 2)}\n`,
    "utf-8",
  );
  return id;
}

// AN ENGINE TREE, beside the vault and never inside it (Phase 2 Task 2).
//
// The verb surface a test controls is now the ENGINE's, because that is where the resolvers look —
// `resolver.ts`'s `engineBin()` is `<activated engine>/bin`, no longer `<home>/bin`. So a test that
// wants a verb to exist makes an engine and puts it there, and `PLAINKEEP_ENGINE` is how this
// process is told which one to use.
//
// Setting the variable directly is legitimate HERE and nowhere else, and the distinction is the same
// one that already applies to `PLAINKEEP_HOME`: in a real invocation `runCore()` overwrites it from
// the executable's own location before anything reads it, so a caller cannot steer it; in a unit
// test there is no caller, and the test IS the thing choosing. The parity oracle is what proves the
// production property, by poisoning `PLAINKEEP_ENGINE` on all 203 catalog invocations.
//
// `bin/lib` is symlinked in rather than copied: it is what `guardrail.py` and `resolver.py` import,
// it carries no cmd.json or run.py so the resolver never sees it as a verb, and copying a few
// hundred KB per fixture buys nothing.
export function makeEngine(dir: string): string {
  const bin = path.join(dir, "bin");
  mkdirSync(bin, { recursive: true });
  if (!existsSync(path.join(bin, "lib"))) {
    symlinkSync(path.join(REPO, "bin", "lib"), path.join(bin, "lib"));
  }
  // `enginetree.require_intact()` asks for <engine>/VERSION on every real dispatch, and
  // `manifest.py` reads it as the engine version.
  if (!existsSync(path.join(dir, "VERSION"))) {
    writeFileSync(path.join(dir, "VERSION"), "0.0.0-fixture\n", "utf-8");
  }
  process.env.PLAINKEEP_ENGINE = dir;
  return dir;
}

// A verb in an ENGINE tree. It took a vault root through Phase 1 — see `makeEngine` for why that
// stopped being the right argument. It is NOT folded into `makeEngine` on purpose: the completion
// tests assert the exact engine verb LIST, and a verb injected into every fixture would silently
// rewrite what they are checking.
export function addVerb(engine: string, name: string): string {
  const d = path.join(engine, "bin", name);
  mkdirSync(d, { recursive: true });
  writeFileSync(path.join(d, "run.py"), "raise SystemExit('fixture verb: never meant to run')\n", "utf-8");
  return d;
}
