// guardrail.ts — a faithful TypeScript port of the DISPATCHER-FACING subset of bin/lib/guardrail.py
// (lines 199–326: _known_verbs, _cmd_field, risk_of, _declares_dry_run, _plugin_lock,
// _plugin_ceiling, gate, _log, _remediation, exit_code_for, main_cli). The verb-side classify()
// path-wall/transmit/secret logic stays Python-only and is NOT ported.
//
// This module is PURE in the sense the brief requires: no process.exit() lives in any library
// function — mainCli() returns a CoreResult and runCore() owns the exit. The one side effect it
// keeps (faithful to Python _log) is appending the gate audit line to $PLAINKEEP_HOME/.logs; those
// writes are swallowed on any error, exactly like the original.
//
// It consumes the ported resolver (knownVerbs / cmdJsonPath / sourceOf) exactly as the Python
// guardrail imports `resolver` — the resolver is the single source of truth for the verb set and
// cmd.json lookup, so plugin verbs are gated identically to engine ones. The bin/-glob fallback in
// the Python original is for standalone loads (parity execs the file directly) and does not apply in
// the compiled binary, so only the resolver-present branch is ported.
import fs from "node:fs";
import path from "node:path";
import type { CoreResult } from "./cli.js";
import { cmdJsonPath, knownVerbs, sourceOf } from "./resolver.js";

// Exit-code protocol (Part 0.3), frozen: confirm→3, not-found→4, deny→5. Mirrors bin/lib/output.py,
// which guardrail.py imports (falling back to these same literals when loaded in isolation).
export const EXIT_OK = 0;
export const EXIT_USAGE = 2;
export const EXIT_CONFIRM = 3;
export const EXIT_NOT_FOUND = 4;
export const EXIT_DENY = 5;

const ALLOW = "allow";
const CONFIRM = "confirm";
const DENY = "deny";
type Verdict = typeof ALLOW | typeof CONFIRM | typeof DENY;

export interface Decision {
  verdict: Verdict;
  reason: string;
  riskClass: string;
}

// Mirrors Python Decision.__str__: `{verdict.upper()} [{risk_class}] — {reason}` (em-dash U+2014,
// single spaces around it). Byte-exact — the gate CLI prints this verbatim to stderr.
export function decisionStr(d: Decision): string {
  return `${d.verdict.toUpperCase()} [${d.riskClass}] — ${d.reason}`;
}

// PLAINKEEP_HOME resolution — identical to resolver.ts opsHome() so plugins.lock.json and the .logs
// audit trail land where the resolver looks for verbs. Read PER CALL (no caching) like Python.
function plainkeepHome(): string {
  const env = process.env.PLAINKEEP_HOME;
  if (env) return env;
  return path.resolve(path.dirname(process.execPath), "..", "..");
}

// _cmd_field: read one key from a verb's RESOLVED cmd.json (engine wins over plugins). Returns null
// on ANY failure — missing file, unreadable, malformed JSON, or a non-object top level — NEVER
// throws. Faithful to Python `json.loads(...).get(key)` guarded by a bare `except`: a missing key
// yields null; `.get` on a non-dict raises in Python (→ caught → None), which a null return matches.
function cmdField(verb: string, key: string): unknown {
  const f = cmdJsonPath(verb);
  if (!f) return null;
  try {
    const data = JSON.parse(fs.readFileSync(f, "utf-8")) as unknown;
    if (data === null || typeof data !== "object") return null;
    const v = (data as Record<string, unknown>)[key];
    return v === undefined ? null : v;
  } catch {
    return null;
  }
}

// A verb's declared risk from its cmd.json — the RAW decoded JSON value (Python risk_of returns
// _cmd_field verbatim; its `str | None` annotation is not enforced). No type clamp: a non-string
// value flows through gate() with Python semantics (0/""/null/[]/{} → default confirm via truthiness;
// 5 / true / "weird" pass through to the ALLOW reason). Null when undeclared.
export function riskOf(verb: string): unknown {
  return cmdField(verb, "risk");
}

// True iff the verb's cmd.json `dry_run` field is PYTHON-truthy (guardrail.py: bool(_cmd_field(...))).
// Must use pythonTruthy, NOT JS Boolean(): JS Boolean([]) / Boolean({}) are true but Python bool([]) /
// bool({}) are false — the JS primitive would downgrade a confirm verb on `dry_run: []` UNSAFELY.
function declaresDryRun(verb: string): boolean {
  return pythonTruthy(cmdField(verb, "dry_run"));
}

