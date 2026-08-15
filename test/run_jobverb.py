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
import shutil
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


def default_jobs(home: Path) -> dict:
    """`launchdlib.DEFAULT_JOBS` — the engine's canonical job definitions, read from a child process
    for the same reason `render_plist` does: this suite never puts `bin/lib` on its own `sys.path`.
    (`lib.paths` refuses to guess a PLAINKEEP_HOME, so the child gets one even though the constant
    does not depend on it.)"""
    code = ("import json, sys; sys.path.insert(0, %r)\n"
            "from lib import launchdlib\n"
            "sys.stdout.write(json.dumps(launchdlib.DEFAULT_JOBS))\n" % str(REPO / "bin"))
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                       env={**os.environ, "PLAINKEEP_HOME": str(home)})
    return json.loads(r.stdout) if r.returncode == 0 else {}


def registry_of(home: Path) -> dict:
    return json.loads(text(home / "jobs" / "registry.json") or "{}")


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


def case_traversing_key_is_refused_before_anything_runs(td: Path) -> None:
    """A REGISTRY KEY IS A FILENAME, SO IT IS VALIDATED AS ONE (r1/M2).

    The key becomes `com.plainkeep.<name>.plist` in two directories, one of them outside the vault.
    The pathwall exemption and the docs claimed the destination was bounded because it never comes
    from an argument — true, and not the operative bound: it comes from a registry KEY, and the
    registry is vault content. What actually stopped a traversing key was the path wall firing on the
    vault-side render, which is a backstop that fires MID-LOOP, after earlier jobs are bootstrapped.

    Now it is a legality rule, checked with the rest of §15 before anything is rendered — so the
    refusal is whole-command and nothing at all is installed. It moved once more since: the rule now
    lives at `load_registry()` (r2/M6), UPSTREAM of every reader rather than on the paths that
    install, so the refusal below arrives before the verb has an action at all. Its message is on
    stderr for the same reason every other refusal's is."""
    h = td / "traverse"
    (h / "jobs").mkdir(parents=True)
    vaultfx.mark_vault(h)
    (h / "jobs" / "registry.json").write_text(json.dumps({"external_allowlist": [], "jobs": {
        "aaa_first": {"command": "plainkeep index", "schedule": {"interval_minutes": 60}, "risk": "read"},
        "../../../../../../tmp/pk-escape": {"command": "plainkeep index",
                                            "schedule": {"daily": "07:30"}, "risk": "read"},
    }}), encoding="utf-8")
    agents = FAKE["fx"].agents
    FAKE["fx"].clear()
    before = set(p.name for p in agents.glob("*.plist"))
    r = run(h, "enable", "--all", "--yes")
    out = r.stdout + r.stderr
    check("a traversing registry key is refused as a NAME, before any render",
          r.returncode == 1 and "not a plain identifier" in out, f"rc={r.returncode} {out}")
    check("nothing escapes the roots, and nothing at all was installed",
          not Path("/tmp/pk-escape.plist").exists()
          and set(p.name for p in agents.glob("*.plist")) == before
          and not FAKE["fx"].calls(), f"calls={FAKE['fx'].calls()}")
    lst = run(h, "list")
    check("job list refuses the bad key too (one legality model, all readers)",
          lst.returncode != 0 and "not a plain identifier" in (lst.stdout + lst.stderr),
          f"rc={lst.returncode} {lst.stdout}{lst.stderr}")


