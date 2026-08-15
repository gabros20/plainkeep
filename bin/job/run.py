#!/usr/bin/env python3
"""
plainkeep job list | run <name> | apply | set <name> | enable | disable | status — the §15
scheduler-neutral jobs surface. Definitions live in jobs/registry.json; `run` executes any job
manually (the universal fallback); `apply` renders launchd plists into jobs/launchd/; `set` changes
a job's schedule (see `_set`). Jobs call ONE verb, log to .logs/jobs/, and only read/safe_write may
be scheduled.

ACTIVATION IS A VERB NOW (ADR-022). `apply` used to end by PRINTING a `ln -sf` and a `launchctl
load` for the operator to paste, on the principle that the privileged, out-of-vault step was theirs.
The result was that automation — which this system offers as the default way to use it — was reliably
rendered and unreliably running, and nothing could tell the difference: a plist in `jobs/launchd/`
proves a file was written, not that launchd ever read it. `enable`/`disable` do that step, and
`status` reports the three facts separately (rendered / installed / loaded) so the gap between them
is visible instead of assumed.

The step keeps its weight rather than losing it: `enable` and `disable` are CONFIRM-class subactions
(`--yes`, or exit 3), because they write outside the vault (`~/Library/LaunchAgents`) and mutate a
live launchd domain. `--dry-run` previews both without needing `--yes` — it is a read.

A COPY, not a symlink. `apply`'s old hint linked the vault's rendered file into LaunchAgents; recent
macOS is unreliable about symlinked plists (bootstrap intermittently refuses them), and a symlink
also means editing a vault file silently changes what a privileged loader reads. The vault artefact
stays the record; the LaunchAgents file is an installed copy of it.
"""
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lib import enginetree, launchdlib, output, paths, vaultio  # noqa: E402

GREEN, RED, YEL, DIM, RESET = "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[0m"
BIN = Path(__file__).resolve().parents[1]
REGISTRY = paths.PLAINKEEP_HOME / "jobs" / "registry.json"
SCHEDULABLE = launchdlib.SCHEDULABLE


def _known_verbs():
    return {p.parent.name for p in BIN.glob("*/run.py")}


def _load():
    """The registry, or a refusal that says what is wrong with it.

    Through `launchdlib.read_registry()` rather than `json.loads` here, because the registry-KEY rule
    now lives at the read (r2/M6): a key becomes a plist filename in `~/Library/LaunchAgents`, and
    `disable` — which stays deliberately permissive about risk class — used to `unlink` a path built
    from an unvalidated one. Every action in this verb goes through this function, so the rule cannot
    be routed around by adding a surface."""
    if not REGISTRY.exists():
        print(f"no jobs registry at {REGISTRY.relative_to(paths.PLAINKEEP_HOME)}", file=sys.stderr)
        sys.exit(1)
    try:
        return launchdlib.read_registry()
    except launchdlib.RegistryError as exc:
        output.fail(output.EXIT_UNEXPECTED, f"{RED}refusing to read jobs/registry.json — {exc}{RESET}",
                    hint="fix the offending entry in jobs/registry.json; a job name becomes a plist "
                         "filename outside the vault, so it is validated before anything reads it",
                    verb="job")


def _sched_str(s: dict) -> str:
    return launchdlib.schedule_str(s)


