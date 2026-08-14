#!/usr/bin/env python3
"""run_health.py — exercises `plainkeep doctor` (self-check) and `plainkeep wiki` (navigation), temp PLAINKEEP_HOME."""
from __future__ import annotations
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from lib.hermetic import seal
seal()   # hermetic: an empty throwaway registry, never the developer's real vault

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib import launchdfx  # noqa: E402
GREEN, RED, DIM, BOLD, RESET = "\033[32m", "\033[31m", "\033[2m", "\033[1m", "\033[0m"
results = []

# Doctor's automation rows (ADR-022) ask launchd what it has loaded. On the developer's own Mac that
# would be the developer's own launchd session, so the seam is installed process-wide for this whole
# suite: EVERY `run()` below carries the fake, whether or not the check under test involves jobs.
FAKE = None


def check(name, cond, detail=""):
    results.append((name, cond, detail))


def run(home, verb, *args, stdin=None):
    env = {**os.environ, "PLAINKEEP_HOME": str(home)}
    if FAKE is not None:
        env.update(FAKE.env)
    return subprocess.run([sys.executable, str(REPO / "bin" / verb / "run.py"), *args],
                          input=stdin, capture_output=True, text=True, env=env)


def wellformed(h: Path) -> None:
    """The minimum a vault needs for `plainkeep doctor` to have no FAIL — factored out so a case that
    is about something else (the automation rows) can assert on the exit code without a red herring."""
    shutil.copy(REPO / "AGENTS.md", h / "AGENTS.md")
    shutil.copy(REPO / "CLAUDE.md", h / "CLAUDE.md")
    (h / "skills" / "operate-plainkeep").mkdir(parents=True, exist_ok=True)
    (h / ".codex").mkdir(exist_ok=True); (h / ".claude").mkdir(exist_ok=True)
    (h / ".codex" / "config.toml").write_text('sandbox_mode="workspace-write"\n')
    (h / ".claude" / "settings.json").write_text('{"permissions":{"allow":["Bash(plainkeep:*)"]}}')
    os.symlink("../skills", h / ".codex" / "skills"); os.symlink("../skills", h / ".claude" / "skills")
    run(h, "doctor", "--init")
    run(h, "help")


def note(home, rel, typ, title, updated, body=""):
    p = home / "wiki" / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(f"---\ntype: {typ}\ntitle: {title}\nstatus: active\ncreated: 2026-01-01\n"
                 f"updated: {updated}\ntags: []\n---\n# {title}\n\n{body}\n", encoding="utf-8")


def case_automation_rows(tmp: Path) -> None:
    """Doctor's ADVISORY automation rows (ADR-022).

    Rendering a plist and having launchd load it are different facts, and until now doctor could see
    neither. The rows are WARN-only on purpose: an operator who deliberately runs plainkeep by hand
    has a healthy vault, and a health check that fails for a declined optional layer is one people
    learn to ignore. What it must not do is stay silent when the schedule the operator DID ask for
    is not running."""
    h = tmp / "vault"
    h.mkdir()
    wellformed(h)
    (h / "jobs").mkdir(exist_ok=True)
    (h / "jobs" / "registry.json").write_text(json.dumps({
        "external_allowlist": [],
        "jobs": {"start": {"command": "plainkeep start --automated", "schedule": {"daily": "07:30"},
                           "risk": "safe_write"}}}), encoding="utf-8")

    # 1. Nothing rendered: silence. Declining automation is a choice, not a defect.
    r = run(h, "doctor")
    check("doctor stays quiet about automation when nothing is rendered",
          r.returncode == 0 and "job enable" not in r.stdout, r.stdout)

    # 2. Rendered, never loaded — the state the old printed handoff left behind.
    r = run(h, "job", "apply")
    r = run(h, "doctor")
    check("doctor WARNs when plists are rendered but not loaded",
          "warn" in r.stdout and "not loaded" in r.stdout and "plainkeep job enable --all" in r.stdout,
          r.stdout)
    check("the rendered-not-loaded row is advisory (doctor still exits 0)", r.returncode == 0,
          f"rc={r.returncode}")

    # 3. Loaded: an ok row, no nag.
    FAKE.mark_loaded("com.plainkeep.start")
    r = run(h, "doctor")
    check("doctor reports an ok row once the jobs are loaded",
          r.returncode == 0 and "not loaded" not in r.stdout
          and "scheduled job" in r.stdout, r.stdout)

    # 4. Drift — a rendered plist that no longer matches the registry. Different cause, different
    #    remedy: re-render with `apply`, not re-activate with `enable`.
    (h / "jobs" / "launchd" / "com.plainkeep.start.plist").write_text(
        "<plist>stale</plist>\n", encoding="utf-8")
    r = run(h, "doctor")
    check("doctor WARNs on a rendered plist that drifted from the registry",
          "warn" in r.stdout and "plainkeep job apply" in r.stdout, r.stdout)
    check("the drift row is advisory too (doctor still exits 0)", r.returncode == 0, f"rc={r.returncode}")
    check("doctor never FAILs on an automation row", "FAIL" not in r.stdout, r.stdout)


def main() -> int:
    global FAKE
    # ---- doctor: a well-formed vault should pass (no FAIL) ----
    with tempfile.TemporaryDirectory() as td:
        FAKE = launchdfx.install(Path(td) / "machine")
        h = Path(td)
        shutil.copy(REPO / "AGENTS.md", h / "AGENTS.md")
        shutil.copy(REPO / "CLAUDE.md", h / "CLAUDE.md")
        # The DIRECTORY only, deliberately: since Phase 2 Task 2 doctor reads SKILL.md from
        # `paths.SKILLS` (the ENGINE tree), so a copy in the fixture vault is inert — the
        # `adapter: skills/operate-plainkeep/SKILL.md present` row passes on the engine's copy either
        # way, and a vault-local copy would make this fixture look like it proves something it does
        # not. What the vault still needs is the directory, because the `.codex/skills` and
        # `.claude/skills` symlinks below resolve `<vault>/skills/operate-plainkeep`.
        (h / "skills" / "operate-plainkeep").mkdir(parents=True)
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
        # The row is worded for what it MEANS rather than for where the skill sits: Phase 2 Task 2
        # moved `operate-plainkeep` into the engine, and a migrated vault reaches it through an
        # adapter pointing there instead of at `<vault>/skills`. The assertion is the same one —
        # this adapter provides the skill — and this fixture still satisfies it the Phase 1 way.
        check("doctor verifies per-agent adapters", ".claude/settings.json parses" in r.stdout
              and ".codex/skills provides operate-plainkeep" in r.stdout, r.stdout)
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

    # ---- doctor: the advisory automation rows (ADR-022) ----
    with tempfile.TemporaryDirectory() as td:
        FAKE = launchdfx.install(Path(td) / "machine")
        case_automation_rows(Path(td))

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
