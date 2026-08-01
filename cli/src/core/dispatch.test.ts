// Unit tests for the dispatcher's pure seams. The AUTHORITY on dispatcher behavior is the
// Python-owned differential matrix (test/cases/core-parity/dispatcher.json), which runs the same
// invocations through this binary and through the bash floor; these tests exist for dev speed and to
// pin the two decisions that have no Python counterpart to compare against: the argv preamble's
// bash-isms, and the choice NOT to canonicalize PLAINKEEP_HOME.
import { test, expect } from "bun:test";
import { spawnSync } from "node:child_process";
import { mkdtempSync, readFileSync, rmSync, symlinkSync, mkdirSync, writeFileSync, chmodSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { EXIT_DENY } from "./guardrail.js";
import {
  blockingRestoreFailure,
  classifySpawnOutcome,
  dispatch,
  INTERCEPTS,
  interceptionFor,
  pickPython,
  resolveHome,
  signalNumberOf,
  verbFromArgv,
  type SpawnOutcome,
} from "./dispatch.js";

function withHome<T>(home: string, fn: () => T): T {
  const prev = process.env.PLAINKEEP_HOME;
  process.env.PLAINKEEP_HOME = home;
  try {
    return fn();
  } finally {
    if (prev === undefined) delete process.env.PLAINKEEP_HOME;
    else process.env.PLAINKEEP_HOME = prev;
  }
}

// The async twin, and it is not optional sugar: withHome's `finally` runs when fn RETURNS, so
// handing it an async callback restores PLAINKEEP_HOME while the callback is still suspended and
// every subsequent await runs against the wrong vault. dispatch() may now return a promise, so any
// awaited dispatch belongs here.
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

test("verbFromArgv: no argv is the default verb `help` with no args", () => {
  expect(verbFromArgv([])).toEqual({ verb: "help", args: [] });
});

test("verbFromArgv: an EMPTY first arg is also `help` (bash ${1:-help} substitutes on empty)", () => {
  // and the empty string is CONSUMED by the shift — it must not reappear in args
  expect(verbFromArgv([""])).toEqual({ verb: "help", args: [] });
  expect(verbFromArgv(["", "x"])).toEqual({ verb: "help", args: ["x"] });
});

test("verbFromArgv: -h/--help rewrite to `help` AFTER the shift, so args survive", () => {
  expect(verbFromArgv(["-h"])).toEqual({ verb: "help", args: [] });
  expect(verbFromArgv(["--help", "extra"])).toEqual({ verb: "help", args: ["extra"] });
});

test("verbFromArgv: a normal verb keeps every arg positionally, empties included", () => {
  expect(verbFromArgv(["capture", "", "a b", "😀"])).toEqual({
    verb: "capture",
    args: ["", "a b", "😀"],
  });
});

test("resolveHome: a caller-supplied PLAINKEEP_HOME is returned VERBATIM, never canonicalized", () => {
  const real = mkdtempSync(path.join(tmpdir(), "pk-home-real-"));
  const holder = mkdtempSync(path.join(tmpdir(), "pk-home-link-"));
  const alias = path.join(holder, "vault");
  symlinkSync(real, alias);
  try {
    // The whole point: the alias and its realpath are different strings, and the dispatcher must
    // hand the child the one the caller typed. Canonicalizing here would silently relocate .logs and
    // the resolver's engine bin/ relative to what the operator asked for.
    expect(withHome(alias, resolveHome)).toBe(alias);
    expect(alias).not.toBe(real);
  } finally {
    rmSync(holder, { recursive: true, force: true });
    rmSync(real, { recursive: true, force: true });
  }
});

test("resolveHome: with no env, home is two parents above the executable", () => {
  const prev = process.env.PLAINKEEP_HOME;
  delete process.env.PLAINKEEP_HOME;
  try {
    expect(resolveHome()).toBe(path.resolve(path.dirname(process.execPath), "..", ".."));
  } finally {
    if (prev !== undefined) process.env.PLAINKEEP_HOME = prev;
  }
});

// --------------------------------------------------------------------------------------------------
// The interception seam (INTERCEPTS). Two properties, both of which an interception written in
// cli.ts instead would silently lose: it must be reached for EVERY spelling that normalizes to the
// verb, and the gate's audit line must still be written for the verb that ran.
// --------------------------------------------------------------------------------------------------

// A vault with one read-class verb `help`, so the gate allows and INTERCEPTS decides the outcome.
function helpVault(): string {
  const home = mkdtempSync(path.join(tmpdir(), "pk-intercept-"));
  const d = path.join(home, "bin", "help");
  mkdirSync(d, { recursive: true });
  writeFileSync(path.join(d, "cmd.json"), JSON.stringify({ verb: "help", risk: "read" }));
  writeFileSync(path.join(d, "run.py"), "print('SPAWNED PYTHON')\n");
  return home;
}

test("INTERCEPTS is reached for all six spellings of the verb, and the audit line is written", async () => {
  const home = helpVault();
  const seen: string[][] = [];
  INTERCEPTS.help = {
    comparable: true,
    run: (args) => {
      seen.push(args);
      return { stdout: "in-core help", code: 0 };
    },
  };
  try {
    // The six argv shapes that mean `help`: bare, empty-string, both -h/--help spellings, the literal
    // verb, and a flag spelling carrying args (which must survive normalization).
    const spellings: string[][] = [[], [""], ["-h"], ["--help"], ["help"], ["--help", "topics"]];
    await withHomeAsync(home, async () => {
      for (const argv of spellings) {
        const r = await dispatch(argv);
        // Reached the interception, not the spawn: the fixture's run.py would have printed something
        // else entirely, and stdio: "inherit" means it would not show up here at all.
        expect([argv, r.code, r.stdout]).toEqual([argv, 0, "in-core help"]);
      }
    });
    expect(seen).toEqual([[], [], [], [], [], ["topics"]]);
    // ...and the gate still logged every one of them. An interception placed before mainCli() would
    // leave this file absent — the Global Constraints' "unwritten audit line" hazard.
    const log = readFileSync(path.join(home, ".logs", "plainkeep.log"), "utf-8");
    const lines = log.trimEnd().split("\n");
    expect(lines).toHaveLength(spellings.length);
    for (const line of lines) expect(line).toContain("\thelp");
    expect(lines[lines.length - 1]).toContain("help topics");
  } finally {
    delete INTERCEPTS.help;
    rmSync(home, { recursive: true, force: true });
  }
});

// The eight names that live on Object.prototype. A verb name is a directory name a pack ships, so
// `INTERCEPTS[verb]` resolved an inherited member for each of these and the dispatcher treated it as
// a registered interception — the verb never ran, and toString/constructor exited 0 while the audit
// line said the verb was allowed. The authority on this is dispatcher.json's prototype-named-verbs
// case (both dispatchers, real verbs, real markers); these two pin the lookup itself.
const PROTOTYPE_NAMES = [
  "toString", "constructor", "valueOf", "hasOwnProperty",
  "isPrototypeOf", "propertyIsEnumerable", "toLocaleString", "__proto__",
];

test("interceptionFor: an Object.prototype member name is NOT a registered interception", () => {
  for (const name of PROTOTYPE_NAMES) {
    expect([name, interceptionFor(name)]).toEqual([name, undefined]);
  }
  // ...and the one real registration still resolves, so this is not "always undefined".
  expect(interceptionFor("__complete")?.comparable).toBe(true);
});

test("dispatch: a verb named after an Object.prototype member RUNS, it is not swallowed", async () => {
  const home = mkdtempSync(path.join(tmpdir(), "pk-proto-verb-"));
  try {
    for (const name of PROTOTYPE_NAMES) {
      const d = path.join(home, "bin", name);
      mkdirSync(d, { recursive: true });
      writeFileSync(path.join(d, "cmd.json"), JSON.stringify({ verb: name, risk: "read" }));
      // Exit 7 is the discriminator: it can only come from the verb having actually run. The old
      // behavior returned undefined (which process.exit renders as 0) or threw.
      writeFileSync(path.join(d, "run.py"), "raise SystemExit(7)\n");
    }
    for (const name of PROTOTYPE_NAMES) {
      const r = await withHomeAsync(home, async () => dispatch([name]));
      expect([name, r.code]).toEqual([name, 7]);
    }
  } finally {
    rmSync(home, { recursive: true, force: true });
  }
});

test("INTERCEPTS does not fire for a verb the gate refused — the gate still decides first", async () => {
  const home = mkdtempSync(path.join(tmpdir(), "pk-intercept-deny-"));
  const d = path.join(home, "bin", "danger");
  mkdirSync(d, { recursive: true });
  writeFileSync(path.join(d, "cmd.json"), JSON.stringify({ verb: "danger", risk: "deny" }));
  let fired = false;
  INTERCEPTS.danger = {
    comparable: true,
    run: () => {
      fired = true;
      return { code: 0 };
    },
  };
  try {
    const r = await withHomeAsync(home, async () => dispatch(["danger"]));
    expect(r.code).toBe(EXIT_DENY);
    expect(fired).toBe(false);
  } finally {
    delete INTERCEPTS.danger;
    rmSync(home, { recursive: true, force: true });
  }
});

// --------------------------------------------------------------------------------------------------
// Spawn-failure classification. Synthetic outcomes rather than real resource exhaustion: reaching
// EMFILE or EAGAIN for real means filling the process's fd table or the machine's process slots,
// which is not something a unit test should do to the machine running it.
// --------------------------------------------------------------------------------------------------

test("classifySpawnOutcome: only ENOENT is reported as a missing interpreter", () => {
  const enoent = classifySpawnOutcome({ status: null, signal: null, error: { code: "ENOENT" } }, "python3");
  expect(enoent.code).toBe(127);
  expect(enoent.stderr).toBe("plainkeep: interpreter not found: 'python3'");
});

test("classifySpawnOutcome: a non-ENOENT spawn failure names the errno and claims nothing else", () => {
  for (const errno of ["EAGAIN", "EMFILE", "ENFILE", "ENOMEM", "ENOEXEC"]) {
    const r = classifySpawnOutcome({ status: null, signal: null, error: { code: errno } }, "python3");
    // Not 127: that is the shell's "command not found", a diagnosis this outcome does not support.
    expect([errno, r.code]).toEqual([errno, 201]);
    expect(r.stderr).toBe(`plainkeep: could not start interpreter 'python3' (${errno})`);
    expect(r.stderr).not.toContain("not found");
  }
  const bare = classifySpawnOutcome({ status: null, signal: null }, "python3");
  expect(bare.code).toBe(201);
  expect(bare.stderr).toContain("unknown error");
});

test("classifySpawnOutcome: EACCES stays the 126 'cannot execute' case", () => {
  const r = classifySpawnOutcome({ status: null, signal: null, error: { code: "EACCES" } }, "/x/python3");
  expect(r.code).toBe(126);
  expect(r.stderr).toBe("plainkeep: cannot execute interpreter '/x/python3'");
});

test("classifySpawnOutcome: an EMPTY signal name is a signal death, not a missing interpreter", () => {
  // `if (r.signal)` is false for "" as well as for null, so this outcome used to fall through to the
  // spawn-failure branch and be reported as an interpreter that could not be found.
  const r = classifySpawnOutcome({ status: null, signal: "" }, "python3");
  expect(r.code).toBe(200);
  expect(r.stderr).toContain("unnamed signal");
  expect(r.stderr).not.toContain("not found");
});

test("classifySpawnOutcome: a status wins over anything bun puts in `error`", () => {
  // bun sets error.code to the exit status on a normal non-zero exit, so `error` can never be the
  // signal that a spawn FAILED.
  expect(classifySpawnOutcome({ status: 7, signal: null, error: { code: "7" } }, "python3")).toEqual({ code: 7 });
  expect(classifySpawnOutcome({ status: 0, signal: null }, "python3")).toEqual({ code: 0 });
});

test("classifySpawnOutcome: a named signal death carries the number for main.ts to re-raise", () => {
  expect(classifySpawnOutcome({ status: null, signal: "SIGTERM" }, "python3")).toEqual({ code: 143, signal: 15 });
  const unknown = classifySpawnOutcome({ status: null, signal: "SIGNOTREAL" }, "python3");
  expect(unknown.code).toBe(200);
  expect(unknown.signal).toBeUndefined();
});

test("blockingRestoreFailure: a helper that ran and exited 0 says nothing", () => {
  // The overwhelmingly common outcome, and the one where a diagnostic would be pure noise: this runs
  // on every piped verb.
  expect(blockingRestoreFailure({ status: 0, signal: null })).toBeNull();
});

test("blockingRestoreFailure: every way the helper can fail produces a reason", () => {
  // Silence here is the CRITICAL O_NONBLOCK defect restored with nothing said about it, so each of
  // these must be a line on stderr rather than a fall-through.
  expect(blockingRestoreFailure({ status: null, signal: null, error: { code: "EMFILE" } })).toBe("EMFILE");
  expect(blockingRestoreFailure({ status: null, signal: null, error: { code: "EAGAIN" } })).toBe("EAGAIN");
  expect(blockingRestoreFailure({ status: null, signal: "SIGKILL" })).toBe("killed by SIGKILL");
  expect(blockingRestoreFailure({ status: 1, signal: null })).toBe("exit 1");
});

test("classifySpawnOutcome: agrees with what spawnSync really reports for a missing interpreter", () => {
  // The synthetic cases above are only as good as their shape, so pin the shape against reality once.
  const r = spawnSync("plainkeep-definitely-not-an-interpreter", ["x"], { stdio: "ignore" });
  expect(classifySpawnOutcome(r as SpawnOutcome, "plainkeep-definitely-not-an-interpreter").code).toBe(127);
});

// --------------------------------------------------------------------------------------------------
// Signal-number recovery. These kill REAL children, because the thing under test is not our table —
// it is whether our table still inverts the naming convention the running bun actually uses. Bun
// names a child's death signal with the LINUX name for its number, which on macOS is the wrong name
// for this platform (30 -> "SIGPWR", 31 -> "SIGSYS", 10 -> "SIGUSR1"), so trusting the name re-raises
// a DIFFERENT signal than the one that killed the verb. If a future bun switches to platform names,
// the inversion becomes the bug — and this test is what says so, in either direction.
// --------------------------------------------------------------------------------------------------

// Numbers that terminate by default on BOTH macOS and Linux, including all five whose names the two
// platforms disagree about (7, 10, 12, 30, 31 — where the whole defect lives).
const TERMINATING_SIGNALS = [1, 2, 3, 6, 7, 10, 12, 13, 14, 15, 30, 31];

test("signalNumberOf recovers the TRUE number for every signal a child can die of", () => {
  for (const n of TERMINATING_SIGNALS) {
    const r = spawnSync(
      "python3",
      ["-c", `import os, signal\nsignal.signal(${n}, signal.SIG_DFL)\nos.kill(os.getpid(), ${n})`],
      { stdio: "ignore" },
    );
    expect(r.status).toBeNull(); // it really died by the signal rather than exiting
    expect(r.signal).toBeTruthy();
    // The assertion that matters: the number we recover is the number that was sent, whatever bun
    // decided to call it.
    expect([n, signalNumberOf(r.signal as string)]).toEqual([n, n]);
  }
});

test("signalNumberOf never yields 0 or a bogus number for an unknown name", () => {
  // A name in neither table (Linux real-time signals are the realistic case) must be null, so the
  // caller reports it instead of re-raising signal 0 and exiting a meaningless 128.
  expect(signalNumberOf("SIGNOTAREALSIGNAL")).toBeNull();
  expect(signalNumberOf("")).toBeNull();
  for (const n of TERMINATING_SIGNALS) expect(signalNumberOf(`SIG_${n}`)).toBeNull();
});

// Signal DELIVERY, the half the r1 sweep left unmeasured. Recovering the right number proves nothing
// about whether re-raising it kills us by it: spec re-review r2 found five signals where it does not,
// and neither the r1 unit sweep (which stopped at number recovery) nor the r1 matrix (seven signals,
// none of the five) could see it. So this pins the DELIVERY class of every signal the matrix pins
// end-to-end, and it does so by re-raising in a CHILD bun — the same runtime that will do it for
// real, without killing the test runner.
//
// This is deliberately NOT a re-implementation of the end-to-end dispatcher check: whether a verb's
// death reaches the CALLER intact needs a real vault, a real floor and a real binary, which is the
// Python matrix's job (dispatcher.json, signal-passthrough-matrix). This is the runtime primitive
// underneath it, measured here because a bun upgrade is the thing that moves it.
//
// bun 1.3.14 / macOS arm64, from .orchestrate/raw/task4-fix2-signal-matrix.log.
const DELIVERY: Array<[string, number, "delivered" | "sigtrap" | "ignored"]> = [
  ["SIGHUP", 1, "delivered"],
  ["SIGINT", 2, "delivered"],
  ["SIGQUIT", 3, "delivered"],
  ["SIGILL", 4, "sigtrap"],
  ["SIGTRAP", 5, "delivered"],
  ["SIGABRT", 6, "delivered"],
  ["SIGFPE", 8, "sigtrap"],
  ["SIGKILL", 9, "delivered"],
  ["SIGBUS", 10, "sigtrap"],
  ["SIGSEGV", 11, "sigtrap"],
  ["SIGSYS", 12, "delivered"],
  ["SIGPIPE", 13, "ignored"],
  ["SIGALRM", 14, "delivered"],
  ["SIGTERM", 15, "delivered"],
  ["SIGXCPU", 24, "delivered"],
  ["SIGXFSZ", 25, "ignored"],
  ["SIGVTALRM", 26, "delivered"],
  ["SIGPROF", 27, "delivered"],
  ["SIGUSR1", 30, "delivered"],
  ["SIGUSR2", 31, "delivered"],
];

test("re-raising a signal in the bun runtime delivers exactly the classes the matrix pins", () => {
  // macOS numbers; on another platform the same numbers name different signals, so only assert the
  // classes where the two agree — the Python matrix, which resolves names per platform, is the
  // portable guard.
  if (process.platform !== "darwin") return;
  for (const [name, n, expected] of DELIVERY) {
    const r = spawnSync(process.execPath, ["-e", `process.kill(process.pid, ${n})`], { stdio: "ignore" });
    const actual =
      r.signal === null ? "ignored" : signalNumberOf(r.signal) === n ? "delivered" : "sigtrap";
    // WHEN THIS GOES RED for a "sigtrap" or "ignored" row: bun now delivers that signal, so the
    // dispatcher reproduces the floor for it — flip that row, flip the matching cell in
    // dispatcher.json to {"core":"signal","floor":"signal"}, and delete its bullet in dispatch.ts.
    expect([name, actual]).toEqual([name, expected]);
    if (actual === "sigtrap") expect(signalNumberOf(r.signal as string)).toBe(5);
  }
});

test("SIGPIPE is still ignored by the bun runtime — the documented 141 divergence still applies", () => {
  // Run in a CHILD so a future bun that stops ignoring SIGPIPE reddens this test instead of killing
  // the test runner. WHEN THIS GOES RED: bun now delivers SIGPIPE, so the dispatcher's re-raise will
  // work and the divergence is over — delete the SIGPIPE paragraphs in dispatch.ts/main.ts and turn
  // dispatcher.json's signal-sigpipe-divergence case into a normal agreeing signal case.
  const r = spawnSync(process.execPath, ["-e", "process.kill(process.pid, 13); console.log('survived')"], {
    encoding: "utf-8",
  });
  expect(r.stdout.trim()).toBe("survived");
  expect(r.status).toBe(0);
  expect(r.signal).toBeNull();
  // ...so a SIGPIPE-killed verb takes the 128+N fallback, which is exactly 141.
  expect(128 + (signalNumberOf("SIGPIPE") as number)).toBe(141);
});

test("pickPython: no venv at all falls back to bare python3 from PATH", () => {
  const home = mkdtempSync(path.join(tmpdir(), "pk-venv-none-"));
  try {
    expect(pickPython(home)).toBe("python3");
  } finally {
    rmSync(home, { recursive: true, force: true });
  }
});

test("pickPython: an executable venv python that does NOT start is refused (the liveness probe)", () => {
  // The failure mode `-x` alone misses: a .venv/bin/python3 that survived a system-python upgrade.
  // Trusting it would return 126/127 on every verb.
  const home = mkdtempSync(path.join(tmpdir(), "pk-venv-broken-"));
  const bin = path.join(home, ".venv", "bin");
  mkdirSync(bin, { recursive: true });
  const py = path.join(bin, "python3");
  writeFileSync(py, "#!/bin/sh\nexit 1\n");
  chmodSync(py, 0o755);
  try {
    expect(pickPython(home)).toBe("python3");
  } finally {
    rmSync(home, { recursive: true, force: true });
  }
});

test("pickPython: a venv python that exists AND starts is preferred (ADR-008)", () => {
  const home = mkdtempSync(path.join(tmpdir(), "pk-venv-live-"));
  const bin = path.join(home, ".venv", "bin");
  mkdirSync(bin, { recursive: true });
  const py = path.join(bin, "python3");
  writeFileSync(py, '#!/bin/sh\nexec /usr/bin/env python3 "$@"\n');
  chmodSync(py, 0o755);
  try {
    expect(pickPython(home)).toBe(py);
  } finally {
    rmSync(home, { recursive: true, force: true });
  }
});