def _validate(name, job, external):
    """§15 legality of one job — returns list of warning strings.

    The name rule and the schedule shape are BOTH `launchdlib`'s (one legality model, all readers).
    The name is checked here as a backstop only: `_load()` refuses a registry with an illegal key
    before this function is ever reached (r2/M6), so a warning here means someone passed a job dict
    from somewhere other than the file.

    The SCHEDULE check is the new one, and it closes the half of r2/I5 that containment left open. A
    malformed schedule was survivable — `apply`/`enable` caught the render failure per job — but it
    was only ever reported as an exception string, and never at all until something tried to render
    it. Reading `parse_schedule` here makes it a §15 warning like any other, which means `job list`
    flags it and `_legal_or_refuse` blocks `apply`/`enable` whole-command, before anything is
    written."""
    warns = []
    if not launchdlib.name_ok(name):
        warns.append(f"job name {name!r} {launchdlib.NAME_RULE}")
    try:
        launchdlib.parse_schedule(job.get("schedule"))
    except launchdlib.ScheduleError as exc:
        warns.append(str(exc))
    toks = job["command"].split()
    if job.get("risk") not in SCHEDULABLE:
        warns.append(f"risk {job.get('risk')!r} is not schedulable (must be read/safe_write)")
    if any(c in job["command"] for c in ("|", "&&", ";", "$(", "`")):
        warns.append("inline logic — a job must call ONE verb")
    if toks and toks[0] == "plainkeep":
        if (len(toks) < 2) or (toks[1] not in _known_verbs()):
            warns.append(f"verb {toks[1] if len(toks) > 1 else ''!r} is not a built verb")
    elif toks and toks[0] not in external:
        warns.append(f"external command {toks[0]!r} is not in external_allowlist")
    return warns


def _plist(name, job) -> str:
    """The rendered launchd plist. The renderer itself lives in `lib/launchdlib.py` because three
    surfaces now need the SAME bytes — this verb, the `automation` setup layer, and doctor's drift
    check — and a second copy would disagree with the first the moment the template changed."""
    return launchdlib.plist(name, job)


USAGE = ("usage: plainkeep job list | run <name> | apply | set <name> <schedule> | status | "
         "enable [<name>…|--all] --yes | disable [<name>…|--all] --yes")

# `job set`'s four schedule flags → the registry key each writes. One flag per invocation: a job has
# exactly one cadence, so accepting two would mean silently picking one of them.
_SET_FLAGS = {"--daily": "daily", "--weekly": "weekly", "--monthly": "monthly",
              "--every": "interval_minutes"}
_SET_USAGE = ('usage: plainkeep job set <name> (--daily HH:MM | --weekly "Day HH:MM" | '
              '--monthly "D HH:MM" | --every <minutes>) [--dry-run]')


def _schedule_flags(argv):
    """Pull `job set`'s ONE schedule flag out of argv → (schedule dict | None, remaining argv).

    Done before the generic flag strip in `main` because these flags take a VALUE, and a value left
    behind ("08:00") would be read as a second job name. Refuses here rather than returning something
    ambiguous: zero flags and two flags are different mistakes and get different sentences."""
    schedule, seen, rest, i = None, [], [], 0
    while i < len(argv):
        tok = argv[i]
        # BOTH SPELLINGS. `--daily=08:00` contains no space, so falling through to the positional
        # branch made it a "second job name" and borrowed the unquoted-`Sun 03:00` message for it —
        # telling the operator something untrue about their own input. `--flag=value` is the house
        # form for a value flag (`plugin sync --find-links=<dir>` is the only other one in the repo,
        # and it accepts ONLY that spelling), so both are taken here.
        flag, eq, inline = tok.partition("=")
        if flag not in _SET_FLAGS:
            rest.append(tok); i += 1
            continue
        if eq:
            value, step = inline, 1
            if not value:
                output.fail(output.EXIT_USAGE, f"{flag}= needs a value.   {_SET_USAGE}", verb="job")
        elif i + 1 >= len(argv):
            output.fail(output.EXIT_USAGE, f"{tok} needs a value.   {_SET_USAGE}", verb="job")
        else:
            value, step = argv[i + 1], 2
        seen.append(flag)
        key = _SET_FLAGS[flag]
        # `--every` is the only one whose value is a number rather than a time string. A
        # non-numeric value is handed to `parse_schedule` as-is so the refusal comes from the one
        # place that owns what a schedule may be.
        schedule = {key: int(value) if (key == "interval_minutes" and value.lstrip("-").isdigit())
                    else value}
        i += step
    if len(seen) > 1:
        output.fail(output.EXIT_USAGE,
                    f"a job has exactly ONE cadence — you gave {', '.join(seen)}.   {_SET_USAGE}",
                    verb="job")
    return schedule, rest