def case_enable_contains_a_per_job_failure(td: Path) -> None:
    """ONE JOB'S FAILURE IS NOT THE LOOP'S (r1/I4).

    `_enable` mutates as it goes — by the third job the first two are already bootstrapped — so an
    exception escaping the loop left a nonzero exit and no statement of what had been enabled. The
    `bootstrap` failure path right beside it already collected per-job results; every other per-job
    failure now takes the same path.

    The injected failure is a directory sitting where one job's installed plist must go, so
    `copyfile` raises for exactly that job. That is a real OSError on the real path, not a patched
    function — and it lands mid-loop, between two jobs that must both still be handled."""
    h = td / "perjob"
    (h / "jobs").mkdir(parents=True)
    vaultfx.mark_vault(h)
    (h / "jobs" / "registry.json").write_text(json.dumps({"external_allowlist": [], "jobs": {
        "aaa_first": {"command": "plainkeep index", "schedule": {"interval_minutes": 60}, "risk": "read"},
        "mmm_blocked": {"command": "plainkeep index", "schedule": {"daily": "07:30"}, "risk": "read"},
        "zzz_last": {"command": "plainkeep consolidate", "schedule": {"daily": "02:30"},
                     "risk": "safe_write"},
    }}), encoding="utf-8")
    agents = FAKE["fx"].agents
    (agents / "com.plainkeep.mmm_blocked.plist").mkdir()      # a directory where the file must go
    FAKE["fx"].clear()
    r = run(h, "enable", "--all", "--yes", "--json")
    env = json.loads(r.stdout.splitlines()[0]) if r.stdout.strip() else {}
    results_by = {d["name"]: d for d in env.get("data", {}).get("results", [])}
    check("the loop finishes: every job is reported, not just the ones before the failure",
          set(results_by) == {"aaa_first", "mmm_blocked", "zzz_last"}, str(sorted(results_by)))
    check("the failing job is reported as that job's failure, with its reason",
          results_by.get("mmm_blocked", {}).get("ok") is False
          and results_by["mmm_blocked"]["error"], str(results_by.get("mmm_blocked")))
    check("the jobs on either side of it still succeeded",
          results_by.get("aaa_first", {}).get("ok") is True
          and results_by.get("zzz_last", {}).get("ok") is True, str(results_by))
    check("a per-job failure still exits nonzero", r.returncode == 1, f"rc={r.returncode}")
    check("the job AFTER the failure really was bootstrapped",
          any(c.startswith("bootstrap ") and "zzz_last" in c for c in FAKE["fx"].calls()),
          str(FAKE["fx"].calls()))
    (agents / "com.plainkeep.mmm_blocked.plist").rmdir()
    run(h, "disable", "--all", "--yes")


_MALFORMED_REGISTRY = {"external_allowlist": [], "jobs": {
    # aaa/zzz sort either side of the malformed entry, so the loop provably continues past it.
    "aaa_ok": {"command": "plainkeep index", "schedule": {"interval_minutes": 60}, "risk": "read"},
    # A CONTROL CHARACTER IN THE COMMAND, and the choice is deliberate. This entry used to be a
    # missing `schedule`, which is now caught EARLIER — `_validate()` reads the shared schedule parser,
    # so a malformed schedule is a §15 legality warning and `apply`/`enable` refuse whole-command
    # before the loop is reached (`case_a_malformed_schedule_is_DIAGNOSED`). The containment r2/I5
    # asked for is still the thing under test here, so the injected fault has to be one the legality
    # model legitimately does NOT model: `plistlib` refuses a control character in a string, the §15
    # token rules have no opinion about one, and the first two tokens are a real verb.
    "mmm_unrenderable": {"command": "plainkeep index --tag \x01", "risk": "read",
                         "schedule": {"daily": "07:30"}},
    "zzz_ok": {"command": "plainkeep consolidate", "schedule": {"daily": "02:30"}, "risk": "safe_write"},
}}


