// complete.ts — `plainkeep __complete` answered IN-PROCESS, for exactly the cases the verb surface
// alone can answer. A faithful port of the grammar half of bin/lib/completion.py (which stays the
// one completion brain for Python: `plainkeep complete --json` and the floor still run it).
//
// WHY A SPLIT AND NOT A FULL PORT — the seam is where correctness gets expensive. completion.py has
// two kinds of answer in it:
//
//   * the GRAMMAR, derived from the cmd.json sidecars: the verb list, each compound verb's
//     actions[], an arg's inline `enum`, and the many paths that answer nothing at all. That is
//     contract DATA — small, closed, and exhaustively parity-testable against the Python verb, which
//     is why it is worth porting: the TAB path is the hottest, most latency-visible invocation
//     plainkeep has, and this removes its Python spawn entirely.
//   * the PROVIDERS (`note-slug`, `asset-slug`, `task-id`, `hub`, `note-type`, `status`, `layer`),
//     which read the live vault — every wiki/**.md's frontmatter, the tasks/ tree, the notetype
//     registry, filing.STATUSES. Porting those means porting paths/filing/notetype and owning a
//     SECOND frontmatter parser whose divergences would be unbounded (vault content is live and
//     user-authored, so no catalog can enumerate it).
//
// So: grammar in TS, providers fall through — the fall-through spawns the Python verb exactly as
// dispatch() would have, which costs precisely what today costs and can never be worse.
//
// THE SAME ASYMMETRY GOVERNS EVERY MALFORMED-INPUT DECISION BELOW, and it is the reason this file
// is as strict as it is: a cmd.json is attacker-shaped input (a pack ships it), and Python's walk
// raises, coerces, or iterates surprising things on shapes that are not what it expects. Rather
// than model each of those, anything whose Python behavior is not plainly a value this port
// reproduces BAILS to the fall-through — where the Python verb's own behavior is the answer by
// definition, traceback included. Over-strictness costs one spawn; under-strictness costs parity.
import fs from "node:fs";
import type { CoreResult } from "./cli.js";
import { EXIT_OK, codePointCompare, pythonTruthy } from "./guardrail.js";
import { iterCmds } from "./resolver.js";

// The closed provider set completion.py's PROVIDERS dict names. A `complete` value that is NOT one
// of these is not a provider at all: Python's `prov in PROVIDERS` is simply False and the arg
// contributes no candidates, which this port answers in-core.
//
// `layer` is in the set even though its Python provider is `lambda: []` (it is reserved for a future
// setup-layer completion and no verb consumes it yet). Answering it in-core would be trivially
// correct TODAY and wrong the day it grows a body, so it falls through with the other six: the
// boundary is "does the answer come from a PROVIDER", not "does the provider happen to be empty".
const PROVIDER_NAMES = new Set([
  "note-slug", "asset-slug", "task-id", "hub", "note-type", "status", "layer",
]);

// The nesting band this port models, mirroring guardrail.ts's MAX_JSON_DEPTH and chosen for the same
// reason: CPython's json.loads raises RecursionError somewhere around depth 1498 (measured in
// guardrail.ts), where JSON.parse is iterative and parses any depth. Python's raise is swallowed by
// manifest.load_cmds()'s bare `except Exception: pass`, i.e. the verb VANISHES from the surface,
// while this port would have kept it. The exact CPython threshold is frame-dependent and not
// reproducible, so instead of guessing it, anything past a depth no real sidecar comes near bails.
const MAX_JSON_DEPTH = 100;

// Iterative (explicit-stack) depth probe — recursion here would reintroduce the stack overflow the
// cap exists to avoid. `root` is a JSON-decoded value, hence acyclic.
//
// Exported for mcp.ts, which loads the same sidecars under the same cap. Shared rather than copied
// because the cap is a CLAIM about CPython's recursion limit: two copies would drift, and the second
// one would be the one nobody re-measured.
export function jsonDepthExceeds(root: unknown, max: number): boolean {
  const stack: Array<[unknown, number]> = [[root, 1]];
  while (stack.length) {
    const [v, depth] = stack.pop() as [unknown, number];
    if (v === null || typeof v !== "object") continue;
    if (depth > max) return true;
    for (const child of Array.isArray(v) ? v : Object.values(v as Record<string, unknown>)) {
      stack.push([child, depth + 1]);
    }
  }
  return false;
}