def _set(names, schedule, jobs, dry) -> int:
    """`plainkeep job set <name>` — the schedule times as a product surface.

    The times used to be two literals in `jobs/registry.json` that only a hand-edit could change. For
    a human whose day runs 08:00-22:00 that meant the system opened their day an hour before they
    existed, and the remedy was to open a JSON file and know which key to touch. The registry stays
    the single source of truth — no config file, no env var, no template indirection — and this is
    the surface that edits it.

    IT DOES NOT ACTIVATE ANYTHING. Re-enabling here is the tempting thing and would quietly make a
    safe_write action write outside the vault and mutate a live launchd domain without the `--yes`
    that `enable` exists to demand. So a `set` on a job launchd is already running says so, and names
    the one command that closes the gap. `job status`'s drift column and doctor's advisory row report
    the same fact from the other side, with no new plumbing."""
    if not names:
        output.fail(output.EXIT_USAGE, f"{_SET_USAGE}   (one of: {', '.join(jobs)})", verb="job")
    if len(names) > 1:
        # Almost always an unquoted `--weekly Sun 03:00`: the shell split it, the flag took `Sun`,
        # and `03:00` arrived here looking like a second job name. Say that, rather than silently
        # acting on the first name with half a schedule.
        output.fail(output.EXIT_USAGE,
                    f"plainkeep job set takes ONE job name — got: {', '.join(names)}",
                    hint='a schedule containing a space must be quoted: '
                         '--weekly "Sun 03:00"   --monthly "1 04:00"',
                    verb="job")
    name = names[0]
    if schedule is None:
        output.fail(output.EXIT_USAGE, f"plainkeep job set needs a schedule.   {_SET_USAGE}",
                    verb="job")
    if name not in jobs and name not in launchdlib.DEFAULT_JOBS:
        seedable = launchdlib.seedable_defaults(jobs)
        output.fail(output.EXIT_USAGE,
                    f"no job '{name}' in jobs/registry.json   (one of: {', '.join(jobs)})",
                    hint=(f"or seed one from the engine defaults: {', '.join(seedable)}"
                          if seedable else "every engine default is already in this registry"),
                    verb="job")
    try:
        res = launchdlib.set_schedule(name, schedule, dry_run=dry)
    except launchdlib.RegistryError as exc:
        # Exit 2, not 1: the value came from the command line, so this is a usage error the operator
        # can fix by retyping — and the message carries the correction rather than the fault.
        output.fail(output.EXIT_USAGE, f"{RED}{exc}{RESET}", hint=_SET_USAGE, verb="job")
    except OSError as exc:
        # A MUTATING PATH NEVER HANDS THE OPERATOR A TRACEBACK (r1/I1, the post-r2/I5 house rule).
        # A full disk, a read-only mount or a quota escaped from the write itself as a stack trace —
        # while `_wizard_times`, the same writer one file over, already contained it. The verb must
        # not be less careful than the wizard.
        #
        # The write is not atomic (a registered follow-up, not this fix), so the honest thing to say
        # is that the file may be half-written, and to name the way back. Everything here is in git,
        # and a `job set` that can no longer re-read its own output is exactly the moment an operator
        # needs telling that — otherwise the surface that exists to stop them hand-editing JSON has
        # left them hand-editing JSON.
        output.fail(output.EXIT_UNEXPECTED,
                    f"{RED}could not write jobs/registry.json: {exc}{RESET}",
                    hint="the write is not atomic, so jobs/registry.json may be incomplete — restore "
                         "it with `git -C \"$PLAINKEEP_HOME\" checkout -- jobs/registry.json` "
                         "(everything in the vault is in git), then retry",
                    verb="job")

    # WHAT THE PROBE ANSWERED IS THE CLAIM (r1/M2). Probed for the ONE job, not the whole registry,
    # so `set` costs at most one `launchctl print` — and under `--dry-run` too, because "this edit
    # would leave a stale schedule loaded" is exactly what a preview is for. Reading launchd's state
    # is not mutating it; `bootstrap`/`bootout` stay with `enable`/`disable`, which is what makes
    # this action safe_write rather than confirm.
    #
    # `installed` and `loaded` are two different facts and were collapsed into one sentence: on a
    # vault whose plist is installed but which launchd has booted out, `set` said "launchd is still
    # running the OLD schedule" one command after `job status` said `installed, not loaded` about
    # the same job. The probe was issued, answered correctly, and its answer discarded. Two surfaces
    # contradicting each other about one fact is worse than either staying quiet — and the three
    # columns exist precisely because these are three different states.
    row = launchdlib.job_states({"jobs": {name: res["job"]}})[0]
    installed, loaded = bool(row.get("installed")), bool(row.get("loaded"))
    stale = installed or loaded
    remedy = f"plainkeep job enable {name} --yes" if stale else ""
    data = {"action": "set", "name": name, "schedule": res["schedule"], "previous": res["previous"],
            "seeded": res["seeded"], "job": res["job"], "stale": stale, "remedy": remedy,
            "installed": installed, "loaded": loaded, "registry": "jobs/registry.json"}
    if dry:
        data["dry_run"] = True

    def render(_):
        was = f"  {DIM}(was {_sched_str(res['previous'])}){RESET}" if res["previous"] else ""
        if dry:
            print(f"would set '{name}' to {_sched_str(res['schedule'])}{was}"
                  "  (dry run — nothing written)")
        elif res["seeded"]:
            print(f"{GREEN}seeded '{name}' from engine defaults{RESET} "
                  f"-> {_sched_str(res['schedule'])}  [{res['job'].get('risk')}] "
                  f"{res['job'].get('command')}")
        else:
            print(f"set '{name}' -> {_sched_str(res['schedule'])}{was}")
        print(f"  {'would write' if dry else 'wrote'} jobs/registry.json")
        if loaded:
            # Named plainly rather than hinted at: the schedule launchd is running and the schedule
            # the file now says are two different things until someone re-enables.
            print(f"\n{YEL}⚠ launchd is {'still ' if not dry else ''}running the "
                  f"{'OLD' if not dry else 'CURRENT'} schedule for '{name}' — "
                  f"{'the loaded schedule is stale' if not dry else 'this edit would leave it stale'}"
                  f"{RESET}")
            print(f"  make it live:  {remedy}")
        elif installed:
            # Installed but NOT loaded — what `job status` calls `installed, not loaded`. The file in
            # ~/Library/LaunchAgents is out of date; nothing is running it. Same remedy (`enable`
            # re-renders and re-bootstraps), different fact.
            print(f"\n{YEL}⚠ the installed launch agent for '{name}' is now out of date "
                  f"(launchd has not loaded it){RESET}")
            print(f"  install the new schedule and load it:  {remedy}")
        else:
            print(f"\n  schedule it:  plainkeep job enable {name} --yes"
                  "     see what's live:  plainkeep job status")

    return output.emit(data, "job", human=render)

