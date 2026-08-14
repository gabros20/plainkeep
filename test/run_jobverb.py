#!/usr/bin/env python3
"""run_jobverb.py — exercises the `plainkeep job` verb: list (with legality flags), run <name>
(manual fallback), apply (render launchd plists, skipping non-schedulable jobs), and the §15
ACTIVATION lifecycle — enable / disable / status against a FAKE launchctl. Temp PLAINKEEP_HOME.

NOTHING HERE MAY TOUCH THE REAL MACHINE. Activation is the one part of this verb whose blast radius
is outside the vault (`~/Library/LaunchAgents`, the user's live launchd domain), so both halves of it
are redirected for every invocation in this suite, in `run()` and with no per-call opt-in:

  * `PLAINKEEP_LAUNCH_AGENTS_DIR` → a temp directory, so an installed plist lands in the fixture.
  * `PLAINKEEP_LAUNCHCTL` → `fake-launchctl`, a shell script written below that RECORDS its argv and
    keeps a tiny "loaded" state directory. Ordering (bootout before bootstrap), the domain target and
    the exact plist path are asserted from that log rather than believed.

The fake is also what makes the suite host-independent: `bootstrap`/`bootout`/`print` behave the same
on a Linux runner as on a Mac, and no real `launchctl` is ever executed."""
from __future__ import annotations
import json
import os
import stat
import subprocess
import sys
import tempfile
from pathlib import Path
from lib.hermetic import seal
seal()   # hermetic: an empty throwaway registry, never the developer's real vault

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib import vaultfx  # noqa: E402
GREEN, RED, DIM, BOLD, RESET = "\033[32m", "\033[31m", "\033[2m", "\033[1m", "\033[0m"
results = []

REGISTRY = {
    "external_allowlist": [],
    "jobs": {
        "index":       {"command": "plainkeep index",      "schedule": {"interval_minutes": 60}, "risk": "read"},
        "consolidate": {"command": "plainkeep consolidate", "schedule": {"daily": "02:30"},       "risk": "safe_write"},
        "danger":      {"command": "plainkeep capture x",   "schedule": {"daily": "09:00"},       "risk": "confirm"},
    },
}

# The fake `launchctl`. It models exactly the three subcommands the verb uses, including the failure
# the real one returns for a bootout of something that was never loaded (which `enable` must ignore):
#   bootstrap <domain> <plist>   -> remembers the label, rc 0
#   bootout   <domain>/<label>   -> forgets it (rc 0), or rc 3 when it was not loaded
#   print     <domain>/<label>   -> rc 0 iff loaded
FAKE_LAUNCHCTL = """#!/bin/sh
printf '%s\\n' "$*" >> "$PK_FAKE_LOG"
mkdir -p "$PK_FAKE_STATE"
case "$1" in
  bootstrap)
    label=`basename "$3" .plist`
    touch "$PK_FAKE_STATE/$label"
    exit 0 ;;
  bootout)
    label=${2##*/}
    if [ -e "$PK_FAKE_STATE/$label" ]; then rm -f "$PK_FAKE_STATE/$label"; exit 0; fi
    exit 3 ;;
  print)
    label=${2##*/}
    if [ -e "$PK_FAKE_STATE/$label" ]; then exit 0; fi
    exit 1 ;;
esac
exit 1
"""

FAKE: dict[str, Path] = {}


