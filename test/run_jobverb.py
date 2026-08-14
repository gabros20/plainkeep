#!/usr/bin/env python3
"""run_jobverb.py — exercises the `plainkeep job` verb: list (with legality flags), run <name>
(manual fallback), apply (render launchd plists, skipping non-schedulable jobs), and the §15
ACTIVATION lifecycle — enable / disable / status against a FAKE launchctl. Temp PLAINKEEP_HOME.

NOTHING HERE MAY TOUCH THE REAL MACHINE. Activation is the one part of this verb whose blast radius
is outside the vault (`~/Library/LaunchAgents`, the user's live launchd domain), so both halves of it
are redirected for every invocation in this suite, in `run()` and with no per-call opt-in:

  * `PLAINKEEP_LAUNCH_AGENTS_DIR` → a temp directory, so an installed plist lands in the fixture.
  * `PLAINKEEP_LAUNCHCTL` → the fake from `test/lib/launchdfx.py`, which RECORDS its argv and keeps a
    tiny "loaded" state directory. Ordering (bootout before bootstrap), the domain target and the
    exact plist path are asserted from that log rather than believed.

The fake is also what makes the suite host-independent: `bootstrap`/`bootout`/`print` behave the same
on a Linux runner as on a Mac, and no real `launchctl` is ever executed."""
from __future__ import annotations
import json
import os
import plistlib
import subprocess
import sys
import tempfile
from pathlib import Path
from lib.hermetic import seal
seal()   # hermetic: an empty throwaway registry, never the developer's real vault

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib import launchdfx, vaultfx  # noqa: E402
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

FAKE: dict[str, object] = {}


def text(p: Path) -> str:
    """Read a file that a not-yet-implemented action may not have created — so a missing artefact
    reports as a FAILED check rather than a traceback that hides every check after it."""
    try:
        return p.read_text(encoding="utf-8")
    except OSError:
        return ""


def fake_log() -> list[str]:
    return FAKE["fx"].calls()


def clear_log() -> None:
    FAKE["fx"].clear()


def check(name, cond, detail=""):
    results.append((name, cond, detail))


def run(home, *args):
    env = {**os.environ, "PLAINKEEP_HOME": str(home)}
    if FAKE:
        env.update(FAKE["fx"].env)
    return subprocess.run([sys.executable, str(REPO / "bin" / "job" / "run.py"), *args],
                          capture_output=True, text=True, env=env)


def render_plist(home: Path, name: str) -> str:
    """`launchdlib.plist()` for one registry job, obtained from a child process so this suite keeps
    its subprocess shape (and never puts `bin/lib` on its own `sys.path`)."""
    code = ("import sys; sys.path.insert(0, %r)\n"
            "from lib import launchdlib\n"
            "reg = launchdlib.load_registry()\n"
            "sys.stdout.write(launchdlib.plist(%r, reg['jobs'][%r]))\n"
            % (str(REPO / "bin"), name, name))
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                       env={**os.environ, "PLAINKEEP_HOME": str(home)})
    return r.stdout if r.returncode == 0 else ""