# What the operator is agreeing to, at the moment they are asked (exit 3). Both lines name the two
# things that make this confirm-class rather than safe_write: a write OUTSIDE the vault, and a change
# to a running system daemon's state.
_CONFIRM = {
    "enable": "enable installs launch agents into ~/Library/LaunchAgents and loads them into launchd "
              "(a write outside the vault, and a change to a running launchd domain)",
    "disable": "disable unloads the jobs from launchd and removes their ~/Library/LaunchAgents copies "
               "(the rendered plists under jobs/launchd/ are kept)",
}


def _targets(action, names, jobs, all_):
    """Which jobs an enable/disable acts on, and which were skipped. Refuses before anything runs.

    `--all` SKIPS a non-schedulable job (the same thing `apply` does — the registry may legally hold
    one, it just may never be scheduled), but an explicitly NAMED one is REFUSED: someone who typed
    the name is owed the reason rather than a silent no-op."""
    if all_ and names:
        output.fail(output.EXIT_USAGE,
                    f"plainkeep job {action} takes either job names or --all, not both", verb="job")
    if not all_ and not names:
        output.fail(output.EXIT_USAGE,
                    f"usage: plainkeep job {action} [<name>…|--all] [--yes] [--dry-run]   "
                    f"(one of: {', '.join(jobs)})", verb="job")
    if all_:
        return ([n for n, j in jobs.items() if j.get("risk") in SCHEDULABLE],
                [n for n, j in jobs.items() if j.get("risk") not in SCHEDULABLE])
    unknown = [n for n in names if n not in jobs]
    if unknown:
        output.fail(output.EXIT_USAGE,
                    f"unknown job(s): {', '.join(unknown)}   (one of: {', '.join(jobs)})", verb="job")
    # `disable` stays permissive: it only ever REMOVES, and a job whose risk class was tightened
    # after it was enabled is exactly the one an operator most needs to be able to turn off.
    if action == "enable":
        bad = [n for n in names if jobs[n].get("risk") not in SCHEDULABLE]
        if bad:
            output.fail(output.EXIT_UNEXPECTED,
                        f"{RED}refusing to enable {', '.join(bad)}: "
                        f"risk is not schedulable (must be read/safe_write){RESET}",
                        hint="only read/safe_write jobs may run unattended (§15)", verb="job")
    return list(names), []


