"""launchdlib.py — the ACTIVATION seam: rendering a job's launchd plist, installing it, and asking
launchd what it currently believes.

Why this is a library and not three copies. Activation used to be a printed handoff (`plainkeep job
apply` ended with a `ln -sf` and a `launchctl load` for the operator to paste), so exactly one place
knew how to render a plist and nowhere knew whether one had ever been loaded. Making activation a
product verb creates three readers of the same facts at once — `plainkeep job enable/disable/status`,
the `automation` setup layer, and `plainkeep doctor`'s advisory rows — and the interesting property
is a COMPARISON (is the file on disk still what the registry says?), which two implementations would
answer differently the first time the plist template changed. One renderer, one label spelling, one
launchctl seam.

THE TEST SEAM, and why it is two variables rather than one. Everything here reaches outside the vault
— `~/Library/LaunchAgents` is the operator's machine, and `launchctl` mutates a live launchd domain —
so both halves are injectable:

  * `PLAINKEEP_LAUNCHCTL` — an absolute path to the binary to invoke instead of `launchctl`. The test
    suites point it at a fake that records its argv, so ordering and arguments are ASSERTED rather
    than assumed, and no suite ever touches the developer's own launchd session.
  * `PLAINKEEP_LAUNCH_AGENTS_DIR` — where an installed plist goes. `Path.home()` would already be
    redirected by a sandboxed `HOME`, but the suites here set `PLAINKEEP_HOME` per invocation and
    leave `HOME` alone (they run verbs as subprocesses of the developer's own shell), so relying on
    that would have meant every job test writing into the real `~/Library/LaunchAgents`.

Both are documented in `docs/machine-contract.md §9`. Neither has a use in normal operation; they
exist so that "this never touches your machine" is a property of the code rather than a promise.
"""
from __future__ import annotations
import json
import os
import platform
import shutil
import subprocess
from pathlib import Path

from lib import enginetree, paths

# Only these two risk classes may be scheduled (§15) — the same gate `job list`/`apply` apply, and
# the one `enable` refuses to cross.
SCHEDULABLE = {"read", "safe_write"}
LABEL_PREFIX = "com.plainkeep"
# How long a single launchctl probe may take before we call it "not loaded". A `print` against a
# healthy domain answers in milliseconds; the timeout exists so a wedged launchd cannot hang
# `plainkeep doctor`.
PROBE_TIMEOUT = 10


def is_darwin() -> bool:
    return platform.system() == "Darwin"


def registry_path() -> Path:
    return paths.PLAINKEEP_HOME / "jobs" / "registry.json"


def render_dir() -> Path:
    """The VAULT-side artefact directory. These files are the record of what the schedule is; they
    are versioned with the vault and survive a `disable`."""
    return paths.PLAINKEEP_HOME / "jobs" / "launchd"


def launch_agents_dir() -> Path:
    """The MACHINE-side directory launchd actually reads. Overridable for tests (see the module
    docstring); otherwise `~/Library/LaunchAgents`."""
    override = os.environ.get("PLAINKEEP_LAUNCH_AGENTS_DIR")
    return Path(override).expanduser() if override else Path.home() / "Library" / "LaunchAgents"


def launchctl_bin() -> str:
    return os.environ.get("PLAINKEEP_LAUNCHCTL") or "launchctl"


def launchctl_available() -> bool:
    """Is there something to invoke? An explicit override is trusted as-is; otherwise `launchctl`
    must be on PATH, which off macOS it is not."""
    if os.environ.get("PLAINKEEP_LAUNCHCTL"):
        return True
    return shutil.which("launchctl") is not None


def label(name: str) -> str:
    return f"{LABEL_PREFIX}.{name}"


def domain() -> str:
    """The per-user GUI domain — the one a login session's agents live in. `gui/<uid>`, never
    `user/<uid>`: a job that opens the journal for a human is a login-session agent."""
    return f"gui/{os.getuid()}"


def service_target(name: str) -> str:
    return f"{domain()}/{label(name)}"


def rendered_path(name: str) -> Path:
    return render_dir() / f"{label(name)}.plist"


def installed_path(name: str) -> Path:
    return launch_agents_dir() / f"{label(name)}.plist"


def launchctl(*args: str) -> subprocess.CompletedProcess:
    """Invoke launchctl and return the completed process. NEVER raises: a missing binary or a
    timeout is reported as a nonzero returncode, because every caller here is either advisory
    (status/doctor) or already reporting per-job outcomes."""
    try:
        return subprocess.run([launchctl_bin(), *args], capture_output=True, text=True,
                              timeout=PROBE_TIMEOUT)
    except (OSError, subprocess.SubprocessError) as exc:
        return subprocess.CompletedProcess([launchctl_bin(), *args], 127, "", str(exc))