// Python bool() over a JSON-decoded value: false/null/0/0.0/""/[]/{} are falsy, everything else
// (incl. "false", [0], {"a":0}, non-zero numbers) is truthy. This is the single coercion primitive
// the gate uses wherever guardrail.py calls bool(...) or relies on `or` truthiness.
export function pythonTruthy(v: unknown): boolean {
  if (v === null || v === undefined) return false;
  if (typeof v === "boolean") return v;
  if (typeof v === "number") return v !== 0; // 0, -0, 0.0 falsy; NaN is not JSON-representable
  if (typeof v === "string") return v.length > 0;
  if (Array.isArray(v)) return v.length > 0;
  if (typeof v === "object") return Object.keys(v).length > 0;
  return Boolean(v);
}

// Render a value the way Python's f-string / str() prints it, so a pass-through non-string risk
// reaches the ALLOW reason + audit log byte-identically. Top-level string → itself (no quotes);
// everything else uses repr() (as Python str() does for non-strings). Verified against the Python CLI:
// 5 → "5", true → "True", [0] → "[0]", {"a":0} → "{'a': 0}". LIMITATION (disclosed, non-authored): a
// JSON float that is integer-valued ("risk": 5.0) is indistinguishable from int 5 after JSON.parse, so
// it renders "5" here vs Python's "5.0"; no realistic cmd.json authors a numeric risk at all.
function pythonNumStr(v: number): string {
  return String(v);
}

function pyStrRepr(s: string): string {
  const quote = s.includes("'") && !s.includes('"') ? '"' : "'";
  let out = quote;
  for (const ch of s) {
    if (ch === "\\") out += "\\\\";
    else if (ch === quote) out += "\\" + quote;
    else if (ch === "\n") out += "\\n";
    else if (ch === "\r") out += "\\r";
    else if (ch === "\t") out += "\\t";
    else {
      const code = ch.codePointAt(0) as number;
      out += code < 0x20 || code === 0x7f ? "\\x" + code.toString(16).padStart(2, "0") : ch;
    }
  }
  return out + quote;
}

function pythonRepr(v: unknown): string {
  if (v === null || v === undefined) return "None";
  if (typeof v === "boolean") return v ? "True" : "False";
  if (typeof v === "number") return pythonNumStr(v);
  if (typeof v === "string") return pyStrRepr(v);
  if (Array.isArray(v)) return "[" + v.map(pythonRepr).join(", ") + "]";
  if (typeof v === "object") {
    const parts = Object.entries(v as Record<string, unknown>).map(
      ([k, val]) => pyStrRepr(k) + ": " + pythonRepr(val),
    );
    return "{" + parts.join(", ") + "}";
  }
  return String(v);
}

export function pythonStr(v: unknown): string {
  return typeof v === "string" ? v : pythonRepr(v);
}

// The `{pack: entry}` map from plugins/plugins.lock.json (empty on any failure). Faithful to Python
// `json.loads(...).get("plugins", {})`: a missing "plugins" key or a non-object file yields {}.
function pluginLock(): Record<string, unknown> {
  try {
    const f = path.join(plainkeepHome(), "plugins", "plugins.lock.json");
    const data = JSON.parse(fs.readFileSync(f, "utf-8")) as unknown;
    if (data === null || typeof data !== "object") return {};
    const plugins = (data as Record<string, unknown>).plugins;
    return plugins && typeof plugins === "object" ? (plugins as Record<string, unknown>) : {};
  } catch {
    return {};
  }
}

// The trust ceiling for a verb from an EXTERNALLY-installed pack (proposal Part 2.2): "confirm" if
// the verb belongs to a pack recorded in plugins.lock.json that has NOT been trusted — a pack's
// self-declared risk never takes effect at install. null for engine verbs, for user-placed packs
// with no lock entry (plugins/local, $PLAINKEEP_PATH), and for explicitly trusted packs.
function pluginCeiling(verb: string): "confirm" | null {
  const src = sourceOf(verb);
  if (!src || !src.startsWith("plugin:")) return null;
  const pack = src.slice("plugin:".length);
  const entry = pluginLock()[pack];
  if (entry === undefined || entry === null) return null;
  if (typeof entry === "object" && (entry as Record<string, unknown>).trusted) return null;
  return "confirm";
}

