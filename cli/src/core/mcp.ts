// mcp.ts — `plainkeep mcp` answered IN-PROCESS: the stateless stdio JSON-RPC 2.0 server that
// bin/mcp/run.py serves on the bash floor, ported so the core binary can serve an agent host without
// spawning Python. run.py is UNTOUCHED and still serves `PLAINKEEP_CORE=off`.
//
// It is the SECOND stdio-owning interception (after `ui`) and the FIRST with a stdin lifecycle, which
// is what makes it a different problem rather than a bigger one: a TUI that drops a frame looks
// glitchy, a JSON-RPC server that drops or truncates one hands its peer a malformed session.
//
// ─── WHAT "COMPATIBLE WITH run.py" MEANS HERE, precisely ────────────────────────────────────────────
//
// BYTE-compatible, not merely shape-compatible. CPython's `json.dumps` defaults to the separators
// `", "` and `": "` — WITH the spaces — where `JSON.stringify` emits none, so every single frame would
// differ by whitespace if this used the built-in serializer. Measured on the floor (od -c of a real
// session, .orchestrate/raw/task7-floor-frames.log):
//
//     {"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": "2024-11-05", …}}
//
// so pyJsonDumps() below reproduces CPython's spacing. That is not cosmetic: it is what lets
// test/run_mcp_protocol.py byte-compare whole frames across the two modes instead of comparing parsed
// shapes, which is a strictly weaker oracle (it cannot see an encoding difference at all).
//
// CONTROL FLOW is reproduced too, INCLUDING the shapes where run.py DIES. `handle()` calls
// `method.startswith(…)` without checking the type, `_tool()` indexes `cmd["verb"]` without a default,
// and `serve()` calls `msg.get(…)` on whatever `json.loads` returned — so a peer that sends
// `{"method": 5, "id": 1}`, a JSON array as a frame, or a pack that ships a cmd.json with no `verb`
// kills the Python server with a traceback. Those are not hypothetical edges to paper over: a port
// that quietly kept serving would be a DIFFERENT server, and the difference would show up as the two
// modes disagreeing about whether a session survived. Each one throws ProtocolFatal here, which ends
// the session with a stderr diagnostic naming the cause.
//
// THREE DELIBERATE DIVERGENCES, enumerated because "byte-compatible" must not be read as "identical in
// every respect". Each is a case where fidelity would damage the protocol stream itself:
//
//   1. THE CHILD'S STDIN. `subprocess.run` inherits it, so under run.py a verb that read stdin would
//      eat the peer's un-processed JSON-RPC frames. spawnSync here gives the child an empty pipe
//      (immediate EOF). No verb in bin/ reads stdin today, so this is unobservable in practice and
//      strictly safer if one ever does.
//   2. SIGTERM/SIGINT. run.py takes the default disposition and dies instantly — mid-frame if it is
//      writing. This installs a handler that stops accepting frames, finishes the in-flight one,
//      drains, and returns on the frozen protocol. Costs and second-signal escape hatch: see
//      SHUTDOWN_SIGNALS below.
//   3. EXIT CODES. Every way run.py dies exits 1 (an uncaught traceback), which is OFF the frozen
//      protocol (0/2/3/4/5). runOwningStdio's clamp maps those to EXIT_ANOMALOUS (5). The floor's 1 is
//      not reproducible without putting 1 back on the wire, which is the one thing the clamp exists to
//      prevent.
//
// KNOWN, MEASURED FIDELITY LIMITS — the shapes where this port cannot match run.py byte for byte.
// None is reachable from a cmd.json in this repo or from a conforming MCP client; they are listed
// because an unqualified "byte-compatible" claim would be false, not because they are live defects:
//
//   * A cmd.json containing a `NaN`/`Infinity` literal. `json.loads` accepts both, `JSON.parse` does
//     not, so Python exposes that verb as a tool and this drops it (same direction complete.ts's
//     readCmd() takes, minus the fall-through, which a session cannot have).
//   * A cmd.json nesting deeper than MAX_JSON_DEPTH. Python drops it only past CPython's ~1498-frame
//     recursion limit, so the two disagree in the band 101…~1498.
//   * An integer-valued JSON float (`"default": 5.0`). Indistinguishable from `5` after JSON.parse,
//     so it renders "5" where Python renders "5.0" — the same irreducible limit guardrail.ts's
//     pythonStr() already discloses, and it is inherited from there rather than re-created.
//   * A lone UTF-16 surrogate in a description. `json.dumps(ensure_ascii=False)` emits it raw and then
//     dies encoding stdout; JSON.stringify escapes it to \udXXX and this keeps serving.
//   * INTEGER-LIKE KEYS INSIDE A cmd.json VALUE — a non-string `hints` or `summary` such as
//     `{"2": "b", "1": "a"}`, which rides into the tool `description` verbatim. `JSON.parse` builds an
//     ordinary object, and ECMAScript hoists integer-index keys to the front in ascending order, so
//     the object is ALREADY reordered before this module sees it; Python keeps insertion order. This
//     is the parser's doing, not the serializer's — no serializer can recover an order the parser
//     discarded — and only a hand-written JSON parser could fix it. Pinned as an expected divergence
//     by test/run_mcp_protocol.py so a red suite has a named cause. The same class is what the
//     fuzz suite already XFAILs for `pythonStr` (`json='{"2":1,"1":2}'`).
//
//     NOTE the shape that is NOT on this list any more: an integer-like ARG NAME. That produced the
//     same divergence in `inputSchema.properties` until the r1 fix wave; it is now ELIMINATED rather
//     than documented, because those keys are built here (pyJsonDumps' key-order note) instead of
//     arriving through the parser.
import { spawnSync } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import type { CoreResult } from "./cli.js";
import { jsonDepthExceeds, pyStrip } from "./complete.js";
import { resolveHome, signalNumberOf } from "./dispatch.js";
import { EXIT_CONFIRM, EXIT_OK, pythonStr, pythonTruthy } from "./guardrail.js";
import { EXIT_ANOMALOUS, runOwningStdio } from "./interception.js";
import { iterCmds } from "./resolver.js";