def case_plist_is_not_injectable(td: Path) -> None:
    """THE PLIST IS DATA, NOT A STRING TEMPLATE (r1/B1).

    `jobs/registry.json` is vault content: agent-writable, and it syncs between machines. Rendering it
    into an f-string of XML meant a command could close the `<array>` and open top-level launchd keys
    of its own — `Program` (which OVERRIDES the executable `ProgramArguments` names) and `RunAtLoad`
    (which fires it at login instead of at 07:30). Its first two tokens were `plainkeep index`, so the
    §15 token validation passed it, and before this branch the result was an inert file in the vault.
    This branch installs and bootstraps it.

    The check is a ROUND TRIP through `plistlib` — the same parser launchd uses — because that is the
    only question that matters: does the document a parser sees contain exactly the argv the registry
    named, and nothing else? Asserting on the rendered text (no `<key>Program`) would pass for an
    escaping scheme that was merely different rather than correct."""
    h = td / "inject"
    (h / "jobs").mkdir(parents=True)
    vaultfx.mark_vault(h)
    payload = ("plainkeep index --q</string></array>"
               "<key>Program</key><string>/bin/sh</string>"
               "<key>RunAtLoad</key><true/>"
               "<key>ProgramArguments2</key><array><string>x")
    ampersand = "plainkeep index --tag a&b<c"
    (h / "jobs" / "registry.json").write_text(json.dumps({"external_allowlist": [], "jobs": {
        "evil": {"command": payload, "schedule": {"daily": "07:30"}, "risk": "read"},
        "amp": {"command": ampersand, "schedule": {"daily": "07:30"}, "risk": "read"},
    }}), encoding="utf-8")

    def parsed(name: str) -> dict:
        """Parse the render, or report an unparseable one as a failed check rather than a traceback
        (an unescaped `&` produces invalid XML, which is its own kind of broken)."""
        try:
            return plistlib.loads(render_plist(h, name).encode("utf-8"))
        except Exception as exc:
            check(f"the rendered plist for '{name}' PARSES", False, str(exc))
            return {}

    doc = parsed("evil")
    check("a hostile registry command renders a plist that still parses", bool(doc))

    expected_keys = {"Label", "ProgramArguments", "EnvironmentVariables",
                     "StartCalendarInterval", "StandardOutPath", "StandardErrorPath"}
    check("registry content cannot introduce launchd keys of its own",
          set(doc) == expected_keys, f"got {sorted(doc)}")
    check("registry content cannot set Program (which overrides the executable)",
          "Program" not in doc, str(sorted(doc)))
    check("registry content cannot set RunAtLoad (fire at login, not at the scheduled time)",
          "RunAtLoad" not in doc, str(sorted(doc)))
    check("the payload survives as ARGUMENTS, verbatim and inert",
          doc.get("ProgramArguments", [])[1:] == payload.split()[1:],
          str(doc.get("ProgramArguments")))
    check("the plist's label still names the job, not something the command supplied",
          doc.get("Label") == "com.plainkeep.evil", str(doc.get("Label")))

    amp_doc = parsed("amp")
    check("XML metacharacters in a command round-trip exactly (& and <)",
          amp_doc.get("ProgramArguments", [])[1:] == ["index", "--tag", "a&b<c"],
          str(amp_doc.get("ProgramArguments")))


def case_illegal_jobs_are_never_scheduled(td: Path) -> None:
    """WHAT `job run` REFUSES, `job enable` MUST NOT SCHEDULE (r1/B2).

    `_validate()` is the §15 legality model — one verb, no inline logic, a verb that exists, an
    external command on the allowlist. `list` shows its warnings and `run` refuses on them. `enable`
    and `apply` checked only the RISK CLASS, which is a self-declared field in the same file, so a
    job the product refuses to run once by hand was installed and bootstrapped to run unattended
    forever. That is the ratchet pointing the wrong way.

    The refusal is WHOLE-COMMAND rather than per-job-skip: an illegal entry means the registry is
    malformed, and the operator has to see that. See the report's fix-wave design decisions."""
    h = td / "illegal"
    (h / "jobs").mkdir(parents=True)
    vaultfx.mark_vault(h)
    (h / "jobs" / "registry.json").write_text(json.dumps({"external_allowlist": [], "jobs": {
        "evil_external": {"command": "/bin/sh -c curl-evil-placeholder",
                          "schedule": {"daily": "07:30"}, "risk": "safe_write"},
        "xml_smuggle": {"command": "plainkeep index</string><string>--x",
                        "schedule": {"daily": "07:30"}, "risk": "read"},
        "ghost_verb": {"command": "plainkeep notaverb", "schedule": {"daily": "07:30"}, "risk": "read"},
        "fine": {"command": "plainkeep index", "schedule": {"interval_minutes": 60}, "risk": "read"},
    }}), encoding="utf-8")
    agents = FAKE["fx"].agents

    r = run(h, "list")
    check("job list still flags all three illegal jobs (the model itself is unchanged)",
          all(n in r.stdout for n in ("evil_external", "xml_smuggle", "ghost_verb"))
          and r.stdout.count("⚠") >= 3, r.stdout)

    FAKE["fx"].clear()
    before = set(p.name for p in agents.glob("*.plist"))
    r = run(h, "enable", "--all", "--yes")
    after = set(p.name for p in agents.glob("*.plist"))
    check("job enable --all refuses a registry holding illegal jobs",
          r.returncode == 1 and "refusing" in (r.stdout + r.stderr), f"rc={r.returncode} {r.stdout}{r.stderr}")
    check("the refusal names every offender, not just the first",
          all(n in (r.stdout + r.stderr) for n in ("evil_external", "xml_smuggle", "ghost_verb")),
          r.stdout + r.stderr)
    check("a refused enable installs NOTHING and calls no launchctl",
          after == before and not FAKE["fx"].calls(),
          f"installed={sorted(after - before)} calls={FAKE['fx'].calls()}")

    r = run(h, "enable", "ghost_verb", "--yes")
    check("job enable <name> refuses a named illegal job",
          r.returncode == 1 and "not a built verb" in (r.stdout + r.stderr), r.stdout + r.stderr)

    r = run(h, "apply")
    check("job apply refuses to render an illegal registry",
          r.returncode == 1 and not (h / "jobs" / "launchd").exists(), r.stdout + r.stderr)

    # And the legal case still works — a refusal that fires on everything teaches nothing.
    (h / "jobs" / "registry.json").write_text(json.dumps({"external_allowlist": [], "jobs": {
        "fine": {"command": "plainkeep index", "schedule": {"interval_minutes": 60}, "risk": "read"},
    }}), encoding="utf-8")
    r = run(h, "enable", "--all", "--yes")
    check("a legal registry still enables normally",
          r.returncode == 0 and (agents / "com.plainkeep.fine.plist").exists(), r.stdout + r.stderr)
    run(h, "disable", "--all", "--yes")


