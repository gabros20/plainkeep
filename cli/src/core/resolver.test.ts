// Dev-speed bun mirror of a few resolver behaviors. The AUTHORITATIVE gate is the Python-owned
// differential oracle (test/run_core_parity.py); this file only spot-checks the port in-process,
// most importantly the per-call env re-read that a fresh-subprocess parity run cannot show directly.
import { test, expect, afterEach } from "bun:test";
import { mkdtempSync, mkdirSync, writeFileSync, rmSync, realpathSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { resolve, sourceOf, pluginNames, iterCmds, shadowed, knownVerbs } from "./resolver.js";
import { makeEngine } from "./vault-fixture.js";

let vault = "";
let engine = "";
// The two trees Phase 2 Task 2 separates: the ENGINE holds the reserved verb surface (which is what
// `engineBin()` now resolves against), the VAULT holds plugin packs and $PLAINKEEP_PATH roots.
function makeVault(): string {
  vault = realpathSync(mkdtempSync(path.join(tmpdir(), "pk-resolver-")));
  engine = makeEngine(realpathSync(mkdtempSync(path.join(tmpdir(), "pk-resolver-engine-"))));
  return vault;
}
function verb(base: string, name: string, files: string[] = ["run.py", "cmd.json"]): void {
  const d = path.join(base, name);
  mkdirSync(d, { recursive: true });
  for (const f of files) writeFileSync(path.join(d, f), f === "cmd.json" ? "{}" : "x");
}

afterEach(() => {
  if (vault) rmSync(vault, { recursive: true, force: true });
  if (engine) rmSync(engine, { recursive: true, force: true });
  vault = "";
  engine = "";
  delete process.env.PLAINKEEP_HOME;
  delete process.env.PLAINKEEP_PATH;
  delete process.env.PLAINKEEP_ENGINE;
});

test("engine bin/ wins over a plugin of the same name; the plugin is shadowed()", () => {
  const v = makeVault();
  verb(path.join(engine, "bin"), "search");
  verb(path.join(v, "plugins", "packA"), "search", ["cmd.json"]);
  verb(path.join(v, "plugins", "packA"), "pfoo");
  process.env.PLAINKEEP_HOME = v;
  process.env.PLAINKEEP_PATH = "";
  expect(sourceOf("search")).toBe("engine");
  expect(sourceOf("pfoo")).toBe("plugin:packA");
  expect(shadowed()).toEqual([["search", "packA"]]);
  expect(pluginNames()).toEqual(["packA"]);
  expect([...knownVerbs()].sort()).toEqual(["pfoo", "search"]);
  expect(iterCmds().map(([, s]) => s)).toEqual(["engine", "plugin:packA"]);
});

test("PLAINKEEP_PATH is re-read PER CALL — reordering it between calls flips resolution", () => {
  const v = makeVault();
  verb(path.join(v, "_roots", "rootX"), "shared");
  verb(path.join(v, "_roots", "rootY"), "shared");
  process.env.PLAINKEEP_HOME = v;
  const x = path.join(v, "_roots", "rootX");
  const y = path.join(v, "_roots", "rootY");

  process.env.PLAINKEEP_PATH = `${x}:${y}`;
  expect(sourceOf("shared")).toBe("plugin:rootX");
  // no module reload — the same in-process resolver must observe the new env on the next call
  process.env.PLAINKEEP_PATH = `${y}:${x}`;
  expect(sourceOf("shared")).toBe("plugin:rootY");
});

test("unknown verb resolves to null", () => {
  const v = makeVault();
  process.env.PLAINKEEP_HOME = v;
  process.env.PLAINKEEP_PATH = "";
  expect(resolve("nope-verb")).toBeNull();
});