// run.py's two module constants, verbatim.
const PROTOCOL_VERSION = "2024-11-05"; // echoed back to the client if it doesn't pin its own
const SERVER_NAME = "plainkeep";

// The nesting band this port models. Same value and same reasoning as complete.ts's cap: past it,
// CPython's json.loads raises RecursionError (swallowed by manifest.load_cmds()'s bare except, i.e.
// the verb VANISHES) while JSON.parse keeps going. Also what bounds pyJsonDumps' work stack.
const MAX_JSON_DEPTH = 100;

function isPlainObject(v: unknown): v is Record<string, unknown> {
  return typeof v === "object" && v !== null && !Array.isArray(v);
}

// The one way this module ends a session, thrown wherever bin/mcp/run.py would raise. Carrying the
// Python-side cause rather than a generic message is the point: the operator's server just died, and
// "a cmd.json declares no `verb`" is actionable where "internal error" is not.
class ProtocolFatal extends Error {
  constructor(readonly why: string) {
    super(why);
    this.name = "ProtocolFatal";
  }
}

function fatal(why: string): never {
  throw new ProtocolFatal(why);
}

// Diagnostics go to fd 2 DIRECTLY, never through console.*: this module's whole contract is that
// stdout carries nothing but JSON-RPC frames, and fs.writeSync bypasses the stream buffer so a
// diagnostic still lands when the stream itself is the thing that is backed up (interception.ts uses
// it for the same reason).
function diag(text: string): void {
  try {
    fs.writeSync(2, text.endsWith("\n") ? text : `${text}\n`);
  } catch {
    // a diagnostic that cannot be written must not become the failure it is reporting
  }
}

// --------------------------------------------------------------------------------------------------
// CPython-compatible JSON serialization
// --------------------------------------------------------------------------------------------------

// One scalar, as `json.dumps(v, ensure_ascii=False)` renders it. JSON.stringify already agrees on
// string escaping under ensure_ascii=False (both escape only `"`, `\` and < 0x20, both leave U+2028
// and non-ASCII raw); the two places it does not are `allow_nan` (Python emits the bare tokens NaN /
// Infinity, JSON.stringify emits null) and lone surrogates (disclosed in the header).
function scalarJson(v: unknown): string {
  if (v === null || v === undefined) return "null";
  if (typeof v === "boolean") return v ? "true" : "false";
  if (typeof v === "number") {
    if (Number.isNaN(v)) return "NaN";
    if (v === Infinity) return "Infinity";
    if (v === -Infinity) return "-Infinity";
    return JSON.stringify(v) as string;
  }
  return JSON.stringify(String(v));
}

/**
 * `json.dumps(v, ensure_ascii=False)` — CPython's DEFAULT separators, `", "` between items and `": "`
 * after a key. JSON.stringify cannot be configured to emit them and post-processing its output is
 * unsafe (a string value may itself contain `","`), so the containers are walked here.
 *
 * ITERATIVE, for the reason guardrail.ts's pythonRepr() is: the values reaching this come off disk
 * (a cmd.json's summary/hints/default ride into the tool description verbatim), and one stack frame
 * per nesting level would turn a deep sidecar into a stack overflow inside a live session. Input is
 * JSON-decoded, hence acyclic.
 *
 * KEY ORDER, which is the second thing a plain-object walk gets wrong. ECMAScript enumerates
 * INTEGER-INDEX property keys FIRST, in ascending numeric order, before the string keys in insertion
 * order; a CPython dict preserves insertion order for every key alike. So a pack declaring
 * `args: [{"name":"alpha"},{"name":"0"},{"name":"zeta"},{"name":"2"}]` used to render
 * `inputSchema.properties` as `0, 2, alpha, zeta` here against Python's `alpha, 0, zeta, 2` — same
 * frame length, one differing byte in the middle (measured by the r1 spec review at byte 25947 of a
 * 26744-byte tools/list frame). Not reachable from any sidecar in this repo, but a pack could do it,
 * and a byte difference with no named cause is the worst way to find that out.
 *
 * So a `Map` is accepted as the ordered form of a JSON object, and every object this module builds
 * whose keys can come from a cmd.json is built as one. The frames' own keys (`jsonrpc`, `id`,
 * `result`, `type`, `properties`, …) are fixed literals that are never integer-like, so those stay
 * plain objects.
 *
 * WHAT THIS CANNOT FIX, and it is the residual limit disclosed in the header: an object that arrives
 * through `JSON.parse` — a non-string `hints`/`summary` riding into a tool description — has ALREADY
 * been reordered before this function sees it, because JSON.parse builds an ordinary object. No
 * serializer can recover an order the parser discarded; only a hand-written JSON parser could, and
 * the shape is not worth one.
 */
