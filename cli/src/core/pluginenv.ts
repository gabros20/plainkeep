// pluginenv.ts — a faithful TypeScript port of bin/lib/pluginenv.py (the PERMANENT Python module).
//
// The plugin spawn contract: what a dispatcher adds to a PLUGIN verb's environment, and nothing
// else. Read the Python module for why it exists — the short version is that Phase 2 Task 2 moved
// the engine out of the vault, every plugin ever scaffolded bootstraps the SDK through
// `$PLAINKEEP_HOME/bin`, and `PLAINKEEP_API_VERSION = "1.0"` promises those plugins keep working
// with zero edits. The dispatcher puts the engine's own `bin/` on PYTHONPATH for the spawn and the
// stale insert becomes a no-op (CPython skips a nonexistent sys.path entry).
//
// This file is a PORT, not a second design. The order of the entries, the merge with the caller's
// own PYTHONPATH, and the decision to add nothing at all for an engine verb are all decided in
// pluginenv.py; test/cases/core-parity/dispatcher.json compares the child environment the two
// dispatchers actually produce, so a divergence here reddens a named case rather than showing up as
// a plugin that works on the floor and not through the core.
import path from "node:path";

export const PACK_ENV = "PLAINKEEP_PLUGIN_PACK";
export const DEPS_DIRNAME = ".deps";
const SOURCE_PLUGIN_PREFIX = "plugin:";

export function packOf(source: string | null | undefined): string | null {
  if (typeof source !== "string" || !source.startsWith(SOURCE_PLUGIN_PREFIX)) return null;
  return source.slice(SOURCE_PLUGIN_PREFIX.length) || null;
}

export function depsDir(vault: string): string {
  return path.join(vault, "plugins", DEPS_DIRNAME);
}

// ORDER IS THE CONTRACT: the vault's dependency overlay first, then the engine tree. A pack's
// DECLARED dependency beats the engine's incidental top-level names (`bin/models/`, `bin/files/` and
// `bin/index/` all become importable namespace packages once `bin/` is on the path).
export function sdkPathEntries(engine: string, vault: string): string[] {
  return [depsDir(vault), path.join(engine, "bin")];
}

// Duplicates are deliberately NOT filtered — see the Python original. A filter is a second behavior
// this port would have to reproduce byte-exactly, and a repeated sys.path entry costs one failed stat.
export function prependPath(entries: string[], existing: string | null | undefined): string {
  const tail = existing ?? "";
  return entries.join(path.delimiter) + (tail ? path.delimiter + tail : "");
}

// The environment ADDITIONS for one spawn — empty for an engine verb.
export function spawnEnv(
  engine: string,
  vault: string,
  source: string | null,
  env: Record<string, string | undefined>,
): Record<string, string> {
  const pack = packOf(source);
  if (pack === null) return {};
  return {
    PYTHONPATH: prependPath(sdkPathEntries(engine, vault), env.PYTHONPATH),
    [PACK_ENV]: pack,
  };
}