def case_enable_contains_a_mid_loop_refusal(td: Path) -> None:
    """A REFUSAL IS ONE JOB'S FAILURE, NOT THE LOOP'S (r1/I4).

    A registry key that traverses (`../../..`) is correctly refused by the path wall when the
    VAULT-side render is attempted — that is what actually stops an escape, and it is load-bearing.
    But the refusal used to `sys.exit(5)` from inside the loop, so any job processed earlier was
    already installed and bootstrapped and the operator got exit 5 with no statement of what had been
    left enabled. `enable` already collects per-job results for a failed `bootstrap`; a guardrail
    refusal now takes the same path."""
    h = td / "midloop"
    (h / "jobs").mkdir(parents=True)
    vaultfx.mark_vault(h)
    (h / "jobs" / "registry.json").write_text(json.dumps({"external_allowlist": [], "jobs": {
        "aaa_first": {"command": "plainkeep index", "schedule": {"interval_minutes": 60}, "risk": "read"},
        "../../../../../../tmp/pk-escape": {"command": "plainkeep index",
                                            "schedule": {"daily": "07:30"}, "risk": "read"},
        "zzz_last": {"command": "plainkeep consolidate", "schedule": {"daily": "02:30"},
                     "risk": "safe_write"},
    }}), encoding="utf-8")
    agents = FAKE["fx"].agents
    FAKE["fx"].clear()
    r = run(h, "enable", "--all", "--yes", "--json")
    out = r.stdout + r.stderr
    check("a traversing registry key is still refused (nothing escapes the roots)",
          not Path("/tmp/pk-escape.plist").exists()
          and not (agents / "com.plainkeep.../../../../../../tmp/pk-escape.plist").exists(), out)
    check("the loop finishes and reports the refusal as that job's failure",
          "zzz_last" in out and ("refus" in out.lower() or "deny" in out.lower()
                                or "escape" in out.lower()), out)
    check("a mid-loop refusal exits nonzero but still summarizes partial state",
          r.returncode != 0 and "aaa_first" in out, f"rc={r.returncode} {out}")
    run(h, "disable", "--all", "--yes")


def main() -> int:
    with tempfile.TemporaryDirectory() as td:
        # The vault and the "machine" (LaunchAgents + the fake launchctl's log/state) are SIBLINGS,
        # not nested: an installed plist must be provably outside the vault, the way the real one is.
        machine = Path(td) / "machine"
        machine.mkdir()
        FAKE["fx"] = launchdfx.install(machine)
        FAKE["agents"] = FAKE["fx"].agents
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

        # r1 fix wave: the adversarial cases. Same fixture root, fresh vaults.
        case_plist_is_not_injectable(Path(td))
        case_illegal_jobs_are_never_scheduled(Path(td))
        case_enable_contains_a_mid_loop_refusal(Path(td))

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
