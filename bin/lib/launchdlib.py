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
import copy
import json
import os
import platform
import plistlib
import re
import shutil
import subprocess
from contextlib import contextmanager
from pathlib import Path

from lib import enginetree, paths, vaultio

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


# ONE ANSWER PER LABEL, FOR THE LENGTH OF AN EXPLICIT READ-ONLY SPAN (r1/M5).
#
# `plainkeep doctor` reads `job_states()` twice — once for the `automation` setup-layer row, once for
# its own per-job rows — so every rendered job cost two `launchctl print` subprocesses answering the
# same question twice.
#
# The memo is OPT-IN rather than process-wide, and that is the whole design. A process-wide cache
# looks equivalent for a short-lived verb and is not: `setuplib.status()` is also called in-process,
# repeatedly, by callers that CHANGE the state in between, and such a caller would silently read the
# answer from before the change. `cached_probes()` marks the span where nothing mutates launchd, so
# the reuse is a property of that span rather than an assumption about the whole program.
_loaded_memo: dict[str, bool] = {}
_memo_on = False


@contextmanager
def cached_probes():
    """Reuse each label's `launchctl print` answer for the duration of the block.

    ONLY legal around a span that does not change what launchd has loaded — `plainkeep doctor`, which
    never mutates anything, is the caller this exists for. The memo is cleared on both entry and exit,
    so it can never outlive the block or inherit an earlier one."""
    global _memo_on
    _loaded_memo.clear()
    outer, _memo_on = _memo_on, True
    try:
        yield
    finally:
        _memo_on = outer
        _loaded_memo.clear()


def is_loaded(name: str) -> bool:
    """Does launchd currently know this label? `launchctl print` exits 0 for a loaded service and
    nonzero otherwise — the one question that a rendered file on disk cannot answer."""
    if _memo_on and name in _loaded_memo:
        return _loaded_memo[name]
    if not launchctl_available():
        return False
    answer = launchctl("print", service_target(name)).returncode == 0
    if _memo_on:
        _loaded_memo[name] = answer
    return answer


# ------------------------------------------------------------------ the registry: names, schedules
#
# THE REGISTRY IS VAULT CONTENT, and everything below exists because of that one fact. It is
# agent-writable, it syncs between machines, `plainkeep job set` edits it, and a human may open it in
# an editor — so this file cannot treat it as a data structure it wrote itself. Two rules and one
# parser, in ONE implementation, read by every surface that touches it.


class RegistryError(ValueError):
    """A `jobs/registry.json` this product refuses to act on — with the correction in the message.

    A `ValueError` on purpose: `job enable`/`apply` already contain a per-job render failure with
    `except Exception` (r2/I5), so an entry that slips past the up-front legality check still lands
    in that job's result rather than as a traceback. The point of the type is the DIAGNOSIS, not the
    containment — a `KeyError: 'schedule'` in an error field is an exception leaking through a
    guard, not a product telling an operator what is wrong with their file."""


class ScheduleError(RegistryError):
    """A schedule entry launchd could not be given. Its message names the correction, not the fault
    ("'7am' is not HH:MM — write 07:00"), because the person reading it is mid-edit."""


class RegistryMissing(RegistryError):
    """There is no `jobs/registry.json` in this vault at all.

    A separate type because ABSENCE AND REFUSAL ARE DIFFERENT ANSWERS, and collapsing them is how
    the checker ended up greener than the product (r1/I2): a vault that never had jobs and a vault
    whose registry this product refuses to read both arrived at the status probes as `None`, so
    `plainkeep doctor` iterated zero jobs and certified a file `plainkeep job list` refuses. Callers
    that only care "are there jobs" catch `RegistryError` and get both; callers that report to a
    human distinguish them."""


# A registry key becomes a FILENAME in two directories, one of which is outside the vault
# (`com.plainkeep.<name>.plist`). The pathwall exemption, machine-contract §9 and ADR-022 all claimed
# the destination was bounded because "neither the directory nor the filename comes from an argument"
# — true, and not the operative bound: the filename comes from a registry KEY.
#
# THE RULE LIVES AT THE READ, not on the paths that write (r2/M6). It used to gate `enable`/`apply`
# only, and `disable` stays deliberately permissive — a job whose risk class was tightened after it
# was enabled is exactly the one an operator most needs to be able to turn off — so `disable` did
# `unlink` on a path built from an unvalidated key. With a hand-made `com.plainkeep.*` hop directory
# inside the LaunchAgents dir, `job disable --all --yes` removed a file outside it. Validating here,
# upstream of every reader, is the only placement that cannot be routed around by adding a surface.
NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*\Z")
NAME_RULE = ("is not a plain identifier "
             "(letters, digits, '_', '.', '-'; it becomes a plist filename)")

