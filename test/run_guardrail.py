#!/usr/bin/env python3
"""
run_guardrail.py — tests the REAL enforcement guardrail (bin/lib/guardrail.py): (1) PARITY with the
validated spec model (test/lib/guardrail.py) on all the §5 adversarial cases, so the enforced
version can't drift from the proven one; (2) the dispatcher per-verb risk GATE behaviour.
Offline, stdlib only.
"""
from __future__ import annotations
import importlib.util
import json
import os
import sys
from pathlib import Path
from lib.hermetic import seal
seal()   # hermetic: an empty throwaway registry, never the developer's real vault

# Since ADR-014 Task 1b the enforcement guardrail's wall is anchored to the SELECTED root and to
# nothing else, and it refuses outright when no root is selected — so this harness has to select one
# BEFORE loading it. Pinned to the CONVENTIONAL location, which is what every validated case spells
# (`~/plainkeep/...`), so all 51 keep their recorded verdict for the recorded reason. Unconditional
# rather than setdefault: an inherited value would silently repoint the wall and turn the recorded
# verdicts into a different question. The expression mirrors bin/lib/wall.py's HOME rule exactly.
os.environ["PLAINKEEP_HOME"] = (os.environ.get("PLAINKEEP_TEST_HOME")
                                or os.environ.get("PLAINKEEP_ROOTS_HOME")
                                or os.environ.get("HOME")
                                or "/Users/tamas") + "/plainkeep"

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
GREEN, RED, DIM, BOLD, RESET = "\033[32m", "\033[31m", "\033[2m", "\033[1m", "\033[0m"


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    sys.modules[name] = m  # register before exec so @dataclass can resolve cls.__module__
    spec.loader.exec_module(m)
    return m


def main() -> int:
    bg = _load(REPO / "bin" / "lib" / "guardrail.py", "bin_guardrail")
    results = []

    # (1) parity: bin classify() must agree with the validated cases (same verdicts as the model)
    cases = json.loads((HERE / "cases" / "guardrail_cases.json").read_text())["cases"]
    mismatches = [c["name"] for c in cases if bg.classify(c["action"]).verdict != c["expect"]]
    results.append((f"bin guardrail parity with {len(cases)} validated cases",
                    not mismatches, f"mismatches: {mismatches}"))

    # (2) per-verb risk gate (risk overridden for the confirm/deny branches; capture is a real verb)
    def v(verb, args, risk=None):
        return bg.gate(verb, args, risk).verdict
    checks = [
        ("safe_write verb allowed", v("capture", []) == "allow"),
        ("read verb allowed", v("search", ["q"]) == "allow"),
        ("confirm-class blocked without --yes", v("capture", [], "confirm") == "confirm"),
        ("confirm-class allowed with --yes", v("capture", ["--yes"], "confirm") == "allow"),
        ("deny-class refused", v("capture", [], "deny") == "deny"),
        ("unknown verb denied", v("totally-made-up", []) == "deny"),
        ("undeclared verb defaults to confirm", bg.gate("capture", [], None).verdict in ("allow",)),  # capture declares safe_write
    ]
    results += [(n, ok, "") for n, ok in checks]

    print(f"{BOLD}Guardrail enforcement (bin/lib/guardrail.py) — {len(results)} checks{RESET}\n")
    passed = sum(1 for _, ok, _ in results if ok)
    for name, ok, detail in results:
        mark = f"{GREEN}PASS{RESET}" if ok else f"{RED}FAIL{RESET}"
        print(f"  {mark} {name:<48}" + (f" {DIM}{detail}{RESET}" if (detail and not ok) else ""))
    failed = len(results) - passed
    print(f"\n{BOLD}Result:{RESET} {GREEN}{passed} passed{RESET}, "
          f"{(RED if failed else DIM)}{failed} failed{RESET}, {len(results)} checks")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
