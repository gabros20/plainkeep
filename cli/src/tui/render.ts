// Render a plainkeep --json result for a human. The envelope's error.hint / exit-3 re-run / exit-4
// did-you-mean ARE the UX ("refusals teach") — we surface them verbatim, never flatten to "failed".
// Rule: NEVER print raw JSON. Every shape has a human rendering — arrays of objects become tables,
// nested objects become indented key/value blocks, long strings wrap. The TUI exists so you can SEE
// what's going on; a JSON blob is the agent surface leaking through.
import pc from "picocolors";
import type { RunResult } from "./plainkeep.js";
import { EXIT } from "./plainkeep.js";
import { groupVerbs, type Manifest } from "./contract.js";

const MAX_ROWS = 50;
const MAX_CELL = 40;

function termWidth(): number {
  return Math.max(60, Math.min(process.stdout.columns ?? 100, 140));
}

export function renderResult(res: RunResult): void {
  const env = res.envelope;

  // Error / refusal envelope.
  if (env && env.ok === false && env.error) {
    const { code, message, hint } = env.error;
    const label =
      code === EXIT.CONFIRM ? pc.yellow("needs confirmation")
      : code === EXIT.NOT_FOUND ? pc.yellow("not found")
      : code === EXIT.DENY ? pc.red("denied")
      : code === EXIT.USAGE ? pc.yellow("usage")
      : pc.red("error");
    console.log(`  ${label}: ${message}`);
    if (hint) console.log(pc.dim(`  → ${hint}`));
    return;
  }

  // The help manifest is its own view: a grouped verb catalog, not a data dump.
  if (env?.data && Array.isArray((env.data as Record<string, unknown>).verbs)) {
    renderHelp(env.data as unknown as Manifest & { ops_version?: string });
    return;
  }

  // Rows (list verbs): print a compact table.
  if (res.rows.length > 0) {
    printRows(res.rows);
    if (env?.count != null) console.log(pc.dim(`  ${env.count} result${env.count === 1 ? "" : "s"}`));
    return;
  }

  // Scalar data: structured key/value block (tables for nested arrays, never JSON).
  if (env?.data && Object.keys(env.data).length > 0) {
    printObject(env.data, "  ");
    return;
  }

  // Fallback: raw stdout (e.g. a verb run without --json), else a bare ok.
  if (res.raw.trim()) console.log(res.raw.trimEnd());
  else if (env?.ok) console.log(pc.green("  ✓ done"));
}

// --- plainkeep help: the verb catalog, grouped exactly like the main palette ---
function renderHelp(data: Manifest & { ops_version?: string }): void {
  const groups = groupVerbs(data);
  const total = data.verbs.filter((v) => !v.hidden).length;
  const meta = [data.schema, data.ops_version ? `v${data.ops_version}` : "", `${total} verbs`]
    .filter(Boolean).join(" · ");
  console.log(pc.dim(`  ${meta}`));
  const nameW = Math.min(14, Math.max(...groups.flatMap((g) => g.verbs.map((v) => v.verb.length))));
  for (const g of groups) {
    console.log(`\n  ${pc.bold(g.group)}`);
    for (const v of g.verbs) {
      const name = v.verb.length > nameW ? v.verb : v.verb.padEnd(nameW);
      const extras = [
        v.actions?.length ? `${v.actions.length} actions` : "",
        v.dry_run ? "dry-run" : "",
      ].filter(Boolean).join(" · ");
      const summary = clip(v.summary ?? "", termWidth() - nameW - 10 - (extras ? extras.length + 3 : 0));
      console.log(`    ${pc.cyan(name)}  ${summary}${extras ? pc.dim(`  (${extras})`) : ""}`);
    }
  }
  console.log(pc.dim("\n  pick any verb from the main menu to see its actions and run it guided"));
}

