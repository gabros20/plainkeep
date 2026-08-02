#!/usr/bin/env python3
"""
run_state.py — consistency-invariant checks. Offline, no LLM.

Tests the four areas in statemodel.py: task folder-wins, index-rebuild-equals-files,
journal atomic-append vs read-modify-write, and restore ordering.

Usage:  python3 test/run_state.py
"""
from __future__ import annotations
import sys
from pathlib import Path
from lib.hermetic import seal
seal()   # hermetic: an empty throwaway registry, never the developer's real vault

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib.statemodel import (effective_status, status_drift, derive_index, rebuild_index,  # noqa: E402
                            atomic_append, read_modify_write, line_count, validate_restore_order)

GREEN, RED, DIM, BOLD, RESET = "\033[32m", "\033[31m", "\033[2m", "\033[1m", "\033[0m"
results: list[tuple[str, bool, str]] = []


def check(name, cond, detail=""):
    results.append((name, cond, detail))


def t_task_folder_wins():
    # folder=active, frontmatter says done -> effective is active, and drift is detected
    check("folder wins over frontmatter", effective_status("active", "done") == "active")
    check("status drift detected", status_drift("active", "done") is True)
    check("no drift when aligned", status_drift("done", "done") is False)


def t_index_rebuild():
    files = {"a.md": "alpha", "b.md": "bravo", "c.md": "charlie"}
    truth = derive_index(files)
    # corrupt index: wrong hash for a, a stale entry for a deleted file, missing c
    corrupt = dict(truth)
    corrupt["a.md"] = "DEADBEEF"
    corrupt["zz-deleted.md"] = "ghost"
    del corrupt["c.md"]
    rebuilt = rebuild_index(files, corrupt)
    check("rebuild equals files-derived truth", rebuilt == truth, f"{rebuilt}")
    check("stale DB-only entry dropped", "zz-deleted.md" not in rebuilt)
    check("missing entry restored", "c.md" in rebuilt)
    check("corrupted hash fixed", rebuilt["a.md"] == truth["a.md"])


def t_journal_append():
    base = "day start\n"
    lines = ["09:00 driverA captured note", "09:00 driverB closed task T-1"]
    atomic = atomic_append(base, lines)
    rmw = read_modify_write(base, lines)
    check("atomic append keeps all entries", line_count(atomic) == 3, f"lines={line_count(atomic)}")
    check("read-modify-write LOSES an entry (why append-only is required)",
          line_count(rmw) < 3, f"rmw lines={line_count(rmw)}")


def t_restore_order():
    good = ["toolchain", "auth", "clone_plainkeep", "doctor_init", "clone_work", "restic_restore"]
    bad = ["toolchain", "clone_plainkeep", "auth", "clone_work", "restic_restore", "doctor_init"]
    check("correct restore order has no violations", validate_restore_order(good) == [],
          f"{validate_restore_order(good)}")
    v = validate_restore_order(bad)
    check("clone-before-auth flagged", any("clone_plainkeep" in x and "auth" in x for x in v), f"{v}")
    check("restic-before-skeleton flagged", any("restic_restore" in x for x in v), f"{v}")


def main() -> int:
    for t in (t_task_folder_wins, t_index_rebuild, t_journal_append, t_restore_order):
        t()
    print(f"{BOLD}State & consistency invariants{RESET} — {len(results)} assertions\n")
    passed = 0
    for name, ok, detail in results:
        mark = f"{GREEN}PASS{RESET}" if ok else f"{RED}FAIL{RESET}"
        passed += 1 if ok else 0
        print(f"  {mark} {name:<52}" + (f" {DIM}{detail}{RESET}" if (detail and not ok) else ""))
    failed = len(results) - passed
    print(f"\n{BOLD}Result:{RESET} {GREEN}{passed} passed{RESET}, "
          f"{(RED if failed else DIM)}{failed} failed{RESET}, {len(results)} assertions")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
