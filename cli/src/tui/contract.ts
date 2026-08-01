// The plainkeep.json/3 machine contract, loaded from `plainkeep help --json`. plainkeep-ui generates
// its menus + forms from this — new verbs/actions appear automatically; nothing is hardcoded.
import { runPlainkeep } from "./plainkeep.js";

export type Risk = "read" | "safe_write" | "draft_only" | "confirm" | "deny";
export type ArgType = "string" | "int" | "enum" | "slug" | "path" | "flag";

export interface Arg {
  name: string; // positional name, or "--flag"
  type?: ArgType;
  enum?: string[];
  complete?: string; // provider id: note-slug | task-id | hub | note-type | status | layer | asset-slug
  required?: boolean;
  default?: unknown;
  help?: string;
  example?: string;
}

export interface Action {
  name: string;
  summary?: string;
  risk?: Risk;
  dry_run?: boolean; // omitted ⇒ this action does NOT support --dry-run (no inheritance)
  default?: boolean; // a tokenless default action (e.g. share publish)
  args?: Arg[];
}

export interface Verb {
  verb: string;
  summary?: string;
  usage?: string;
  risk?: Risk;
  group?: string;
  dry_run?: boolean;
  tty?: boolean;
  hints?: string;
  args?: Arg[]; // top-level (default action) args
  actions?: Action[]; // compound-verb subcommand grammar
  output?: { mode?: "scalar" | "rows"; fields?: Record<string, string> };
  hidden?: boolean;
  source?: string;
}

export interface Manifest {
  schema: string;
  ops_version?: string;
  capabilities?: Record<string, unknown>;
  verbs: Verb[];
}

const SUPPORTED_SCHEMA_MAJOR = 3;

export async function loadManifest(): Promise<Manifest> {
  const res = await runPlainkeep(["help"]);
  const data = res.envelope?.data as any;
  if (!data || !Array.isArray(data.verbs)) {
    throw new Error("`plainkeep help --json` did not return a verb manifest — is this a plainkeep.json/3 vault?");
  }
  const schema: string = data.schema ?? "plainkeep.json/?";
  const major = Number(String(schema).split("/")[1]);
  if (Number.isFinite(major) && major < SUPPORTED_SCHEMA_MAJOR) {
    throw new Error(
      `this vault reports ${schema}; plainkeep-ui needs plainkeep.json/${SUPPORTED_SCHEMA_MAJOR}+. ` +
        `Update the plainkeep engine (script/update) — the older contract lacks the actions[] grammar.`,
    );
  }
  return data as Manifest;
}

// Group the visible verbs by their display group. Daily-flow groups first, plumbing last (the
// manifest emits group names uppercase, e.g. "FLOW"/"SYSTEM"; match case-insensitively).
const GROUP_ORDER = ["FLOW", "KNOWLEDGE", "TASKS", "WORK", "BUSINESS", "JOBS", "SYSTEM", "OTHER"];

export function groupVerbs(m: Manifest): { group: string; verbs: Verb[] }[] {
  const visible = m.verbs.filter((v) => !v.hidden);
  const byGroup = new Map<string, Verb[]>();
  for (const v of visible) {
    const g = v.group || "OTHER";
    if (!byGroup.has(g)) byGroup.set(g, []);
    byGroup.get(g)!.push(v);
  }
  const rank = (g: string) => (GROUP_ORDER.indexOf(g.toUpperCase()) + 1 || 99);
  const groups = [...byGroup.keys()].sort((a, b) => rank(a) - rank(b));
  return groups.map((g) => ({ group: g, verbs: byGroup.get(g)!.sort((a, b) => a.verb.localeCompare(b.verb)) }));
}

// Completion candidates for an arg, via `plainkeep complete --json`.
export interface Candidate { value: string; description?: string; kind?: string }

export async function complete(priorWords: string[]): Promise<Candidate[]> {
  try {
    const res = await runPlainkeep(["complete", ...priorWords]);
    return res.rows as unknown as Candidate[];
  } catch {
    return [];
  }
}
