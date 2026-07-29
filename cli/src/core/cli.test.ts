import { test, expect } from "bun:test";
import { runCore, CORE_IDENTITY } from "./cli.js";
import { CORE_VERSION } from "./version.js";

test("--version prints the core identity and exits 0", () => {
  const r = runCore(["--version"]);
  expect(r.code).toBe(0);
  expect(r.stdout).toBe(CORE_IDENTITY);
  expect(r.stdout).toContain("plainkeep-core");
  expect(r.stdout).toContain(CORE_VERSION);
});

test("-v is an alias for --version", () => {
  expect(runCore(["-v"])).toEqual(runCore(["--version"]));
});

test("--core-selftest identifies the core binary and exits 0", () => {
  const r = runCore(["--core-selftest"]);
  expect(r.code).toBe(0);
  expect(r.stdout).toContain("plainkeep-core");
  expect(r.stdout).toContain(CORE_VERSION);
});

test("any other argv exits 2 with a one-line 'not yet wired' stderr", () => {
  for (const argv of [[], ["help"], ["capture", "hi"], ["--version", "extra"], ["--nope"]]) {
    const r = runCore(argv);
    expect(r.code).toBe(2);
    expect(r.stdout).toBeUndefined();
    expect(r.stderr).toBeDefined();
    expect(r.stderr).toContain("not yet wired");
    expect(r.stderr!.split("\n")).toHaveLength(1);
  }
});

test("CORE_VERSION is a non-empty semver-ish string", () => {
  expect(CORE_VERSION).toMatch(/^\d+\.\d+\.\d+/);
});
