// Unit tests for the `ui` interception's decisions that are cheap to pin here. The AUTHORITY on the
// TUI actually launching and an action actually running is test/run_tui_pty.py, which drives a real
// PTY — a bun test cannot make process.stdin a terminal, so anything asserted here is deliberately
// the part that does NOT need one: the TTY PREDICATE (given the flags, what would we do), the
// version probe, and the registration's comparability bucket.
import { test, expect } from "bun:test";
import { mkdtempSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { INTERCEPTS, interceptionFor, dispatch } from "./dispatch.js";
import { bareTtyLaunchesUi, isInteractiveTerminal, isVersionProbe, uiIntercept } from "./ui.js";
import { VERSION } from "../tui/version.js";

// process.stdin/stdout.isTTY are plain properties on the stream objects, so the predicate can be
// driven both ways without a terminal. Restores whatever was there, including `undefined` — under
// `bun test` stdout IS a tty and stdin is not, and clobbering either permanently would leak into
// every later test file.
function withTty<T>(stdin: boolean, stdout: boolean, fn: () => T): T {
  const pin = Object.getOwnPropertyDescriptor(process.stdin, "isTTY");
  const pout = Object.getOwnPropertyDescriptor(process.stdout, "isTTY");
  Object.defineProperty(process.stdin, "isTTY", { value: stdin, configurable: true });
  Object.defineProperty(process.stdout, "isTTY", { value: stdout, configurable: true });
  try {
    return fn();
  } finally {
    if (pin) Object.defineProperty(process.stdin, "isTTY", pin);
    else delete (process.stdin as unknown as Record<string, unknown>).isTTY;
    if (pout) Object.defineProperty(process.stdout, "isTTY", pout);
    else delete (process.stdout as unknown as Record<string, unknown>).isTTY;
  }
}

async function withHomeAsync<T>(home: string, fn: () => Promise<T>): Promise<T> {
  const prev = process.env.PLAINKEEP_HOME;
  process.env.PLAINKEEP_HOME = home;
  try {
    return await fn();
  } finally {
    if (prev === undefined) delete process.env.PLAINKEEP_HOME;
    else process.env.PLAINKEEP_HOME = prev;
  }
}

// --------------------------------------------------------------------------------------------------
// DECISION 1 — the TTY predicate. Pinned BOTH ways, because the failure that matters is the widening
// one: if this ever became "stdout only" or "either", a piped bare `plainkeep` would stop printing
// help and every script and agent calling it would break.
// --------------------------------------------------------------------------------------------------

test("bare argv launches the TUI only when BOTH stdin and stdout are terminals", () => {
  expect(withTty(true, true, () => bareTtyLaunchesUi([]))).toBe(true);
  expect(withTty(false, true, () => bareTtyLaunchesUi([]))).toBe(false);
  expect(withTty(true, false, () => bareTtyLaunchesUi([]))).toBe(false);
  expect(withTty(false, false, () => bareTtyLaunchesUi([]))).toBe(false);
});

test("isInteractiveTerminal is the same condition the TUI itself guards on", () => {
  // app.ts refuses with `!process.stdin.isTTY || !process.stdout.isTTY`. If these two ever disagree,
  // bare `plainkeep` on a half-terminal would route to a TUI that immediately refuses with exit 2.
  expect(withTty(true, true, isInteractiveTerminal)).toBe(true);
  expect(withTty(false, true, isInteractiveTerminal)).toBe(false);
  expect(withTty(true, false, isInteractiveTerminal)).toBe(false);
});

test("only a COMPLETELY empty argv is bare — never `plainkeep \"\"`, never a real verb", () => {
  withTty(true, true, () => {
    expect(bareTtyLaunchesUi([])).toBe(true);
    // `plainkeep ""` also dispatches `help` (bash's ${1:-help}), but it is an argument the user
    // typed; silently turning it into a TUI would run a different command than the one they ran.
    expect(bareTtyLaunchesUi([""])).toBe(false);
    expect(bareTtyLaunchesUi(["help"])).toBe(false);
    expect(bareTtyLaunchesUi(["ui"])).toBe(false);
    expect(bareTtyLaunchesUi(["--help"])).toBe(false);
  });
});

// A vault whose only verb is `ui`, read-class exactly as bin/ui/cmd.json declares it, so the gate
// allows and the interception decides the outcome. run.py prints a marker no interception produces,
// which is how a test can tell "the interception ran" from "Python ran" rather than assuming.
function uiVault(): string {
  const home = mkdtempSync(path.join(tmpdir(), "pk-ui-intercept-"));
  const d = path.join(home, "bin", "ui");
  mkdirSync(d, { recursive: true });
  writeFileSync(path.join(d, "cmd.json"), JSON.stringify({ verb: "ui", risk: "read", tty: true }));
  writeFileSync(path.join(d, "run.py"), "print('SPAWNED PYTHON')\n");
  return home;
}

test("bare argv on a terminal dispatches the `ui` VERB — gate line and all", async () => {
  const home = uiVault();
  // The registered interception would open a real TUI, so swap in a probe for the duration. This is
  // the same INTERCEPTS-mutation pattern dispatch.test.ts uses.
  const real = INTERCEPTS.ui;
  const seen: string[][] = [];
  INTERCEPTS.ui = { comparable: false, run: (args) => { seen.push(args); return { code: 0 }; } };
  try {
    const r = await withHomeAsync(home, async () => withTty(true, true, () => dispatch([])));
    expect(r).toEqual({ code: 0 });
    expect(seen).toEqual([[]]);
    // The audit line must name `ui`, not `help`: `ui` is the verb that actually ran, and the log is
    // the record of what this binary did. A line saying `help` would be a false record.
    const log = readFileSync(path.join(home, ".logs", "plainkeep.log"), "utf-8");
    const lines = log.trimEnd().split("\n");
    expect(lines).toHaveLength(1);
    expect(lines[0].split("\t").slice(1)).toEqual(["ui ", "allow", "read"]);
  } finally {
    INTERCEPTS.ui = real;
  }
});

test("bare argv with NO terminal still dispatches the default verb, never the TUI", async () => {
  const home = uiVault();
  const real = INTERCEPTS.ui;
  let reached = false;
  INTERCEPTS.ui = { comparable: false, run: () => { reached = true; return { code: 0 }; } };
  try {
    // No `help` verb exists in this vault, so the gate refuses with not-found (4). That IS the
    // assertion: it proves the dispatch went to `help` and never came near `ui`. Asserting only
    // "the interception was not reached" would also pass if dispatch had crashed.
    const r = await withHomeAsync(home, async () => withTty(false, false, () => dispatch([])));
    expect(reached).toBe(false);
    expect((await r).code).toBe(4);
  } finally {
    INTERCEPTS.ui = real;
  }
});

// --------------------------------------------------------------------------------------------------
// DECISION 4 — `plainkeep ui --version`, answered in-process and headlessly.
// --------------------------------------------------------------------------------------------------

test("--version / -v are recognized anywhere in the args, like the standalone binary", () => {
  expect(isVersionProbe(["--version"])).toBe(true);
  expect(isVersionProbe(["-v"])).toBe(true);
  // index.ts uses process.argv.includes(), i.e. position-independent — preserved here.
  expect(isVersionProbe(["--json", "-v"])).toBe(true);
  expect(isVersionProbe([])).toBe(false);
  expect(isVersionProbe(["--verbose"])).toBe(false);
});

test("the version probe answers WITHOUT a terminal and without touching the TUI", async () => {
  // The setup layer probes headlessly; if this ever fell through to the TTY guard it would answer
  // "run me in a real terminal" (exit 2) and a stale-install check would read that as "unknown".
  const r = await withTty(false, false, () => uiIntercept(["--version"]));
  expect(r).toEqual({ stdout: VERSION, code: 0 });
});

test("the version served is the one compiled into THIS binary's TUI", () => {
  // Not the standalone plainkeep-ui's, which may be absent or stale. version.test.ts separately
  // pins VERSION against bin/ui/version.txt, so this chain ends at the engine-owned file.
  expect(VERSION).toMatch(/^\d+\.\d+\.\d+$/);
});

// --------------------------------------------------------------------------------------------------
// The registration itself.
// --------------------------------------------------------------------------------------------------

test("`ui` is registered NON-comparable, so the parity harness never diffs it against the floor", () => {
  const reg = interceptionFor("ui");
  expect(reg).toBeDefined();
  expect(reg?.comparable).toBe(false);
});
