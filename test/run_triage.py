#!/usr/bin/env python3
"""run_triage.py — exercises `plainkeep triage`: dry-run, --yes auto-file, and the interactive
override -> filing-rule learning loop (§10). Temp PLAINKEEP_HOME; stdlib only."""
from __future__ import annotations
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from lib.hermetic import seal
seal()   # hermetic: an empty throwaway registry, never the developer's real vault

REPO = Path(__file__).resolve().parents[1]
GREEN, RED, DIM, BOLD, RESET = "\033[32m", "\033[31m", "\033[2m", "\033[1m", "\033[0m"
results = []

TASK_CAP = "---\ntype: capture\ncreated: 2026-06-25\n---\nfix the Designatives webhook timeout"
NOTE_CAP = "---\ntype: capture\ncreated: 2026-06-25\n---\nRRF merges BM25 and vector rankings without score scaling"


def check(name, cond, detail=""):
    results.append((name, cond, detail))


def seed(home: Path, caps: dict):
    (home / "inbox").mkdir(parents=True, exist_ok=True)
    (home / "wiki").mkdir(parents=True, exist_ok=True)
    (home / "wiki" / "conventions.md").write_text("# Conventions\n\n## Filing rules\n", encoding="utf-8")
    for name, body in caps.items():
        (home / "inbox" / name).write_text(body, encoding="utf-8")


def triage(home: Path, *args, stdin=None):
    env = {**os.environ, "PLAINKEEP_HOME": str(home)}
    return subprocess.run([sys.executable, str(REPO / "bin/triage/run.py"), *args],
                          input=stdin, capture_output=True, text=True, env=env)


def main() -> int:
    # A) dry-run: proposes, changes nothing
    with tempfile.TemporaryDirectory() as td:
        h = Path(td); seed(h, {"cap-a.md": TASK_CAP, "cap-b.md": NOTE_CAP})
        r = triage(h, "--dry-run")
        check("dry-run proposes TASK and NOTE", "TASK ->" in r.stdout and "NOTE ->" in r.stdout, r.stdout)
        check("dry-run changes nothing", len(list((h / "inbox").glob("cap-*.md"))) == 2)

    # B) --yes: files both correctly and empties inbox
    with tempfile.TemporaryDirectory() as td:
        h = Path(td); seed(h, {"cap-a.md": TASK_CAP, "cap-b.md": NOTE_CAP})
        r = triage(h, "--yes")
        tasks = list((h / "tasks" / "active").glob("T-*.md")) if (h / "tasks" / "active").exists() else []
        notes = list((h / "wiki" / "notes").glob("*.md")) if (h / "wiki" / "notes").exists() else []
        check("--yes creates a task for the action item", len(tasks) == 1, r.stdout + r.stderr)
        check("--yes creates a note for the fact", len(notes) == 1)
        check("--yes empties the inbox", len(list((h / "inbox").glob("cap-*.md"))) == 0)
        if tasks:
            check("task captured the text", "webhook timeout" in tasks[0].read_text())

    # C) interactive override (note->task) records a filing rule
    with tempfile.TemporaryDirectory() as td:
        h = Path(td); seed(h, {"cap-b.md": NOTE_CAP})           # classifies as NOTE
        r = triage(h, stdin="t\ny\n")                           # override to TASK, then record rule
        tasks = list((h / "tasks" / "active").glob("T-*.md")) if (h / "tasks" / "active").exists() else []
        conv = (h / "wiki" / "conventions.md").read_text()
        check("override files as TASK", len(tasks) == 1, r.stdout + r.stderr)
        check("override records a filing rule", "-> task" in conv, conv)

    # D) headless `triage decide` — the --json apply path (Wave 3): a frontend files ONE item
    with tempfile.TemporaryDirectory() as td:
        h = Path(td); seed(h, {"cap-a.md": TASK_CAP, "cap-b.md": NOTE_CAP})
        r = triage(h, "decide", "cap-b.md", "note", "--json")
        env = json.loads(r.stdout.splitlines()[0]) if r.stdout.strip() else {}
        notes = list((h / "wiki" / "notes").glob("*.md")) if (h / "wiki" / "notes").exists() else []
        check("triage decide <item> note --json: ok envelope + filed",
              r.returncode == 0 and env.get("ok") is True and env.get("data", {}).get("filed"), r.stdout[:160])
        check("triage decide files the note + removes the item", len(notes) == 1
              and not (h / "inbox" / "cap-b.md").exists(), r.stdout + r.stderr)
        r = triage(h, "decide", "cap-a.md", "skip", "--json")
        env = json.loads(r.stdout.splitlines()[0]) if r.stdout.strip() else {}
        check("triage decide <item> skip --json: no-op, item stays",
              env.get("data", {}).get("filed") is None and (h / "inbox" / "cap-a.md").exists(), r.stdout[:160])
        r = triage(h, "decide", "cap-a.md", "bogus", "--json")
        check("triage decide bad decision -> usage error (2)", r.returncode == 2, f"rc={r.returncode}")
        r = triage(h, "decide", "nope.md", "note", "--json")
        check("triage decide missing item -> not-found (4)", r.returncode == 4, f"rc={r.returncode}")

    # E) headless `triage drafts decide` — promote/reject an agent-drafted note (Wave 3)
    with tempfile.TemporaryDirectory() as td:
        h = Path(td); (h / "wiki" / "notes").mkdir(parents=True)
        draft = h / "wiki" / "notes" / "concept-x.md"
        draft.write_text("---\ntype: note\nauthor: agent\nstatus: draft\ntitle: Concept X\n---\n# Concept X\n",
                         encoding="utf-8")
        r = triage(h, "drafts", "decide", "concept-x", "accept", "--json")
        env = json.loads(r.stdout.splitlines()[0]) if r.stdout.strip() else {}
        check("triage drafts decide <slug> accept --json: promotes to active",
              r.returncode == 0 and env.get("data", {}).get("status") == "active"
              and "status: active" in draft.read_text(), r.stdout[:160])
        # reject deletes
        draft2 = h / "wiki" / "notes" / "concept-y.md"
        draft2.write_text("---\ntype: note\nauthor: agent\nstatus: draft\ntitle: Concept Y\n---\n# Concept Y\n",
                          encoding="utf-8")
        r = triage(h, "drafts", "decide", "concept-y", "reject", "--json")
        check("triage drafts decide <slug> reject --json: deletes the draft",
              r.returncode == 0 and not draft2.exists(), r.stdout + r.stderr)

    print(f"{BOLD}triage (inbox -> tasks/wiki, with learning loop) — {len(results)} checks{RESET}\n")
    passed = sum(1 for _, ok, _ in results if ok)
    for name, ok, detail in results:
        mark = f"{GREEN}PASS{RESET}" if ok else f"{RED}FAIL{RESET}"
        print(f"  {mark} {name:<42}" + (f" {DIM}{detail.strip()[:80]}{RESET}" if (detail and not ok) else ""))
    failed = len(results) - passed
    print(f"\n{BOLD}Result:{RESET} {GREEN}{passed} passed{RESET}, "
          f"{(RED if failed else DIM)}{failed} failed{RESET}, {len(results)} checks")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