def case_enable_contains_a_malformed_entry(td: Path) -> None:
    """A MALFORMED ENTRY IS THAT JOB'S FAILURE, NOT THE LOOP'S CRASH (r2/I5).

    r1/I4's containment caught only OSError, but `launchdlib.plist()` raises for a registry entry it
    cannot render — which escaped `_enable` as a raw traceback AFTER earlier jobs were already
    bootstrapped: the exact complaint I4 was commissioned to close, through a different door. The
    read path (`job_states`) already treats "can't re-render" as data; the mutating path must not be
    less careful.

    The malformed SCHEDULE that used to stand in for this is now diagnosed and refused up front
    (`case_a_malformed_schedule_is_DIAGNOSED`), so the fault injected here is one the §15 legality
    model does not claim to model at all — see `_MALFORMED_REGISTRY`. The containment must survive
    the diagnosis being added, not be replaced by it: it is the backstop for every fault nobody has
    thought of yet."""
    h = td / "malformed_enable"
    (h / "jobs").mkdir(parents=True)
    vaultfx.mark_vault(h)
    (h / "jobs" / "registry.json").write_text(json.dumps(_MALFORMED_REGISTRY), encoding="utf-8")
    clear_log()
    r = run(h, "enable", "--all", "--yes", "--json")
    check("a malformed entry raises no traceback out of enable",
          "Traceback" not in r.stderr, r.stderr[:200])
    env = json.loads(r.stdout.splitlines()[0]) if r.stdout.strip() else {}
    results_by = {d["name"]: d for d in env.get("data", {}).get("results", [])}
    check("the loop finishes past the malformed entry: every job is reported",
          set(results_by) == {"aaa_ok", "mmm_unrenderable", "zzz_ok"}, str(sorted(results_by)))
    check("the malformed entry is its own recorded failure, with the reason",
          results_by.get("mmm_unrenderable", {}).get("ok") is False
          and results_by["mmm_unrenderable"]["error"], str(results_by.get("mmm_unrenderable")))
    check("the job sorting AFTER the malformed one was still bootstrapped",
          any(c.startswith("bootstrap ") and "zzz_ok" in c for c in fake_log()), str(fake_log()))
    check("a malformed entry still exits nonzero from enable", r.returncode == 1, f"rc={r.returncode}")
    run(h, "disable", "--all", "--yes")


def case_apply_contains_a_malformed_entry(td: Path) -> None:
    """`apply` renders the same projection `enable` installs, so the same malformed entry must be
    its recorded failure there too — partial renders reported, not a traceback (r2/I5)."""
    h = td / "malformed_apply"
    (h / "jobs").mkdir(parents=True)
    vaultfx.mark_vault(h)
    (h / "jobs" / "registry.json").write_text(json.dumps(_MALFORMED_REGISTRY), encoding="utf-8")
    r = run(h, "apply", "--json")
    check("a malformed entry raises no traceback out of apply",
          "Traceback" not in r.stderr, r.stderr[:200])
    env = json.loads(r.stdout.splitlines()[0]) if r.stdout.strip() else {}
    data = env.get("data", {})
    check("apply still renders the well-formed jobs either side of it",
          set(data.get("rendered", [])) == {"com.plainkeep.aaa_ok.plist", "com.plainkeep.zzz_ok.plist"},
          str(data.get("rendered")))
    check("apply reports the malformed entry as that job's failure, with the reason",
          [f["name"] for f in data.get("failed", [])] == ["mmm_unrenderable"]
          and data["failed"][0].get("error"), str(data.get("failed")))
    check("a malformed entry still exits nonzero from apply", r.returncode == 1, f"rc={r.returncode}")


# ---------------------------------------------------------------------------------------------
# `plainkeep job set` — the schedule times as a PRODUCT SURFACE.
#
# The registry stays the single source of truth (engine.txt calls it "yours"), so the times have to
# be editable through the surface rather than by hand-editing JSON — and editable WITHOUT activating
# anything: `set` is a vault write, and consent for launchd stays with the confirm-class `enable`.
# ---------------------------------------------------------------------------------------------