// The bail mechanism. Thrown from wherever the decision is made (loading a sidecar, validating a
// shape, reaching a provider) and caught once at the entry point, so every "this one is Python's"
// decision reads as a single line at the place that knows, instead of threading a sentinel through
// every return type. `why` is diagnostic only — nothing user-visible depends on it.
class FallThrough extends Error {
  constructor(readonly why: string) {
    super(why);
    this.name = "FallThrough";
  }
}

function bail(why: string): never {
  throw new FallThrough(why);
}

// (value, description) — the two fields bin/__complete/run.py prints. completion.py's third tuple
// member (`kind`) exists for `plainkeep complete --json`, which this port does not serve, and the
// one decision it drives here (provider → fall through) is taken before a row is ever built.
type Row = [value: string, description: string];

interface ArgSpec {
  name: string;
  // `--x` whose declared `type` is not "flag": it swallows the next token as its value.
  isValueFlag: boolean;
  // Non-null ONLY for a Python-truthy `enum`, i.e. a non-empty list of strings.
  enumValues: string[] | null;
  // Non-null ONLY for a `complete` naming one of PROVIDER_NAMES. An unknown name is null (no
  // candidates), which is what Python's `prov in PROVIDERS` test produces.
  provider: string | null;
}

interface ActionSpec {
  name: string;
  summary: string;
  isDefault: boolean;
  args: ArgSpec[];
}

interface CmdSpec {
  verb: string;
  summary: string;
  // null when the verb declares no actions[] (a scalar verb) — Python's `if not actions` branch,
  // which an empty list also takes.
  actions: ActionSpec[] | null;
}

function isPlainObject(v: unknown): v is Record<string, unknown> {
  return typeof v === "object" && v !== null && !Array.isArray(v);
}

// A field Python reads with `.get(key, "")` and then either prints or passes to `_clean`. A string
// is itself; a Python-FALSY value (absent, null, "", 0, false, [], {}) is indistinguishable from ""
// downstream, because run.py's `if desc` is false for all of them and prints the bare value. A
// truthy non-string would reach `_clean`'s `.replace` and raise AttributeError — Python's answer,
// not this port's.
function pyStringField(v: unknown): string {
  if (typeof v === "string") return v;
  if (pythonTruthy(v)) bail("a summary field is truthy but not a string");
  return "";
}

function validateArg(raw: unknown): ArgSpec {
  if (!isPlainObject(raw)) bail("an args[] entry is not a JSON object");
  const name = raw.name;
  // Every use of an arg name is a string operation in Python (`.startswith`, set membership against
  // tokens, equality with a pending flag), so a non-string name raises there.
  if (typeof name !== "string") bail("an arg declares no string `name`");
  let enumValues: string[] | null = null;
  const rawEnum = raw.enum;
  if (pythonTruthy(rawEnum)) {
    // Python iterates whatever `enum` is — a string yields its CHARACTERS, a dict its KEYS, a number
    // raises — and then formats each element with an f-string, where str(1.0) is "1.0" but
    // String(1.0) is "1". Only a list of strings is reproduced here.
    if (!Array.isArray(rawEnum) || !rawEnum.every((v) => typeof v === "string")) {
      bail("`enum` is truthy but not a list of strings");
    }
    enumValues = rawEnum as string[];
  }
  let provider: string | null = null;
  const rawProv = raw.complete;
  if (pythonTruthy(rawProv)) {
    if (typeof rawProv === "string") {
      if (PROVIDER_NAMES.has(rawProv)) provider = rawProv;
    } else if (Array.isArray(rawProv) || isPlainObject(rawProv)) {
      // `prov in PROVIDERS` raises TypeError on an unhashable value. A truthy number/bool IS
      // hashable, is not a provider name, and correctly yields no candidates.
      bail("`complete` is truthy but unhashable (a list or object)");
    }
  }
  return { name, isValueFlag: name.startsWith("-") && raw.type !== "flag", enumValues, provider };
}