// --- rows table ---
function printRows(rows: Record<string, unknown>[], indent = "  "): void {
  // Columns: keys in discovery order that hold at least one primitive value (pure object/array
  // columns can't render in a cell), greedily fitted to the terminal width, max 6.
  const keys = [...new Set(rows.flatMap((r) => Object.keys(r)))];
  const usable = keys.filter((k) => rows.some((r) => isPrim(r[k]) && String(r[k] ?? "") !== ""));
  const cols: string[] = [];
  let budget = termWidth() - indent.length;
  for (const k of usable) {
    const w = Math.min(MAX_CELL, Math.max(k.length, ...rows.map((r) => cell(r[k]).length)));
    if (cols.length >= 6 || (cols.length > 0 && budget - (w + 2) < 0)) break;
    cols.push(k);
    budget -= w + 2;
  }
  if (cols.length === 0) {
    // No primitive columns at all — render each row as its own key/value block.
    rows.slice(0, MAX_ROWS).forEach((r, i) => {
      console.log(`${indent}${pc.dim(`#${i + 1}`)}`);
      printObject(r, indent + "  ");
    });
    return;
  }
  const widths = cols.map((c) =>
    Math.min(MAX_CELL, Math.max(c.length, ...rows.map((r) => cell(r[c]).length))));
  const fit = (s: string, w: number) => (s.length > w ? s.slice(0, w - 1) + "…" : s.padEnd(w));
  console.log(indent + pc.dim(cols.map((c, i) => fit(c, widths[i])).join("  ")));
  for (const r of rows.slice(0, MAX_ROWS)) {
    console.log(indent + cols.map((c, i) => fit(cell(r[c]), widths[i])).join("  "));
  }
  if (rows.length > MAX_ROWS) console.log(pc.dim(`${indent}… and ${rows.length - MAX_ROWS} more`));
}

// --- structured key/value block for scalar envelopes and nested objects ---
function printObject(obj: Record<string, unknown>, indent: string): void {
  for (const [k, v] of Object.entries(obj)) {
    if (v == null || (Array.isArray(v) && v.length === 0)) {
      console.log(`${indent}${pc.dim(k)}: ${pc.dim("—")}`);
    } else if (Array.isArray(v) && v.every(isPrim)) {
      printWrapped(`${pc.dim(k)}: `, v.map(prim).join(", "), indent);
    } else if (Array.isArray(v)) {
      console.log(`${indent}${pc.dim(k)}: ${pc.dim(`(${v.length})`)}`);
      printRows(v as Record<string, unknown>[], indent + "  ");
    } else if (typeof v === "object") {
      console.log(`${indent}${pc.dim(k)}:`);
      printObject(v as Record<string, unknown>, indent + "  ");
    } else {
      printWrapped(`${pc.dim(k)}: `, prim(v), indent);
    }
  }
}

// A long value wraps with a hanging indent instead of running off (or truncating away) the screen.
function printWrapped(label: string, text: string, indent: string): void {
  const labelLen = label.replace(/\x1b\[[0-9;]*m/g, "").length;
  const width = Math.max(20, termWidth() - indent.length - labelLen);
  const words = text.split(/\s+/);
  const lines: string[] = [];
  let line = "";
  for (const w of words) {
    if (line && line.length + 1 + w.length > width) { lines.push(line); line = w; }
    else line = line ? `${line} ${w}` : w;
  }
  if (line) lines.push(line);
  console.log(`${indent}${label}${lines[0] ?? ""}`);
  const hang = " ".repeat(indent.length + labelLen);
  for (const l of lines.slice(1)) console.log(hang + l);
}

function clip(s: string, max: number): string {
  return s.length > Math.max(8, max) ? s.slice(0, Math.max(8, max) - 1) + "…" : s;
}

// --- value formatting: cells and primitives, never JSON, never [object Object] ---
function isPrim(v: unknown): boolean {
  return v == null || ["string", "number", "boolean"].includes(typeof v);
}

function prim(v: unknown): string {
  if (v == null) return "—";
  if (typeof v === "boolean") return v ? "yes" : "no";
  return String(v);
}

function cell(v: unknown): string {
  if (v == null) return "";
  if (typeof v === "boolean") return v ? "yes" : "no";
  if (Array.isArray(v)) {
    if (v.length === 0) return "";
    if (v.every(isPrim)) return v.map(prim).join(", ");
    return `${v.length} item${v.length === 1 ? "" : "s"}`;
  }
  if (typeof v === "object") {
    return Object.entries(v as Record<string, unknown>)
      .filter(([, x]) => isPrim(x))
      .map(([k, x]) => `${k}=${prim(x)}`)
      .join(" ");
  }
  return String(v);
}
