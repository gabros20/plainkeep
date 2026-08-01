// Dev-speed bun mirror of a few resolver behaviors. The AUTHORITATIVE gate is the Python-owned
// differential oracle (test/run_core_parity.py); this file only spot-checks the port in-process,
// most importantly the per-call env re-read that a fresh-subprocess parity run cannot show directly.
import { test, expect, afterEach } from "bun:test";
import { mkdtempSync, mkdirSync, writeFileSync, rmSync, realpathSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { resolve, sourceOf, pluginNames, iterCmds, shadowed, knownVerbs } from "./resolver.js";

let vault = "";
function makeVault(): string {
  vault = realpathSync(mkdtempSync(path.join(tmpdir(), "pk-resolver-")));
  mkdirSync(path.join(vault, "bin"), { recursive: true });
  return vault;
}
function verb(base: string, name: string, files: string[] = ["run.py", "cmd.json"]): void {
  const d = path.join(base, name);
  mkdirSync(d, { recursive: true });
  for (const f of files) writeFileSync(path.join(d, f), f === "cmd.json" ? "{}" : "x");
}

afterEach(() => {
  if (vault) rmSync(vault, { recursive: true, force: true });
  vault = "";
  delete process.env.PLAINKEEP_HOME;
  delete process.env.PLAINKEEP_PATH;
});

test("engine bin/ wins over a plugin of the same name; the plugin is shadowed()", () => {
  const v = makeVault();
  verb(path.join(v, "bin"), "search");
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