WEEKDAYS = {"Sun": 0, "Mon": 1, "Tue": 2, "Wed": 3, "Thu": 4, "Fri": 5, "Sat": 6}
# The four cadences, spelled the way they must appear in the file. Every refusal below quotes this
# list, so a person who typed the wrong shape is shown all four rather than told about one.
SCHEDULE_FORMS = ('"daily": "HH:MM"', '"weekly": "Day HH:MM"', '"monthly": "D HH:MM"',
                  '"interval_minutes": <positive int>')
_HHMM_RE = re.compile(r"([01][0-9]|2[0-3]):([0-5][0-9])\Z")
_LOOSE_TIME_RE = re.compile(r"\A(\d{1,2})(?::(\d{1,2}))?\s*(am|pm)?\Z", re.IGNORECASE)


def name_ok(name: object) -> bool:
    return bool(NAME_RE.match(str(name)))


def _suggest_time(value: str) -> str:
    """The CORRECTION for an unusable time, when one can be guessed. `7am` → `07:00`, `8:00` →
    `08:00`. A refusal that only says "invalid" makes the person guess at the format twice; one that
    shows the same time written correctly is read once."""
    m = _LOOSE_TIME_RE.match(value.strip())
    if m:
        hour, minute, meridiem = int(m.group(1)), int(m.group(2) or 0), (m.group(3) or "").lower()
        if meridiem == "pm" and hour < 12:
            hour += 12
        elif meridiem == "am" and hour == 12:
            hour = 0
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            return f"{hour:02d}:{minute:02d}"
    return "07:00"


def _hhmm(value: str, whole: str) -> str:
    if not _HHMM_RE.match(value):
        raise ScheduleError(f"{whole!r} is not HH:MM — write {_suggest_time(value)} "
                            "(24-hour, zero-padded: 00:00 to 23:59)")
    return value


def parse_schedule(schedule: object) -> dict:
    """Validate one job's `schedule` and return it in canonical form — the ONE implementation.

    It used to be inline in `plist()`, which meant the shape of a schedule was decided by whatever
    `str.split()` happened to survive: an unknown weekday silently became Sunday (`wd.get(day, 0)`),
    `"7am"` raised a bare `ValueError` about unpacking, and a missing `schedule` key raised
    `KeyError`. r2/I5 CONTAINED those — each became that job's recorded failure instead of a
    traceback — but containing an exception is not diagnosing it, and nothing checked a schedule
    until something tried to render it, by which point `job set` had already written the file and
    `job list` had shown the entry with a green tick.

    So there is one parser, and three surfaces read it: `plist()` (which must not invent a Sunday),
    `_validate()` in the job verb (so a malformed schedule is a §15 legality warning like any other,
    which makes `apply`/`enable` refuse whole-command before rendering anything), and `set_schedule`
    (which validates BEFORE it opens the file — a validation that fires after the write is a
    corruption with a good error message).

    Canonical means: exactly one cadence key, the day-of-month normalized to an int, and every time
    a zero-padded 24-hour `HH:MM`. Nothing is coerced quietly — an unusable value is refused with the
    correction, never rounded into a schedule the operator did not ask for."""
    forms = " | ".join(SCHEDULE_FORMS)
    if schedule is None:
        raise ScheduleError(f"no schedule — a job needs exactly one of: {forms}")
    if not isinstance(schedule, dict):
        raise ScheduleError(f"schedule must be an object, not {type(schedule).__name__} "
                            f"— one of: {forms}")
    keys = [k for k in ("daily", "weekly", "monthly", "interval_minutes") if k in schedule]
    if not keys:
        raise ScheduleError(f"schedule {sorted(schedule)} names no cadence — use one of: {forms}")
    if len(keys) > 1:
        raise ScheduleError(f"schedule names {len(keys)} cadences ({', '.join(keys)}) "
                            f"— a job has exactly one: {forms}")
    key, raw = keys[0], schedule[keys[0]]

    if key == "interval_minutes":
        # `bool` is an `int` in Python and `true` is a perfectly good JSON value, so it is excluded
        # explicitly — `StartInterval: 60` from `"interval_minutes": true` would be a schedule
        # nobody wrote.
        if isinstance(raw, bool) or not isinstance(raw, int) or raw <= 0:
            raise ScheduleError(f"interval_minutes {raw!r} is not a positive whole number of minutes "
                                "— write 60")
        return {"interval_minutes": raw}

    if not isinstance(raw, str):
        raise ScheduleError(f"{key} {raw!r} must be a string — {forms}")
    value = raw.strip()
    if key == "daily":
        return {"daily": _hhmm(value, raw)}

    parts = value.split()
    if len(parts) != 2:
        example = '"Sun 03:00"' if key == "weekly" else '"1 04:00"'
        lead = "Day" if key == "weekly" else "D"
        raise ScheduleError(f"{key} {raw!r} is not \"{lead} HH:MM\" — write {example}")
    lead, hhmm = parts
    if key == "weekly":
        if lead not in WEEKDAYS:
            raise ScheduleError(f"weekly day {lead!r} is not one of {', '.join(WEEKDAYS)} "
                                '— write "Sun 03:00"')
        return {"weekly": f"{lead} {_hhmm(hhmm, raw)}"}
    # monthly. 28 is the ceiling on purpose: `Day: 31` simply never fires in February, which is a
    # schedule that looks set and silently is not — the failure mode this whole file exists to end.
    if not lead.isdigit() or not (1 <= int(lead) <= 28):
        raise ScheduleError(f"monthly day-of-month {lead!r} is not 1-28 — write \"1 04:00\" "
                            "(28 is the last day every month has)")
    return {"monthly": f"{int(lead)} {_hhmm(hhmm, raw)}"}


