// Unit tests for the `__complete` interception. The AUTHORITY on its behavior is the Python-owned
// catalog (test/cases/core-parity/complete.json), which runs the same invocations through this
// binary and through the bash floor's real Python verb and byte-compares; these exist for dev speed
// and to pin the two things a differential cannot show on its own:
//   * WHICH SIDE ANSWERED — a fall-through and an in-core answer are byte-identical by construction,
//     so only a direct observation distinguishes them. Every test below asserts it.
//   * the audit line for an intercepted verb, asserted here against a vault whose `__complete`
//     run.py would have printed something else entirely had the spawn happened.
import { test, expect } from "bun:test";
import { mkdtempSync, mkdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { completeIntercept } from "./complete.js";
import { dispatch } from "./dispatch.js";

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

// A vault whose engine bin/ holds the given cmd.json sidecars, keyed by directory name.
function vault(cmds: Record<string, unknown>): string {
  const home = mkdtempSync(path.join(tmpdir(), "pk-complete-"));
  for (const [dir, cmd] of Object.entries(cmds)) {
    const d = path.join(home, "bin", dir);
    mkdirSync(d, { recursive: true });
    writeFileSync(path.join(d, "cmd.json"), JSON.stringify(cmd));
    writeFileSync(path.join(d, "run.py"), "print('SPAWNED PYTHON')\n");
  }
  return home;
}

// Run the interception and report BOTH the answer and whether the fall-through was taken, so no
// assertion can be satisfied by the wrong side answering.
function complete(home: string, args: string[]): { out: string | undefined; fellThrough: boolean } {
  let fellThrough = false;
  const r = withHome(home, () =>
    completeIntercept(args, () => {
      fellThrough = true;
      return { stdout: "FELL THROUGH", code: 0 };
    }),
  );
  expect(r.code).toBe(0);
  return { out: r.stdout, fellThrough };
}

const GRAMMAR = {
  // A compound verb with a tokenless default action, a value-flag, an enum, and a provider arg.
  share: {
    verb: "share",
    summary: "publish a note",
    actions: [
      {
        name: "publish",
        default: true,
        args: [
          { name: "slug", complete: "note-slug" },
          { name: "--expires", type: "string" },
          { name: "--gist", type: "flag" },
        ],
      },
      { name: "list", summary: "the ledger", args: [] },
      {
        name: "move",
        summary: "move it",
        args: [
          { name: "id" },
          { name: "status", enum: ["active", "waiting", "done"] },
          { name: "--kind", type: "string", enum: ["products", "labs"] },
        ],
      },
    ],
  },
  // A scalar verb: no actions[] at all.
  status: { verb: "status", summary: "where things stand" },
  // Hidden — present on disk (the guardrail still reads its risk) but never a candidate.
  __complete: { verb: "__complete", summary: "internal", hidden: true },
};

test("no prior words: the verb list, sorted, hidden verbs filtered out", () => {
  const home = vault(GRAMMAR);
  try {
    const { out, fellThrough } = complete(home, []);
    expect(fellThrough).toBe(false);
    expect(out).toBe("share:publish a note\nstatus:where things stand");
  } finally {
    rmSync(home, { recursive: true, force: true });
  }
});

test("`help <TAB>` completes to the same verb list (help itself is NOT intercepted — D6)", () => {
  const home = vault(GRAMMAR);
  try {
    expect(complete(home, ["help"]).out).toBe("share:publish a note\nstatus:where things stand");
  } finally {
    rmSync(home, { recursive: true, force: true });
  }
});

test("the surface follows the cmd.json `verb` FIELD, not the directory name", () => {
  const home = vault({ dirname: { verb: "declared", summary: "s" } });
  try {
    expect(complete(home, []).out).toBe("declared:s");
  } finally {
    rmSync(home, { recursive: true, force: true });
  }
});

test("an unknown verb and a scalar verb both answer NOTHING — not a blank line", () => {
  const home = vault(GRAMMAR);
  try {
    for (const prior of [["nope"], ["nope", "x"], ["status"], ["status", "x"]]) {
      const { out, fellThrough } = complete(home, prior);
      expect([prior, out, fellThrough]).toEqual([prior, undefined, false]);
    }
  } finally {
    rmSync(home, { recursive: true, force: true });
  }
});

test("slot 1: keyworded actions only, and the tokenless default action's first positional", () => {
  const home = vault(GRAMMAR);
  try {
    // `share`'s default action is `publish`, whose first positional is a note-slug PROVIDER — so
    // slot 1 for this verb cannot be answered in-core at all, keyworded actions included.
    expect(complete(home, ["share"]).fellThrough).toBe(true);
    // The same slot with the default action's first positional carrying an ENUM stays in-core.
    const home2 = vault({
      x: {
        verb: "x",
        actions: [
          { name: "run", default: true, args: [{ name: "mode", enum: ["fast", "slow"] }] },
          { name: "list", summary: "l", args: [] },
        ],
      },
    });
    try {
      const { out, fellThrough } = complete(home2, ["x"]);
      expect(fellThrough).toBe(false);
      expect(out).toBe("list:l\nfast\nslow");
    } finally {
      rmSync(home2, { recursive: true, force: true });
    }
  } finally {
    rmSync(home, { recursive: true, force: true });
  }
});

test("within an action: enum positional, positional exhaustion, and a pending value-flag", () => {
  const home = vault(GRAMMAR);
  try {
    // move's arg 0 is a bare positional (no enum, no provider) -> nothing.
    expect(complete(home, ["share", "move"]).out).toBeUndefined();
    // ...arg 1 is the enum, reached once one positional has been consumed.
    expect(complete(home, ["share", "move", "T-1"]).out).toBe("active\nwaiting\ndone");
    // positionals exhausted -> nothing.
    expect(complete(home, ["share", "move", "T-1", "done"]).out).toBeUndefined();
    // a value-flag as the LAST token: the next word is its value (here, an enum).
    expect(complete(home, ["share", "move", "--kind"]).out).toBe("products\nlabs");
    // ...and once its value is typed, the flag+value pair consumed NO positional, so arg 0 is
    // still the next one to fill.
    expect(complete(home, ["share", "move", "--kind", "labs"]).out).toBeUndefined();
    expect(complete(home, ["share", "move", "--kind", "labs", "T-1"]).out).toBe("active\nwaiting\ndone");
    // The DEFAULT action's own name is not a keyword: `share publish` routes to the default action
    // with "publish" consumed as its first positional (the slug), so the slug is already filled and
    // a `type: flag` flag adds nothing after it.
    expect(complete(home, ["share", "publish"]).out).toBeUndefined();
    expect(complete(home, ["share", "publish", "--gist"]).out).toBeUndefined();
    // an action name that is NOT keyworded falls to the default action, which consumes it as its
    // own first positional -> publish's slug is filled, nothing left.
    expect(complete(home, ["share", "notanaction", "extra"]).out).toBeUndefined();
    // ...but a value-flag consumes its value and NO positional, so the slug is still next — and the
    // slug is a note-slug provider, so this is the fall-through.
    expect(complete(home, ["share", "--expires", "7d"]).fellThrough).toBe(true);
  } finally {
    rmSync(home, { recursive: true, force: true });
  }
});

test("a provider arg falls through — one call, and its result is returned verbatim", () => {
  const home = vault(GRAMMAR);
  try {
    let calls = 0;
    const r = withHome(home, () =>
      completeIntercept(["share", "--expires", "7d"], () => {
        calls += 1;
        return { stdout: "alpha:note", code: 0 };
      }),
    );
    expect([calls, r.stdout, r.code]).toEqual([1, "alpha:note", 0]);
  } finally {
    rmSync(home, { recursive: true, force: true });
  }
});

test("EVERY name in completion.py's PROVIDERS falls through — `layer` included", () => {
  // `layer`'s Python provider is `lambda: []`, so its OUTPUT is indistinguishable from an in-core
  // empty answer and the parity catalog cannot tell which side produced it. Only this assertion
  // pins the routing, which is the part that has to survive `layer` growing a body.
  for (const provider of ["note-slug", "asset-slug", "task-id", "hub", "note-type", "status", "layer"]) {
    const home = vault({ x: { verb: "x", actions: [{ name: "go", args: [{ name: "a", complete: provider }] }] } });
    try {
      expect([provider, complete(home, ["x", "go"]).fellThrough]).toEqual([provider, true]);
    } finally {
      rmSync(home, { recursive: true, force: true });
    }
  }
});

test("a `complete` naming something that is not a provider yields no candidates, in-core", () => {
  const home = vault({ x: { verb: "x", actions: [{ name: "go", args: [{ name: "a", complete: "not-a-provider" }] }] } });
  try {
    const { out, fellThrough } = complete(home, ["x", "go"]);
    expect([out, fellThrough]).toEqual([undefined, false]);
  } finally {
    rmSync(home, { recursive: true, force: true });
  }
});

test("descriptions are colon-cleaned and Python-stripped", () => {
  // \x1c is whitespace to Python's str.strip() and NOT to JS's trim() — the reason pyStrip exists.
  const home = vault({ x: { verb: "x", summary: "  a: b:c \x1c" } });
  try {
    expect(complete(home, []).out).toBe("x:a - b -c");
  } finally {
    rmSync(home, { recursive: true, force: true });
  }
});

test("a malformed sidecar falls through rather than guessing what Python made of it", () => {
  const home = mkdtempSync(path.join(tmpdir(), "pk-complete-bad-"));
  try {
    const d = path.join(home, "bin", "x");
    mkdirSync(d, { recursive: true });
    // No `verb` key: Python's `{c["verb"]: c for c in load_cmds()}` raises KeyError, which is an
    // answer this port cannot reproduce, so the Python verb gets to give it.
    writeFileSync(path.join(d, "cmd.json"), JSON.stringify({ summary: "no verb key" }));
    expect(complete(home, []).fellThrough).toBe(true);
    // Not JSON at all: JSON.parse rejects what Python's json.loads may still accept (NaN/Infinity).
    writeFileSync(path.join(d, "cmd.json"), "{ NaN }");
    expect(complete(home, []).fellThrough).toBe(true);
  } finally {
    rmSync(home, { recursive: true, force: true });
  }
});

test("a non-object sidecar is DROPPED (as Python drops it), not a reason to fall through", () => {
  const home = vault({ ok: { verb: "ok", summary: "s" } });
  try {
    const d = path.join(home, "bin", "arr");
    mkdirSync(d, { recursive: true });
    writeFileSync(path.join(d, "cmd.json"), "[1,2,3]");
    const { out, fellThrough } = complete(home, []);
    expect([out, fellThrough]).toEqual(["ok:s", false]);
  } finally {
    rmSync(home, { recursive: true, force: true });
  }
});

test("a HIDDEN verb's malformed grammar is never inspected — hidden is filtered first", () => {
  const home = vault({ ok: { verb: "ok", summary: "s" } });
  try {
    const d = path.join(home, "bin", "h");
    mkdirSync(d, { recursive: true });
    // `actions` truthy but not a list would bail on a VISIBLE verb; hidden drops it before that,
    // exactly as manifest.load_cmds() `continue`s before anything reads the grammar.
    writeFileSync(path.join(d, "cmd.json"), JSON.stringify({ verb: "h", hidden: true, actions: "nope" }));
    const { out, fellThrough } = complete(home, []);
    expect([out, fellThrough]).toEqual(["ok:s", false]);
  } finally {
    rmSync(home, { recursive: true, force: true });
  }
});

test("`hidden` uses PYTHON truthiness: an empty list keeps the verb visible", () => {
  // Boolean([]) is true in JS and bool([]) is false in Python — the silent-divergence trap.
  const home = vault({ x: { verb: "x", summary: "s", hidden: [] }, y: { verb: "y", summary: "t", hidden: 1 } });
  try {
    expect(complete(home, []).out).toBe("x:s");
  } finally {
    rmSync(home, { recursive: true, force: true });
  }
});

test("dispatch: `__complete` is answered in-core AND the gate's audit line is written", () => {
  const home = vault({
    ...GRAMMAR,
    // The gate must know __complete to allow it; its run.py would print this marker if the
    // interception had not taken the invocation. stdio is inherited on a real spawn, so the marker
    // showing up in the RESULT is impossible — the assertion is that the in-core answer is returned.
    __complete: { verb: "__complete", summary: "internal", hidden: true, risk: "read" },
  });
  try {
    const r = withHome(home, () => dispatch(["__complete", "share", "move", "T-1"]));
    expect([r.code, r.stdout]).toEqual([0, "active\nwaiting\ndone"]);
    const log = readFileSync(path.join(home, ".logs", "plainkeep.log"), "utf-8").trimEnd().split("\n");
    expect(log).toHaveLength(1);
    expect(log[0]).toContain("\t__complete share move T-1\tallow\t");
  } finally {
    rmSync(home, { recursive: true, force: true });
  }
});
