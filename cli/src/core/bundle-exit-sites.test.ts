// The audit that holds up the cancel-vs-error decision.
//
// interception.ts treats a dependency-initiated `process.exit(0)` as a DELIBERATE QUIT and lets it
// exit 0, because the one in-window call site that produces it is @clack/core's `block()` Ctrl-C
// handler. That is only safe while a second proposition is true: NO OTHER in-window call site exits
// 0. Nothing else in the suite checks that. The pty cancel row and the `dependency-exit-zero` unit
// test both assert "Ctrl-C still exits 0" — a new dependency, or a clack bump, that added an error
// path exiting 0 would leave every one of them green while silently converting a failure into a
// reported success. That is the silent-success class the Global Constraints forbid.
//
// It has to be a BUNDLE check. The property is "what is reachable in the shipped artifact", which no
// source-level test can see: a transitive dependency three levels down is invisible to `grep src/`,
// and a source audit cannot tell what the bundler actually included.
//
// Cost: `bun build --target=bun` of this entry measures 0.01–0.06 s (three runs), against a suite
// that takes ~1.5 s, so it stays in the default `bun test` run rather than being hidden behind a
// flag nobody sets.
import { test, expect } from "bun:test";
import { mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";

const ENTRY = path.join(import.meta.dir, "main.ts");
const CWD = path.join(import.meta.dir, "..", "..");

interface KnownSite {
  id: string;
  why: string;
  // Every marker must appear in the context window around the call. Chosen to be text the SOURCE
  // owns, never a bundler-generated identifier — `D`, `r2` and friends are renamed at will, and a
  // marker that depends on one would go red on an unrelated bundler upgrade.
  markers: string[];
  // The call itself, so swapping two sites' bodies cannot satisfy the audit by count alone.
  call: RegExp;
}

const KNOWN_SITES: KnownSite[] = [
  {
    id: "clack-block-ctrl-c",
    why:
      "@clack/core's block() keypress handler. The ONLY process.exit reachable from INSIDE an " +
      "stdio-owning interception, and it fires on the Ctrl-C byte — i.e. it means 'the user quit', " +
      "which is why interception.ts honours its 0 instead of reporting an anomaly.",
    markers: ["\\x03", "cursor.show"],
    call: /process\.exit\(0\)/,
  },
  {
    id: "main-terminal-exit",
    why:
      "main.ts's own final exit, which is OUTSIDE any interception window (the guard has already " +
      "been restored by then). It exits with the CoreResult's already-clamped code.",
    markers: ["process.kill(process.pid"],
    call: /process\.exit\([A-Za-z_$][\w$]*\.code\)/,
  },
];

// Wide enough to reach each site's markers (measured: the furthest is ~90 chars back), narrow enough
// that two sites 90 KB apart can never see each other's markers.
const BEFORE = 300;
const AFTER = 80;

function buildBundle(): string {
  const dir = mkdtempSync(path.join(tmpdir(), "pk-bundle-audit-"));
  const out = path.join(dir, "bundle.js");
  try {
    const r = Bun.spawnSync(
      ["bun", "build", ENTRY, "--target=bun", "--outfile", out],
      { cwd: CWD, env: { ...process.env } },
    );
    if (r.exitCode !== 0) {
      throw new Error(`bun build failed (${r.exitCode}): ${r.stderr.toString()}`);
    }
    return readFileSync(out, "utf-8");
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
}

test("the shipped bundle contains EXACTLY the two known process.exit call sites", () => {
  const bundle = buildBundle();

  // `\.exit\(` rather than `process\.exit\(`: deliberately wider, so an aliased or destructured
  // reference (`const { exit } = process` is not matched, but `p.exit(...)` after `const p = process`
  // is) still shows up. The paren keeps `.exitCode` out.
  const found = [...bundle.matchAll(/\.exit\(/g)].map((m) => {
    const at = m.index as number;
    return { at, context: bundle.slice(Math.max(0, at - BEFORE), at + AFTER) };
  });

  const unmatched: { at: number; context: string }[] = [];
  const matchedBy = new Map<string, number>();

  for (const site of found) {
    const hit = KNOWN_SITES.find(
      (k) => k.markers.every((mk) => site.context.includes(mk)) && k.call.test(site.context),
    );
    if (!hit) unmatched.push(site);
    else matchedBy.set(hit.id, (matchedBy.get(hit.id) ?? 0) + 1);
  }

  // A NEW site is the failure this test exists for, so it fails loudly and prints what appeared —
  // whoever trips this needs to see the code, not a count.
  if (unmatched.length > 0) {
    const detail = unmatched
      .map((u) => `\n  at offset ${u.at}:\n${u.context.replace(/^/gm, "    | ")}`)
      .join("\n");
    throw new Error(
      `bundle-exit-sites: ${unmatched.length} UNKNOWN process.exit call site(s) appeared in the ` +
        `shipped bundle.\n\n` +
        `This is not a lint failure — it invalidates a decision. interception.ts lets a ` +
        `dependency-initiated exit(0) through as a DELIBERATE QUIT (exit 0), which is only safe ` +
        `while clack's Ctrl-C handler is the sole in-window site that exits 0. If the site below ` +
        `is reachable from inside an interception AND can exit 0 on an error path, then a failure ` +
        `is now being reported as success and the mapping in interception.ts must change.\n\n` +
        `Decide which it is, then either add it to KNOWN_SITES with the reason, or change the ` +
        `mapping.${detail}`,
    );
  }

  // Every known site must be present EXACTLY once. This is the half that catches a removal or a
  // swap, which a count-only assertion would sail past: if clack stopped exiting and something else
  // started, `found.length` would still be 2.
  for (const k of KNOWN_SITES) {
    expect(matchedBy.get(k.id), `known exit site '${k.id}' is missing from the bundle — ${k.why}`)
      .toBe(1);
  }
  expect(found.length).toBe(KNOWN_SITES.length);
});