def _legal_or_refuse(action, targets, jobs, external):
    """WHAT `job run` REFUSES, NOTHING MAY SCHEDULE (r1/B2).

    `_validate()` is the §15 legality model, and `list`/`run` were its only readers: `apply` and
    `enable` looked at the RISK CLASS alone — a field the same registry file declares about itself.
    So a job the product refuses to run once by hand (`/bin/sh -c …` off the allowlist, inline shell
    logic, a verb that does not exist) was rendered, installed and bootstrapped to run unattended
    forever, exit 0, with a green tick beside it. The ratchet pointed the wrong way.

    WHOLE-COMMAND, not per-job skip. `--all` legitimately SKIPS a non-schedulable job — the registry
    may hold one, it simply may never be scheduled, and that is a property of that entry. An ILLEGAL
    entry is different in kind: it means the registry is malformed, and quietly scheduling the other
    five would leave the operator believing a schedule they cannot see is broken. The remedy is local
    and the refusal names it."""
    offenders = [(n, _validate(n, jobs[n], external)) for n in targets]
    offenders = [(n, w) for n, w in offenders if w]
    if not offenders:
        return
    lines = "; ".join(f"{n}: {'; '.join(w)}" for n, w in offenders)
    output.fail(output.EXIT_UNEXPECTED,
                f"{RED}refusing to {action} {len(offenders)} illegal job(s) — {lines}{RESET}",
                hint="fix jobs/registry.json (see `plainkeep job list`); a job the product refuses "
                     "to run by hand must never be scheduled to run unattended",
                verb="job")


def _enable(targets, jobs, agents):
    """Render fresh → install a COPY → bootout → bootstrap, per job.

    RENDERED FRESH, ALWAYS. Whatever sits in `jobs/launchd/` may predate the registry (that is what
    `status`'s `drift` column reports), and installing a stale file would load a schedule nobody
    wrote. The registry is the source; the rendered file is a projection of it.

    BOOTOUT BEFORE BOOTSTRAP, and the bootout's failure is IGNORED. `bootstrap` refuses a label that
    is already loaded, so re-enabling after an edit needs the unload first; `bootout` on a label that
    was never loaded exits nonzero, which here means "already in the state we wanted".

    ONE JOB'S FAILURE IS NOT THE LOOP'S (r1/I4). This loop MUTATES as it goes — by the third job the
    first two are already bootstrapped — so an exception escaping it left the operator with a nonzero
    exit and no statement of what had been enabled. A per-job failure is now recorded the same way a
    failed `bootstrap` already was, and the loop finishes; `main` reports the partial state and exits
    nonzero. The containment is `except Exception`, not `except OSError` (r2/I5): `plist()` raises
    KeyError/ValueError for a malformed registry entry, and the read path (`job_states`) already
    treats "can't re-render" as data — the mutating path must not be less careful."""
    vaultio.mkdir(launchdlib.render_dir())
    agents.mkdir(parents=True, exist_ok=True)
    out = []
    for name in targets:
        dst = launchdlib.installed_path(name)
        try:
            src = launchdlib.rendered_path(name)
            vaultio.write_text(src, launchdlib.plist(name, jobs[name]), encoding="utf-8")
            shutil.copyfile(src, dst)
        except Exception as exc:  # r2/I5: a malformed entry too, not just a filesystem refusal
            out.append({"name": name, "ok": False, "plist": str(dst),
                        "error": f"{type(exc).__name__}: {exc}"[:200]})
            continue
        launchdlib.launchctl("bootout", launchdlib.service_target(name))
        r = launchdlib.launchctl("bootstrap", launchdlib.domain(), str(dst))
        out.append({"name": name, "ok": r.returncode == 0, "plist": str(dst),
                    "error": (r.stderr or r.stdout).strip()[:200] if r.returncode else ""})
    return out