_SET_REGISTRY = {
    "description": "fixture registry — key order is asserted, so this literal order is the contract",
    "external_allowlist": ["restic"],
    "jobs": {
        # NOTE: no `start` — the seed-from-engine-defaults case needs a canonical job to be MISSING,
        # which is the state every vault created before ADR-022 is actually in.
        "index": {"command": "plainkeep index", "schedule": {"interval_minutes": 60},
                  "risk": "read", "writes": ["~/plainkeep/.index"]},
        "close_nudge": {"command": "plainkeep close --automated", "schedule": {"daily": "18:30"},
                        "risk": "safe_write", "writes": ["~/plainkeep/journal"]},
        "organize_scan": {"command": "plainkeep organize scan", "schedule": {"weekly": "Sun 03:00"},
                          "risk": "safe_write", "writes": ["~/plainkeep/inbox/organize"]},
    },
}


def _set_vault(td: Path, leaf: str) -> Path:
    h = td / leaf
    (h / "jobs").mkdir(parents=True)
    vaultfx.mark_vault(h)
    (h / "jobs" / "registry.json").write_text(json.dumps(_SET_REGISTRY, indent=2), encoding="utf-8")
    return h


def case_set_edits_only_the_schedule(td: Path) -> None:
    """A READ-MODIFY-WRITE OF THE PARSED JSON, NOT A REGENERATION.

    `jobs/registry.json` is vault content the human also edits: it carries a `description`, an
    `external_allowlist`, per-job `writes` declarations and a deliberate job order. A `set` that
    rebuilt the file from a model would silently drop whatever the model does not know about, which
    is the failure mode that makes people stop trusting a config-editing command. So the assertion
    is not "the schedule changed" — it is "the schedule changed AND nothing else did"."""
    h = _set_vault(td, "set_happy")
    reg_file = h / "jobs" / "registry.json"

    r = run(h, "set", "close_nudge", "--daily", "22:00")
    reg = registry_of(h)
    check("job set <name> --daily rewrites that job's schedule",
          r.returncode == 0 and reg.get("jobs", {}).get("close_nudge", {}).get("schedule") == {"daily": "22:00"},
          f"rc={r.returncode} {r.stdout}{r.stderr}")
    check("job set preserves every other field of the edited job",
          {k: v for k, v in reg.get("jobs", {}).get("close_nudge", {}).items() if k != "schedule"}
          == {k: v for k, v in _SET_REGISTRY["jobs"]["close_nudge"].items() if k != "schedule"},
          str(reg.get("jobs", {}).get("close_nudge")))
    check("job set preserves the registry's top-level keys and job ORDER",
          list(reg) == list(_SET_REGISTRY)
          and list(reg.get("jobs", {})) == list(_SET_REGISTRY["jobs"]), str(list(reg.get("jobs", {}))))
    check("job set leaves the OTHER jobs untouched",
          reg.get("jobs", {}).get("index") == _SET_REGISTRY["jobs"]["index"]
          and reg.get("jobs", {}).get("organize_scan") == _SET_REGISTRY["jobs"]["organize_scan"],
          str(reg.get("jobs")))
    check("job set names the new schedule and the file it wrote",
          "22:00" in r.stdout and "jobs/registry.json" in r.stdout, r.stdout)

    r = run(h, "set", "organize_scan", "--weekly", "Mon 04:15")
    check("job set --weekly takes a 'Day HH:MM'",
          r.returncode == 0 and registry_of(h)["jobs"]["organize_scan"]["schedule"] == {"weekly": "Mon 04:15"},
          f"rc={r.returncode} {r.stdout}{r.stderr}")

    r = run(h, "set", "index", "--every", "30")
    check("job set --every takes a minute interval",
          r.returncode == 0 and registry_of(h)["jobs"]["index"]["schedule"] == {"interval_minutes": 30},
          f"rc={r.returncode} {r.stdout}{r.stderr}")

    r = run(h, "set", "close_nudge", "--monthly", "1 04:00")
    check("job set --monthly takes a 'D HH:MM'",
          r.returncode == 0 and registry_of(h)["jobs"]["close_nudge"]["schedule"] == {"monthly": "1 04:00"},
          f"rc={r.returncode} {r.stdout}{r.stderr}")

    # --dry-run is a READ: it prints the entry it WOULD write and touches nothing.
    before = reg_file.read_bytes()
    r = run(h, "set", "close_nudge", "--daily", "06:00", "--dry-run")
    check("job set --dry-run prints the resulting entry and writes NOTHING",
          r.returncode == 0 and reg_file.read_bytes() == before and "06:00" in r.stdout,
          f"rc={r.returncode} {r.stdout}{r.stderr}")

    # Exactly one schedule flag, always: no flag is a question with no answer, two is an ambiguity.
    before = reg_file.read_bytes()
    r = run(h, "set", "close_nudge")
    check("job set with no schedule flag refuses and names all four forms",
          r.returncode == 2 and reg_file.read_bytes() == before
          and all(f in (r.stdout + r.stderr) for f in ("--daily", "--weekly", "--monthly", "--every")),
          f"rc={r.returncode} {r.stdout}{r.stderr}")
    r = run(h, "set", "close_nudge", "--daily", "08:00", "--every", "30")
    check("job set with TWO schedule flags refuses, naming both (one cadence per job)",
          r.returncode == 2 and "--daily" in (r.stdout + r.stderr) and "--every" in (r.stdout + r.stderr)
          and reg_file.read_bytes() == before, f"rc={r.returncode} {r.stdout}{r.stderr}")
    r = run(h, "set")
    check("job set with no name refuses and lists the job names",
          r.returncode == 2 and "close_nudge" in (r.stdout + r.stderr), f"rc={r.returncode} {r.stdout}{r.stderr}")


