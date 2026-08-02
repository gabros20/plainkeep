// Dev-speed bun mirror of the plugin spawn contract. The AUTHORITATIVE gate is the differential
// oracle (test/cases/core-parity/dispatcher.json's `plugin-spawn-environment`, which compares the
// child environment BOTH dispatchers actually produce) plus test/run_pluginsdk.py, which runs a real
// unmodified plugin through a real dispatch. This file only pins the pure port in-process — the two
// cases a subprocess differential shows less sharply: the EMPTY answer for an engine verb, and the
// exact merge with a caller's own PYTHONPATH.
import { test, expect } from "bun:test";
import path from "node:path";
import { PACK_ENV, packOf, depsDir, sdkPathEntries, prependPath, spawnEnv } from "./pluginenv.js";

const ENGINE = "/eng/current";
const VAULT = "/vault";
const DEPS = path.join(VAULT, "plugins", ".deps");
const BIN = path.join(ENGINE, "bin");

test("packOf reads a resolver source string and nothing else", () => {
  expect(packOf("plugin:greeter")).toBe("greeter");
  expect(packOf("engine")).toBeNull();
  expect(packOf(null)).toBeNull();
  expect(packOf(undefined)).toBeNull();
  // A source with the prefix and no name is not a pack — it would produce PLAINKEEP_PLUGIN_PACK=""
  // which reads as "no pack" everywhere downstream and as "a pack" to a truthiness test.
  expect(packOf("plugin:")).toBeNull();
});

test("the overlay comes before the engine — order is the contract", () => {
  expect(depsDir(VAULT)).toBe(DEPS);
  expect(sdkPathEntries(ENGINE, VAULT)).toEqual([DEPS, BIN]);
});

test("prependPath prepends and never replaces", () => {
  expect(prependPath([DEPS, BIN], undefined)).toBe(`${DEPS}:${BIN}`);
  expect(prependPath([DEPS, BIN], "")).toBe(`${DEPS}:${BIN}`);
  expect(prependPath([DEPS, BIN], "/caller/one:/caller/two")).toBe(
    `${DEPS}:${BIN}:/caller/one:/caller/two`,
  );
  // Duplicates are NOT filtered, deliberately: a filter is a second behavior the Python original
  // would have to reproduce byte-exactly, and a repeated sys.path entry costs one failed stat.
  expect(prependPath([DEPS, BIN], BIN)).toBe(`${DEPS}:${BIN}:${BIN}`);
});

test("an ENGINE verb gets NOTHING — the whole per-spawn decision, in one assertion", () => {
  expect(spawnEnv(ENGINE, VAULT, "engine", { PYTHONPATH: "/caller" })).toEqual({});
  expect(spawnEnv(ENGINE, VAULT, null, {})).toEqual({});
});

test("a PLUGIN verb gets exactly two variables", () => {
  const e = spawnEnv(ENGINE, VAULT, "plugin:greeter", { PYTHONPATH: "/caller" });
  expect(Object.keys(e).sort()).toEqual([PACK_ENV, "PYTHONPATH"]);
  expect(e[PACK_ENV]).toBe("greeter");
  expect(e.PYTHONPATH).toBe(`${DEPS}:${BIN}:/caller`);
});
