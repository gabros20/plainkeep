// The guided loop: group → verb → (action) → form → optional dry-run preview → run → render →
// exit-3 confirm. Every action re-enters `plainkeep <verb> --json` as a subprocess (one door).
import * as p from "@clack/prompts";
import pc from "picocolors";
import { loadManifest, groupVerbs, type Manifest, type Verb, type Action } from "./contract.js";
import { fillForm } from "./form.js";
import { renderResult } from "./render.js";
import { runPlainkeep, runPlainkeepInteractive, PlainkeepMissing, EXIT } from "./plainkeep.js";

export async function main(): Promise<number> {
  // Interactive-only, like `plainkeep setup --wizard`: no TTY means no menus. Point at the agent surface.
  if (!process.stdin.isTTY || !process.stdout.isTTY) {
    console.error(
      "plainkeep-ui is an interactive terminal UI — run it in a real terminal.\n" +
        "For non-interactive / scripted use, drive plainkeep directly: `plainkeep <verb> --json` (see `plainkeep help`).",
    );
    return 2;
  }
  console.clear();
  p.intro(pc.bgCyan(pc.black(" plainkeep ")) + pc.dim("  the guided terminal UI — humans point-and-pick, agents use plainkeep <verb> --json"));

  let manifest: Manifest;
  try {
    manifest = await loadManifest();
  } catch (e) {
    if (e instanceof PlainkeepMissing) p.cancel(e.message);
    else p.cancel(String((e as Error).message ?? e));
    return 1;
  }
  p.log.info(pc.dim(`${manifest.schema} · ${manifest.verbs.filter((v) => !v.hidden).length} verbs`));

  // Main loop.
  for (;;) {
    const verb = await pickVerb(manifest);
    if (!verb) break;
    if (verb === "__capture__") {
      await quickCapture();
      continue;
    }
    await drive(verb);
  }

  p.outro(pc.dim("bye — everything you did went through `plainkeep`, guardrail and all."));
  return 0;
}

// --- verb selection: quick capture + grouped palette ---
async function pickVerb(m: Manifest): Promise<Verb | "__capture__" | null> {
  const groups = groupVerbs(m);
  const options: { value: string; label: string; hint?: string }[] = [
    { value: "__capture__", label: pc.cyan("＋ Capture a thought"), hint: "quick note → inbox" },
  ];
  for (const g of groups) {
    for (const v of g.verbs) {
      options.push({ value: v.verb, label: `${riskDot(v.risk)} ${v.verb}`, hint: v.summary });
    }
  }
  options.push({ value: "__quit__", label: pc.dim("✕ quit") });

  const pick = await p.select({ message: "What do you want to do? (type to filter)", options, maxItems: 14 });
  if (p.isCancel(pick) || pick === "__quit__") return null;
  if (pick === "__capture__") return "__capture__";
  return m.verbs.find((v) => v.verb === pick) ?? null;
}

// --- drive one verb: pick an action, fill the form, preview, run ---
async function drive(verb: Verb): Promise<void> {
  let action: Action | null = null;
  if (verb.actions?.length) {
    const pick = await p.select({
      message: `${verb.verb} — pick an action`,
      options: verb.actions.map((a) => ({
        value: a.name,
        label: `${riskDot(a.risk ?? verb.risk)} ${a.name}${a.default ? pc.dim(" (default)") : ""}`,
        hint: a.summary,
      })),
    });
    if (p.isCancel(pick)) return;
    action = verb.actions.find((a) => a.name === pick) ?? null;
  }

  const form = await fillForm(verb, action);
  if (form.cancelled) return;

  // tty verbs (wiki edit → $EDITOR, backup init password): hand off the real terminal.
  if (verb.tty) {
    p.log.info(pc.dim("handing the terminal to plainkeep…"));
    await runPlainkeepInteractive([verb.verb, ...form.argv]);
    return;
  }

  const supportsDry = action ? action.dry_run === true : verb.dry_run === true;
  const effectiveRisk = action?.risk ?? verb.risk;

  // Optional dry-run preview (a dry-run is a read — no --yes needed).
  if (supportsDry) {
    const preview = await p.confirm({
      message: "Preview first? (dry-run — shows what would happen, writes nothing)",
      initialValue: effectiveRisk === "confirm",
    });
    if (p.isCancel(preview)) return;
    if (preview) {
      const s = p.spinner();
      s.start("previewing");
      const res = await runPlainkeep([verb.verb, ...form.argv, "--dry-run"]);
      s.stop("preview");
      renderResult(res);
      const go = await p.confirm({ message: "Run it for real now?", initialValue: false });
      if (p.isCancel(go) || !go) return;
    }
  }

  await execute(verb, form.argv);
}

// Run the verb; if it refuses with exit 3 (confirm), surface the message and offer the exact re-run.
async function execute(verb: Verb, argv: string[]): Promise<void> {
  const s = p.spinner();
  s.start(`running plainkeep ${verb.verb}${argv.length ? " " + argv.join(" ") : ""}`);
  let res = await runPlainkeep([verb.verb, ...argv]);
  s.stop(`plainkeep ${verb.verb}`);

  if (res.exitCode === EXIT.CONFIRM) {
    // The verb self-gated — "refusals teach". Show its message + hint, then offer to confirm.
    renderResult(res);
    const yes = await p.confirm({ message: "Confirm and run with --yes?", initialValue: false });
    if (p.isCancel(yes) || !yes) return;
    const s2 = p.spinner();
    s2.start("running (confirmed)");
    res = await runPlainkeep([verb.verb, ...argv, "--yes"]);
    s2.stop("done");
  }
  renderResult(res);
}

// --- the one hand-built quick flow: capture a thought straight to the inbox ---
async function quickCapture(): Promise<void> {
  const text = await p.text({ message: "Capture", placeholder: "a thought → inbox (triage later)" });
  if (p.isCancel(text) || !String(text).trim()) return;
  const s = p.spinner();
  s.start("capturing");
  const res = await runPlainkeep(["capture", String(text)]);
  s.stop("captured");
  renderResult(res);
}

function riskDot(risk?: string): string {
  switch (risk) {
    case "read": return pc.green("●");
    case "safe_write": return pc.cyan("●");
    case "draft_only": return pc.blue("●");
    case "confirm": return pc.yellow("●");
    case "deny": return pc.red("●");
    default: return pc.dim("●");
  }
}