export function pyJsonDumps(v: unknown): string {
  interface Frame {
    open: string;
    close: string;
    values: unknown[];
    keys: string[] | null;
    i: number;
    parts: string[];
  }
  const stack: Frame[] = [];
  let value: unknown = v;
  let done: string | null = null;
  for (;;) {
    if (done === null) {
      if (Array.isArray(value)) {
        stack.push({ open: "[", close: "]", values: value, keys: null, i: 0, parts: [] });
      } else if (value instanceof Map) {
        // A Map is how this module spells "a JSON object whose key ORDER is mine to control" — see
        // the ordering note above. Maps iterate in insertion order for every key shape, which a
        // plain object does not.
        const entries = [...(value as Map<string, unknown>).entries()];
        stack.push({
          open: "{",
          close: "}",
          values: entries.map(([, val]) => val),
          keys: entries.map(([k]) => k),
          i: 0,
          parts: [],
        });
      } else if (isPlainObject(value)) {
        const entries = Object.entries(value);
        stack.push({
          open: "{",
          close: "}",
          values: entries.map(([, val]) => val),
          keys: entries.map(([k]) => k),
          i: 0,
          parts: [],
        });
      } else {
        done = scalarJson(value);
        continue;
      }
    }
    const top = stack[stack.length - 1];
    if (!top) return done as string; // a top-level scalar
    if (done !== null) {
      top.parts.push(top.keys ? `${scalarJson(top.keys[top.i])}: ${done}` : done);
      top.i++;
      done = null;
    }
    if (top.i < top.values.length) {
      value = top.values[top.i];
      continue;
    }
    stack.pop();
    done = top.open + top.parts.join(", ") + top.close;
    if (stack.length === 0) return done;
  }
}

// --------------------------------------------------------------------------------------------------
// The plainkeep surface → the MCP tool list
// --------------------------------------------------------------------------------------------------

// `manifest.load_cmds()`: every visible verb's cmd.json, in RESOLUTION order (engine sorted by
// directory name, then packs), read from the LIVE sidecars through the resolver on every call.
//
// NEVER plainkeep.json (run.md D9). That file is a build artifact of `plainkeep index`; reading it
// would freeze the tool list at the last index, and a pack installed while an agent session is open
// would stay invisible until someone re-indexed. Python re-walks the sidecars per call and so does
// this — which is why the plugin-mid-session case in test/run_mcp_protocol.py is a real assertion and
// not a formality.
//
// NOTE the difference from complete.ts's loadCmds(): that one returns a Map keyed by the verb FIELD
// (mirroring completion.py's dict comprehension, where a later duplicate wins). manifest.load_cmds()
// returns a LIST, de-duplicated by resolver.iterCmds() on the DIRECTORY name only, and _tools() maps
// over it in order — so two sidecars declaring the same `verb` field produce two tools here.
function loadCmds(): Record<string, unknown>[] {
  const out: Record<string, unknown>[] = [];
  for (const [file] of iterCmds()) {
    let data: unknown;
    try {
      // fatal:true so undecodable bytes raise here exactly as Python's read_text(encoding="utf-8")
      // does — replacement would keep a file Python drops.
      data = JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(fs.readFileSync(file)));
    } catch {
      continue; // load_cmds()'s bare `except Exception: pass`
    }
    // `d.get("hidden")` on a non-dict is an AttributeError, caught by that same bare except.
    if (!isPlainObject(data)) continue;
    if (pythonTruthy(data.hidden)) continue;
    if (jsonDepthExceeds(data, MAX_JSON_DEPTH)) continue;
    out.push(data);
  }
  return out;
}

// A dict key as `json.dumps` renders it. Python allows any HASHABLE key and coerces the scalar ones
// on the way out (`5` → "5", `True` → "true", `None` → "null"); a list or dict key is unhashable and
// raises at the assignment, before any JSON is produced.
function dictKey(name: unknown): string {
  if (typeof name === "string") return name;
  if (name === null || name === undefined) return "null";
  if (typeof name === "boolean") return name ? "true" : "false";
  if (typeof name === "number") return scalarJson(name);
  return fatal("an arg `name` is a list or object — Python raises TypeError: unhashable type");
}