def case_set_refuses_and_teaches(td: Path) -> None:
    """REFUSALS TEACH, and a schedule is exactly where that pays: `7am` is what a person types and
    `07:00` is what launchd needs. The refusal has to carry the correction, and it has to leave the
    file alone — a validation that fires AFTER the write is a corruption with a good error message."""
    h = _set_vault(td, "set_refuse")
    reg_file = h / "jobs" / "registry.json"
    before = reg_file.read_bytes()

    r = run(h, "set", "close_nudge", "--daily", "7am")
    out = r.stdout + r.stderr
    check("job set refuses a non-HH:MM time and shows the correction",
          r.returncode == 2 and "07:00" in out and "7am" in out, f"rc={r.returncode} {out}")
    check("a refused job set writes NOTHING — validation runs BEFORE the write",
          reg_file.read_bytes() == before, "")

    for bad, why in (("24:00", "hour out of range"), ("08:60", "minute out of range"), ("8:00", "unpadded hour")):
        r = run(h, "set", "close_nudge", "--daily", bad)
        check(f"job set refuses {bad!r} ({why}), quoting it back",
              r.returncode == 2 and bad in (r.stdout + r.stderr) and reg_file.read_bytes() == before,
              f"rc={r.returncode} {r.stdout}{r.stderr}")

    r = run(h, "set", "organize_scan", "--weekly", "Funday 03:00")
    out = r.stdout + r.stderr
    check("job set refuses an unknown weekday and names the seven it takes",
          r.returncode == 2 and "Sun" in out and "Sat" in out and reg_file.read_bytes() == before,
          f"rc={r.returncode} {out}")

    r = run(h, "set", "index", "--every", "0")
    check("job set refuses a non-positive interval, naming the flag",
          r.returncode == 2 and "--every" in (r.stdout + r.stderr) and reg_file.read_bytes() == before,
          f"rc={r.returncode} {r.stdout}{r.stderr}")

    r = run(h, "set", "nope", "--daily", "08:00")
    out = r.stdout + r.stderr
    check("job set refuses an unknown name, listing the known jobs AND the seedable defaults",
          r.returncode == 2 and "close_nudge" in out and "start" in out
          and reg_file.read_bytes() == before, f"rc={r.returncode} {out}")


