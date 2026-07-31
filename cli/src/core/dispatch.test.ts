// Unit tests for the dispatcher's pure seams. The AUTHORITY on dispatcher behavior is the
// Python-owned differential matrix (test/cases/core-parity/dispatcher.json), which runs the same
// invocations through this binary and through the bash floor; these tests exist for dev speed and to
// pin the two decisions that have no Python counterpart to compare against: the argv preamble's
// bash-isms, and the choice NOT to canonicalize PLAINKEEP_HOME.
import { test, expect } from "bun:test";
import { mkdtempSync, rmSync, symlinkSync, mkdirSync, writeFileSync, chmodSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { pickPython, resolveHome, verbFromArgv } from "./dispatch.js";

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
