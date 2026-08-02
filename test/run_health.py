#!/usr/bin/env python3
"""run_health.py — exercises `plainkeep doctor` (self-check) and `plainkeep wiki` (navigation), temp PLAINKEEP_HOME."""
from __future__ import annotations
import os
import shutil
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


def run(home, verb, *args, stdin=None):
    env = {**os.environ, "PLAINKEEP_HOME": str(home)}
    return subprocess.run([sys.executable, str(REPO / "bin" / verb / "run.py"), *args],
                          input=stdin, capture_output=True, text=True, env=env)


def note(home, rel, typ, title, updated, body=""):
    p = home / "wiki" / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(f"---\ntype: {typ}\ntitle: {title}\nstatus: active\ncreated: 2026-01-01\n"
                 f"updated: {updated}\ntags: []\n---\n# {title}\n\n{body}\n", encoding="utf-8")


def main() -> int:
    # ---- doctor: a well-formed vault should pass (no FAIL) ----
    with tempfile.TemporaryDirectory() as td:
        h = Path(td)
        shutil.copy(REPO / "AGENTS.md", h / "AGENTS.md")
        shutil.copy(REPO / "CLAUDE.md", h / "CLAUDE.md")
        (h / "skills" / "operate-plainkeep").mkdir(parents=True)
        shutil.copy(REPO / "skills" / "operate-plainkeep" / "SKILL.md", h / "skills" / "operate-plainkeep" / "SKILL.md")
        # per-agent adapters (relative skill symlinks, like the real repo)
        (h / ".codex").mkdir(); (h / ".claude").mkdir()
        (h / ".codex" / "config.toml").write_text('sandbox_mode="workspace-write"\n')
        (h / ".claude" / "settings.json").write_text('{"permissions":{"allow":["Bash(plainkeep:*)"]}}')
        os.symlink("../skills", h / ".codex" / "skills"); os.symlink("../skills", h / ".claude" / "skills")
        run(h, "doctor", "--init")   # create skeleton folders
        run(h, "help")               # generate plainkeep.json
        r = run(h, "doctor")
        check("doctor: well-formed vault has no FAIL", r.returncode == 0 and "FAIL" not in r.stdout, r.stdout)
        check("doctor checks adapters + manifest", "AGENTS.md present" in r.stdout and "plainkeep.json parses" in r.stdout, r.stdout)
        check("doctor verifies per-agent adapters", ".claude/settings.json parses" in r.stdout
              and ".codex/skills → skills/ resolves" in r.stdout, r.stdout)
        # a broken adapter symlink must FAIL
        (h / ".claude" / "skills").unlink(); os.symlink("../nope", h / ".claude" / "skills")
        rb = run(h, "doctor")
        check("doctor FAILs on a broken adapter symlink", rb.returncode == 1 and "BROKEN" in rb.stdout, rb.stdout)
        os.unlink(h / ".claude" / "skills"); os.symlink("../skills", h / ".claude" / "skills")
        # `plainkeep index --manifest` regenerates plainkeep.json from the cmd.json sidecars
        (h / "plainkeep.json").unlink(missing_ok=True)
        rm = run(h, "index", "--manifest")
        check("plainkeep index --manifest regenerates plainkeep.json", rm.returncode == 0 and (h / "plainkeep.json").exists(), rm.stdout + rm.stderr)
        r2 = run(h, "doctor")  # idempotent
        check("doctor is idempotent", r2.returncode == 0)

    # ---- doctor catches a missing adapter ----
    with tempfile.TemporaryDirectory() as td:
        h = Path(td)
        run(h, "doctor", "--init")
        r = run(h, "doctor")
        check("doctor FAILs when AGENTS.md is missing", r.returncode == 1 and "AGENTS.md MISSING" in r.stdout, r.stdout)

    # ---- wiki: open / new / backlinks / stale / orphans ----
    with tempfile.TemporaryDirectory() as td:
        h = Path(td)
        note(h, "notes/alpha.md", "note", "Alpha", "2026-06-20", "see [[beta]]")
        note(h, "notes/beta.md", "note", "Beta", "2026-06-20")
        note(h, "notes/orphan.md", "note", "Orphan", "2026-06-20")
        note(h, "notes/old.md", "note", "Old note", "2025-01-01")

        r = run(h, "wiki", "new", "note", "Gamma Idea")
        check("wiki new creates a slugged note", (h / "wiki" / "notes" / "gamma-idea.md").exists(), r.stdout + r.stderr)
        r = run(h, "wiki", "new", "note", "Gamma Idea")
        check("wiki new refuses a duplicate slug", r.returncode == 1, r.stdout + r.stderr)
        r = run(h, "wiki", "backlinks", "beta")
        check("wiki backlinks finds the linker", "alpha" in r.stdout, r.stdout)
        r = run(h, "wiki", "orphans")
        check("wiki orphans lists an unlinked note", "orphan" in r.stdout, r.stdout)
        r = run(h, "wiki", "stale", "90")
        check("wiki stale lists an old note", "old" in r.stdout, r.stdout)
        r = run(h, "wiki", "open", "beta")
        check("wiki open prints the note", "# Beta" in r.stdout, r.stdout)

    print(f"{BOLD}Health verbs (doctor, wiki) — {len(results)} checks{RESET}\n")
    passed = sum(1 for _, ok, _ in results if ok)
    for name, ok, detail in results:
        mark = f"{GREEN}PASS{RESET}" if ok else f"{RED}FAIL{RESET}"
        print(f"  {mark} {name:<44}" + (f" {DIM}{detail.strip()[:80]}{RESET}" if (detail and not ok) else ""))
    failed = len(results) - passed
    print(f"\n{BOLD}Result:{RESET} {GREEN}{passed} passed{RESET}, "
          f"{(RED if failed else DIM)}{failed} failed{RESET}, {len(results)} checks")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