def case_set_seeds_a_missing_canonical_job(td: Path) -> None:
    """HOW AN EXISTING VAULT ADOPTS A NEW CANONICAL JOB.

    `jobs/registry.json` is vault content, so the template's copy seeds NEW vaults only — an engine
    update never delivers it. Every vault created before ADR-022 therefore has no `start` entry, and
    no amount of `plainkeep setup` will give it one. `set` on a name the engine knows seeds the whole
    default entry (command, risk, writes) with the schedule the operator just asked for, and SAYS it
    did rather than pretending the entry was there."""
    h = _set_vault(td, "set_seed")
    defaults = default_jobs(h)
    check("the engine ships DEFAULT_JOBS for the canonical six",
          set(defaults) == {"start", "index", "consolidate", "organize_scan", "close_nudge", "backup_check"},
          str(sorted(defaults)))

    r = run(h, "set", "start", "--daily", "08:00")
    entry = registry_of(h).get("jobs", {}).get("start", {})
    check("job set seeds a missing canonical job from the engine defaults",
          r.returncode == 0 and entry == {**defaults.get("start", {}), "schedule": {"daily": "08:00"}},
          f"rc={r.returncode} entry={entry} default={defaults.get('start')}")
    check("the seed says so, rather than pretending the entry was already there",
          "seeded" in r.stdout and "engine defaults" in r.stdout, r.stdout)
    check("the seeded job is appended without disturbing the existing order",
          list(registry_of(h).get("jobs", {})) == [*_SET_REGISTRY["jobs"], "start"],
          str(list(registry_of(h).get("jobs", {}))))
    # And it is a REAL job from that moment: legal, renderable, schedulable.
    r = run(h, "apply")
    check("the seeded job renders like any other",
          r.returncode == 0 and (h / "jobs" / "launchd" / "com.plainkeep.start.plist").exists(),
          r.stdout + r.stderr)


def case_set_never_touches_launchd_but_says_the_schedule_is_stale(td: Path) -> None:
    """`set` IS A VAULT WRITE; ACTIVATION STAYS WITH `enable` (ADR-022).

    The tempting thing is to re-enable after a successful `set` — the operator clearly wants the new
    time to be live. That would make a safe_write action install plists outside the vault and mutate
    a running launchd domain WITHOUT the `--yes` that `enable` exists to demand, which is precisely
    the confirm-class boundary the automation task was commissioned to fix. So `set` calls no
    launchctl at all, and instead states the gap it has just opened and names the one command that
    closes it."""
    h = _set_vault(td, "set_stale")
    agents = FAKE["fx"].agents
    run(h, "enable", "close_nudge", "--yes")
    installed = agents / "com.plainkeep.close_nudge.plist"
    check("fixture: the job is installed and loaded before the edit", installed.exists(), "")

    clear_log()
    r = run(h, "set", "close_nudge", "--daily", "22:00")
    out = r.stdout + r.stderr
    # The invariant is MUTATION, not silence: `set` has to ask launchd whether this job is loaded in
    # order to say the loaded schedule is stale, and `launchctl print` is that question. What it must
    # never do is bootstrap or bootout — those are `enable`/`disable`'s, and they need `--yes`.
    check("job set issues no MUTATING launchctl call (only the read probe)",
          not [c for c in fake_log() if c.split()[:1] in (["bootstrap"], ["bootout"])],
          str(fake_log()))
    check("job set's only launchctl call is the loaded-state probe",
          all(c.startswith("print ") for c in fake_log()), str(fake_log()))
    check("job set does not re-install the plist behind the operator's back",
          "<integer>18</integer>" in text(installed), text(installed)[:400])
    check("job set states plainly that the LOADED schedule is now stale",
          r.returncode == 0 and "stale" in out.lower(), out)
    check("job set names the remedy, and it is the confirm-class verb",
          "plainkeep job enable close_nudge --yes" in out, out)

    r = run(h, "status", "--json")
    rows = [json.loads(ln) for ln in r.stdout.splitlines() if ln.strip()]
    by_name = {row["name"]: row for row in rows[1:]}
    check("the existing drift machinery surfaces the edit with no new plumbing",
          by_name.get("close_nudge", {}).get("drift") is True, str(by_name.get("close_nudge")))

    # A job nobody has enabled has no stale schedule to warn about — a warning that always fires is
    # a warning nobody reads.
    r = run(h, "set", "organize_scan", "--weekly", "Tue 05:00")
    check("job set says nothing about launchd for a job that was never enabled",
          r.returncode == 0 and "stale" not in (r.stdout + r.stderr).lower(), r.stdout)
    run(h, "disable", "--all", "--yes")