def install_fake_launchctl(tmp: Path) -> None:
    """Write the fake binary + its log/state dirs, and the redirected LaunchAgents dir."""
    exe = tmp / "fake-launchctl"
    exe.write_text(FAKE_LAUNCHCTL, encoding="utf-8")
    exe.chmod(exe.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    FAKE["exe"] = exe
    FAKE["log"] = tmp / "launchctl.log"
    FAKE["state"] = tmp / "launchctl-state"
    FAKE["agents"] = tmp / "LaunchAgents"
    FAKE["log"].write_text("", encoding="utf-8")
    FAKE["state"].mkdir()
    FAKE["agents"].mkdir()


def text(p: Path) -> str:
    """Read a file that a not-yet-implemented action may not have created — so a missing artefact
    reports as a FAILED check rather than a traceback that hides every check after it."""
    try:
        return p.read_text(encoding="utf-8")
    except OSError:
        return ""


def fake_log() -> list[str]:
    return [ln for ln in FAKE["log"].read_text(encoding="utf-8").splitlines() if ln.strip()]


def clear_log() -> None:
    FAKE["log"].write_text("", encoding="utf-8")


def check(name, cond, detail=""):
    results.append((name, cond, detail))


def run(home, *args):
    env = {**os.environ, "PLAINKEEP_HOME": str(home)}
    if FAKE:
        env.update({"PLAINKEEP_LAUNCHCTL": str(FAKE["exe"]),
                    "PLAINKEEP_LAUNCH_AGENTS_DIR": str(FAKE["agents"]),
                    "PK_FAKE_LOG": str(FAKE["log"]), "PK_FAKE_STATE": str(FAKE["state"])})
    return subprocess.run([sys.executable, str(REPO / "bin" / "job" / "run.py"), *args],
                          capture_output=True, text=True, env=env)


def main() -> int:
    with tempfile.TemporaryDirectory() as td:
        # The vault and the "machine" (LaunchAgents + the fake launchctl's log/state) are SIBLINGS,
        # not nested: an installed plist must be provably outside the vault, the way the real one is.
        machine = Path(td) / "machine"
        machine.mkdir()
        install_fake_launchctl(machine)
        h = Path(td) / "vault"
        h.mkdir()
        (h / "wiki" / "notes").mkdir(parents=True)
        (h / "wiki" / "notes" / "alpha.md").write_text(
            "---\ntype: note\nupdated: 2026-06-20\n---\n# Alpha\nretrieval and ranking\n")
        (h / "jobs").mkdir()
        (h / "jobs" / "registry.json").write_text(json.dumps(REGISTRY), encoding="utf-8")
        # `plainkeep job run` now re-enters the DISPATCHER (Task 9, "one door") instead of calling the
        # verb's run.py directly — so the temp PLAINKEEP_HOME needs a real dispatcher tree. Symlink the
        # engine `plainkeep` + `bin` in; `job run index`/`consolidate` then pass the guardrail like any call.
        os.symlink(REPO / "plainkeep", h / "plainkeep")
        os.symlink(REPO / "bin", h / "bin")
        vaultfx.mark_vault(h)   # Task 1b: the dispatcher validates the root before it dispatches

        r = run(h, "list")
        check("job list shows the jobs", "index" in r.stdout and "consolidate" in r.stdout, r.stdout)
        check("job list flags a non-schedulable job", "danger" in r.stdout and "not schedulable" in r.stdout, r.stdout)

        r = run(h, "run", "index")
        check("job run index builds the index (rc 0)", r.returncode == 0 and (h / ".index" / "plainkeep.sqlite").exists(), r.stdout + r.stderr)

        r = run(h, "run", "consolidate")
        j = "\n".join(p.read_text() for p in (h / "journal").rglob("*.md")) if (h / "journal").exists() else ""
        check("job run consolidate writes a digest", r.returncode == 0 and "## Consolidate" in j, r.stdout + r.stderr)

        r = run(h, "run", "danger")
        check("job run refuses a non-schedulable job", r.returncode == 1 and "refusing" in (r.stdout + r.stderr), r.stdout + r.stderr)

        r = run(h, "apply")
        ld = h / "jobs" / "launchd"
        check("job apply renders schedulable plists", (ld / "com.plainkeep.index.plist").exists() and (ld / "com.plainkeep.consolidate.plist").exists(), r.stdout + r.stderr)
        check("job apply skips non-schedulable", not (ld / "com.plainkeep.danger.plist").exists() and "skipped" in r.stdout, r.stdout)
        if (ld / "com.plainkeep.index.plist").exists():
            pl = (ld / "com.plainkeep.index.plist").read_text()
            check("plist is well-formed launchd", "StartInterval" in pl and "com.plainkeep.index" in pl and "PLAINKEEP_HOME" in pl, pl[:200])
        check("job apply's hint points at the activation VERB, not a manual launchctl line",
              "plainkeep job enable" in r.stdout and "launchctl load" not in r.stdout, r.stdout)
        r = run(h, "run", "nope")
        check("job run rejects an unknown job name", r.returncode == 2, r.stdout + r.stderr)

        # ---------------------------------------------------------------------------------
        # The §15 ACTIVATION lifecycle: enable / disable / status. Everything below runs
        # against the fake launchctl and the redirected LaunchAgents dir installed above.
        # ---------------------------------------------------------------------------------
        agents = FAKE["agents"]

        # status after `apply` alone: rendered, but neither installed nor loaded. This is exactly the
        # gap the old manual handoff left open, and the row is what makes it visible.
        clear_log()
        r = run(h, "status", "--json")
        rows = [json.loads(ln) for ln in r.stdout.splitlines() if ln.strip()]
        by_name = {row["name"]: row for row in rows[1:]}
        check("job status reports rendered/installed/loaded per job",
              r.returncode == 0 and {"index", "consolidate"} <= set(by_name)
              and all({"rendered", "installed", "loaded", "drift"} <= set(row) for row in by_name.values()),
              r.stdout + r.stderr)
        check("job status: rendered-but-not-installed after apply alone",
              by_name.get("index", {}).get("rendered") is True
              and by_name.get("index", {}).get("installed") is False
              and by_name.get("index", {}).get("loaded") is False, str(by_name.get("index")))

        # The confirm gate. `enable` writes outside the vault and mutates the live launchd domain, so
        # it is confirm-class: no --yes (and no tty) must refuse with exit 3 having done NOTHING.
        clear_log()
        r = run(h, "enable", "--all")
        check("job enable without --yes refuses (exit 3) and installs nothing",
              r.returncode == 3 and not list(agents.glob("*.plist")) and not fake_log(),
              f"rc={r.returncode} agents={list(agents.glob('*.plist'))} log={fake_log()}")

        # --dry-run is a READ: it prints the plan, needs no --yes, writes nothing and calls nothing.
        clear_log()
        r = run(h, "enable", "--all", "--dry-run")
        check("job enable --dry-run writes nothing and calls no launchctl",
              r.returncode == 0 and not list(agents.glob("*.plist")) and not fake_log()
              and "com.plainkeep.index.plist" in r.stdout,
              f"rc={r.returncode} out={r.stdout} log={fake_log()}")

        # The real thing.
        clear_log()
        r = run(h, "enable", "--all", "--yes")
        log = fake_log()
        installed = sorted(p.name for p in agents.glob("*.plist"))
        check("job enable --all --yes installs a COPY into LaunchAgents (never a symlink)",
              r.returncode == 0
              and installed == ["com.plainkeep.consolidate.plist", "com.plainkeep.index.plist"]
              and not any((agents / n).is_symlink() for n in installed),
              f"rc={r.returncode} installed={installed} err={r.stderr}")
        check("job enable skips the non-schedulable job under --all",
              not (agents / "com.plainkeep.danger.plist").exists() and "danger" in r.stdout, r.stdout)
        idx_out = [ln for ln in log if "com.plainkeep.index" in ln]
        check("job enable boots OUT before it bootstraps (idempotent reload)",
              len(idx_out) == 2 and idx_out[0].startswith("bootout ") and idx_out[1].startswith("bootstrap "),
              str(log))
        check("job enable targets the gui domain and the INSTALLED plist path",
              any(ln == f"bootout gui/{os.getuid()}/com.plainkeep.index" for ln in log)
              and any(ln == f"bootstrap gui/{os.getuid()} {agents / 'com.plainkeep.index.plist'}"
                      for ln in log), str(log))
        check("the installed copy is byte-identical to the rendered vault artefact",
              bool(text(agents / "com.plainkeep.index.plist"))
              and text(agents / "com.plainkeep.index.plist") == text(ld / "com.plainkeep.index.plist"))

        r = run(h, "status", "--json")
        rows = [json.loads(ln) for ln in r.stdout.splitlines() if ln.strip()]
        by_name = {row["name"]: row for row in rows[1:]}
        check("job status reports loaded once enabled",
              all(by_name.get(n, {}).get("installed") and by_name.get(n, {}).get("loaded")
                  for n in ("index", "consolidate")),
              str(by_name))

        # Drift: a rendered plist that no longer matches a fresh render of the registry. Stale files
        # are never trusted — `status` says so, and `enable` re-renders rather than copying the stale one.
        (ld / "com.plainkeep.index.plist").write_text("<plist>stale</plist>\n", encoding="utf-8")
        r = run(h, "status", "--json")
        rows = [json.loads(ln) for ln in r.stdout.splitlines() if ln.strip()]
        by_name = {row["name"]: row for row in rows[1:]}
        check("job status flags DRIFT when a rendered plist no longer matches the registry",
              by_name.get("index", {}).get("drift") is True
              and by_name.get("consolidate", {}).get("drift") is False, str(by_name))
        r = run(h, "enable", "index", "--yes")
        check("job enable re-renders from the registry (never copies a stale file)",
              r.returncode == 0 and "stale" not in text(ld / "com.plainkeep.index.plist")
              and bool(text(agents / "com.plainkeep.index.plist"))
              and "stale" not in text(agents / "com.plainkeep.index.plist"),
              r.stdout + r.stderr)

        # A named non-schedulable job is REFUSED (skipping it silently under an explicit name would
        # be the wrong lesson); an unknown name is a usage error.
        r = run(h, "enable", "danger", "--yes")
        check("job enable refuses a named non-schedulable job",
              r.returncode == 1 and "not schedulable" in (r.stdout + r.stderr)
              and not (agents / "com.plainkeep.danger.plist").exists(), r.stdout + r.stderr)
        r = run(h, "enable", "nope", "--yes")
        check("job enable rejects an unknown job name (exit 2)", r.returncode == 2, r.stdout + r.stderr)

        # disable: bootout + remove the LaunchAgents copy. The rendered vault artefact STAYS — it is
        # the vault-side record of what the schedule is, and `apply` is what owns it.
        clear_log()
        r = run(h, "disable", "--all", "--dry-run")
        check("job disable --dry-run removes nothing and calls no launchctl",
              r.returncode == 0 and (agents / "com.plainkeep.index.plist").exists() and not fake_log(),
              f"rc={r.returncode} log={fake_log()}")
        r = run(h, "disable", "--all")
        check("job disable without --yes refuses (exit 3)",
              r.returncode == 3 and (agents / "com.plainkeep.index.plist").exists(), r.stdout + r.stderr)
        clear_log()
        r = run(h, "disable", "--all", "--yes")
        check("job disable --all --yes boots out and removes the LaunchAgents copies",
              r.returncode == 0 and not list(agents.glob("*.plist"))
              and any(ln == f"bootout gui/{os.getuid()}/com.plainkeep.index" for ln in fake_log()),
              f"rc={r.returncode} log={fake_log()} left={list(agents.glob('*.plist'))}")
        check("job disable leaves the rendered vault artefacts in place",
              (ld / "com.plainkeep.index.plist").exists() and (ld / "com.plainkeep.consolidate.plist").exists())
        r2 = run(h, "disable", "--all", "--yes")
        check("job disable is idempotent when nothing is loaded", r2.returncode == 0, r2.stdout + r2.stderr)
        r = run(h, "status", "--json")
        rows = [json.loads(ln) for ln in r.stdout.splitlines() if ln.strip()]
        by_name = {row["name"]: row for row in rows[1:]}
        check("job status is back to rendered-only after disable",
              by_name.get("index", {}).get("rendered") and not by_name.get("index", {}).get("installed")
              and not by_name.get("index", {}).get("loaded"), str(by_name))

    print(f"{BOLD}Jobs scheduler verb (job list/run/apply/enable/disable/status) — {len(results)} checks{RESET}\n")
    passed = sum(1 for _, ok, _ in results if ok)
    for name, ok, detail in results:
        mark = f"{GREEN}PASS{RESET}" if ok else f"{RED}FAIL{RESET}"
        print(f"  {mark} {name:<46}" + (f" {DIM}{detail.strip()[:80]}{RESET}" if (detail and not ok) else ""))
    failed = len(results) - passed
    print(f"\n{BOLD}Result:{RESET} {GREEN}{passed} passed{RESET}, "
          f"{(RED if failed else DIM)}{failed} failed{RESET}, {len(results)} checks")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