// run.py's `_tool(cmd)`: one MCP tool from one cmd.json.
function toolOf(cmd: Record<string, unknown>): Record<string, unknown> {
  // `cmd.get("summary", "")` then `if cmd.get("hints")`. `desc` may end up holding a NON-string (the
  // raw `hints` value) — Python's `desc = … if desc else cmd["hints"]` assigns the value, not str(it)
  // — and that value rides straight into the JSON, so it is kept raw here too.
  let desc: unknown = Object.hasOwn(cmd, "summary") ? cmd.summary : "";
  const hints = Object.hasOwn(cmd, "hints") ? cmd.hints : undefined;
  if (pythonTruthy(hints)) {
    desc = pythonTruthy(desc) ? `${pythonStr(desc)}\n\n${pythonStr(hints)}` : hints;
  }

  // A Map, not an object literal: `props`' keys are ARG NAMES a pack authors, so an integer-like one
  // ("0", or a JSON number 5 that dictKey() renders "5") would be hoisted to the front of a plain
  // object and diverge from Python's insertion order. See pyJsonDumps' key-order note.
  const props = new Map<string, unknown>();
  const required: unknown[] = [];
  const rawArgs = Object.hasOwn(cmd, "args") ? cmd.args : [];
  // `for a in cmd.get("args", [])` — a present `null` reaches `for a in None` and raises, a dict
  // iterates its KEYS and then `a["name"]` indexes a string. Only a real list is reproduced.
  if (!Array.isArray(rawArgs)) fatal("a cmd.json's `args` is present but not a list");
  for (const a of rawArgs) {
    if (!isPlainObject(a)) fatal("a cmd.json's args[] entry is not an object");
    if (!Object.hasOwn(a, "name")) fatal("a cmd.json arg declares no `name` — Python raises KeyError");
    const name = a.name;
    // `"" if a.get("default") is None else f" (default: {a['default']})"` — `is None` is true for an
    // absent key and for a JSON null, and for NOTHING else (0, false and "" all render).
    const dflt = Object.hasOwn(a, "default") ? a.default : null;
    const rendered = dflt === null || dflt === undefined ? "" : ` (default: ${pythonStr(dflt)})`;
    props.set(dictKey(name), { type: "string", description: `positional arg${rendered}` });
    if (pythonTruthy(a.required)) required.push(name);
  }
  // Set AFTER the loop, so a verb that declares an arg literally named `args` has that property
  // overwritten while keeping its original position — true of a Python dict and of a Map alike
  // (`Map.set` on an existing key updates the value and leaves the slot where it was).
  props.set("args", {
    type: "array",
    items: { type: "string" },
    description: "additional positional args / flags (e.g. sub-action tokens, --yes)",
  });
  const schema: Record<string, unknown> = { type: "object", properties: props };
  if (required.length) schema.required = required;

  // `cmd["verb"]` — a KeyError, and it is raised AFTER the args walk above, which is where Python
  // reaches it too.
  if (!Object.hasOwn(cmd, "verb")) {
    fatal("a cmd.json declares no `verb` — Python raises KeyError generating the tool list");
  }
  const verb = cmd.verb;
  return { name: verb, description: pythonTruthy(desc) ? desc : verb, inputSchema: schema };
}

// --------------------------------------------------------------------------------------------------
// tools/call — the one door
// --------------------------------------------------------------------------------------------------

// run.py's `_argv_from`: the DECLARED args first, in the order the cmd.json declares them, then the
// free-form `args` passthrough. Argument ORDER is therefore a property of the sidecar, not of the
// order the client happened to serialize its `arguments` object in — which is what makes the
// argument-ordering case in the protocol suite meaningful.
function argvFrom(cmd: Record<string, unknown> | undefined, args: Record<string, unknown>): string[] {
  const argv: string[] = [];
  const rawArgs = cmd && Object.hasOwn(cmd, "args") ? cmd.args : [];
  if (!Array.isArray(rawArgs)) fatal("a cmd.json's `args` is present but not a list");
  for (const a of rawArgs) {
    if (!isPlainObject(a)) fatal("a cmd.json's args[] entry is not an object");
    if (!Object.hasOwn(a, "name")) fatal("a cmd.json arg declares no `name` — Python raises KeyError");
    const name = a.name;
    // Object.hasOwn, never a bare index: `args` is JSON.parse output, so it carries Object.prototype,
    // and a verb declaring an arg named `toString` would otherwise read an inherited FUNCTION where
    // Python's `arguments.get("toString")` is None. Same hazard dispatch.ts's interceptionFor()
    // guards, arriving through a pack-authored arg name instead of a verb name.
    //
    // A non-string declared name can never match: JSON-RPC object keys are strings, so Python's
    // `arguments.get(5)` is None.
    const v = typeof name === "string" && Object.hasOwn(args, name) ? args[name] : undefined;
    if (v !== undefined && v !== null) argv.push(pythonStr(v));
  }
  const extra = Object.hasOwn(args, "args") ? args.args : undefined;
  if (Array.isArray(extra)) for (const x of extra) argv.push(pythonStr(x));
  return argv;
}

