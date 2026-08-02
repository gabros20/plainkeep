#!/usr/bin/env python3
"""
run_wiki_edges.py — link-reliability edge cases for the wiki. Offline, no LLM.

Asserts the checker handles the hazards a flat-slug wiki invites: slug collisions making
[[bare]] links ambiguous, [[slug#heading]]/[[slug|alias]] syntax resolving, self-links, and
cycles (no infinite loop). Edge-focused: checks only the categories declared in the fixture.

Usage:  python3 test/run_wiki_edges.py
"""
from __future__ import annotations
import json
import sys
from pathlib import Path
from lib.hermetic import seal
seal()   # hermetic: an empty throwaway registry, never the developer's real vault

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib.wiki import check_wiki  # noqa: E402

CORPUS = Path(__file__).resolve().parent / "world" / "wiki_reliability.json"
GREEN, RED, DIM, BOLD, RESET = "\033[32m", "\033[31m", "\033[2m", "\033[1m", "\033[0m"


def _norm(x):
    return sorted([tuple(i) if isinstance(i, list) else i for i in x])


def main() -> int:
    corpus = json.loads(CORPUS.read_text(encoding="utf-8"))
    out = check_wiki(corpus)        # must not raise (cycles handled)
    issues = out["issues"]
    asserts = corpus["assert_categories"]

    results = []
    for cat, exp in asserts.items():
        got = issues.get(cat, [])
        results.append((f"issues.{cat}", _norm(got) == _norm(exp), f"expected {exp}, got {got}"))
    # cycle sanity: cycle-a/cycle-b each see the other as a backlink, no crash, not broken
    bl = out["backlinks"]
    cyc_ok = "notes/cycle-b" in bl.get("notes/cycle-a", []) and "notes/cycle-a" in bl.get("notes/cycle-b", [])
    results.append(("cycle handled (mutual backlinks, no loop)", cyc_ok, f"a<-{bl.get('notes/cycle-a')}, b<-{bl.get('notes/cycle-b')}"))
    # anchor/alias links resolved (not counted broken)
    broken_targets = [t for _, t in issues["broken_links"]]
    alias_ok = "exponential-backoff#jitter" not in broken_targets and "designatives|the studio" not in broken_targets
    results.append(("anchor/alias links resolve (not broken)", alias_ok, f"broken={issues['broken_links']}"))

    print(f"{BOLD}Wiki link-reliability edges{RESET} — {len(corpus['notes'])} notes, {len(results)} checks\n")
    passed = 0
    for name, ok, detail in results:
        mark = f"{GREEN}PASS{RESET}" if ok else f"{RED}FAIL{RESET}"
        passed += 1 if ok else 0
        print(f"  {mark} {name:<44}" + ("" if ok else f" {DIM}{detail}{RESET}"))
    failed = len(results) - passed
    print(f"\n{BOLD}Result:{RESET} {GREEN}{passed} passed{RESET}, "
          f"{(RED if failed else DIM)}{failed} failed{RESET}, {len(results)} checks")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
