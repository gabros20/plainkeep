import { test, expect } from "bun:test";
import { mkdtempSync, rmSync } from "node:fs";
import { markVault } from "./vault-fixture.js";
import { tmpdir } from "node:os";
import path from "node:path";
import { runCore, CORE_IDENTITY } from "./cli.js";
import { EXIT_NOT_FOUND } from "./guardrail.js";
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

test("CORE_VERSION is a non-empty semver-ish string", () => {
  expect(CORE_VERSION).toMatch(/^\d+\.\d+\.\d+/);
});