# THE ENGINE'S COPY OF THE CANONICAL JOBS, and why one has to exist.
#
# `jobs/registry.json` is VAULT content: the template's copy seeds a NEW vault and an engine update
# never delivers it (that is the point — it is the operator's file). So a canonical job added after a
# vault was created can never reach that vault, and `start` is exactly that case: every vault made
# before ADR-022 schedules the day's close and not its start, and no amount of `plainkeep setup`
# fixes it. `plainkeep job set <name>` seeds from here when the name is one the engine knows.
#
# It is a SECOND copy of the definitions in `jobs/registry.json`, which is a thing to be nervous
# about, so it is pinned: `test/run_jobs.py` asserts these are byte-for-byte the shipped registry's
# jobs, in the same order, and that the design-model fixture agrees about every job they share.
DEFAULT_JOBS: dict[str, dict] = {
    "start":         {"command": "plainkeep start --automated", "schedule": {"daily": "07:30"},
                      "risk": "safe_write", "writes": ["~/plainkeep/journal"]},
    "index":         {"command": "plainkeep index", "schedule": {"interval_minutes": 60},
                      "risk": "read", "writes": ["~/plainkeep/.index"]},
    "consolidate":   {"command": "plainkeep consolidate", "schedule": {"daily": "02:30"},
                      "risk": "safe_write", "writes": ["~/plainkeep/journal"]},
    "organize_scan": {"command": "plainkeep organize scan", "schedule": {"weekly": "Sun 03:00"},
                      "risk": "safe_write", "writes": ["~/plainkeep/inbox/organize"]},
    "close_nudge":   {"command": "plainkeep close --automated", "schedule": {"daily": "18:30"},
                      "risk": "safe_write", "writes": ["~/plainkeep/journal"]},
    "backup_check":  {"command": "plainkeep backup", "schedule": {"weekly": "Fri 17:00"},
                      "risk": "read", "writes": []},
}
# The two ends of the day, named once. The wizard asks about exactly these, `docs/setup.md` documents
# them, and both read the pair from here rather than repeating the two strings.
DAY_START_JOB, DAY_CLOSE_JOB = "start", "close_nudge"


