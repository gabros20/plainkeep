#!/usr/bin/env python3
"""
run_jobs.py — check the §15 jobs registry against the design's own job rules. Offline, no LLM.

A FAIL here is a spec inconsistency: a job that can't legally be scheduled, references a verb
the surface doesn't document, declares the wrong risk, or writes where the path wall forbids.

Usage:  python3 test/run_jobs.py
"""
from __future__ import annotations
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from lib.hermetic import seal
seal()   # hermetic: an empty throwaway registry, never the developer's real vault

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib.jobsmodel import check_jobs  # noqa: E402
from lib import vaultfx  # noqa: E402

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
# The design-model registry (adversarial fixture) AND the registry actually shipped in the repo —
# both must obey §15. The shipped one being legal is what makes `plainkeep job apply` safe to run.
REGISTRIES = [("model fixture", HERE / "world" / "jobs.json"),
              ("shipped jobs/registry.json", HERE.parent / "jobs" / "registry.json")]
GREEN, RED, DIM, BOLD, RESET = "\033[32m", "\033[31m", "\033[2m", "\033[1m", "\033[0m"

extra: list[tuple[str, bool, str]] = []


def check(name, cond, detail=""):
    extra.append((name, bool(cond), detail))


def case_day_bookends_are_scheduled() -> None:
    """THE BOTH-ENDS CHECK (ADR-022). The registry has scheduled the day's CLOSE since §15 and never
    its START, which made the product's own claim — "the day begins and ends without you asking" —
    true at one end only. `close_nudge` is the shape the `start` job has to match: one verb, the
    `--automated` marker, safe_write, journal-only writes."""
    reg = json.loads((REPO / "jobs" / "registry.json").read_text(encoding="utf-8"))
    jobs = reg.get("jobs", {})
    start = jobs.get("start")
    check("shipped registry schedules the day's START, not only its close", bool(start), str(sorted(jobs)))
    if not start:
        return
    check("start job runs the verb with the --automated marker",
          start.get("command") == "plainkeep start --automated", str(start.get("command")))
    check("start job is a morning daily schedule", start.get("schedule") == {"daily": "07:30"},
          str(start.get("schedule")))
    check("start job is safe_write and writes only the journal",
          start.get("risk") == "safe_write" and start.get("writes") == ["~/plainkeep/journal"], str(start))


def case_engine_defaults_match_the_shipped_registry() -> None:
    """THE PARITY PIN: two copies of the canonical jobs, one file apart.

    `plainkeep job set` can SEED a canonical job an existing vault never received (`jobs/registry.json`
    is vault content, so an engine update never delivers it), which means the engine now carries its
    own copy of those definitions — `launchdlib.DEFAULT_JOBS`. A second copy of anything drifts, and
    this one drifts SILENTLY and asymmetrically: a new vault gets the template's registry, an old one
    gets the seed, and nothing compares them. So they are compared here, exactly — same names, same
    definitions, same order — and the design-model fixture (`test/world/jobs.json`, which the §15
    invariants above are run against) must agree about every job it shares with them."""
    shipped = json.loads((REPO / "jobs" / "registry.json").read_text(encoding="utf-8"))["jobs"]
    code = ("import json, sys; sys.path.insert(0, %r)\n"
            "from lib import launchdlib\n"
            "sys.stdout.write(json.dumps(launchdlib.DEFAULT_JOBS))\n" % str(REPO / "bin"))
    with tempfile.TemporaryDirectory() as _td:   # lib.paths refuses to guess a PLAINKEEP_HOME
        proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                              env={**os.environ, "PLAINKEEP_HOME": _td})
    defaults = json.loads(proc.stdout) if proc.returncode == 0 else {}
    check("the engine exposes DEFAULT_JOBS (launchdlib)", bool(defaults),
          (proc.stderr or proc.stdout).strip()[-200:])
    check("DEFAULT_JOBS names exactly the shipped registry's jobs, in the same order",
          list(defaults) == list(shipped), f"{list(defaults)} != {list(shipped)}")
    differing = sorted(n for n in set(defaults) | set(shipped) if defaults.get(n) != shipped.get(n))
    check("every DEFAULT_JOBS definition is identical to the shipped one", not differing, str(differing))

    model = json.loads((HERE / "world" / "jobs.json").read_text(encoding="utf-8"))["jobs"]
    # The model fixture is adversarial by design (it holds jobs the product does not ship, to exercise
    # the §15 rules), so the pin is on the OVERLAP: where both describe the same job, they agree.
    shared = sorted(set(model) & set(defaults))
    drifted = [n for n in shared if model[n].get("schedule") != defaults[n].get("schedule")
               or model[n].get("command") != defaults[n].get("command")
               or model[n].get("risk") != defaults[n].get("risk")]
    check("the design-model fixture agrees with the engine defaults where they overlap",
          not drifted, f"shared={shared} drifted={drifted}")


