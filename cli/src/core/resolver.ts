// resolver.ts — a faithful TypeScript port of bin/lib/resolver.py (the PERMANENT Python module).
//
// Same one source of truth for turning a verb name into the directory that holds its run.py +
// cmd.json, in STRICT precedence:
//
//   1. <engine>/bin/<verb>/       — the engine, RESERVED (a plugin can never shadow it)
//   2. <home>/plugins/<pack>/<verb>/ — user packs inside the vault (PLAINKEEP_HOME), sorted
//   3. $PLAINKEEP_PATH roots       — colon-separated extra pack roots, each a dir of <verb>/ folders
//
// PLAINKEEP_HOME / PLAINKEEP_PATH are read PER CALL (no caching across calls) so the running process
// and the test harness see the same resolution, exactly like the Python original.
//
// ENGINE_BIN derivation (REWRITTEN in Phase 2 Task 2). The Python original pins ENGINE_BIN to the
// resolver FILE location (`Path(__file__).resolve().parents[1]`), i.e. to the tree the CODE lives in,
// and it has always been right to do so. This port could not say the same thing: with no file
// location of its own inside the engine, it derived engine bin/ as `<home>/bin` — correct only for
// as long as the engine lived inside the vault, and a plain restatement of the assumption ADR-014
// deletes. Two consequences, both live: a vault that carried a `bin/<verb>/` of its own would have
// had it resolved as an ENGINE verb (which no plugin may shadow), and a relocated engine would not
// have been found at all.
//
// It is now `engineRoot()/bin` — the same code-relative answer the Python original gives, reached
// through the executable's own location. `opsHome()` below keeps the data root, and it keeps it for
// exactly one thing: PLUGIN packs, which are the user's and do live in the vault.
import fs from "node:fs";
import path from "node:path";
import os from "node:os";
import { requireEngine, requireHome } from "./vaultroot.js";

export type Source = "engine" | `plugin:${string}`;

function statOrNull(p: string): fs.Stats | null {
  try {
    return fs.statSync(p);
  } catch {
    return null;
  }
}

// Path.exists() / Path.is_dir() follow symlinks and return False on a missing path — statSync mirrors
// both (it follows symlinks; the catch turns ENOENT/ELOOP into "absent").
function pathExists(p: string): boolean {
  return statOrNull(p) !== null;
}

function isDir(p: string): boolean {
  return statOrNull(p)?.isDirectory() ?? false;
}

// Python os.path.expanduser: "~" -> $HOME, "~/x" -> $HOME/x. $HOME wins over the passwd db on POSIX,
// so read process.env.HOME first (os.homedir() as the same fallback Python's pwd lookup provides).
function expanduser(p: string): string {
  if (p !== "~" && !p.startsWith("~/")) return p;
  const home = process.env.HOME ?? os.homedir();
  if (p === "~") return home;
  return home + p.slice(1);
}

// Directory children sorted by name — the effect of Python's `sorted(dir.iterdir())` /
// `sorted(dir.glob("*/..."))` on sibling paths that share a common prefix.
function sortedChildNames(dir: string): string[] {
  let names: string[];
  try {
    names = fs.readdirSync(dir);
  } catch {
    return [];
  }
  return names.sort();
}

// The SELECTED data root — where PLUGIN packs live. The executable-relative default that used to
// sit here (mirroring Python `_ops_home()`'s `ENGINE_BIN.parent`) is deleted with the rest of them
// (ADR-014 D2, Task 1b): a plugin scan is one of the things that must not happen before a root is
// validated, and a guessed root would scan — and trust — packs from wherever the binary landed.
function opsHome(): string {
  return requireHome();
}

// The ENGINE's bin/ — the tree runCore() activated from the executable's own location, never
// derived from the data root. Read through requireEngine() for the same reason opsHome() reads
// requireHome(): one place holds the answer, so one place could ever grow a fallback.
function engineBin(): string {
  return path.join(requireEngine(), "bin");
}

function isVerbDir(d: string): boolean {
  return pathExists(path.join(d, "run.py")) || pathExists(path.join(d, "cmd.json"));
}

// (pack_name, pack_dir) for each plugins/<pack>/ under PLAINKEEP_HOME (sorted), then each
// $PLAINKEEP_PATH root (the root itself is the pack). Order is the resolution order after the engine.
function pluginPacks(): Array<[string, string]> {
  const packs: Array<[string, string]> = [];
  const pdir = path.join(opsHome(), "plugins");
  if (isDir(pdir)) {
    for (const name of sortedChildNames(pdir)) {
      const sub = path.join(pdir, name);
      if (isDir(sub)) packs.push([name, sub]);
    }
  }
  for (const raw of (process.env.PLAINKEEP_PATH ?? "").split(":")) {
    const root = raw.trim();
    if (!root) continue;
    const rp = expanduser(root);
    if (isDir(rp)) packs.push([path.basename(rp), rp]);
  }
  return packs;
}

