#!/usr/bin/env python3
"""run_agentlib.py — the §6 agent indirection: run_agent dispatch + the deterministic fallback, and
that a configured agent (faked via PLAINKEEP_AGENT_CMD) actually overrides triage's shell classifier."""
from __future__ import annotations
import importlib.util
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


def check(name, cond, detail=""):
    results.append((name, cond, detail))


def load_agent():
    spec = importlib.util.spec_from_file_location("plainkeep_agent", REPO / "bin" / "lib" / "agent.py")
    m = importlib.util.module_from_spec(spec)
    sys.modules["plainkeep_agent"] = m
    spec.loader.exec_module(m)
    return m


def main() -> int:
    ag = load_agent()
    for k in ("PLAINKEEP_AGENT", "PLAINKEEP_AGENT_CMD", "PLAINKEEP_AGENT_MODEL"):
        os.environ.pop(k, None)

    check("no agent → unavailable", not ag.available())
    check("no agent → run_agent returns None (caller falls back)", ag.run_agent("x") is None)
    os.environ["PLAINKEEP_AGENT"] = "none"
    check("PLAINKEEP_AGENT=none → unavailable", not ag.available())
    os.environ["PLAINKEEP_AGENT"] = "bogus"
    check("unknown agent → available but run_agent None", ag.available() and ag.run_agent("x") is None)
    os.environ.pop("PLAINKEEP_AGENT")

    with tempfile.TemporaryDirectory() as td:
        scr = Path(td) / "fake.sh"; scr.write_text('#!/usr/bin/env bash\necho task\n'); scr.chmod(0o755)
        os.environ["PLAINKEEP_AGENT_CMD"] = str(scr)
        check("PLAINKEEP_AGENT_CMD → available", ag.available())
        check("PLAINKEEP_AGENT_CMD → run_agent returns the agent's output", ag.run_agent("anything") == "task")
        os.environ.pop("PLAINKEEP_AGENT_CMD")

    # end-to-end: a deterministically-'note' item, with a fake agent forcing 'task', is filed as a task
    with tempfile.TemporaryDirectory() as td:
        h = Path(td); (h / "inbox").mkdir(); (h / "wiki").mkdir()
        (h / "wiki" / "conventions.md").write_text("# c\n## Filing rules\n")
        (h / "inbox" / "cap.md").write_text("---\ntype: capture\n---\nRRF merges BM25 and vectors")  # → note by the shell rule
        scr = h / "fake.sh"; scr.write_text('#!/usr/bin/env bash\necho task\n'); scr.chmod(0o755)
        env = {**os.environ, "PLAINKEEP_HOME": str(h), "PLAINKEEP_AGENT_CMD": str(scr)}
        r = subprocess.run([sys.executable, str(REPO / "bin" / "triage" / "run.py"), "--yes"],
                           capture_output=True, text=True, env=env)
        tasks = list((h / "tasks" / "active").glob("T-*.md")) if (h / "tasks" / "active").exists() else []
        notes = list((h / "wiki" / "notes").glob("*.md")) if (h / "wiki" / "notes").exists() else []
        check("agent overrides the shell classifier in triage", len(tasks) == 1 and len(notes) == 0, r.stdout + r.stderr)

    print(f"{BOLD}Agent indirection (§6 run_agent + fallback) — {len(results)} checks{RESET}\n")
    passed = sum(1 for _, ok, _ in results if ok)
    for name, ok, detail in results:
        mark = f"{GREEN}PASS{RESET}" if ok else f"{RED}FAIL{RESET}"
        print(f"  {mark} {name:<48}" + (f" {DIM}{detail.strip()[:80]}{RESET}" if (detail and not ok) else ""))
    failed = len(results) - passed
    print(f"\n{BOLD}Result:{RESET} {GREEN}{passed} passed{RESET}, "
          f"{(RED if failed else DIM)}{failed} failed{RESET}, {len(results)} checks")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
