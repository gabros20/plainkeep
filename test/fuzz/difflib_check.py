#!/usr/bin/env python3
"""The Python half of the difflib differential fuzz: re-computes every case emitted by
difflib_fuzz.ts with the real CPython difflib and fails on any mismatch.

Usage:  bun run test/fuzz/difflib_fuzz.ts | python3 test/fuzz/difflib_check.py
        (or just: python3 test/run_fuzz.py)
"""
import difflib
import json
import sys

data = json.load(sys.stdin)
bad = 0
for i, c in enumerate(data):
    exp = difflib.get_close_matches(c["w"], c["possibilities"], n=3, cutoff=0.6)
    if exp != c["res"]:
        bad += 1
        if bad <= 20:
            print(f"MISMATCH #{i} w={c['w']!r} pos={c['possibilities']} ts={c['res']} py={exp}")
print(f"total={len(data)} mismatches={bad}")
sys.exit(1 if bad else 0)
