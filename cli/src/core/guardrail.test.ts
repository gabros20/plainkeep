// Dev-speed bun mirror of a few guardrail behaviors. The AUTHORITATIVE gate is the Python-owned
// differential oracle (test/run_core_parity.py, guardrail.json), which proves byte-for-byte parity
// with bin/lib/guardrail.py on exit code, stdout, stderr AND the audit log. This file spot-checks the
// port in-process — most importantly the difflib did-you-mean ranking and the gate risk branches.
import { test, expect, afterEach } from "bun:test";
import { mkdtempSync, mkdirSync, writeFileSync, rmSync, realpathSync, readFileSync, existsSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { gate, mainCli, getCloseMatches, decisionStr, EXIT_CONFIRM, EXIT_DENY, EXIT_NOT_FOUND } from "./guardrail.js";

let vault = "";
function makeVault(): string {
  vault = realpathSync(mkdtempSync(path.join(tmpdir(), "pk-guardrail-")));
  mkdirSync(path.join(vault, "bin"), { recursive: true });
  process.env.PLAINKEEP_HOME = vault;
  process.env.PLAINKEEP_PATH = "";
  return vault;
}
function engineVerb(name: string, cmd?: Record<string, unknown>, files: string[] = ["run.py", "cmd.json"]): void {
  const d = path.join(vault, "bin", name);
  mkdirSync(d, { recursive: true });
  for (const f of files) {
    if (f === "cmd.json") writeFileSync(path.join(d, f), JSON.stringify(cmd ?? {}));
    else writeFileSync(path.join(d, f), "def main(argv):\n    return 0\n");
  }
}
function pack(name: string, verb: string, cmd: Record<string, unknown>): void {
  const d = path.join(vault, "plugins", name, verb);
  mkdirSync(d, { recursive: true });
  writeFileSync(path.join(d, "cmd.json"), JSON.stringify(cmd));
}
// Writes a verb's cmd.json as RAW text — needed for a pathologically deep document, where
// JSON.stringify would itself recurse per nesting level.
function engineVerbRaw(name: string, cmdText: string): void {
  const d = path.join(vault, "bin", name);
  mkdirSync(d, { recursive: true });
  writeFileSync(path.join(d, "run.py"), "def main(argv):\n    return 0\n");
  writeFileSync(path.join(d, "cmd.json"), cmdText);
}
function nestedRisk(depth: number): string {
  return `{"verb": "x", "risk": ${"[".repeat(depth)}1${"]".repeat(depth)}}`;
}
function readLog(): string {
  const f = path.join(vault, ".logs", "plainkeep.log");
  return existsSync(f) ? readFileSync(f, "utf-8") : "";
}
function lock(obj: unknown): void {
  const d = path.join(vault, "plugins");
  mkdirSync(d, { recursive: true });
  writeFileSync(path.join(d, "plugins.lock.json"), typeof obj === "string" ? obj : JSON.stringify(obj));
}

afterEach(() => {
  if (vault) rmSync(vault, { recursive: true, force: true });
  vault = "";
  delete process.env.PLAINKEEP_HOME;
  delete process.env.PLAINKEEP_PATH;
});

test("gate enforces declared risk classes", () => {
  makeVault();
  engineVerb("v_read", { risk: "read" });
  engineVerb("v_confirm", { risk: "confirm" });
  engineVerb("v_deny", { risk: "deny" });
  engineVerb("v_runonly", undefined, ["run.py"]); // undeclared -> confirm
  expect(gate("v_read", []).verdict).toBe("allow");
  expect(gate("v_confirm", []).verdict).toBe("confirm");
  expect(gate("v_confirm", ["--yes"]).verdict).toBe("allow");
  expect(gate("v_confirm", ["-y"]).verdict).toBe("allow");
  expect(gate("v_deny", []).verdict).toBe("deny");
  expect(gate("v_deny", ["--dry-run"]).verdict).toBe("deny"); // deny beats dry-run
  expect(gate("v_runonly", []).verdict).toBe("confirm"); // undeclared defaults to confirm
});

test("--dry-run downgrades only a declared dry_run confirm/safe_write verb", () => {
  makeVault();
  engineVerb("v_dry", { risk: "confirm", dry_run: true });
  engineVerb("v_nodry", { risk: "confirm" });
  expect(gate("v_dry", ["--dry-run"])).toEqual({ verdict: "allow", reason: "--dry-run downgrades confirm to read (no side effect)", riskClass: "read" });
  expect(gate("v_nodry", ["--dry-run"]).verdict).toBe("confirm"); // no dry_run declaration
});

test("untrusted pack caps read/safe_write to confirm and blocks the dry-run downgrade", () => {
  makeVault();
  pack("untrusted", "pu_read", { risk: "read" });
  pack("untrusted", "pu_dry", { risk: "safe_write", dry_run: true });
  pack("trusted", "pt_read", { risk: "read" });
  lock({ plugins: { untrusted: {}, trusted: { trusted: true } } });
  expect(gate("pu_read", []).verdict).toBe("confirm"); // capped
  expect(gate("pu_dry", ["--dry-run"]).verdict).toBe("confirm"); // ceiling blocks downgrade
  expect(gate("pt_read", []).verdict).toBe("allow"); // trusted keeps its risk
});

test("malformed cmd.json and plugins.lock.json are swallowed, never throw", () => {
  makeVault();
  engineVerb("v_bad", "not json" as unknown as Record<string, unknown>, ["cmd.json"]);
  pack("somepack", "sp_read", { risk: "read" });
  lock("{ broken");
  expect(gate("v_bad", []).verdict).toBe("confirm"); // null risk -> default confirm
  expect(gate("sp_read", []).verdict).toBe("allow"); // empty lock -> no ceiling
});

test("mainCli: unknown verb yields did-you-mean or the help hint at exit 4", () => {
  makeVault();
  for (const v of ["capture", "cap", "search", "status", "stash", "sweep"]) engineVerb(v, { risk: "read" });
  const near = mainCli(["captur"]);
  expect(near.code).toBe(EXIT_NOT_FOUND);
  expect(near.stderr).toBe("plainkeep: unknown verb 'captur'. did you mean: capture, cap?");
  const far = mainCli(["xyzzy"]);
  expect(far.code).toBe(EXIT_NOT_FOUND);
  expect(far.stderr).toBe("plainkeep: unknown verb 'xyzzy'. (run: plainkeep help)");
});

test("mainCli: confirm prints the exact remediation to stderr at exit 3", () => {
  makeVault();
  engineVerb("v_confirm", { risk: "confirm" });
  const r = mainCli(["v_confirm", "x"]);
  expect(r.code).toBe(EXIT_CONFIRM);
  expect(r.stderr).toBe(
    "guardrail: CONFIRM [confirm] — 'v_confirm' is confirm-class — re-run with --yes to proceed\n  re-run: plainkeep v_confirm x --yes",
  );
  const d = mainCli(["v_confirm", "--yes"]);
  expect(d.code).toBe(0);
  expect(d.stderr).toBeUndefined();
});

test("mainCli: deny prints the decision to stderr at exit 5", () => {
  makeVault();
  engineVerb("v_deny", { risk: "deny" });
  const r = mainCli(["v_deny"]);
  expect(r.code).toBe(EXIT_DENY);
  expect(r.stderr).toBe("guardrail: DENY [deny] — 'v_deny' is deny-class — never run");
});

test("getCloseMatches mirrors difflib ordering and cutoff", () => {
  const known = ["cap", "capture", "search", "stash", "status", "sweep"];
  expect(getCloseMatches("captur", known)).toEqual(["capture", "cap"]);
  expect(getCloseMatches("stas", known)).toEqual(["stash", "status"]);
  expect(getCloseMatches("serch", known)).toEqual(["search"]);
  expect(getCloseMatches("xyzzy", known)).toEqual([]);
});

test("decisionStr formats verdict, risk class, and reason with the em-dash", () => {
  expect(decisionStr({ verdict: "allow", reason: "read", riskClass: "read" })).toBe("ALLOW [read] — read");
});

test("dry_run field uses Python bool() truthiness, not JS Boolean()", () => {
  makeVault();
  // JS Boolean([]) / Boolean({}) are true, but Python bool([]) / bool({}) are false — an empty
  // container must NOT downgrade a confirm verb (the I-1 unsafe divergence).
  engineVerb("d_list", { risk: "confirm", dry_run: [] });
  engineVerb("d_obj", { risk: "confirm", dry_run: {} });
  engineVerb("d_strfalse", { risk: "confirm", dry_run: "false" }); // non-empty string is truthy
  engineVerb("d_one", { risk: "confirm", dry_run: 1 });
  expect(gate("d_list", ["--dry-run"]).verdict).toBe("confirm");
  expect(gate("d_obj", ["--dry-run"]).verdict).toBe("confirm");
  expect(gate("d_strfalse", ["--dry-run"]).verdict).toBe("allow");
  expect(gate("d_one", ["--dry-run"]).verdict).toBe("allow");
});

test("plugin trust ceiling reads `trusted` with Python bool(), not JS truthiness", () => {
  makeVault();
  for (const p of ["t_list", "t_obj", "t_zero", "t_one", "t_str"]) pack(p, `pv_${p}`, { risk: "read" });
  lock({
    plugins: {
      t_list: { trusted: [] },
      t_obj: { trusted: {} },
      t_zero: { trusted: 0 },
      t_one: { trusted: 1 },
      t_str: { trusted: "x" },
    },
  });
  // Python bool([]) / bool({}) / bool(0) are FALSE, so the pack stays UNTRUSTED and the ceiling caps
  // its declared read up to confirm. JS Boolean([]) / Boolean({}) are true — reading the lock the JS
  // way let an untrusted pack escape the ceiling and exit 0 where the protocol demands 3.
  expect(gate("pv_t_list", []).verdict).toBe("confirm");
  expect(gate("pv_t_obj", []).verdict).toBe("confirm");
  expect(gate("pv_t_zero", []).verdict).toBe("confirm");
  // Truthy on both sides — the pack is trusted and its declared read stands.
  expect(gate("pv_t_one", []).verdict).toBe("allow");
  expect(gate("pv_t_str", []).verdict).toBe("allow");
});

test("nested risk values render exactly as Python str() at ordinary depths", () => {
  makeVault();
  engineVerbRaw("v_d5", nestedRisk(5));
  engineVerbRaw("v_d50", nestedRisk(50));
  expect(gate("v_d5", []).reason).toBe(`${"[".repeat(5)}1${"]".repeat(5)}`);
  expect(gate("v_d50", []).reason).toBe(`${"[".repeat(50)}1${"]".repeat(50)}`);
});

test("a pathologically deep cmd.json caps to null, never throws, and still writes the audit line", () => {
  makeVault();
  engineVerbRaw("v_deep", nestedRisk(50000));
  // Python's json.loads raises RecursionError here, which _cmd_field swallows to None -> the default
  // confirm. The depth cap reproduces that verdict instead of allowing (unsafe) or blowing the JS
  // call stack inside gate() — which used to escape as exit 1 with NO audit line written.
  const r = mainCli(["v_deep"]);
  expect(r.code).toBe(EXIT_CONFIRM);
  expect(readLog()).toContain("\tv_deep \tconfirm\t'v_deep' is confirm-class");
});

test("getCloseMatches breaks score TIES by name descending (difflib's heapq.nlargest tuple order)", () => {
  expect(getCloseMatches("abc", ["abd", "abe", "abf"])).toEqual(["abf", "abe", "abd"]);
  expect(getCloseMatches("abc", ["abd", "abe"])).toEqual(["abe", "abd"]);
  expect(getCloseMatches("capture", ["captura", "capturb", "capturz"])).toEqual([
    "capturz",
    "capturb",
    "captura",
  ]);
});

test("getCloseMatches iterates and orders by CODE POINT, not UTF-16 unit", () => {
  // "ab😀" is 3 code points to Python but 4 UTF-16 units: iterating units would score it
  // 2*2/7 = 0.571 and drop it below the 0.6 cutoff instead of tying with "ab" at 2*2/6.
  // U+1F600 sorts ABOVE U+E000 by code point and BELOW it by UTF-16 unit (lead surrogate 0xD83D),
  // so a descending tie-break puts the emoji first only if the comparison is Python's.
  expect(getCloseMatches("abc", ["ab\u{1F600}", "ab"])).toEqual(["ab\u{1F600}", "ab"]);
});

test("non-string risk passes through raw and renders as Python str() (no clamp)", () => {
  makeVault();
  engineVerb("r_five", { risk: 5 });
  engineVerb("r_true", { risk: true });
  engineVerb("r_empty", { risk: "" }); // falsy -> default confirm
  engineVerb("r_null", { risk: null }); // falsy -> default confirm
  expect(gate("r_five", [])).toEqual({ verdict: "allow", reason: "5", riskClass: "5" });
  expect(gate("r_true", [])).toEqual({ verdict: "allow", reason: "True", riskClass: "True" });
  expect(gate("r_empty", []).verdict).toBe("confirm");
  expect(gate("r_null", []).verdict).toBe("confirm");
});
