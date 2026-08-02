#!/usr/bin/env python3
"""
run_fuzz.py — the differential FUZZ suite for the hybrid-core TS port (Task 3 fix wave r2).

Two harness pairs live under test/fuzz/, each a TS producer whose output the Python half re-computes
with the real CPython implementation:

  * difflib_fuzz.ts  / difflib_check.py — getCloseMatches() vs difflib.get_close_matches() over
    ~6000 deterministic random cases (an ASCII alphabet and a non-ASCII one that exercises the port's
    code-point iteration and code-point tie-break), a realistic verb battery, and CONSTRUCTED SCORE
    TIES, which random words hit only by accident and which the tie-break line depends on.
  * pysem_fuzz.ts    / pysem_check.py   — pythonTruthy()/pythonStr() vs Python bool()/f"{v}" over a
    battery of JSON-decodable values, including nested ones the iterative renderer must handle.

This is the harness the port's difflib fidelity was originally PROVEN with; it lives in the repo (not
in a scratch directory, not with absolute imports) so the proof stays reproducible on any checkout.

It is an OPT-IN suite: it needs `bun` on PATH, since only bun runs the TypeScript sources directly.
Missing bun prints a LOUD single SKIP line and exits 0 — unless PLAINKEEP_REQUIRE_CORE=1, where the
same line is an error and the suite exits 1 (the convention run_core_parity.py uses for the binary).

Usage:  python3 test/run_fuzz.py
"""
from __future__ import annotations
import shutil
import subprocess
import sys
from pathlib import Path
from lib.hermetic import seal
seal()   # hermetic: an empty throwaway registry, never the developer's real vault

HERE = Path(__file__).resolve().parent
FUZZ = HERE / "fuzz"
PY = sys.executable
GREEN, RED, YELLOW, DIM, BOLD, RESET = (
    "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[1m", "\033[0m",
)

SKIP_LINE = "SKIP core-fuzz: bun not on PATH (install bun to run the TS<->Python differential fuzz)"

HARNESSES = [
    ("difflib get_close_matches", "difflib_fuzz.ts", "difflib_check.py"),
    ("python bool()/str() semantics", "pysem_fuzz.ts", "pysem_check.py"),
]


def _run_pair(bun: str, producer: str, checker: str) -> tuple[bool, str]:
    gen = subprocess.run([bun, "run", str(FUZZ / producer)], capture_output=True, text=True)
    if gen.returncode != 0:
        return False, f"{producer} failed (rc={gen.returncode}): {gen.stderr.strip()[:400]}"
    chk = subprocess.run([PY, str(FUZZ / checker)], input=gen.stdout, capture_output=True, text=True)
    out = (chk.stdout + chk.stderr).strip()
    return chk.returncode == 0, out


def main() -> int:
    bun = shutil.which("bun")
    if bun is None:
        import os
        if os.environ.get("PLAINKEEP_REQUIRE_CORE") == "1":
            print(f"{RED}{BOLD}{SKIP_LINE}{RESET}", file=sys.stderr)
            print(f"{RED}PLAINKEEP_REQUIRE_CORE=1 — a missing bun is a FAILURE.{RESET}", file=sys.stderr)
            return 1
        print(f"{YELLOW}{BOLD}{SKIP_LINE}{RESET}")
        return 0

    print(f"{BOLD}core-fuzz differential harnesses (TS producer vs CPython checker){RESET}\n")
    failed = 0
    for label, producer, checker in HARNESSES:
        ok, detail = _run_pair(bun, producer, checker)
        failed += not ok
        mark = f"{GREEN}PASS{RESET}" if ok else f"{RED}FAIL{RESET}"
        print(f"  {mark} {label}")
        for line in detail.splitlines():
            print(f"       {DIM}{line}{RESET}")
    passed = len(HARNESSES) - failed
    print(f"\n{BOLD}Result:{RESET} {GREEN}{passed} passed{RESET}, "
          f"{(RED if failed else DIM)}{failed} failed{RESET}, {len(HARNESSES)} checks")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
