import { test, expect } from "bun:test";
import { mkdtempSync, rmSync } from "node:fs";
import { markVault } from "./vault-fixture.js";
import { tmpdir } from "node:os";
import path from "node:path";
import { runCore, CORE_IDENTITY } from "./cli.js";
import { EXIT_NOT_FOUND } from "./guardrail.js";
import { EXIT_USAGE, VaultRefusal } from "./vaultroot.js";
import { CORE_VERSION } from "./version.js";

test("--version prints the core identity and exits 0", async () => {
  const r = await runCore(["--version"]);
  expect(r.code).toBe(0);
  expect(r.stdout).toBe(CORE_IDENTITY);
  expect(r.stdout).toContain("plainkeep-core");
  expect(r.stdout).toContain(CORE_VERSION);
});

test("-v is an alias for --version", async () => {
  expect(await runCore(["-v"])).toEqual(await runCore(["--version"]));
});

test("--core-selftest identifies the core binary and exits 0", async () => {
  const r = await runCore(["--core-selftest"]);
  expect(r.code).toBe(0);
  expect(r.stdout).toContain("plainkeep-core");
  expect(r.stdout).toContain(CORE_VERSION);
});

// Replaces the Task 1 skeleton's "any other argv exits 2 with a one-line 'not yet wired' stderr".
// That placeholder IS what Task 4 removes: every non-flag argv is now a verb dispatch. The contract
// asserted here is strictly stronger — the argv still does not run anything, but because the GATE
// refused it by name (not-found, 4, with the guardrail's own stderr), which is what the bash floor
// does for the same input. PLAINKEEP_HOME is pinned to an empty temp vault so the verb set is empty
// by construction: the assertion can never depend on the developer's real vault, and dispatch can
// never reach a spawn.
test("a non-flag argv dispatches: an unknown verb is the gate's not-found (4)", async () => {
  const prev = process.env.PLAINKEEP_HOME;
  const home = mkdtempSync(path.join(tmpdir(), "pk-core-cli-"));
  markVault(home);  // Task 1b: an unmarked directory is no longer a root any dispatch accepts
  process.env.PLAINKEEP_HOME = home;
  try {
    for (const argv of [[], ["help"], ["capture", "hi"], ["--version", "extra"], ["--nope"]]) {
      const r = await runCore(argv);
      expect(r.code).toBe(EXIT_NOT_FOUND);
      expect(r.stdout).toBeUndefined();
      expect(r.stderr).toBeDefined();
      expect(r.stderr).toContain("unknown verb");
      expect(r.stderr!.split("\n")).toHaveLength(1);
    }
  } finally {
    if (prev === undefined) delete process.env.PLAINKEEP_HOME;
    else process.env.PLAINKEEP_HOME = prev;
    rmSync(home, { recursive: true, force: true });
  }
});

// `--vault` reaching a hidden `--core-*` probe must REFUSE, not be dropped.
//
// The probes are consumed only by test/run_core_parity.py, which always sets PLAINKEEP_HOME — so
// silently discarding the selector costs nothing today and is still the wrong failure mode: it makes
// `plainkeep-core --vault X --core-gate capture foo` answer for whatever root PLAINKEEP_HOME happens
// to name while the caller believes it answered for X. For the ONE flag this task exists for, an
// unimplemented position must be a refusal and not a silent default. (Spec review MINOR-4.)
//
// `--version` and `--core-selftest` are deliberately NOT in this list: `plainkeep --vault work
// --version` is a documented identity probe that ignores the selection, and the parity oracle pins
// that shape on both dispatchers.
test("--vault on a hidden --core-* probe REFUSES (2), never silently ignored", async () => {
  const prev = process.env.PLAINKEEP_HOME;
  const home = mkdtempSync(path.join(tmpdir(), "pk-core-vaultprobe-"));
  markVault(home);
  process.env.PLAINKEEP_HOME = home;
  try {
    for (const argv of [
      ["--vault", "work", "--core-gate", "capture", "foo"],
      ["--vault", "work", "--core-resolve", "capture"],
      ["--vault", "work", "--core-api"],
    ]) {
      // Thrown, not returned, exactly as `--vault` with no value already is: main.ts maps a
      // VaultRefusal to its own code and stderr, and collapsing it into a CoreResult here would
      // make this refusal the only one in the file that does not travel that path.
      let caught: unknown;
      try {
        await runCore(argv);
      } catch (e) {
        caught = e;
      }
      expect(caught).toBeInstanceOf(VaultRefusal);
      expect((caught as VaultRefusal).code).toBe(EXIT_USAGE);
      expect((caught as VaultRefusal).message).toContain("--vault");
    }
    // and the same probes without a selector still work exactly as before
    expect((await runCore(["--core-resolve", "definitely-not-a-verb"])).code).toBe(4);
  } finally {
    if (prev === undefined) delete process.env.PLAINKEEP_HOME;
    else process.env.PLAINKEEP_HOME = prev;
    rmSync(home, { recursive: true, force: true });
  }
});

test("CORE_VERSION is a non-empty semver-ish string", () => {
  expect(CORE_VERSION).toMatch(/^\d+\.\d+\.\d+/);
});
