#!/usr/bin/env python3
"""
run_jobs.py — check the §15 jobs registry against the design's own job rules. Offline, no LLM.

A FAIL here is a spec inconsistency: a job that can't legally be scheduled, references a verb
the surface doesn't document, declares the wrong risk, or writes where the path wall forbids.

Usage:  python3 test/run_jobs.py
"""
from __future__ import annotations
import json
import sys
from pathlib import Path
from lib.hermetic import seal
seal()   # hermetic: an empty throwaway registry, never the developer's real vault

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib.jobsmodel import check_jobs  # noqa: E402

HERE = Path(__file__).resolve().parent
# The design-model registry (adversarial fixture) AND the registry actually shipped in the repo —
# both must obey §15. The shipped one being legal is what makes `plainkeep job apply` safe to run.
REGISTRIES = [("model fixture", HERE / "world" / "jobs.json"),
              ("shipped jobs/registry.json", HERE.parent / "jobs" / "registry.json")]
GREEN, RED, DIM, BOLD, RESET = "\033[32m", "\033[31m", "\033[2m", "\033[1m", "\033[0m"


def main() -> int:
    total_passed = total = 0
    failed_overall = False
    for label, reg_path in REGISTRIES:
        if not reg_path.exists():
            continue
        registry = json.loads(reg_path.read_text(encoding="utf-8"))
        findings = check_jobs(registry)
        passed = sum(1 for f in findings if f.ok)
        failed = len(findings) - passed
        total_passed += passed; total += len(findings)
        failed_overall = failed_overall or bool(failed)
        print(f"{BOLD}Jobs-registry invariants — {label}{RESET} — {len(findings)} checks over {len(registry['jobs'])} jobs\n")
        cur = None
        for f in findings:
            if f.job != cur:
                cur = f.job
                print(f"  {BOLD}{f.job}{RESET}")
            mark = f"{GREEN}PASS{RESET}" if f.ok else f"{RED}FAIL{RESET}"
            print(f"      {mark} {f.rule:<22} {DIM}{f.detail}{RESET}")
        print()
    print(f"{BOLD}Result:{RESET} {GREEN}{total_passed} passed{RESET}, "
          f"{(RED if failed_overall else DIM)}{total - total_passed} failed{RESET}, {total} checks")
    return 1 if failed_overall else 0


if __name__ == "__main__":
    raise SystemExit(main())