def case_start_automated_marks_the_journal() -> None:
    """`plainkeep start --automated` must behave EXACTLY like `plainkeep start` except for the audit
    line, which says who asked. That is the same contract `close --automated` already keeps, and it
    is what lets a human read the journal in the morning and know whether the 07:30 agent opened the
    day or they did."""
    with tempfile.TemporaryDirectory() as td:
        h = Path(td) / "vault"
        h.mkdir()
        vaultfx.mark_vault(h)
        env = {**os.environ, "PLAINKEEP_HOME": str(h)}
        r = subprocess.run([sys.executable, str(REPO / "bin" / "start" / "run.py"), "--automated"],
                           capture_output=True, text=True, env=env)
        journal = "\n".join(p.read_text(encoding="utf-8") for p in (h / "journal").rglob("*.md")) \
            if (h / "journal").exists() else ""
        check("start --automated exits 0 and seeds the day's journal",
              r.returncode == 0 and "# " in journal, f"rc={r.returncode} {r.stdout}{r.stderr}")
        check("start --automated marks the audit line '(automated)'",
              "started the day (automated)" in journal, journal[-400:])

    with tempfile.TemporaryDirectory() as td:
        h = Path(td) / "vault"
        h.mkdir()
        vaultfx.mark_vault(h)
        env = {**os.environ, "PLAINKEEP_HOME": str(h)}
        subprocess.run([sys.executable, str(REPO / "bin" / "start" / "run.py")],
                       capture_output=True, text=True, env=env)
        journal = "\n".join(p.read_text(encoding="utf-8") for p in (h / "journal").rglob("*.md"))
        check("a manual start is NOT marked automated",
              "started the day" in journal and "(automated)" not in journal, journal[-400:])

    # Idempotent for a scheduled run: the 07:30 job may fire on a day the human already started, and
    # must not seed the carry-forward block twice.
    with tempfile.TemporaryDirectory() as td:
        h = Path(td) / "vault"
        h.mkdir()
        vaultfx.mark_vault(h)
        env = {**os.environ, "PLAINKEEP_HOME": str(h)}
        for _ in range(2):
            subprocess.run([sys.executable, str(REPO / "bin" / "start" / "run.py"), "--automated"],
                           capture_output=True, text=True, env=env)
        journal = "\n".join(p.read_text(encoding="utf-8") for p in (h / "journal").rglob("*.md"))
        check("a repeated automated start does not re-seed the day's note",
              journal.count("## Carried forward") <= 1, journal[-400:])


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

    case_day_bookends_are_scheduled()
    case_engine_defaults_match_the_shipped_registry()
    case_start_automated_marks_the_journal()
    print(f"{BOLD}The automated day (ADR-022) — {len(extra)} checks{RESET}\n")
    for name, ok, detail in extra:
        mark = f"{GREEN}PASS{RESET}" if ok else f"{RED}FAIL{RESET}"
        print(f"  {mark} {name:<58}" + (f" {DIM}{detail.strip()[:70]}{RESET}" if (detail and not ok) else ""))
    print()
    total_passed += sum(1 for _, ok, _ in extra if ok)
    total += len(extra)
    failed_overall = failed_overall or any(not ok for _, ok, _ in extra)

    print(f"{BOLD}Result:{RESET} {GREEN}{total_passed} passed{RESET}, "
          f"{(RED if failed_overall else DIM)}{total - total_passed} failed{RESET}, {total} checks")
    return 1 if failed_overall else 0


if __name__ == "__main__":
    raise SystemExit(main())
