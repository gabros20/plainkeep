// Unit tests for the dispatcher's pure seams. The AUTHORITY on dispatcher behavior is the
// Python-owned differential matrix (test/cases/core-parity/dispatcher.json), which runs the same
// invocations through this binary and through the bash floor; these tests exist for dev speed and to
// pin the two decisions that have no Python counterpart to compare against: the argv preamble's
// bash-isms, and the choice NOT to canonicalize PLAINKEEP_HOME.
import { test, expect } from "bun:test";
import { spawnSync } from "node:child_process";
import { mkdtempSync, rmSync, symlinkSync, mkdirSync, writeFileSync, chmodSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { pickPython, resolveHome, signalNumberOf, verbFromArgv } from "./dispatch.js";

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