// The child's exit status as Python's `proc.returncode` reports it: a signal death is NEGATIVE.
// signalNumberOf() is dispatch.ts's measured inversion of bun's macOS signal NAMING, reused rather
// than re-derived — reading the platform table here would report the wrong number for exactly the
// signals where the two disagree.
// `signal` is typed WIDER than bun's declaration on purpose: dispatch.ts measured that a runtime can
// report a signal death it declines to name, and `NodeJS.Signals` cannot express the empty string
// that arrives when it does.
function returnCodeOf(status: number | null | undefined, signal: string | null | undefined): number {
  if (status !== null && status !== undefined) return status;
  if (typeof signal === "string" && signal !== "") {
    const n = signalNumberOf(signal);
    if (n !== null) return -n;
    fatal(`the verb was killed by an unrecognized signal '${signal}'`);
  }
  // Neither: the child never ran. subprocess.run raises OSError here (FileNotFoundError when the
  // dispatcher binary is gone, EAGAIN/EMFILE otherwise), which kills run.py.
  return fatal("the dispatcher could not be started for a tool call");
}

function textResult(text: string, isError = false): Record<string, unknown> {
  return { content: [{ type: "text", text }], isError };
}

function resultFrame(mid: unknown, result: Record<string, unknown>): Record<string, unknown> {
  return { jsonrpc: "2.0", id: mid, result };
}

function errorFrame(mid: unknown, code: number, message: string): Record<string, unknown> {
  return { jsonrpc: "2.0", id: mid, error: { code, message } };
}

function callTool(mid: unknown, params: Record<string, unknown>): Record<string, unknown> {
  const rawName = Object.hasOwn(params, "name") ? params.name : undefined;
  const name = pythonTruthy(rawName) ? rawName : ""; // `params.get("name") or ""`
  const rawArguments = Object.hasOwn(params, "arguments") ? params.arguments : undefined;
  const argumentsObj = pythonTruthy(rawArguments) ? rawArguments : {};
  if (!isPlainObject(argumentsObj)) {
    fatal("`arguments` is truthy but not an object — Python raises AttributeError on .get");
  }
  if (typeof name !== "string") {
    fatal("`name` is truthy but not a string — Python raises TypeError building the child argv");
  }

  // `{c["verb"]: c for c in manifest.load_cmds()}` — a missing `verb` is a KeyError. A non-string
  // verb is a legal dict key that no string tool name can ever match, so it is simply not indexed.
  const cmds = new Map<string, Record<string, unknown>>();
  for (const c of loadCmds()) {
    if (!Object.hasOwn(c, "verb")) {
      fatal("a cmd.json declares no `verb` — Python raises KeyError building the tool map");
    }
    if (typeof c.verb === "string") cmds.set(c.verb, c);
  }

  const cmd = cmds.get(name);
  const argv = argvFrom(cmd, argumentsObj);

  // D8 — self-exec THIS binary's own path, never a PATH lookup and never a re-derived shim path.
  // process.execPath IS a dispatcher, so the call re-enters the gate and the audit log exactly as a
  // human typing the verb would, and it keeps working when no `plainkeep` is on PATH at all. Python
  // shells out to $PLAINKEEP_HOME/plainkeep (the shim), which under PLAINKEEP_CORE=auto lands in this
  // same binary one exec later; the observable result is identical and the extra hop is not.
  //
  // `--json` is appended and `--yes` NEVER is. Auto-appending --yes would let an agent execute a
  // confirm-class verb it was never authorized for; the exit-3 branch below hands the confirmation
  // decision back to the caller instead.
  const r = spawnSync(process.execPath, [name, ...argv, "--json"], {
    // stdin is an empty pipe rather than our own: see divergence 1 in the header.
    stdio: ["pipe", "pipe", "pipe"],
    maxBuffer: Number.POSITIVE_INFINITY,
  });
  if (r.error && r.status === null && r.signal === null) {
    fatal(`the dispatcher could not be started for a tool call (${(r.error as NodeJS.ErrnoException).code ?? "unknown error"})`);
  }
  const rc = returnCodeOf(r.status, r.signal);
  // text=True decodes strictly on the Python side, so an undecodable byte kills run.py; fatal:true
  // reproduces that rather than silently substituting U+FFFD into a tool result.
  const decode = (b: Buffer | null): string => {
    try {
      return new TextDecoder("utf-8", { fatal: true }).decode(b ?? new Uint8Array());
    } catch {
      return fatal("a verb wrote bytes that are not valid UTF-8 — Python raises UnicodeDecodeError");
    }
  };
  const out = pyStrip(decode(r.stdout as Buffer | null));
  const err = pyStrip(decode(r.stderr as Buffer | null));

  if (rc === EXIT_CONFIRM) {
    // The rerun string is spelled `plainkeep …`, LITERALLY — it is a line for a human or an agent to
    // run, not a path this process resolved, and run_mcp.py pins the exact text.
    const rerun = ["plainkeep", name, ...argv, "--yes"].join(" ");
    const payload = {
      ops_confirm_needed: true,
      verb: name,
      rerun,
      detail: out || err || "this call is confirm-class — re-run with --yes",
    };
    return resultFrame(mid, textResult(pyJsonDumps(payload), true));
  }
  if (rc === EXIT_OK) return resultFrame(mid, textResult(out || "{}"));
  return resultFrame(mid, textResult(out || err || `exit ${rc}`, true));
}

