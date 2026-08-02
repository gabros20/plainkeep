// Dev-speed bun mirror of the plugin spawn contract. The AUTHORITATIVE gate is the differential
// oracle (test/cases/core-parity/dispatcher.json's `plugin-spawn-environment`, which compares the
// child environment BOTH dispatchers actually produce) plus test/run_pluginsdk.py, which runs a real
// unmodified plugin through a real dispatch. This file only pins the pure port in-process — the two
// cases a subprocess differential shows less sharply: the REMOVAL an engine verb gets, and the
// exact merge with a caller's own PYTHONPATH.
import { test, expect } from "bun:test";
import path from "node:path";
import { PACK_ENV, packOf, depsDir, sdkPathEntries, prependPath, spawnEnv } from "./pluginenv.js";

const ENGINE = "/eng/current";
const VAULT = "/vault";
const DEPS = path.join(VAULT, ".plugin-deps");
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

test("an ENGINE verb gets the marker REMOVED, not merely not-added", () => {
  // `{}` here — "add nothing" — was the finding. It is correct only for a caller whose own
  // environment does not already carry the marker, and a plugin verb that re-enters the dispatcher
  // is exactly the caller that does: PLAINKEEP_PLUGIN_PACK is inherited to any depth, and anything
  // below that imports `lib.api` reports its own missing module as the named pack's fault. The
  // delta is a REPLACEMENT; `undefined` is the deletion spawnVerb() applies with `delete`.
  expect(spawnEnv(ENGINE, VAULT, "engine", { PYTHONPATH: "/caller" })).toEqual({
    [PACK_ENV]: undefined,
  });
  expect(spawnEnv(ENGINE, VAULT, null, {})).toEqual({ [PACK_ENV]: undefined });
  // ...and it adds nothing else: PYTHONPATH is the caller's own business for an engine verb.
  expect(Object.keys(spawnEnv(ENGINE, VAULT, "engine", {}))).toEqual([PACK_ENV]);
});

test("a PLUGIN verb gets exactly two variables", () => {
  const e = spawnEnv(ENGINE, VAULT, "plugin:greeter", { PYTHONPATH: "/caller" });
  expect(Object.keys(e).sort()).toEqual([PACK_ENV, "PYTHONPATH"]);
  expect(e[PACK_ENV]).toBe("greeter");
  expect(e.PYTHONPATH).toBe(`${DEPS}:${BIN}:/caller`);
});
