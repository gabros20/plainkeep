// pysem_fuzz.ts — the TS half of the Python-semantics differential fuzz. For a battery of
// JSON-decodable values (carried as JSON TEXT so both sides decode the same bytes) it emits
// {json, truthy, str, xfail}; pysem_check.py re-computes `truthy` as Python bool(v) and `str` as
// f"{v}" and fails on any mismatch. These two functions decide, respectively, whether a verb is
// gated and what the ALLOW reason + audit line say.
//
// The import is RELATIVE so the harness runs on any checkout, not just the machine it was written on.
import { pythonTruthy, pythonStr } from "../../cli/src/core/guardrail.ts";

// KNOWN divergences, declared here so they stay VISIBLE in every run instead of living only in a
// report. The checker prints each as XFAIL — and FAILS if one ever starts agreeing, so closing any
// of them forces this list to be updated rather than silently rotting. All of them affect only the
// RENDERED reason/audit text of a truthy non-string `risk`; none changes a verdict or an exit code,
// and no realistic cmd.json authors any of these shapes.
//   "0.0" / "1e-5" / "1e16" — Python's float repr differs from JS's Number→string (and an
//     integer-valued float is indistinguishable from an int once JSON.parse has run, so this cannot
//     be fixed without keeping the raw JSON text).
//   "12345678901234567890"  — JSON integer precision beyond 2^53 is lost by JSON.parse.
//   '{"2":1,"1":2}'         — Object.entries lists integer-like keys first in numeric order, while a
//     Python dict keeps JSON insertion order.
//   '[" "]'            — Python repr escapes non-printable non-ASCII (\xa0); pyStrRepr's escape
//     ladder covers < 0x20 and 0x7f only.
const XFAIL = new Set([
  "0.0",
  "1e-5",
  "1e16",
  "12345678901234567890",
  '{"2":1,"1":2}',
  '["\\u00a0"]',
]);

const jsons = [
  "false", "true", "null", "0", "1", "-1", "5", "0.0", "-0", "3.14", "1e-5", "1e16",
  "12345678901234567890", '""', '"read"', '"weird"', '"false"', '"a\\tb"', "[]", "[0]", "[1,2,3]",
  '["a","b"]', "{}", '{"a":0}', '{"a":1,"b":2}', '{"x":"y"}', '[{"a":0}]', '{"k":[1,2]}', '"it\'s"',
  '"quote\\"x"', "[true,false,null]", '{"n":null}', "100000000", "-42", '"café"', '"日本語"',
  '"😀"', '["😀","é"]', '{"😀":[1,"é"]}', '{"2":1,"1":2}', '["\\u00a0"]', '["\\u0001\\u007f"]',
];

// Nested values at depths the ITERATIVE renderer must handle identically to Python's recursive repr.
// Kept under CPython's own repr recursion limit — deeper inputs are resolved to null by cmdField's
// depth cap before they ever reach pythonStr, and Python cannot render them at all.
for (const depth of [1, 2, 5, 50, 200]) {
  jsons.push("[".repeat(depth) + "1" + "]".repeat(depth));
  jsons.push(`{"a":${"[".repeat(depth)}"x"${"]".repeat(depth)}}`);
}

const out = jsons.map((j) => {
  const v = JSON.parse(j);
  return { json: j, truthy: pythonTruthy(v), str: pythonStr(v), xfail: XFAIL.has(j) };
});
// Emit PURE-ASCII JSON: every non-ASCII code UNIT is escaped as \uXXXX (Python's json decoder joins
// surrogate pairs back into the astral character). Bun corrupts some multi-byte characters when a
// large string is written to a piped stdout, which silently mangles a non-ASCII fuzz case into an
// undecodable byte stream; ASCII output is immune, and the comparison is unaffected.
function asciiJson(v: unknown): string {
  return JSON.stringify(v).replace(/[\u007f-\uffff]/g, (c) =>
    "\\u" + c.charCodeAt(0).toString(16).padStart(4, "0"),
  );
}

console.log(asciiJson(out));
