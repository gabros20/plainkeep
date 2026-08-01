// The async seam's exit protocol, asserted against the REAL main.ts rather than a reconstruction of
// it. Widening Intercept to allow a promise introduced exactly one new way to leave the frozen
// protocol: a rejection. If the await sat outside main.ts's try, bun would report an unhandled
// rejection and exit 1 — not one of 0/2/3/4/5, produced by the guard whose entire job is to keep
// every failure on it, and with no stderr line naming the cause.
//
// main.ts calls process.exit() at top level, so it cannot be imported here; ./async-reject-probe.ts
// registers a rejecting interception and imports it in a CHILD process, and this asserts that child.
import { test, expect } from "bun:test";
import { mkdtempSync, mkdirSync, writeFileSync, rmSync, readFileSync, existsSync, realpathSync } from "node:fs";
import { markVault } from "./vault-fixture.js";
import { tmpdir } from "node:os";
import path from "node:path";

// A vault with one read-class verb the gate will allow, so the INTERCEPTION decides the outcome. Its
// run.py prints a marker: if the interception were somehow skipped, the spawn would print it and the
// exit code would be 0, which is a different failure than the one under test.
function rejectVault(): string {
  const home = realpathSync(mkdtempSync(path.join(tmpdir(), "pk-async-reject-")));
  markVault(home);  // Task 1b: dispatch validates the root before anything else runs
  const d = path.join(home, "bin", "v_reject");
  mkdirSync(d, { recursive: true });
  writeFileSync(path.join(d, "cmd.json"), JSON.stringify({ verb: "v_reject", risk: "read" }));
  writeFileSync(path.join(d, "run.py"), "print('SPAWNED PYTHON')\n");
  return home;
}

test("a REJECTING async interception exits 5 with a named cause — never bun's unhandled-rejection 1", () => {
  const home = rejectVault();
  try {
    const probe = path.join(import.meta.dir, "async-reject-probe.ts");
    const r = Bun.spawnSync(["bun", "run", probe, "v_reject"], {
      cwd: path.join(import.meta.dir, "..", ".."),
      env: { ...process.env, PLAINKEEP_HOME: home, PLAINKEEP_PATH: "" },
    });
    const stdout = r.stdout.toString();
    const stderr = r.stderr.toString();
    // EXIT_DENY (5): an interception that broke is the same class of failure as a gate that broke —
    // refuse, on protocol, in a way --yes cannot clear. 1 would mean the rejection escaped the try.
    expect([r.exitCode, stderr.trim()]).toEqual([
      5,
      "plainkeep-core: internal error (TypeError)",
    ]);
    // The interception really ran (and really rejected) instead of falling through to the spawn.
    expect(stdout).not.toContain("SPAWNED PYTHON");
    // ...and the gate's audit line was still written, because the interception is post-gate. A
    // rejection must not be able to suppress the record that the verb was allowed to run.
    const logFile = path.join(home, ".logs", "plainkeep.log");
    const log = existsSync(logFile) ? readFileSync(logFile, "utf-8") : "";
    // The trailing space is the audit format's naive `${verb} ${args.join(" ")}` with no args —
    // asserted as-is rather than trimmed, since that join is frozen by the Global Constraints.
    expect(log).toContain("\tv_reject \tallow\tread");
  } finally {
    rmSync(home, { recursive: true, force: true });
  }
});

test("a RESOLVING async interception is awaited, and its result is used verbatim", () => {
  // The other half: the widened seam must actually await, not stringify a pending promise. The probe
  // path only covers rejection, so this covers the happy path through the same real main.ts.
  const home = rejectVault();
  try {
    const probe = path.join(import.meta.dir, "async-resolve-probe.ts");
    const r = Bun.spawnSync(["bun", "run", probe, "v_reject"], {
      cwd: path.join(import.meta.dir, "..", ".."),
      env: { ...process.env, PLAINKEEP_HOME: home, PLAINKEEP_PATH: "" },
    });
    expect([r.exitCode, r.stdout.toString()]).toEqual([3, "answered after a turn of the event loop\n"]);
    expect(r.stdout.toString()).not.toContain("[object Promise]");
  } finally {
    rmSync(home, { recursive: true, force: true });
  }
});