def is_loaded(name: str) -> bool:
    """Does launchd currently know this label? `launchctl print` exits 0 for a loaded service and
    nonzero otherwise — the one question that a rendered file on disk cannot answer."""
    if not launchctl_available():
        return False
    return launchctl("print", service_target(name)).returncode == 0


def load_registry() -> dict | None:
    """The §15 registry, or None when this vault has no jobs file. Never raises — every caller is a
    status probe that must report rather than crash."""
    try:
        data = json.loads(registry_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and isinstance(data.get("jobs"), dict) else None


def schedulable(job: dict) -> bool:
    return job.get("risk") in SCHEDULABLE


def plist(name: str, job: dict) -> str:
    # THE LAUNCHER IS ENGINE-OWNED (Phase 2 Task 2). This built
    # `$PLAINKEEP_HOME/plainkeep` — the vault-local shim — and ADR-014 names the line as one that
    # must change: after the engine moves out, that path is ENOENT at 2am, in a sanitized launchd
    # environment where nothing will be there to explain it. The plist keeps naming an ABSOLUTE
    # launcher (a scheduled job must never depend on discovery or on PATH) and both roots are baked
    # in absolutely: the engine's launcher as the program, the validated vault as PLAINKEEP_HOME.
    #
    # `stable_launcher()`, not `launcher()`, and the difference is the whole point: a plist is a
    # PERSISTED artefact. `launcher()` spells the version (`…/engine/4.0.0-dev/plainkeep`) because
    # ENGINE_ROOT resolves through `current`, so a plist written with it keeps running the OLD engine
    # after the next `--activate` — silently — and becomes the 2am ENOENT above the moment that
    # version is pruned. `current` is the name that survives an engine update.
    toks = job["command"].split()
    args = ([str(enginetree.stable_launcher()), *toks[1:]]
            if toks and toks[0] == "plainkeep" else toks)
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
            f'  <key>Label</key>\n  <string>{label(name)}</string>\n'
            f'  <key>ProgramArguments</key>\n  <array>{pa}\n  </array>\n'
            f'  <key>EnvironmentVariables</key>\n  <dict>\n'
            f'    <key>PLAINKEEP_HOME</key><string>{paths.PLAINKEEP_HOME}</string>\n  </dict>\n'
            f'{when}\n'
            f'  <key>StandardOutPath</key>\n  <string>{log}</string>\n'
            f'  <key>StandardErrorPath</key>\n  <string>{log}</string>\n'
            f'</dict>\n</plist>\n')


def _read(p: Path) -> str | None:
    try:
        return p.read_text(encoding="utf-8")
    except OSError:
        return None


def job_states(reg: dict | None = None, *, probe_loaded: bool = True) -> list[dict]:
    """One row per registry job: what is rendered, what is installed, what launchd has loaded.

    `drift` is the comparison that makes a rendered file trustworthy: the registry is the source, so
    a file that no longer equals a FRESH render of its job is stale and must be re-rendered rather
    than copied. That is why `enable` renders first and never installs whatever happens to be lying
    in `jobs/launchd/`.

    THE `loaded` PROBE IS CONDITIONAL ON DISK STATE, deliberately. It spawns `launchctl print` per
    job, and a vault that has never rendered or installed anything (every test fixture, and every
    machine where automation was declined) has nothing for launchd to know about — so nothing is
    spawned at all. That keeps `plainkeep doctor` free on the common path, and it means a suite that
    forgets the fake seam still cannot reach the developer's launchd session by accident.
    """
    reg = reg if reg is not None else load_registry()
    jobs = (reg or {}).get("jobs", {})
    rows = []
    for name, job in jobs.items():
        rendered_file = rendered_path(name)
        current = _read(rendered_file)
        installed = installed_path(name).exists()
        drift = False
        if current is not None:
            try:
                drift = current != plist(name, job)
            except Exception:      # a malformed schedule can't be re-rendered; report it as drift
                drift = True
        loaded = bool(probe_loaded and (current is not None or installed) and is_loaded(name))
        rows.append({
            "name": name,
            "command": job.get("command", ""),
            "risk": job.get("risk"),
            "schedulable": schedulable(job),
            "rendered": current is not None,
            "drift": drift,
            "installed": installed,
            "loaded": loaded,
        })
    return rows
