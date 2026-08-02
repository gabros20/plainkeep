#!/usr/bin/env python3
"""
plainkeep job list | run <name> | apply — the §15 scheduler-neutral jobs surface. Definitions live in
jobs/registry.json; `run` executes any job manually (the universal fallback); `apply` renders
launchd plists into jobs/launchd/ and tells you how to load them (the privileged step stays yours).
Jobs call ONE verb, log to .logs/jobs/, and only read/safe_write may be scheduled.
"""
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lib import enginetree, output, paths, vaultio  # noqa: E402

GREEN, RED, YEL, DIM, RESET = "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[0m"
BIN = Path(__file__).resolve().parents[1]
REGISTRY = paths.PLAINKEEP_HOME / "jobs" / "registry.json"
SCHEDULABLE = {"read", "safe_write"}


def _known_verbs():
    return {p.parent.name for p in BIN.glob("*/run.py")}


def _load():
    if not REGISTRY.exists():
        print(f"no jobs registry at {REGISTRY.relative_to(paths.PLAINKEEP_HOME)}", file=sys.stderr)
        sys.exit(1)
    return json.loads(REGISTRY.read_text(encoding="utf-8"))


def _sched_str(s: dict) -> str:
    if "interval_minutes" in s:
        return f"every {s['interval_minutes']}m"
    for k in ("daily", "weekly", "monthly"):
        if k in s:
            return f"{k} {s[k]}"
    return "?"


def _validate(name, job, external):
    """§15 legality of one job — returns list of warning strings."""
    warns = []
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
    # THE LAUNCHER IS ENGINE-OWNED (Phase 2 Task 2). This built
    # `$PLAINKEEP_HOME/plainkeep` — the vault-local shim — and ADR-014 names the line as one that
    # must change: after the engine moves out, that path is ENOENT at 2am, in a sanitized launchd
    # environment where nothing will be there to explain it. The plist keeps naming an ABSOLUTE
    # launcher (a scheduled job must never depend on discovery or on PATH) and both roots are baked
    # in absolutely: the engine's launcher as the program, the validated vault as PLAINKEEP_HOME.
    toks = job["command"].split()
    args = [str(enginetree.launcher()), *toks[1:]] if toks and toks[0] == "plainkeep" else toks
    pa = "".join(f"\n      <string>{a}</string>" for a in args)
    s = job["schedule"]
    if "interval_minutes" in s:
        when = f"  <key>StartInterval</key>\n  <integer>{int(s['interval_minutes']) * 60}</integer>"
    else:
        cal = {}
        if "daily" in s:
            hh, mm = s["daily"].split(":"); cal = {"Hour": int(hh), "Minute": int(mm)}
        elif "weekly" in s:
            day, hhmm = s["weekly"].split(); hh, mm = hhmm.split(":")
            wd = {"Sun": 0, "Mon": 1, "Tue": 2, "Wed": 3, "Thu": 4, "Fri": 5, "Sat": 6}
            cal = {"Weekday": wd.get(day, 0), "Hour": int(hh), "Minute": int(mm)}
        elif "monthly" in s:
            dom, hhmm = s["monthly"].split(); hh, mm = hhmm.split(":")
            cal = {"Day": int(dom), "Hour": int(hh), "Minute": int(mm)}
        inner = "".join(f"\n    <key>{k}</key><integer>{v}</integer>" for k, v in cal.items())
        when = f"  <key>StartCalendarInterval</key>\n  <dict>{inner}\n  </dict>"
    log = paths.PLAINKEEP_HOME / ".logs" / "jobs" / f"{name}.log"
    return (f'<?xml version="1.0" encoding="UTF-8"?>\n'
            f'<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
            f'"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
            f'<plist version="1.0">\n<dict>\n'
            f'  <key>Label</key>\n  <string>com.plainkeep.{name}</string>\n'
            f'  <key>ProgramArguments</key>\n  <array>{pa}\n  </array>\n'
            f'  <key>EnvironmentVariables</key>\n  <dict>\n'
            f'    <key>PLAINKEEP_HOME</key><string>{paths.PLAINKEEP_HOME}</string>\n  </dict>\n'
            f'{when}\n'
            f'  <key>StandardOutPath</key>\n  <string>{log}</string>\n'
            f'  <key>StandardErrorPath</key>\n  <string>{log}</string>\n'
            f'</dict>\n</plist>\n')


def main(argv):
    _, argv = output.parse_argv(argv)
    dry = "--dry-run" in argv
    argv = [a for a in argv if a != "--dry-run"]
    action = argv[0] if argv else "list"
    reg = _load()
    jobs, external = reg["jobs"], set(reg.get("external_allowlist", []))

    if action == "list":
        rows = []
        for name, job in jobs.items():
            warns = _validate(name, job, external)
            rows.append({"name": name, "schedule": _sched_str(job["schedule"]),
                         "command": job["command"], "risk": job.get("risk"), "warns": warns})

        def render(rs):
            out = [f"{len(rs)} job(s) in {REGISTRY.relative_to(paths.PLAINKEEP_HOME)}:\n"]
            for r in rs:
                flag = f" {RED}⚠ {'; '.join(r['warns'])}{RESET}" if r["warns"] else f" {GREEN}✓{RESET}"
                out.append(f"  {r['name']:<14} {DIM}{r['schedule']:<16}{RESET} {r['command']:<26} "
                           f"[{r['risk']}]{flag}")
            out.append(f"\n  run one now:  plainkeep job run <name>     install schedule:  plainkeep job apply")
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

    elif action == "apply":
        skipped = [n for n, j in jobs.items() if j.get("risk") not in SCHEDULABLE]
        schedulable = [n for n, j in jobs.items() if j.get("risk") in SCHEDULABLE]
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
        written = []
        for name, job in jobs.items():
            if job.get("risk") not in SCHEDULABLE:
                continue
            f = out / f"com.plainkeep.{name}.plist"
            vaultio.write_text(f, _plist(name, job), encoding="utf-8")
            written.append(f)
        data = {"rendered": [f.name for f in written], "skipped": skipped,
                "dir": str(out.relative_to(paths.PLAINKEEP_HOME))}

        def render(_):
            print(f"rendered {len(written)} plist(s) -> {out.relative_to(paths.PLAINKEEP_HOME)}/")
            for f in written:
                print(f"  {f.name}")
            if skipped:
                print(f"{YEL}skipped (not schedulable): {', '.join(skipped)}{RESET}")
            print("\nactivate (the privileged, out-of-root step is yours to run):")
            print(f"  ln -sf {out}/com.plainkeep.*.plist ~/Library/LaunchAgents/")
            print("  for p in ~/Library/LaunchAgents/com.plainkeep.*.plist; do launchctl load \"$p\"; done")

        return output.emit(data, "job", human=render)

    else:
        output.fail(output.EXIT_USAGE, "usage: plainkeep job list | run <name> | apply", verb="job")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
