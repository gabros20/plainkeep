#!/usr/bin/env python3
"""The Python half of the python-semantics differential fuzz: re-computes bool(v) and f"{v}" for
every value emitted by pysem_fuzz.ts and fails on any mismatch.

A case the TS side marked `xfail` carries a KNOWN, documented divergence (see the XFAIL list in
pysem_fuzz.ts and .orchestrate/task-3-report.md). It is printed on every run rather than suppressed,
and an xfail case that unexpectedly AGREES is an ERROR — closing a divergence must update the list.

Usage:  bun run test/fuzz/pysem_fuzz.ts | python3 test/fuzz/pysem_check.py
        (or just: python3 test/run_fuzz.py)
"""
import json
import sys

data = json.load(sys.stdin)
bad = 0
xfail = 0
stale = 0
for c in data:
    v = json.loads(c["json"])
    agrees = bool(v) == c["truthy"] and f"{v}" == c["str"]
    if agrees and c.get("xfail"):
        stale += 1
        print(f"STALE XFAIL json={c['json'][:60]!r} now agrees — remove it from the XFAIL list")
    elif not agrees:
        detail = (
            f"json={c['json'][:60]!r} truthy ts={c['truthy']} py={bool(v)} "
            f"| str ts={c['str'][:60]!r} py={f'{v}'[:60]!r}"
        )
        if c.get("xfail"):
            xfail += 1
            print(f"XFAIL (disclosed) {detail}")
        else:
            bad += 1
            print(f"MISMATCH {detail}")
print(f"total={len(data)} mismatches={bad} xfail={xfail} stale_xfail={stale}")
sys.exit(1 if (bad or stale) else 0)