def _disable(targets, agents):
    """Bootout → remove the LaunchAgents copy. Idempotent by construction: a bootout of something not
    loaded and an unlink of something not there are both fine, so `disable` twice is `disable` once.
    The rendered plists under `jobs/launchd/` are NOT removed — they are the vault's record of the
    schedule, owned by `apply`, and deleting them would make `enable` unable to say what changed.

    Same per-job containment as `_enable` (r1/I4), and it matters more here: a `disable` that aborts
    partway leaves jobs loaded that the operator has just asked to turn off, which is the direction
    you least want to fail silently in."""
    out = []
    for name in targets:
        launchdlib.launchctl("bootout", launchdlib.service_target(name))
        dst = agents / f"{launchdlib.label(name)}.plist"
        try:
            existed = dst.exists()
            dst.unlink(missing_ok=True)
        except Exception as exc:  # r2/I5: same containment breadth as _enable
            out.append({"name": name, "ok": False, "plist": str(dst), "removed": False,
                        "error": str(exc)[:200]})
            continue
        out.append({"name": name, "ok": True, "plist": str(dst), "removed": existed, "error": ""})
    return out


def main(argv):
    _, argv = output.parse_argv(argv)
    # BEFORE the generic flag strip: `job set`'s schedule flags carry a value, and stripping the flag
    # alone would leave "08:00" behind as a positional.
    schedule, argv = _schedule_flags(argv)
    dry = "--dry-run" in argv
    yes = ("--yes" in argv) or ("-y" in argv)
    all_ = "--all" in argv
    argv = [a for a in argv if a not in ("--dry-run", "--yes", "-y", "--all")]
    action = argv[0] if argv else "list"
    if schedule is not None and action != "set":
        output.fail(output.EXIT_USAGE,
                    f"a schedule flag belongs to `plainkeep job set`, not `job {action}`.   "
                    f"{_SET_USAGE}", verb="job")
    reg = _load()
    jobs, external = reg["jobs"], set(reg.get("external_allowlist", []))

    if action == "list":
        rows = []
        for name, job in jobs.items():
            warns = _validate(name, job, external)
            rows.append({"name": name, "schedule": _sched_str(job.get("schedule")),
                         "command": job["command"], "risk": job.get("risk"), "warns": warns})

        def render(rs):
            out = [f"{len(rs)} job(s) in {REGISTRY.relative_to(paths.PLAINKEEP_HOME)}:\n"]
            for r in rs:
                flag = f" {RED}⚠ {'; '.join(r['warns'])}{RESET}" if r["warns"] else f" {GREEN}✓{RESET}"
                out.append(f"  {r['name']:<14} {DIM}{r['schedule']:<16}{RESET} {r['command']:<26} "
                           f"[{r['risk']}]{flag}")
            out.append("\n  run one now:  plainkeep job run <name>"
                       "     schedule them:  plainkeep job enable --all --yes"
                       "     what's live:  plainkeep job status")
            return "\n".join(out)
        return output.emit_rows(rows, "job", human=render)

    elif action == "run":
        if len(argv) < 2 or argv[1] not in jobs:
            output.fail(output.EXIT_USAGE,
                        f"usage: plainkeep job run <name>   (one of: {', '.join(jobs)})", verb="job")
        name = argv[1]; job = jobs[name]
        warns = _validate(name, job, external)
        if warns:
            output.fail(output.EXIT_UNEXPECTED,
                        f"{RED}refusing to run '{name}': {'; '.join(warns)}{RESET}", verb="job")
        if dry:
            return output.emit({"dry_run": True, "name": name, "command": job["command"]}, "job",
                               human=lambda _: f"would run '{name}': {job['command']}  (dry run — nothing executed)")
        toks = job["command"].split()
        if toks[0] == "plainkeep":
            # "One door" (Task 9): a manual `plainkeep job run` re-enters the DISPATCHER, not the verb's
            # run.py directly, so the scheduled verb passes the same guardrail + resolver + logs as
            # any other caller (and the rendered launchd plists already invoke the dispatcher). The
            # jobs are pre-validated read/safe_write, so the gate is a logged pass-through, never a
            # block. Non-recursive: a job calls ONE non-job verb.
            # ...through the ENGINE's launcher (Phase 2 Task 2), for the reason `_plist` states: the
            # vault has no launcher of its own any more.
            cmd = [str(enginetree.launcher()), *toks[1:]]
        else:
            cmd = toks  # allowlisted external
        logdir = paths.PLAINKEEP_HOME / ".logs" / "jobs"; vaultio.mkdir(logdir)
        print(f"running '{name}': {job['command']}")
        r = subprocess.run(cmd, capture_output=True, text=True,
                           env={**__import__("os").environ, "PLAINKEEP_HOME": str(paths.PLAINKEEP_HOME)})
        vaultio.write_text((logdir / f"{name}.log"), r.stdout + r.stderr, encoding="utf-8")
        sys.stdout.write(r.stdout)
        if r.stderr:
            sys.stderr.write(r.stderr)
        paths.append_journal(f"job run {name} (rc={r.returncode})")
        return r.returncode

    elif action == "set":
        return _set(argv[1:], schedule, jobs, dry)

    elif action == "apply":
        skipped = [n for n, j in jobs.items() if j.get("risk") not in SCHEDULABLE]
        schedulable = [n for n, j in jobs.items() if j.get("risk") in SCHEDULABLE]
        # `apply` renders what `enable` installs, so it answers the legality question at the same
        # bar (r1/B2) — otherwise the vault-side artefact could disagree with `job list`'s own flags.
        _legal_or_refuse(action, schedulable, jobs, external)
        out = paths.PLAINKEEP_HOME / "jobs" / "launchd"
        if dry:
            data = {"dry_run": True, "would_render": [f"com.plainkeep.{n}.plist" for n in schedulable],
                    "skipped": skipped}

            def render_dry(_):
                print(f"would render {len(schedulable)} plist(s) -> {out.relative_to(paths.PLAINKEEP_HOME)}/  (dry run — nothing written)")
                for n in schedulable:
                    print(f"  com.plainkeep.{n}.plist")
                if skipped:
                    print(f"{YEL}skipped (not schedulable): {', '.join(skipped)}{RESET}")
            return output.emit(data, "job", human=render_dry)

        vaultio.mkdir(out)
        written, failed = [], []
        for name, job in jobs.items():
            if job.get("risk") not in SCHEDULABLE:
                continue
            f = out / f"com.plainkeep.{name}.plist"
            try:
                vaultio.write_text(f, _plist(name, job), encoding="utf-8")
            except Exception as exc:  # r2/I5: a malformed entry is that job's failure, not the loop's
                failed.append({"name": name, "error": f"{type(exc).__name__}: {exc}"[:200]})
                continue
            written.append(f)
        data = {"rendered": [f.name for f in written], "failed": failed, "skipped": skipped,
                "dir": str(out.relative_to(paths.PLAINKEEP_HOME))}

        def render(_):
            print(f"rendered {len(written)} plist(s) -> {out.relative_to(paths.PLAINKEEP_HOME)}/")
            for f in written:
                print(f"  {f.name}")
            for d in failed:
                print(f"  {RED}✗ com.plainkeep.{d['name']}.plist — {d['error']}{RESET}")
            if skipped:
                print(f"{YEL}skipped (not schedulable): {', '.join(skipped)}{RESET}")
            # `apply` renders; ACTIVATING what it rendered is its own confirm-class step, and it is a
            # command here rather than a shell recipe to paste (ADR-022).
            print("\nactivate the schedule:  plainkeep job enable --all --yes")
            print("check what launchd has:  plainkeep job status")

        output.emit(data, "job", human=render)
        return output.EXIT_UNEXPECTED if failed else output.EXIT_OK

    elif action in ("enable", "disable"):
        targets, skipped = _targets(action, argv[1:], jobs, all_)
        if action == "enable":
            # Before anything is rendered, installed or loaded — including under --dry-run, so the
            # preview cannot promise what the real run would refuse.
            _legal_or_refuse(action, targets, jobs, external)
        agents = launchdlib.launch_agents_dir()
        if dry:
            # A --dry-run is a READ (the guardrail downgrades it), so it never needs --yes and never
            # calls launchctl: it prints the exact files and service targets it WOULD touch.
            data = {"dry_run": True, "action": action, "targets": targets, "skipped": skipped,
                    "launch_agents_dir": str(agents)}

            def render_dry(_):
                print(f"would {action} {len(targets)} job(s)  (dry run — nothing written, launchctl not called)")
                for n in targets:
                    print(f"  {launchdlib.label(n)}.plist -> {agents}/  [{launchdlib.service_target(n)}]")
                if skipped:
                    print(f"{YEL}skipped (not schedulable): {', '.join(skipped)}{RESET}")
            return output.emit(data, "job", human=render_dry)

        if not yes:
            output.fail(output.EXIT_CONFIRM, _CONFIRM[action],
                        hint=f"re-run: plainkeep job {action} "
                             f"{'--all' if all_ else ' '.join(targets)} --yes"
                             f"   (preview first: add --dry-run instead of --yes)", verb="job")
        if not launchdlib.launchctl_available():
            output.fail(output.EXIT_UNEXPECTED,
                        "launchd is not available on this host — scheduling is macOS-only",
                        hint="run any job by hand instead: plainkeep job run <name>", verb="job")
        done = _enable(targets, jobs, agents) if action == "enable" else _disable(targets, agents)
        failed = [d for d in done if not d["ok"]]
        data = {"action": action, "results": done, "skipped": skipped,
                "launch_agents_dir": str(agents)}

        def render(_):
            where = "installed into" if action == "enable" else "removed from"
            print(f"{action}d {len(done) - len(failed)}/{len(done)} job(s) — {where} {agents}/")
            for d in done:
                mark = f"{GREEN}✓{RESET}" if d["ok"] else f"{RED}✗{RESET}"
                print(f"  {mark} {launchdlib.label(d['name'])}"
                      + (f"  {RED}{d['error']}{RESET}" if not d["ok"] else ""))
            if skipped:
                print(f"{YEL}skipped (not schedulable): {', '.join(skipped)}{RESET}")
            print("\ncheck what launchd has:  plainkeep job status")

        output.emit(data, "job", human=render)
        return output.EXIT_UNEXPECTED if failed else output.EXIT_OK

    elif action == "status":
        rows = launchdlib.job_states(reg)

        def render(rs):
            out = [f"{len(rs)} job(s) — rendered (vault) / installed (LaunchAgents) / loaded (launchd):\n"]
            for r in rs:
                if not r["schedulable"]:
                    state = f"{DIM}not schedulable{RESET}"
                elif r["loaded"]:
                    state = f"{GREEN}loaded{RESET}"
                elif r["installed"]:
                    state = f"{YEL}installed, not loaded{RESET}"
                elif r["rendered"]:
                    state = f"{YEL}rendered only{RESET}"
                else:
                    state = f"{DIM}not rendered{RESET}"
                drift = f"  {RED}⚠ drift — re-render with `plainkeep job apply`{RESET}" if r["drift"] else ""
                out.append(f"  {r['name']:<14} {'yes' if r['rendered'] else ' - ':<4} "
                           f"{'yes' if r['installed'] else ' - ':<4} {'yes' if r['loaded'] else ' - ':<4} "
                           f"{state}{drift}")
            out.append("\n  activate:  plainkeep job enable --all --yes"
                       "     turn off:  plainkeep job disable --all --yes")
            return "\n".join(out)
        return output.emit_rows(rows, "job", human=render)

    else:
        output.fail(output.EXIT_USAGE, USAGE, verb="job")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