function validateAction(raw: unknown): ActionSpec {
  if (!isPlainObject(raw)) bail("an actions[] entry is not a JSON object");
  const name = raw.name;
  if (typeof name !== "string") bail("an action declares no string `name`");
  const rawArgs = raw.args;
  // Python's `action.get("args", [])` defaults ONLY when the key is absent: a present `null`
  // reaches `for a in None` and raises. Absent or a real list are the two shapes reproduced here.
  let args: ArgSpec[] = [];
  if (rawArgs !== undefined) {
    if (!Array.isArray(rawArgs)) bail("`args` is present but not a list");
    args = rawArgs.map(validateArg);
  }
  return { name, summary: pyStringField(raw.summary), isDefault: pythonTruthy(raw.default), args };
}

// One cmd.json → a CmdSpec, or null when Python's manifest.load_cmds() would have DROPPED it (its
// bare `except Exception: pass`, plus the explicit `hidden` filter). Bails for everything else.
function readCmd(file: string): CmdSpec | null {
  let text: string;
  try {
    // fatal:true so undecodable bytes are an error here exactly as Python's read_text(encoding=
    // "utf-8") raises UnicodeDecodeError — the default replacement behavior would have kept a file
    // Python drops. An unreadable file lands here too, and Python drops that identically.
    text = new TextDecoder("utf-8", { fatal: true }).decode(fs.readFileSync(file));
  } catch {
    return null;
  }
  let data: unknown;
  try {
    data = JSON.parse(text);
  } catch {
    // NOT a drop: Python's json.loads accepts NaN/Infinity literals that JSON.parse rejects, so a
    // parse failure here does not establish that Python drops the file. (For a plainly malformed
    // sidecar both sides drop it and the fall-through merely costs a spawn.)
    bail("cmd.json is not JSON that JSON.parse accepts");
  }
  if (jsonDepthExceeds(data, MAX_JSON_DEPTH)) bail("cmd.json nests past the modelled depth band");
  // manifest.load_cmds() calls `d.get("hidden")` — on a non-dict that is an AttributeError, caught
  // and dropped. Both checks below must stay in THIS order: a hidden verb is dropped before Python
  // ever looks at the rest of it, so a hidden verb with a malformed grammar must not bail.
  if (!isPlainObject(data)) return null;
  if (pythonTruthy(data.hidden)) return null;

  // `{c["verb"]: c for c in load_cmds()}` — a missing key is a KeyError and a non-string key makes
  // the later `sorted(cmds)` raise TypeError against its string siblings. Note this is the cmd.json
  // FIELD, not the directory name: a sidecar may declare a verb name its directory does not carry,
  // and the surface follows the field.
  const verb = data.verb;
  if (typeof verb !== "string") bail("cmd.json declares no string `verb`");
  const rawActions = data.actions;
  let actions: ActionSpec[] | null = null;
  if (pythonTruthy(rawActions)) {
    // `if not actions: return []` already took every falsy shape (absent, null, [], {}, "", 0).
    if (!Array.isArray(rawActions)) bail("`actions` is truthy but not a list");
    actions = (rawActions as unknown[]).map(validateAction);
  }
  return { verb, summary: pyStringField(data.summary), actions };
}

// completion.py's `_cmds()`: verb → cmd.json, hidden verbs already filtered, read from the LIVE
// sidecars through the resolver (never plainkeep.json — run.md D9). Later entries overwrite earlier
// ones on a duplicate verb FIELD, matching the dict comprehension; resolver.iterCmds() has already
// dropped plugin sidecars shadowed by an engine verb DIRECTORY.
function loadCmds(): Map<string, CmdSpec> {
  const cmds = new Map<string, CmdSpec>();
  for (const [file] of iterCmds()) {
    const spec = readCmd(file);
    if (spec) cmds.set(spec.verb, spec);
  }
  return cmds;
}