// Verb-dir names directly under the engine bin/ (run.py OR cmd.json makes a dir a verb).
function engineNames(): Set<string> {
  const names = new Set<string>();
  const bin = engineBin();
  for (const name of sortedChildNames(bin)) {
    if (isVerbDir(path.join(bin, name))) names.add(name);
  }
  return names;
}

export function resolve(verb: string): [string, Source] | null {
  const d = path.join(engineBin(), verb);
  if (isVerbDir(d)) return [d, "engine"];
  for (const [name, pack] of pluginPacks()) {
    const pd = path.join(pack, verb);
    if (isVerbDir(pd)) return [pd, `plugin:${name}`];
  }
  return null;
}

export function resolveVerb(verb: string): string | null {
  const r = resolve(verb);
  return r ? r[0] : null;
}

export function runPy(verb: string): string | null {
  const d = resolveVerb(verb);
  if (!d) return null;
  const p = path.join(d, "run.py");
  return pathExists(p) ? p : null;
}

export function cmdJsonPath(verb: string): string | null {
  const d = resolveVerb(verb);
  if (!d) return null;
  const p = path.join(d, "cmd.json");
  return pathExists(p) ? p : null;
}

export function sourceOf(verb: string): Source | null {
  const r = resolve(verb);
  return r ? r[1] : null;
}

export function isEngineVerb(verb: string): boolean {
  return isVerbDir(path.join(engineBin(), verb));
}

// Every resolvable verb name across engine + plugins + $PLAINKEEP_PATH. A shadowing plugin does not
// change the NAME set — it only loses the resolution.
export function knownVerbs(): Set<string> {
  const names = engineNames();
  for (const [, pack] of pluginPacks()) {
    for (const name of sortedChildNames(pack)) {
      if (isVerbDir(path.join(pack, name))) names.add(name);
    }
  }
  return names;
}

// Sorted, de-duplicated pack names that contribute at least one (non-shadowed) verb. Matches the
// Python original exactly: `seen` gains a name only in the contributing branch, so a non-contributing
// pack does not block a later same-named pack that does contribute.
export function pluginNames(): string[] {
  const engine = engineNames();
  const seen = new Set<string>();
  const out: string[] = [];
  for (const [name, pack] of pluginPacks()) {
    if (seen.has(name)) continue;
    let contributes = false;
    for (const child of sortedChildNames(pack)) {
      const d = path.join(pack, child);
      if (isDir(d) && isVerbDir(d) && !engine.has(child)) {
        contributes = true;
        break;
      }
    }
    if (contributes) {
      seen.add(name);
      out.push(name);
    }
  }
  return out.sort();
}

// All (cmd.json path, source) in resolution order — engine first (reserved). A cmd.json whose verb
// name is already claimed (by an engine cmd.json or an earlier pack) is SKIPPED. NOTE (faithful to
// Python): `seen` is seeded ONLY from engine *cmd.json* dirs, not run.py-only engine dirs.
export function iterCmds(): Array<[string, Source]> {
  const seen = new Set<string>();
  const out: Array<[string, Source]> = [];
  const bin = engineBin();
  for (const name of sortedChildNames(bin)) {
    const cj = path.join(bin, name, "cmd.json");
    if (pathExists(cj)) {
      seen.add(name);
      out.push([cj, "engine"]);
    }
  }
  for (const [pack, dir] of pluginPacks()) {
    for (const name of sortedChildNames(dir)) {
      const cj = path.join(dir, name, "cmd.json");
      if (!pathExists(cj)) continue;
      if (seen.has(name)) continue;
      seen.add(name);
      out.push([cj, `plugin:${pack}`]);
    }
  }
  return out;
}

// (verb, pack) for every plugin verb IGNORED because it collides with a reserved engine verb.
export function shadowed(): Array<[string, string]> {
  const engine = engineNames();
  const out: Array<[string, string]> = [];
  for (const [name, pack] of pluginPacks()) {
    for (const child of sortedChildNames(pack)) {
      const d = path.join(pack, child);
      if (isDir(d) && isVerbDir(d) && engine.has(child)) {
        out.push([child, name]);
      }
    }
  }
  return out;
}