def case_set_is_declared_on_the_machine_surface(td: Path) -> None:
    """The `--json` envelope and the cmd.json action entry — `run_json.py`/`run_completion.py` police
    well-formedness, this pins that `set` actually carries its own facts on the machine channel."""
    h = _set_vault(td, "set_json")
    r = run(h, "set", "close_nudge", "--daily", "21:45", "--json")
    env = json.loads(r.stdout.splitlines()[0]) if r.stdout.strip() else {}
    data = env.get("data", {})
    check("job set --json emits a well-formed envelope carrying the new schedule",
          env.get("ok") is True and data.get("name") == "close_nudge"
          and data.get("schedule") == {"daily": "21:45"} and data.get("seeded") is False,
          r.stdout[:300])
    acts = json.loads((REPO / "bin" / "job" / "cmd.json").read_text(encoding="utf-8")).get("actions", [])
    by_name = {a["name"]: a for a in acts}
    check("cmd.json declares the `set` action",
          "set" in by_name and by_name["set"].get("risk") == "safe_write"
          and by_name["set"].get("dry_run") is True, str(sorted(by_name)))
    check("the declared `set` action takes a name and the four schedule flags",
          {a["name"] for a in by_name.get("set", {}).get("args", [])}
          >= {"name", "--daily", "--weekly", "--monthly", "--every"},
          str(by_name.get("set", {}).get("args")))


def case_a_malformed_schedule_is_DIAGNOSED(td: Path) -> None:
    """r2/I5 LEFT THE LOOP HALF-CLOSED: contained, but never diagnosed.

    A malformed schedule was survivable — `_enable`/`apply` caught it per job and reported
    `KeyError: 'schedule'` or `ValueError: not enough values to unpack` as that job's error string.
    That is an exception leaking through a containment, not a product telling an operator what is
    wrong with their file. Schedule parsing is now ONE validated implementation
    (`launchdlib.parse_schedule`), `_validate()` reads it like every other §15 rule, and a malformed
    schedule is therefore flagged by `job list` and refused whole-command by `apply`/`enable` —
    before anything is rendered — with the correction spelled out."""
    h = td / "malformed_schedule"
    (h / "jobs").mkdir(parents=True)
    vaultfx.mark_vault(h)
    (h / "jobs" / "registry.json").write_text(json.dumps({"external_allowlist": [], "jobs": {
        "aaa_ok": {"command": "plainkeep index", "schedule": {"interval_minutes": 60}, "risk": "read"},
        "mmm_noschedule": {"command": "plainkeep index", "risk": "read"},
        "sss_7am": {"command": "plainkeep index", "schedule": {"daily": "7am"}, "risk": "read"},
    }}), encoding="utf-8")
    agents = FAKE["fx"].agents

    r = run(h, "list")
    check("job list flags a malformed schedule as a §15 legality warning",
          r.returncode == 0 and "mmm_noschedule" in r.stdout and "sss_7am" in r.stdout
          and r.stdout.count("⚠") >= 2, r.stdout)
    check("the flag is the DIAGNOSIS, not a raw exception repr",
          "07:00" in r.stdout and "KeyError" not in r.stdout and "Traceback" not in r.stdout, r.stdout)

    clear_log()
    before = set(p.name for p in agents.glob("*.plist"))
    r = run(h, "enable", "--all", "--yes")
    out = r.stdout + r.stderr
    check("job enable refuses a registry with a malformed schedule, naming both offenders",
          r.returncode == 1 and "refusing" in out
          and "mmm_noschedule" in out and "sss_7am" in out, f"rc={r.returncode} {out}")
    check("the refused enable installs nothing and calls no launchctl",
          set(p.name for p in agents.glob("*.plist")) == before and not fake_log(),
          f"calls={fake_log()}")
    r = run(h, "apply")
    check("job apply refuses it at the same bar, rendering nothing",
          r.returncode == 1 and not (h / "jobs" / "launchd").exists(), r.stdout + r.stderr)