// `_verb_rows`: sorted(cmds) — Python orders strings by CODE POINT, JS Array.sort by UTF-16 code
// UNIT, and the two disagree for astral verb names (see guardrail.ts's codePointCompare).
function verbRows(cmds: Map<string, CmdSpec>): Row[] {
  return [...cmds.keys()].sort(codePointCompare).map((v) => [v, cmds.get(v)!.summary] as Row);
}

function positionals(action: ActionSpec): ArgSpec[] {
  return action.args.filter((a) => !a.name.startsWith("-"));
}

// `_arg_candidates`: the enum wins over the `complete` provider (an arg may redundantly declare
// both — `task move`'s status does — and the inline enum is the closed set).
function argCandidates(arg: ArgSpec): Row[] {
  if (arg.enumValues !== null) return arg.enumValues.map((v) => [v, ""] as Row);
  if (arg.provider !== null) bail(`the next word is a \`${arg.provider}\` provider value`);
  return [];
}

// `_walk`: replay the tokens already typed for an action → how many POSITIONALS they consumed, and
// which value-flag (if any) is left waiting for its value as the next word.
function walk(action: ActionSpec, argToks: string[]): { consumed: number; pending: string | null } {
  const valueFlags = new Set(action.args.filter((a) => a.isValueFlag).map((a) => a.name));
  let consumed = 0;
  let i = 0;
  let pending: string | null = null;
  while (i < argToks.length) {
    const t = argToks[i];
    if (valueFlags.has(t)) {
      if (i + 1 < argToks.length) {
        i += 2;
      } else {
        pending = t;
        i += 1;
      }
    } else if (t.startsWith("-")) {
      i += 1;
    } else {
      consumed += 1;
      i += 1;
    }
  }
  return { consumed, pending };
}

// `candidates(prior)` — the grammar walk, branch for branch. Every `return []` below is an answer,
// not a failure: it is the shape zsh renders as "no candidates".
function candidates(prior: string[], cmds: Map<string, CmdSpec>): Row[] {
  if (prior.length === 0) return verbRows(cmds);
  const verb = prior[0];
  // `plainkeep help <verb>` completes to the verb list. (`help` itself is NOT intercepted — D6 —
  // but completing ITS argument is pure grammar and stays in-core.)
  if (verb === "help") return verbRows(cmds);
  const c = cmds.get(verb);
  if (!c) return [];
  const actions = c.actions;
  if (!actions) return []; // a scalar/uncompounded verb has no subaction grammar
  const byName = new Map<string, ActionSpec>();
  for (const a of actions) byName.set(a.name, a); // later wins, like the dict comprehension
  const defaultAction = actions.find((a) => a.isDefault) ?? null;
  const toks = prior.slice(1);

  // slot 1 — the subcommand token itself: every keyworded action, plus (for a TOKENLESS default
  // action like `share <slug>`) that action's first positional's values.
  if (toks.length === 0) {
    const out: Row[] = actions.filter((a) => !a.isDefault).map((a) => [a.name, a.summary] as Row);
    if (defaultAction) {
      const pos = positionals(defaultAction);
      if (pos.length) out.push(...argCandidates(pos[0]));
    }
    return out;
  }

  // within an action: resolve WHICH action, then which arg the next word fills.
  const head = toks[0];
  const named = byName.get(head);
  let action: ActionSpec;
  let argToks: string[];
  if (named !== undefined && !named.isDefault) {
    action = named;
    argToks = toks.slice(1);
  } else if (defaultAction) {
    action = defaultAction;
    argToks = toks;
  } else {
    return [];
  }

  const { consumed, pending } = walk(action, argToks);
  if (pending !== null) {
    // `next(a for a in action["args"] if a["name"] == pending)` — pending came out of this action's
    // own args, so the search always succeeds on both sides.
    return argCandidates(action.args.find((a) => a.name === pending) as ArgSpec);
  }
  const pos = positionals(action);
  if (consumed < pos.length) return argCandidates(pos[consumed]);
  return [];
}

