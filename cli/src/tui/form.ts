// Generate a Clack prompt sequence from an action's args schema, then build the argv for `plainkeep`.
// This is the heart of "no flags to memorize": the form comes from plainkeep.json/3, not hardcoded.
import * as p from "@clack/prompts";
import { complete, type Action, type Arg, type Verb } from "./contract.js";

export interface FilledForm {
  argv: string[]; // the args after the verb (+ action token), excluding --json / --yes
  cancelled: boolean;
}

// Prompt for every arg of an action and assemble the argv. Positionals in declared order, then flags.
export async function fillForm(verb: Verb, action: Action | null): Promise<FilledForm> {
  const args = action?.args ?? verb.args ?? [];
  const argv: string[] = [];
  // A compound verb needs its action token first (unless it's the tokenless default action).
  if (action && !action.default) argv.push(action.name);

  const positionals = args.filter((a) => !a.name.startsWith("-"));
  const flags = args.filter((a) => a.name.startsWith("-"));

  for (const a of positionals) {
    const val = await promptArg(verb, a);
    if (p.isCancel(val)) return { argv: [], cancelled: true };
    if (val !== undefined && val !== "") argv.push(String(val));
  }

  // Flags: offer a multiselect of which optional flags to set, then prompt each chosen one.
  if (flags.length) {
    const chosen = await p.multiselect({
      message: "Options (space to toggle, enter to skip)",
      required: false,
      options: flags.map((f) => ({
        value: f.name,
        label: f.name,
        hint: f.help,
      })),
    });
    if (p.isCancel(chosen)) return { argv: [], cancelled: true };
    for (const fname of chosen as string[]) {
      const f = flags.find((x) => x.name === fname)!;
      if (f.type === "flag") {
        argv.push(f.name); // boolean flag
      } else {
        const val = await promptArg(verb, { ...f, required: true });
        if (p.isCancel(val)) return { argv: [], cancelled: true };
        if (val !== undefined && val !== "") argv.push(f.name, String(val));
      }
    }
  }

  return { argv, cancelled: false };
}

async function promptArg(verb: Verb, a: Arg): Promise<string | symbol | undefined> {
  const label = `${a.name.replace(/^-+/, "")}${a.help ? "" : ""}`;
  const message = a.help ? `${label} — ${a.help}` : label;

  // Enum → select.
  if (a.type === "enum" && a.enum?.length) {
    return p.select({
      message,
      options: a.enum.map((e) => ({ value: e, label: e })),
    }) as Promise<string | symbol>;
  }

  // A completion provider → fetch live candidates and offer a select (with a free-text escape).
  if (a.complete) {
    const cands = await complete([verb.verb]);
    // Filter to candidates for THIS arg's provider kind when the row carries a kind; else use all.
    const rows = cands.filter((c) => !c.kind || c.kind === a.complete || c.kind === "value" || isSlugKind(c.kind));
    if (rows.length) {
      const options = [
        ...rows.map((c) => ({ value: c.value, label: c.value, hint: c.description })),
        { value: "__free__", label: pcDim("↳ type a value…") },
      ];
      const pick = await p.select({ message, options });
      if (p.isCancel(pick)) return pick;
      if (pick !== "__free__") return pick as string;
      // fall through to free text
    }
  }

  // Path / string / int → text.
  return p.text({
    message,
    placeholder: a.example ?? (a.default != null ? String(a.default) : ""),
    defaultValue: a.default != null ? String(a.default) : undefined,
    validate: (v) => {
      if (a.required && !v && a.default == null) return "required";
      if (a.type === "int" && v && !/^-?\d+$/.test(v)) return "must be a whole number";
      return undefined;
    },
  }) as Promise<string | symbol>;
}

function isSlugKind(kind?: string): boolean {
  return kind === "note-slug" || kind === "asset-slug" || kind === "task-id" || kind === "hub";
}

// picocolors dim without importing at top (keep this module prompt-focused)
function pcDim(s: string): string {
  return `\x1b[2m${s}\x1b[0m`;
}
