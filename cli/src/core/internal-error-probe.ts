// The injected-throw probe for the gate's last-resort refusal path (MIN-14). NOT named *.test.ts on
// purpose: it replaces ./resolver.js registry-wide with mock.module(), which Bun applies to every file
// in the process, so running it alongside the real suite would break every test that resolves a verb
// for real. `guardrail.internal-error.test.ts` runs it in a CHILD `bun test` process instead, where
// the mock cannot reach anything else, and asserts that child's exit code.
//
// Run directly with: cd cli && bun test src/core/internal-error-probe.ts
import { test, expect, mock } from "bun:test";
import { mkdtempSync, mkdirSync, writeFileSync, rmSync, realpathSync, readFileSync, existsSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import * as realResolver from "./resolver.js";
import { makeEngine } from "./vault-fixture.js";

test("an injected library throw becomes a deterministic deny, and the audit line is still written", async () => {
  const vault = realpathSync(mkdtempSync(path.join(tmpdir(), "pk-guardrail-ie-")));
  // The verb lives in the ENGINE (Phase 2 Task 2), which is a separate tree from the vault. This
  // probe calls mainCli() directly rather than going through runCore(), so nothing activates an
  // engine for it and PLAINKEEP_ENGINE is set here — the same way PLAINKEEP_HOME is.
  const engine = makeEngine(realpathSync(mkdtempSync(path.join(tmpdir(), "pk-guardrail-ie-engine-"))));
  const d = path.join(engine, "bin", "v_boom");
  mkdirSync(d, { recursive: true });
  writeFileSync(path.join(d, "run.py"), "def main(argv):\n    return 0\n");
  writeFileSync(path.join(d, "cmd.json"), JSON.stringify({ verb: "v_boom", risk: "read" }));
  process.env.PLAINKEEP_HOME = vault;
  process.env.PLAINKEEP_PATH = "";

  // cmdJsonPath() is called OUTSIDE cmdField's try — faithful to Python, where the resolver is
  // likewise assumed throw-free — so this propagates through riskOf -> gate, exactly the route a real
  // library defect would take. Every other export stays real.
  mock.module("./resolver.js", () => ({
    ...realResolver,
    cmdJsonPath: () => {
      throw new TypeError("injected resolver failure");
    },
  }));
  const { mainCli, EXIT_DENY } = await import("./guardrail.js");

  const r = mainCli(["v_boom", "--yes"]);
  // DENY (5), not confirm (3): an unevaluable gate must refuse in a way --yes cannot clear, and 5 is
  // the only on-protocol code with that property. Exit 1 plus a stack trace is what this replaced.
  expect(r.code).toBe(EXIT_DENY);
  expect(r.stderr).toBe("guardrail: DENY [deny] — internal gate error (TypeError) — refusing");
  // The audit line is the half of the original defect that mattered most: a throw escaping gate()
  // skipped log() entirely, letting a pathological cmd.json suppress its own record.
  const logFile = path.join(vault, ".logs", "plainkeep.log");
  const line = existsSync(logFile) ? readFileSync(logFile, "utf-8") : "";
  expect(line).toContain("\tv_boom --yes\tdeny\tinternal gate error (TypeError) — refusing");

  rmSync(vault, { recursive: true, force: true });
  rmSync(engine, { recursive: true, force: true });
});
