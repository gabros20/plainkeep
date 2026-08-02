#!/usr/bin/env python3
"""
run_wiki.py — validate the §10 wiki conventions against a fixture corpus. Offline, no LLM.

Asserts the checker finds EXACTLY the controlled defects seeded in the corpus (no more, no
fewer) and that backlinks are derived correctly (containment). A mismatch means either the
convention is under-specified or the checker model drifted from it.

Usage:  python3 test/run_wiki.py
"""
from __future__ import annotations
import json
import sys
from pathlib import Path
from lib.hermetic import seal
seal()   # hermetic: an empty throwaway registry, never the developer's real vault

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib.wiki import check_wiki  # noqa: E402

CORPUS = Path(__file__).resolve().parent / "world" / "wiki_corpus.json"
GREEN, RED, DIM, BOLD, RESET = "\033[32m", "\033[31m", "\033[2m", "\033[1m", "\033[0m"


def _norm(x):
    # normalize lists of lists/strings for order-insensitive comparison
    return sorted([tuple(i) if isinstance(i, list) else i for i in x])


def main() -> int:
    corpus = json.loads(CORPUS.read_text(encoding="utf-8"))
    out = check_wiki(corpus)
    issues, backlinks = out["issues"], out["backlinks"]
    expected = corpus["expected_issues"]

    results = []
    for cat, exp in expected.items():
        got = issues.get(cat, [])
        ok = _norm(got) == _norm(exp)
        results.append((f"issues.{cat}", ok, f"expected {exp}, got {got}"))

    # any issue category NOT declared in expected must be empty (no surprise findings)
    for cat, got in issues.items():
        if cat not in expected:
            results.append((f"issues.{cat}", len(got) == 0, f"unexpected: {got}"))

    # backlink containment
    for key, must_have in corpus.get("expected_backlinks", {}).items():
        got = backlinks.get(key, [])
        ok = set(must_have).issubset(set(got))
        results.append((f"backlinks⊇ {key}", ok, f"need {must_have} ⊆ {got}"))

    print(f"{BOLD}Wiki integrity suite{RESET} — {len(corpus['notes'])} notes, {len(results)} checks\n")
    passed = 0
    for name, ok, detail in results:
        mark = f"{GREEN}PASS{RESET}" if ok else f"{RED}FAIL{RESET}"
        passed += 1 if ok else 0
        line = f"  {mark} {name:<34}"
        print(line if ok else f"{line} {DIM}{detail}{RESET}")
    failed = len(results) - passed
    print(f"\n{BOLD}Result:{RESET} {GREEN}{passed} passed{RESET}, "
          f"{(RED if failed else DIM)}{failed} failed{RESET}, {len(results)} checks")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