def case_a_bad_registry_key_is_refused_at_LOAD(td: Path) -> None:
    """WHERE THE RULE BELONGS (closes followups r2/M6).

    `_NAME_RE` gated the mutating INSTALL path, so `disable` — which stays deliberately permissive,
    because a job whose risk class was tightened is exactly the one you most need to turn off — did
    `unlink` on a path built from an unvalidated registry key. With a hop directory inside the
    LaunchAgents dir, `job disable --all --yes` deleted a file OUTSIDE it. Nothing in plainkeep
    creates that hop directory, so it was an unenforced invariant rather than a live exploit; the fix
    is to enforce it once, at `load_registry()`, which is upstream of EVERY reader."""
    h = td / "badkey"
    (h / "jobs").mkdir(parents=True)
    vaultfx.mark_vault(h)
    agents = FAKE["fx"].agents
    # The hop: `com.plainkeep.<key>.plist` with key `x/../../victim` resolves out of the agents dir.
    (agents / "com.plainkeep.x").mkdir(exist_ok=True)
    victim = agents.parent / "victim.plist"
    victim.write_text("someone else's file\n", encoding="utf-8")
    (h / "jobs" / "registry.json").write_text(json.dumps({"external_allowlist": [], "jobs": {
        "x/../../victim": {"command": "plainkeep index", "schedule": {"daily": "07:30"}, "risk": "read"},
    }}), encoding="utf-8")

    r = run(h, "disable", "--all", "--yes")
    out = r.stdout + r.stderr
    check("job disable refuses a registry whose KEY is not a plain identifier",
          r.returncode != 0 and "not a plain identifier" in out, f"rc={r.returncode} {out}")
    check("the refusal names the offending key",
          "x/../../victim" in out, out)
    check("nothing outside the LaunchAgents dir was unlinked", victim.exists(), str(victim))
    # Every reader, not just the mutating ones: the refusal is at load.
    for action in ("list", "status", "apply"):
        r = run(h, action)
        check(f"job {action} refuses the same registry at load",
              r.returncode != 0 and "not a plain identifier" in (r.stdout + r.stderr),
              f"rc={r.returncode} {r.stdout}{r.stderr}")
    # Tolerant teardown: at the commit this case was written red against, the unlink it is about
    # ACTUALLY HAPPENS, so the victim is gone. Cleaning up defensively keeps the red run reporting
    # failed checks instead of a traceback that would hide every check after it.
    shutil.rmtree(agents / "com.plainkeep.x", ignore_errors=True)
    victim.unlink(missing_ok=True)


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
        case_traversing_key_is_refused_before_anything_runs(Path(td))
        case_enable_contains_a_per_job_failure(Path(td))

        # r2 fix: I5 — containment must cover more than OSError.
        case_enable_contains_a_malformed_entry(Path(td))
        case_apply_contains_a_malformed_entry(Path(td))

        # `job set`: schedule times as a product surface, and the two loops the shared schedule
        # parser closes behind it (I5's diagnosis, M6's registry-key rule).
        case_set_edits_only_the_schedule(Path(td))
        case_set_refuses_and_teaches(Path(td))
        case_set_seeds_a_missing_canonical_job(Path(td))
        case_set_never_touches_launchd_but_says_the_schedule_is_stale(Path(td))
        case_set_is_declared_on_the_machine_surface(Path(td))
        case_a_malformed_schedule_is_DIAGNOSED(Path(td))
        case_a_bad_registry_key_is_refused_at_LOAD(Path(td))

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