// --------------------------------------------------------------------------------------------------
// The JSON-RPC method table
// --------------------------------------------------------------------------------------------------

function paramsOf(msg: Record<string, unknown>): Record<string, unknown> {
  const raw = Object.hasOwn(msg, "params") ? msg.params : undefined;
  const params = pythonTruthy(raw) ? raw : {}; // `msg.get("params") or {}`
  if (!isPlainObject(params)) {
    fatal("`params` is truthy but not an object — Python raises AttributeError on .get");
  }
  return params;
}

/**
 * run.py's `handle()`: a response frame, or null for a notification.
 *
 * ONE QUIRK IS REPRODUCED ON PURPOSE, because it is observable and a "cleaner" port would silently
 * change the wire: the `is_notification` test sits at the BOTTOM of the chain, so an id-less `ping`,
 * `tools/list` or `tools/call` still draws a full response frame carrying `"id": null`. Only
 * `notifications/*` (matched earlier) and unknown methods are answered with silence. Confirmed
 * against the floor byte for byte, and pinned as its own case in the protocol suite.
 */
function handle(msg: unknown): Record<string, unknown> | null {
  if (!isPlainObject(msg)) {
    fatal("a JSON-RPC frame is not a JSON object — Python raises AttributeError on .get");
  }
  const method = Object.hasOwn(msg, "method") ? msg.method : undefined;
  const mid = Object.hasOwn(msg, "id") ? msg.id : null;
  const isNotification = !Object.hasOwn(msg, "id");

  if (method === "initialize") {
    const params = paramsOf(msg);
    const pinned = Object.hasOwn(params, "protocolVersion") ? params.protocolVersion : undefined;
    return resultFrame(mid, {
      protocolVersion: pythonTruthy(pinned) ? pinned : PROTOCOL_VERSION,
      capabilities: { tools: { listChanged: false } },
      serverInfo: { name: SERVER_NAME, version: engineVersion() },
    });
  }
  if (pythonTruthy(method)) {
    if (typeof method !== "string") {
      fatal("`method` is truthy but not a string — Python raises AttributeError on .startswith");
    }
    if (method.startsWith("notifications/")) return null;
  }
  if (method === "ping") return resultFrame(mid, {});
  if (method === "tools/list") return resultFrame(mid, { tools: loadCmds().map(toolOf) });
  if (method === "tools/call") return callTool(mid, paramsOf(msg));
  if (isNotification) return null;
  return errorFrame(mid, -32601, `method not found: ${pythonStr(method)}`);
}

// --------------------------------------------------------------------------------------------------
// Identity + the setup line
// --------------------------------------------------------------------------------------------------

// `manifest._engine_version()` — the repo-root VERSION file, `.strip()`ed, "0.0.0" on any failure.
// Python derives it from the manifest MODULE's location (bin/lib/manifest.py → bin/ → its parent);
// this derives it from PLAINKEEP_HOME, which is the same directory in Phase 1 because the engine bin/
// still lives inside the vault. resolver.ts's engineBin() rests on that identical assumption and
// documents it; when the engine moves out of the vault, both move together.
function engineVersion(): string {
  try {
    return pyStrip(fs.readFileSync(path.join(resolveHome(), "VERSION"), "utf-8"));
  } catch {
    return "0.0.0";
  }
}

// The SHIM path, deliberately — not process.execPath. The line is copied into an agent host's config
// and has to survive a core-binary rebuild, a `PLAINKEEP_CORE=off` fallback, and a machine where the
// binary was never installed; only `$PLAINKEEP_HOME/plainkeep` is stable across all three. It is also
// the exact string bin/mcp/run.py prints, so `--setup` is byte-identical in both modes.
function dispatcherBin(): string {
  return path.join(resolveHome(), "plainkeep");
}

export function setupLine(): string {
  return `claude mcp add plainkeep -- ${dispatcherBin()} mcp`;
}

// --------------------------------------------------------------------------------------------------
// The session
// --------------------------------------------------------------------------------------------------

