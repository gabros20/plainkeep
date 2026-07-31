// difflib_fuzz.ts — the TS half of the difflib differential fuzz. Emits one JSON array of
// {w, possibilities, res} records on stdout; difflib_check.py re-computes every `res` with the real
// CPython difflib.get_close_matches and fails on any mismatch. Run both halves with
// `python3 test/run_fuzz.py` (or by hand: `bun run test/fuzz/difflib_fuzz.ts | python3
// test/fuzz/difflib_check.py`).
//
// The import is RELATIVE so the harness runs on any checkout, not just the machine it was written on.
import { getCloseMatches } from "../../cli/src/core/guardrail.ts";

// Deterministic PRNG so a failure is reproducible and the two halves see identical cases.
let seed = 12345;
function rnd(): number {
  seed = (seed * 1103515245 + 12345) & 0x7fffffff;
  return seed / 0x7fffffff;
}

// Two alphabets. The ASCII one is what verb names actually look like; the second one exercises the
// CODE-POINT iteration (Array.from) and the code-point tie-break ordering — an astral character is
// two UTF-16 units, so unit-based iteration would compute a different ratio, and its lead surrogate
// (0xD83D) sorts BELOW U+E000 as a unit while U+1F600 sorts ABOVE it as a code point.
const ASCII = Array.from("abcdefghijklmnopqrstuvwxyz-_0123456789");
// (the two private-use characters are written as escapes on purpose — they are invisible, and they
// are exactly the BMP code points that sort on the other side of an astral character in UTF-16)
const WIDE = Array.from("ab\u{1F600}\u{1F642}\u{1F680}é日本語\uE000\uE001ß-_");

function word(alphabet: string[], maxLen: number): string {
  const n = 1 + Math.floor(rnd() * maxLen);
  let s = "";
  for (let i = 0; i < n; i++) s += alphabet[Math.floor(rnd() * alphabet.length)];
  return s;
}

interface Case {
  w: string;
  possibilities: string[];
  res: string[];
}
const out: Case[] = [];

function push(w: string, possibilities: string[]): void {
  out.push({ w, possibilities, res: getCloseMatches(w, possibilities, 3, 0.6) });
}

for (const [alphabet, n] of [
  [ASCII, 4000],
  [WIDE, 2000],
] as Array<[string[], number]>) {
  for (let t = 0; t < n; t++) {
    const npos = 1 + Math.floor(rnd() * 8);
    const posSet = new Set<string>();
    for (let i = 0; i < npos; i++) posSet.add(word(alphabet, 9));
    push(word(alphabet, 9), [...posSet].sort());
  }
}

// A battery of realistic verb-name cases.
const verbs = ["capture", "cap", "search", "status", "stash", "sweep", "new", "note", "models", "merge", "doctor", "distill", "daily", "weekly"];
for (const q of ["captur", "cptaure", "serch", "stat", "stas", "swep", "nwe", "noe", "model", "merg", "doctr", "distll", "dayly", "weekley", "zzz", "xyzzy", "c", "se", "statu"]) {
  push(q, verbs);
}

// Constructed SCORE TIES — the tie-break is the port's most delicate line and random words hit exact
// ties only by accident. Includes ties whose names differ at an astral-vs-BMP position, where a
// UTF-16 comparison and a code-point comparison order them oppositely.
push("abc", ["abd", "abe", "abf"]);
push("abc", ["abd", "abe"]);
push("capture", ["captura", "capturb", "capturz"]);
push("abc", ["ab\u{1F600}", "ab\uE000"]);
push("abc", ["ab\u{1F600}", "ab\uE000", "abz"]);
push("😀bc", ["😀bd", "😀be", "😀bf"]);
push("日本語", ["日本", "日本人", "日曜日"]);
for (let i = 0; i < 8; i++) {
  const base = "x".repeat(i + 1);
  push(base + "a", [base + "b", base + "c", base + "\u{1F600}", base + "\uE000"]);
}

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