def read_registry() -> dict:
    """The §15 registry, or a `RegistryError` that says what is wrong with it.

    This is the STRICT reader — the one a verb calls, so that a malformed registry produces a
    refusal an operator can act on. `load_registry()` below is the tolerant wrapper for status
    probes. The key rule is enforced here and only here (see `NAME_RE`)."""
    path = registry_path()
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RegistryMissing(f"no jobs registry at "
                              f"{_relative(path)} ({exc.strerror or exc})") from exc
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise RegistryError(f"{_relative(path)} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("jobs"), dict):
        raise RegistryError(f"{_relative(path)} has no `jobs` object — it must be "
                            '{"jobs": {"<name>": {...}}}')
    for name, job in data["jobs"].items():
        if not name_ok(name):
            raise RegistryError(f"job name {name!r} {NAME_RULE}")
        # THE OTHER SHAPE OF MALFORMED (r1/I3). The keys were validated and the values were not, so
        # `{"jobs": {"broken": "not a dict"}}` reached every action and threw `AttributeError:
        # 'str' object has no attribute 'get'` — from `job.get("command")` in `job_states`, which is
        # BEFORE the per-job containment, and from `_validate` itself, which is what
        # `_legal_or_refuse` calls to decide whether to refuse. Neither guard can catch a fault in
        # the thing it uses to look.
        if not isinstance(job, dict):
            raise RegistryError(f"job {name!r} is not an object — a job is "
                                '{"command": "plainkeep <verb>", "schedule": {…}, "risk": "read"}, '
                                f"not {type(job).__name__}")
    return data


def _relative(path: Path) -> str:
    try:
        return str(path.relative_to(paths.PLAINKEEP_HOME))
    except ValueError:
        return str(path)


def load_registry() -> dict | None:
    """The §15 registry, or None when this vault has no usable jobs file. Never raises — every caller
    is a status probe (doctor's rows, the setup layer, the wizard prompt) that must report rather
    than crash. A registry the strict reader refuses is `None` here too, so a probe never asserts
    things about a file the product will not act on.

    A PROBE THAT REPORTS TO A HUMAN MUST ALSO CALL `registry_error()` (r1/I2). `None` alone cannot
    tell "this vault has no jobs" from "this vault's jobs file is broken", and the second one is a
    finding. Silence about it is how `plainkeep doctor` came to print `ok` for a registry
    `plainkeep job list` refuses."""
    try:
        return read_registry()
    except RegistryError:
        return None


def registry_error() -> str | None:
    """Why `load_registry()` returned None, when the reason is a REFUSAL rather than an absence.

    None means either "the registry is fine" or "there is no registry", which are the two states a
    status probe may legitimately stay quiet about. A string means the file exists and this product
    will not act on it — the diagnosis, ready to be surfaced as a warn."""
    try:
        read_registry()
    except RegistryMissing:
        return None
    except RegistryError as exc:
        return str(exc)
    return None


def seedable_defaults(jobs: dict | None = None) -> list[str]:
    """The canonical jobs this registry does NOT have — what `job set <name>` can seed. Part of the
    refusal for an unknown name: "there is no job called that, and here is what you could create"."""
    have = set(jobs or {})
    return [n for n in DEFAULT_JOBS if n not in have]


def set_schedule(name: str, schedule: dict, *, dry_run: bool = False) -> dict:
    """Rewrite ONE job's schedule in `jobs/registry.json`. The single writer behind `plainkeep job
    set` and the setup wizard's two time questions.

    ONE WRITER, because there are two callers and `setuplib` may not import a verb's `run.py`. A
    second implementation in the wizard would validate differently the first time either changed,
    and the wizard's copy is the one that runs unattended-ish, on a fresh machine, at the moment the
    operator is least able to tell that their answer went nowhere.

    A READ-MODIFY-WRITE OF THE PARSED DOCUMENT, never a regeneration from a model. The file carries
    a `description`, an `external_allowlist`, per-job `writes` declarations and a deliberate job
    order, and a human edits it too — rebuilding it from what this module happens to know would
    silently drop everything else. Only `jobs[name]["schedule"]` is assigned; JSON object order is
    insertion order in both directions, so every other key keeps its place. (The file is re-emitted
    with two-space indent, so hand alignment inside a line does not survive the first `set`. That is
    the cost of editing JSON programmatically at all; the CONTENT is preserved exactly.)

    IT NEVER TOUCHES LAUNCHD. Re-enabling after a successful write is the tempting thing — the
    operator clearly wants the new time to be live — and it would make a safe_write action install
    plists outside the vault and mutate a running launchd domain without the `--yes` that `enable`
    exists to demand. The caller states the gap instead; `job status`'s drift column shows it, and
    `plainkeep job enable <name> --yes` closes it.

    Returns `{name, schedule, previous, seeded, job}`; raises `RegistryError`/`ScheduleError`."""
    if not name_ok(name):
        raise RegistryError(f"job name {name!r} {NAME_RULE}")
    # Validated BEFORE the file is opened: a refusal must leave the registry untouched.
    canonical = parse_schedule(schedule)
    data = read_registry()
    jobs = data["jobs"]
    seeded = name not in jobs
    if seeded:
        if name not in DEFAULT_JOBS:
            raise RegistryError(f"no job {name!r} in {_relative(registry_path())}, and the engine has "
                                f"no default by that name to seed")
        jobs[name] = copy.deepcopy(DEFAULT_JOBS[name])
    previous = jobs[name].get("schedule")
    jobs[name]["schedule"] = canonical
    if not dry_run:
        vaultio.write_text(registry_path(), json.dumps(data, indent=2, ensure_ascii=False) + "\n",
                           encoding="utf-8")
    return {"name": name, "schedule": canonical, "previous": previous, "seeded": seeded,
            "job": jobs[name]}


def schedulable(job: dict) -> bool:
    return job.get("risk") in SCHEDULABLE


def schedule_str(s: object) -> str:
    """A job's cadence in one human phrase — `every 60m`, `daily 07:30`, `weekly Sun 03:00`. Shared
    with the wizard's automation prompt, which names the actual jobs and times it is about to
    schedule rather than restating them in a sentence that can drift from the registry.

    DELIBERATELY TOLERANT, unlike `parse_schedule`: this is a display string, and its callers are
    listing rows next to the very warning that says the schedule is unusable. Refusing to render a
    malformed one would hide the row that explains it."""
    if not isinstance(s, dict):
        return "?"
    if "interval_minutes" in s:
        return f"every {s['interval_minutes']}m"
    for k in ("daily", "weekly", "monthly"):
        if k in s:
            return f"{k} {s[k]}"
    return "?"


def _when(s: object) -> dict:
    """The scheduling key for a job's cadence, as a plist fragment (one key, either shape). Built
    from the CANONICAL schedule, so this function has no opinions of its own to disagree with
    `job list`'s or `job set`'s."""
    sched = parse_schedule(s)
    if "interval_minutes" in sched:
        return {"StartInterval": sched["interval_minutes"] * 60}
    if "daily" in sched:
        hh, mm = sched["daily"].split(":")
        return {"StartCalendarInterval": {"Hour": int(hh), "Minute": int(mm)}}
    if "weekly" in sched:
        day, hhmm = sched["weekly"].split(); hh, mm = hhmm.split(":")
        return {"StartCalendarInterval": {"Weekday": WEEKDAYS[day], "Hour": int(hh), "Minute": int(mm)}}
    dom, hhmm = sched["monthly"].split(); hh, mm = hhmm.split(":")
    return {"StartCalendarInterval": {"Day": int(dom), "Hour": int(hh), "Minute": int(mm)}}


def plist(name: str, job: dict) -> str:
    """The launchd plist for one registry job.

    BUILT AS DATA, SERIALIZED BY `plistlib` — never as an f-string of XML (r1/B1). The previous
    version interpolated each whitespace token of `job["command"]` straight into `<string>{a}</string>`,
    and `jobs/registry.json` is VAULT CONTENT: agent-writable, and it syncs between machines. A
    command could therefore close the `<array>` and open top-level launchd keys of its own — the
    proven payload set `Program` (which overrides the executable `ProgramArguments` names) and
    `RunAtLoad` (fire at login rather than at 07:30), from a command whose first two tokens were
    `plainkeep index` and so passed the §15 token model untouched.

    That was survivable while the rendered file only sat in the vault waiting for a human to paste
    `launchctl load`. It is not survivable now that `job enable` installs and bootstraps it, from a
    setup layer that is on by default — the same registry write became persistence.

    `plistlib` is stdlib, so the stdlib-only floor is unaffected, and the serializer owns escaping:
    a `<` or an `&` in a command is now an argument containing those characters, which is what the
    registry meant, instead of markup. Structure cannot be expressed by content at all — the keys of
    this document are decided here and nowhere else."""
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
    log = paths.PLAINKEEP_HOME / ".logs" / "jobs" / f"{name}.log"
    doc = {
        "Label": label(name),
        "ProgramArguments": list(args),
        "EnvironmentVariables": {"PLAINKEEP_HOME": str(paths.PLAINKEEP_HOME)},
        "StandardOutPath": str(log),
        "StandardErrorPath": str(log),
        **_when(job.get("schedule")),
    }
    return plistlib.dumps(doc, fmt=plistlib.FMT_XML, sort_keys=True).decode("utf-8")


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

    THE `loaded` PROBE IS CONDITIONAL ON **VAULT** STATE, and the word matters (r1/I2). It spawns
    `launchctl print` per job, so it is gated on whether this vault has rendered the job at all — a
    vault that never rendered anything (every test fixture, and every machine where automation was
    declined) asks launchd nothing.

    It used to also probe when the job was INSTALLED, and that read the real `~/Library/LaunchAgents`
    whenever the seam variable was unset. So the guard was really "nothing rendered **and** nothing
    installed on this host", which is a property of the developer's machine rather than of the vault
    under test: `doctor` is spawned by fifteen suites, and the moment one `com.plainkeep.*` plist
    existed on the host they would all have begun querying the developer's live login session. The
    disjunct bought one thing — reporting `loaded` for a job installed from somewhere else — and cost
    a safety property that two documents asserted. The `installed` column still reveals that case;
    only the `loaded` answer for it is now withheld.
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
        loaded = bool(probe_loaded and current is not None and is_loaded(name))
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