// SIGNAL DISPOSITION — and the reason Task 6's "install nothing" must NOT be inherited here.
//
// `ui` installs no handler and survives SIGTERM anyway, because @clack/prompts leaks a SIGTERM
// listener on every spinner() (field-guide item 5). That is an accident of a dependency, and this
// module imports no clack: with bun's defaults, SIGTERM kills the process instantly — including
// halfway through a write(), which hands the peer a truncated frame and a parse error where an exit
// status was the honest answer.
//
// So the disposition is chosen: stop accepting frames, finish the one in flight, drain, resolve 0.
// WHAT IT COSTS, stated because a graceful handler is not free: while the server is blocked awaiting
// "drain" on a peer that has stopped reading, the first signal cannot interrupt it — where the floor,
// on bun's/CPython's default disposition, would die immediately. A SECOND signal is therefore an
// escape hatch: it abandons the wait and returns EXIT_ANOMALOUS, so the process can still be stopped
// without SIGKILL and without a `process.exit` call site (which the bundle audit would flag).
const SHUTDOWN_SIGNALS: NodeJS.Signals[] = ["SIGTERM", "SIGINT"];

// run.py's `serve()`: read newline-delimited JSON-RPC from stdin, write one response line per
// request, EOF = exit 0.
async function serve(): Promise<number> {
  const stdin = process.stdin;
  const stdout = process.stdout;

  let signals = 0;
  let ended = false;
  let stdinFailure: Error | null = null;
  let stdoutFailure: Error | null = null;
  let abandonedDrain = false;
  const chunks: Buffer[] = [];

  // One wake-up channel for every asynchronous event the loop can be waiting on. Without it the loop
  // would sit inside `for await (const chunk of stdin)` and a signal arriving on an IDLE server would
  // not be noticed until the peer happened to send another byte — i.e. the graceful shutdown would
  // work only while the session was busy, which is precisely when it is least needed.
  let waiters: Array<() => void> = [];
  const wake = (): void => {
    const pending = waiters;
    waiters = [];
    for (const w of pending) w();
  };
  const nextEvent = (): Promise<void> => new Promise<void>((resolve) => waiters.push(resolve));

  const onData = (c: Buffer): void => {
    chunks.push(c);
    wake();
  };
  const onEnd = (): void => {
    ended = true;
    wake();
  };
  const onStdinError = (e: Error): void => {
    stdinFailure = e;
    ended = true;
    wake();
  };
  const stdinFailureNow = (): Error | null => stdinFailure;
  const onStdoutError = (e: Error): void => {
    stdoutFailure = e;
    wake();
  };
  const onSignal = (): void => {
    signals += 1;
    wake();
  };

  // A streaming decoder, not a per-chunk one: a multi-byte character split across a pipe read is
  // ordinary, and decoding each chunk independently would corrupt it. fatal:true because CPython's
  // sys.stdin decodes strictly and dies on an undecodable byte.
  const decoder = new TextDecoder("utf-8", { fatal: true });
  let buf = "";

  // Universal newlines, matching `for line in sys.stdin`: CPython's text mode splits on \n, \r\n AND
  // a lone \r. A trailing \r is held back unless we are at EOF, since the next read may complete a
  // \r\n pair.
  const takeLines = (atEof: boolean): string[] => {
    const lines: string[] = [];
    let start = 0;
    let i = 0;
    while (i < buf.length) {
      const ch = buf[i];
      if (ch === "\n") {
        lines.push(buf.slice(start, i));
        i += 1;
        start = i;
      } else if (ch === "\r") {
        if (i === buf.length - 1 && !atEof) break;
        lines.push(buf.slice(start, i));
        i += buf[i + 1] === "\n" ? 2 : 1;
        start = i;
      } else {
        i += 1;
      }
    }
    buf = buf.slice(start);
    return lines;
  };

  // PER-FRAME BACKPRESSURE, which is the thing interception.ts's drainStream is NOT: that one is an
  // end-of-life drain, bounded at 2 s, and a tool result larger than the pipe buffer (64 KiB on macOS)
  // would be cut off by it. `write()` returning false means the kernel buffer is full and the rest is
  // queued in userspace; the only correct response is to wait for "drain" before writing the next
  // frame, which is also what makes the peer's back-pressure propagate into our own read loop.
  //
  // UNBOUNDED on purpose: CPython's blocking write on a full pipe waits forever too, and giving up
  // early would truncate a frame — the exact failure this exists to prevent.
  const writeFrame = async (text: string): Promise<void> => {
    if (stdoutFailure) return;
    if (stdout.write(text)) return;
    const signalsBefore = signals;
    await new Promise<void>((resolve) => {
      const finish = (): void => {
        stdout.off("drain", onDrain);
        stdout.off("error", onWriteError);
        clearInterval(poll);
        resolve();
      };
      const onDrain = (): void => finish();
      const onWriteError = (): void => finish();
      // The second-signal escape hatch. A signal handler cannot resolve this promise directly (it
      // fires on the event loop, which is exactly where we are parked), so the flag is polled — 25 ms
      // is far below human reaction time and costs nothing while the common case (a reading peer)
      // never enters this branch at all.
      const poll = setInterval(() => {
        if (signals >= signalsBefore + 2) {
          abandonedDrain = true;
          finish();
        }
      }, 25);
      poll.unref();
      stdout.on("drain", onDrain);
      stdout.on("error", onWriteError);
    });
  };

  const handleLine = async (raw: string): Promise<void> => {
    const line = pyStrip(raw);
    if (line === "") return; // `if not line: continue`
    let msg: unknown;
    try {
      msg = JSON.parse(line);
    } catch {
      await writeFrame(`${pyJsonDumps(errorFrame(null, -32700, "parse error"))}\n`);
      return;
    }
    const resp = handle(msg);
    if (resp !== null) await writeFrame(`${pyJsonDumps(resp)}\n`);
  };

  stdin.on("data", onData);
  stdin.on("end", onEnd);
  stdin.on("error", onStdinError);
  stdout.on("error", onStdoutError);
  for (const s of SHUTDOWN_SIGNALS) process.on(s, onSignal);

  try {
    // `stopping` is checked before EVERY line, not once per chunk: one read can carry several frames,
    // and "stop accepting frames" has to mean the next one is not started, not that the rest of the
    // batch runs anyway.
    const stopping = (): boolean => signals > 0 || abandonedDrain || stdoutFailure !== null;
    reading: for (;;) {
      const chunk = chunks.shift();
      if (chunk !== undefined) {
        buf += decoder.decode(chunk, { stream: true });
        for (const line of takeLines(false)) {
          if (stopping()) break reading;
          await handleLine(line);
        }
        continue;
      }
      if (stopping()) break;
      if (ended) {
        // Read through the accessor, not the variable: `stdinFailure` is only ever assigned from an
        // event callback, and the checker's flow analysis — which cannot see that — otherwise narrows
        // it to its `null` initializer and makes `.message` an error on type `never`.
        const readFailure = stdinFailureNow();
        if (readFailure) fatal(`stdin could not be read (${readFailure.message})`);
        buf += decoder.decode(); // flush a dangling multi-byte sequence (throws if truncated)
        const rest = takeLines(true);
        // CPython yields a final line with no terminator; so does this.
        if (buf.length) {
          rest.push(buf);
          buf = "";
        }
        for (const line of rest) {
          if (stopping()) break reading;
          await handleLine(line);
        }
        break;
      }
      await nextEvent();
    }

    if (abandonedDrain) {
      diag(
        "plainkeep mcp: a second shutdown signal abandoned an in-flight frame the peer had stopped " +
          "reading; the session's last frame may be incomplete",
      );
      return EXIT_ANOMALOUS;
    }
    if (stdoutFailure) {
      diag(`plainkeep mcp: the frame stream could not be written (${stdoutFailure.message})`);
      return EXIT_ANOMALOUS;
    }
    // EOF and a graceful shutdown are BOTH 0, and the choice is deliberate rather than defaulted.
    // EOF is how this server is designed to end — the agent host closes the pipe when the session is
    // over, exactly as run.py's `for line in sys.stdin` falling off the end returns 0. A signalled
    // shutdown that finished its in-flight frame and drained has likewise done everything it was
    // asked to do; interception.ts uses the same reasoning to let a deliberate Ctrl-C quit exit 0
    // rather than report an anomaly.
    return EXIT_OK;
  } catch (e) {
    if (e instanceof ProtocolFatal) {
      diag(`plainkeep mcp: ${e.why}`);
      return EXIT_ANOMALOUS;
    }
    if (e instanceof TypeError) {
      // The streaming decoder's way of reporting an undecodable byte. Python dies on it too.
      diag(`plainkeep mcp: stdin is not valid UTF-8 (${e.message})`);
      return EXIT_ANOMALOUS;
    }
    throw e;
  } finally {
    // The handlers must not outlive the session, for the reason interception.ts restores process.exit
    // in a finally: a listener left behind changes the disposition of a process that is no longer
    // running an MCP server.
    stdin.off("data", onData);
    stdin.off("end", onEnd);
    stdin.off("error", onStdinError);
    stdin.pause();
    stdout.off("error", onStdoutError);
    for (const s of SHUTDOWN_SIGNALS) process.off(s, onSignal);
  }
}

/**
 * The `mcp` interception, registered into dispatch.ts's INTERCEPTS post-gate and post-normalization,
 * so the audit line is appended for `mcp` itself exactly as when bin/mcp/run.py served it — and every
 * verb a tool call runs writes its own line, because the call re-enters through the binary.
 *
 * `--setup` is the one BUFFERED answer (`{ stdout }`), and it is safe precisely because no session is
 * running: run.py prints the line and returns before `serve()` is ever reached, so there is no frame
 * stream for main.ts's `console.log(r.stdout)` to corrupt. The SESSION path returns `{ code }` and
 * nothing else — one stray `stdout` field there would append a non-JSON-RPC line after the last frame
 * and the peer would see a protocol error.
 */
export function mcpIntercept(args: string[]): CoreResult | Promise<CoreResult> {
  // `output.parse_argv` strips EVERY `--json` before the flag test, so `plainkeep mcp --setup --json`
  // still prints the setup line.
  if (args.filter((a) => a !== "--json").includes("--setup")) {
    return { stdout: setupLine(), code: EXIT_OK };
  }
  return runOwningStdio("mcp", serve);
}