// Dispatcher per-verb gate: enforce the declared risk class (new/undeclared verbs default to
// confirm). `risk` override is for tests; in normal use it is read from the verb's cmd.json.
export function gate(verb: string, args: string[], riskOverride?: unknown): Decision {
  if (!knownVerbs().has(verb)) {
    return { verdict: DENY, reason: `unknown/invented verb: '${verb}' (not in plainkeep.json)`, riskClass: "deny" };
  }
  // Python `risk = risk or risk_of(verb) or "confirm"` — `or` chains on RAW truthiness, so a falsy
  // declared value (0 / "" / null / [] / {}) defaults to confirm while 5 / true / "weird" pass through.
  let risk: unknown = riskOverride;
  if (!pythonTruthy(risk)) risk = riskOf(verb);
  if (!pythonTruthy(risk)) risk = "confirm";
  const yes = args.includes("--yes") || args.includes("-y");
  const dry = args.includes("--dry-run");
  if (risk === "deny") {
    return { verdict: DENY, reason: `'${verb}' is deny-class — never run`, riskClass: "deny" };
  }
  // Trust ceiling (Part 2.2): an untrusted external pack's verb can't self-declare its way below
  // confirm, and can't use --dry-run to bypass it (we can't trust the pack to honour dry-run).
  const capped = pluginCeiling(verb) === "confirm";
  if (!capped && dry && declaresDryRun(verb) && (risk === "confirm" || risk === "safe_write")) {
    return { verdict: ALLOW, reason: `--dry-run downgrades ${risk} to read (no side effect)`, riskClass: "read" };
  }
  if (capped && (risk === "read" || risk === "safe_write")) risk = "confirm";
  if (risk === "confirm" && !yes) {
    return { verdict: CONFIRM, reason: `'${verb}' is confirm-class — re-run with --yes to proceed`, riskClass: "confirm" };
  }
  // Final allow: Python `Decision(ALLOW, f"{risk}", risk)` — render the raw risk value as Python does.
  const rendered = pythonStr(risk);
  return { verdict: ALLOW, reason: rendered, riskClass: rendered };
}

// Append the gate audit line to $PLAINKEEP_HOME/.logs/plainkeep.log (mkdir -p). Format EXACTLY
// `{ts}\t{verb} {args joined by single spaces}\t{verdict}\t{reason}\n`, ts = UTC ISO-8601 seconds
// precision WITH the +00:00 offset (Python datetime.now(timezone.utc).isoformat(timespec="seconds")).
// Empty/tab/newline args are preserved as-is by the naïve space join. Failures are swallowed.
function log(verb: string, args: string[], d: Decision): void {
  try {
    const logdir = path.join(plainkeepHome(), ".logs");
    fs.mkdirSync(logdir, { recursive: true });
    const ts = new Date().toISOString().replace(/\.\d{3}Z$/, "+00:00");
    const line = `${ts}\t${verb} ${args.join(" ")}\t${d.verdict}\t${d.reason}\n`;
    fs.appendFileSync(path.join(logdir, "plainkeep.log"), line, "utf-8");
  } catch {
    // never crash the gate on a logging failure
  }
}

// The exact re-run line a confirm-class refusal prints — self-teaching, not a class name.
function remediation(verb: string, args: string[]): string {
  const parts = ["plainkeep", verb, ...args];
  if (!args.includes("--yes") && !args.includes("-y")) parts.push("--yes");
  return "re-run: " + parts.join(" ");
}

function exitCodeFor(d: Decision): number {
  return d.verdict === ALLOW ? EXIT_OK : d.verdict === CONFIRM ? EXIT_CONFIRM : EXIT_DENY;
}

// --------------------------------------------------------------------------------------------------
// difflib.get_close_matches port — the load-bearing did-you-mean ranking.
//
// A faithful port of CPython Lib/difflib.py: SequenceMatcher.ratio() over get_matching_blocks() with
// the b2j chaining, plus quick_ratio()/real_quick_ratio() as the exact AND-chain get_close_matches
// applies, then heapq.nlargest(n) ordering. autojunk (popularity pruning) only engages for sequences
// ≥200 chars, which verb names never reach, so it is intentionally omitted (noted in the report).
// Sequences are iterated by CODE POINT (Array.from) to match Python's code-point iteration; verb
// names are ASCII in practice, so this only future-proofs a hypothetical Unicode verb name.
// --------------------------------------------------------------------------------------------------

function calcRatio(matches: number, length: number): number {
  return length ? (2.0 * matches) / length : 1.0;
}

class SequenceMatcher {
  private a: string[] = [];
  private b: string[] = [];
  private b2j = new Map<string, number[]>();

  setSeq2(b: string): void {
    this.b = Array.from(b);
    this.b2j = new Map();
    this.b.forEach((elt, i) => {
      const arr = this.b2j.get(elt);
      if (arr) arr.push(i);
      else this.b2j.set(elt, [i]);
    });
    // autojunk (n >= 200) and isjunk (None) omitted — never engage for verb names.
  }

  setSeq1(a: string): void {
    this.a = Array.from(a);
  }