// Python's str.strip() set — the characters Py_UNICODE_ISSPACE accepts. It is NOT JS's trim() set:
// Python strips the file/group/record/unit separators \x1c-\x1f and NEL \x85 (JS does not), and JS
// strips the BOM ﻿ (Python does not). A summary carrying any of those is the only place the
// difference shows, and it shows as a byte difference in the completion line.
const PY_STRIP_CHARS = new Set([
  "\t", "\n", "\v", "\f", "\r", "\x1c", "\x1d", "\x1e", "\x1f", " ", "\x85", "\xa0",
  "\u1680", "\u2000", "\u2001", "\u2002", "\u2003", "\u2004", "\u2005", "\u2006", "\u2007",
  "\u2008", "\u2009", "\u200a", "\u2028", "\u2029", "\u202f", "\u205f", "\u3000",
]);

// Exported for mcp.ts: bin/mcp/run.py `.strip()`s every stdin line and every captured child stream,
// and JS trim() is a different character set (see PY_STRIP_CHARS above) — so the port has to use
// Python's, and it must be the one set, not a second table that agrees today.
export function pyStrip(s: string): string {
  let start = 0;
  let end = s.length;
  while (start < end && PY_STRIP_CHARS.has(s[start])) start += 1;
  while (end > start && PY_STRIP_CHARS.has(s[end - 1])) end -= 1;
  return s.slice(start, end);
}

// bin/__complete/run.py's `_clean`: `s.replace(":", " -").strip()`. The colon is the field separator
// zsh's _describe splits on, so a description may not contain one; replace ALL occurrences.
function clean(s: string): string {
  return pyStrip(s.replaceAll(":", " -"));
}

// A lone (unpaired) UTF-16 surrogate, which a `"\ud800"` escape in a cmd.json puts into a string on
// both sides. Python's print() then raises UnicodeEncodeError encoding stdout; this port would
// happily emit the replacement bytes. Rare, but it is a byte difference, so it goes to Python.
function hasLoneSurrogate(s: string): boolean {
  for (let i = 0; i < s.length; i++) {
    const c = s.charCodeAt(i);
    if (c >= 0xd800 && c <= 0xdbff) {
      const next = s.charCodeAt(i + 1);
      if (!(next >= 0xdc00 && next <= 0xdfff)) return true;
      i += 1;
    } else if (c >= 0xdc00 && c <= 0xdfff) {
      return true;
    }
  }
  return false;
}

/**
 * The `__complete` interception, registered into dispatch.ts's INTERCEPTS (post-gate,
 * post-normalization — so the audit line is written for this verb exactly as when Python served it).
 *
 * `fallThrough` is supplied by the dispatcher and spawns the Python `__complete` verb exactly as
 * dispatch() would have. It is a callback rather than an import so this module never has to reach
 * back into the dispatcher it is registered from.
 *
 * Output is bin/__complete/run.py's, byte for byte: one `value:description` line per candidate
 * (`value` alone when the description is empty), with the description colon-cleaned and stripped.
 * The empty-candidate case prints NOTHING AT ALL — not a blank line — which is why no stdout is set
 * rather than an empty string.
 */
export function completeIntercept(args: string[], fallThrough: () => CoreResult): CoreResult {
  let rows: Row[];
  try {
    rows = candidates(args, loadCmds());
  } catch (e) {
    if (e instanceof FallThrough) return fallThrough();
    throw e;
  }
  if (rows.length === 0) return { code: EXIT_OK };
  // main.ts appends the final newline (console.log), so the lines are joined WITHOUT a trailing one:
  // Python's per-line print() produces exactly one terminating newline for the last candidate too.
  const stdout = rows.map(([value, desc]) => (desc ? `${value}:${clean(desc)}` : value)).join("\n");
  if (hasLoneSurrogate(stdout)) return fallThrough();
  return { stdout, code: EXIT_OK };
}
