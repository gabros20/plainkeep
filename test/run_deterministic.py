#!/usr/bin/env python3
"""
run_deterministic.py — the DETERMINISTIC half of the harness. No LLM, no network, no cost.

It loads the §5 guardrail model and fires every adversarial case in cases/guardrail_cases.json
at it, asserting the verdict. This is pure TDD on the *spec*: a failure here means the design's
guardrail rules, as written, either let something dangerous through or block something benign.

Exit 0 = all pass; exit 1 = at least one mismatch.

Usage:  python3 test/run_deterministic.py
"""
from __future__ import annotations
import json
import os
import sys
from pathlib import Path

# The wall's vault segment is the SELECTED root since ADR-014 Task 1b, so the model has to be told
# which root is selected — BEFORE it is imported, since it reads the value once at module scope.
# Pinned to the CONVENTIONAL location, which is what every one of the validated cases below spells
# (`~/plainkeep/...`), so each keeps its recorded verdict for the recorded reason. Before Task 1b
# this line was unnecessary because the model hard-coded that same path; the constant became
# configuration, and this is where the configuration is stated.
# Unconditional, not setdefault: the cases below are fixed and so are their expected verdicts, so an
# inherited PLAINKEEP_HOME must not silently repoint the wall and turn 51 recorded verdicts into a
# different question. The expression mirrors lib/guardrail.py's own HOME rule exactly.
os.environ["PLAINKEEP_HOME"] = (os.environ.get("PLAINKEEP_TEST_HOME") or "/Users/tamas") + "/plainkeep"

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib.guardrail import classify  # noqa: E402

CASES = Path(__file__).resolve().parent / "cases" / "guardrail_cases.json"

GREEN, RED, DIM, BOLD, RESET = "\033[32m", "\033[31m", "\033[2m", "\033[1m", "\033[0m"


def main() -> int:
    data = json.loads(CASES.read_text(encoding="utf-8"))
    cases = data["cases"]
    passed, failed = 0, 0
    print(f"{BOLD}Deterministic guardrail suite{RESET} — {len(cases)} cases against the §5 model\n")
    for c in cases:
        d = classify(c["action"])
        ok = d.verdict == c["expect"]
        if ok:
            passed += 1
            print(f"  {GREEN}PASS{RESET} {c['name']:<28} expect={c['expect']:<8} {DIM}{d.reason}{RESET}")
        else:
            failed += 1
            print(f"  {RED}FAIL{RESET} {c['name']:<28} expect={c['expect']:<8} got={d.verdict.upper()}  {DIM}{d.reason}{RESET}")
    print(f"\n{BOLD}Result:{RESET} {GREEN}{passed} passed{RESET}, "
          f"{(RED if failed else DIM)}{failed} failed{RESET}, {len(cases)} total")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