  private findLongestMatch(alo: number, ahi: number, blo: number, bhi: number): [number, number, number] {
    const { a, b, b2j } = this;
    let besti = alo;
    let bestj = blo;
    let bestsize = 0;
    let j2len = new Map<number, number>();
    for (let i = alo; i < ahi; i++) {
      const newj2len = new Map<number, number>();
      const js = b2j.get(a[i]) ?? [];
      for (const j of js) {
        if (j < blo) continue;
        if (j >= bhi) break;
        const k = (j2len.get(j - 1) ?? 0) + 1;
        newj2len.set(j, k);
        if (k > bestsize) {
          besti = i - k + 1;
          bestj = j - k + 1;
          bestsize = k;
        }
      }
      j2len = newj2len;
    }
    // No junk: the two non-junk extension loops can grow the match across equal chars; the two
    // junk-suck loops (isbjunk always false here) never run and are omitted.
    while (besti > alo && bestj > blo && a[besti - 1] === b[bestj - 1]) {
      besti--;
      bestj--;
      bestsize++;
    }
    while (besti + bestsize < ahi && bestj + bestsize < bhi && a[besti + bestsize] === b[bestj + bestsize]) {
      bestsize++;
    }
    return [besti, bestj, bestsize];
  }

  private matchesCount(): number {
    const la = this.a.length;
    const lb = this.b.length;
    const queue: Array<[number, number, number, number]> = [[0, la, 0, lb]];
    let total = 0;
    while (queue.length) {
      const [alo, ahi, blo, bhi] = queue.pop() as [number, number, number, number];
      const [i, j, k] = this.findLongestMatch(alo, ahi, blo, bhi);
      if (k) {
        total += k;
        if (alo < i && blo < j) queue.push([alo, i, blo, j]);
        if (i + k < ahi && j + k < bhi) queue.push([i + k, ahi, j + k, bhi]);
      }
    }
    return total;
  }

  ratio(): number {
    return calcRatio(this.matchesCount(), this.a.length + this.b.length);
  }

  realQuickRatio(): number {
    const la = this.a.length;
    const lb = this.b.length;
    return calcRatio(Math.min(la, lb), la + lb);
  }

  quickRatio(): number {
    const fullb = new Map<string, number>();
    for (const e of this.b) fullb.set(e, (fullb.get(e) ?? 0) + 1);
    const avail = new Map<string, number>();
    let matches = 0;
    for (const e of this.a) {
      const numb = avail.has(e) ? (avail.get(e) as number) : (fullb.get(e) ?? 0);
      avail.set(e, numb - 1);
      if (numb > 0) matches++;
    }
    return calcRatio(matches, this.a.length + this.b.length);
  }
}

// difflib.get_close_matches(word, possibilities, n=3, cutoff=0.6). Faithful: set_seq2(word),
// set_seq1(x) per possibility, the real_quick_ratio >= quick_ratio >= ratio AND-chain (all upper
// bounds of ratio, so the cutoff membership equals ratio>=cutoff — computed anyway for fidelity),
// then heapq.nlargest(n) = sort by (ratio, name) DESCENDING (name ties broken lexicographically
// larger-first; verb names are distinct so the nlargest decoration counter never breaks a tie).
export function getCloseMatches(word: string, possibilities: string[], n = 3, cutoff = 0.6): string[] {
  const result: Array<[number, string]> = [];
  const s = new SequenceMatcher();
  s.setSeq2(word);
  for (const x of possibilities) {
    s.setSeq1(x);
    if (s.realQuickRatio() >= cutoff && s.quickRatio() >= cutoff && s.ratio() >= cutoff) {
      result.push([s.ratio(), x]);
    }
  }
  result.sort((p, q) => {
    if (p[0] !== q[0]) return q[0] - p[0]; // ratio descending
    return p[1] < q[1] ? 1 : p[1] > q[1] ? -1 : 0; // name descending
  });
  return result.slice(0, n).map(([, x]) => x);
}

// --------------------------------------------------------------------------------------------------
// main_cli — dispatcher-side gate. Pure: returns a CoreResult (stderr text WITHOUT a trailing
// newline; main.ts's console.error supplies the newline that Python's print() adds). Unknown verb →
// not-found (4) with a did-you-mean; else the risk gate, LOGGED, with confirm→3 (printing the exact
// remediation) and deny→5; allow→0 prints nothing.
// --------------------------------------------------------------------------------------------------

export function mainCli(argv: string[]): CoreResult {
  const verb = argv.length ? argv[0] : "help";
  const args = argv.slice(1);
  const known = knownVerbs();
  if (!known.has(verb)) {
    const near = getCloseMatches(verb, [...known].sort(), 3, 0.6);
    const hint = near.length ? ` did you mean: ${near.join(", ")}?` : " (run: plainkeep help)";
    return { stderr: `plainkeep: unknown verb '${verb}'.${hint}`, code: EXIT_NOT_FOUND };
  }
  const d = gate(verb, args);
  log(verb, args, d);
  if (d.verdict === CONFIRM) {
    return { stderr: `guardrail: ${decisionStr(d)}\n  ${remediation(verb, args)}`, code: EXIT_CONFIRM };
  }
  if (d.verdict === DENY) {
    return { stderr: `guardrail: ${decisionStr(d)}`, code: EXIT_DENY };
  }
  return { code: exitCodeFor(d) };
}
