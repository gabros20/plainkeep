#!/usr/bin/env python3
"""
run_all.py — run every OFFLINE suite (no LLM, no cost) and summarize.

Covers: guardrail (§5), jobs registry (§15), sweep decay machine (§9.4), wiki integrity (§10),
and searchability/vector analysis (§10.2). The LLM-operator simulation (run_simulation.py) is
separate because it needs a model and network.

Usage:  python3 test/run_all.py [--suite PATH ...] [--lock PATH]

Harness guarantees (v2 0.14) — a run can no longer pile up, hang forever or outlive its parent:
  * each suite runs in its own process group with a timeout (default 600 s; a third SUITES tuple
    element overrides per suite; PLAINKEEP_TEST_TIMEOUT=<seconds> overrides everything). A timeout
    is a FAIL whose summary line names the suite and the elapsed seconds.
  * the whole group is killed (TERM, then KILL after 5 s) on timeout, on SIGINT/SIGTERM to this
    process, and — with a SUITE-NOTE — when a suite returns while a grandchild is still alive.
  * one run per checkout: a non-blocking flock on test/.run_all.lock; a second concurrent run
    exits 2 naming the holder's PID. Different worktrees have different locks.
  * `--suite PATH` (repeatable) runs ONLY those scripts instead of SUITES, skipping the seal gate;
    `--lock PATH` moves the lock file. Both exist for test/run_harness.py, which drives this runner
    over fixture suites (the outer run holds the default lock, so the inner run needs its own).
"""
from __future__ import annotations
import argparse
import fcntl
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
GREEN, RED, BOLD, DIM, RESET = "\033[32m", "\033[31m", "\033[1m", "\033[2m", "\033[1m\033[0m"
DEFAULT_TIMEOUT = 600.0
KILL_GRACE = 5.0
LOCK_FILE = HERE / ".run_all.lock"

# (label, script) or (label, script, timeout_seconds) — the optional third element overrides DEFAULT_TIMEOUT.
SUITES = [
    ("guardrail model (§5)", "run_deterministic.py"),
    ("guardrail enforcement (§5)", "run_guardrail.py"),
    ("path-wall on the write path (§5 Iron Law)", "run_pathwall.py"),
    ("append-only originals: ~/files/**/in/ (Phase 2 Task 1c)", "run_originals.py"),
    ("vault marker + registry (ADR-014 Task 1a)", "run_vault.py"),
    ("vault discovery: selection + refusals (ADR-014 Task 1b)", "run_discovery.py"),
    ("jobs registry (§15)", "run_jobs.py"),
    ("jobs scheduler verb (§15)", "run_jobverb.py"),
    ("sweep decay model (§9.4)", "run_sweep.py"),
    ("sweep verb (§9.4)", "run_sweepverb.py"),
    ("wiki integrity (§10)", "run_wiki.py"),
    ("wiki link edges (§10)", "run_wiki_edges.py"),
    ("state & consistency (§7/§8/§10/§14)", "run_state.py"),
    ("searchability model (§10.2)", "run_search.py"),
    ("stage-1 search impl (§10.2)", "run_search_impl.py"),
    ("stage-2 hybrid LanceDB (§10.2)", "run_search_vec.py"),
    ("retrieval bench + gates (v2 0.0/0.1)", "run_bench.py"),
    ("daily-driver verbs (§4/§7)", "run_verbs.py"),
    ("triage flow (§4/§10)", "run_triage.py"),
    ("agent indirection (§6)", "run_agentlib.py"),
    ("host config file (v2 0.5)", "run_hostconfig.py"),
    ("daily/weekly rhythm (§16)", "run_loops.py"),
    ("health verbs: doctor, wiki (§14/§10)", "run_health.py"),
    ("terminal ergonomics: completion + renderer", "run_completion.py"),
    ("note types + bookmarks (issue #1 D+F)", "run_notetypes.py"),
    ("maintenance: backup, consolidate (§14/§15)", "run_maintenance.py"),
    ("backup family + share (5.1/5.2)", "run_backup_share.py"),
    ("setup/update flow (§2/§17)", "run_setup.py"),
    ("engine installer: enginetree (ADR-014 Phase 2 Task 2)", "run_enginetree.py"),
    ("provisioning: uv bootstrap + delivered lock + frozen matrix (ADR-020 Phase 2 Task 4)",
     "run_provision.py"),
    ("engine update + vault init: failure injection (Phase 2 Task 5)", "run_engineupdate.py"),
    ("vault migration off the vault-local engine copy: 13-item bar + AST ratchets + kill matrix "
     "(ADR-014 Phase 2 Task 6)", "run_migrate.py", 1500),
    # run_migrate: 636 s of case time measured on the merged tree (v2 Stage 0), ~20% over the
    # pre-wave base on the same cases (kill matrix 150 s vs 127 s); real migrations run repeatedly.
    ("trust wave: exit codes + update merge + doctor (0.1/0.3/0.4)", "run_trust.py"),
    ("machine contract: --json + plainkeep.json/3 + dry-run (1.1/1.2/0.5)", "run_json.py"),
    ("multi-root verb resolution: plugins + PLAINKEEP_PATH (2.1/0.2)", "run_resolver.py"),
    ("core-parity: TS<->Python resolver differential oracle (hybrid-core)", "run_core_parity.py"),
    ("core-fuzz: TS<->Python difflib + bool()/str() differential fuzz (hybrid-core)", "run_fuzz.py"),
    ("core-tui: in-core terminal UI on a real pty (hybrid-core)", "run_tui_pty.py"),
    ("frozen SDK + plainkeep plugin: api.py + trust ceiling (2.2/2.3)", "run_plugin.py"),
    ("plugin SDK compatibility + dependency contract (ADR-018 Phase 2 Task 3)", "run_pluginsdk.py"),
    ("agent transport: plainkeep mcp stdio server (2.4)", "run_mcp.py"),
    ("core-mcp: in-core MCP server ↔ bin/mcp/run.py protocol differential (hybrid-core)",
     "run_mcp_protocol.py"),
    ("obsidian frontend zero + canvas/bases (3.1/3.2)", "run_obsidian.py"),
    ("terminal ergonomics + raycast: open/orient/search (3.3/3.4)", "run_terminal.py"),
    ("install funnel: script/get (5.4)", "run_get.py"),
    ("tiered extraction + provenance planes (4.1/4.3)", "run_extract.py"),
    ("distill + self-organization loop (4.2/4.4)", "run_organize.py"),
    ("new: scaffold project/client (§4/§12)", "run_new.py"),
    ("repo: ~/work fleet (§4/§12)", "run_repo.py"),
    ("files: binary-assets plane (§9)", "run_files.py"),
    ("archive + invoice (§4)", "run_archive_invoice.py"),
    ("image-reading backend (imagelib)", "run_image_backend.py"),
    ("files: imagelib wiring (metadata + OCR + describe)", "run_files_image.py"),
    ("search enrichment engine", "run_enrich.py"),
    ("plainkeep models verb", "run_models.py"),
    ("setup layers verb", "run_setup_layers.py"),
    ("enrichment pipeline wiring", "run_enrich_pipeline.py"),
    ("ui release gates: version three-way + bun floor (Phase 2 Task 7)", "run_uirelease.py"),
    ("test harness: timeout, process groups, lock, env isolation (v2 0.14)", "run_harness.py"),
]


# A suite may print `SUITE-NOTE: <text>` lines to say what it did NOT cover — a filtered run, a
# deliberately gated case. Without this, such a run reaches the summary below as an unqualified PASS,
# because a suite is reduced to its EXIT STATUS here and nothing else: the qualification scrolls past
# hundreds of lines earlier and the line a reader actually reads says PASS (core-parity quality review
# r2, N2). A note NEVER changes a verdict — it travels with it.
SUITE_NOTE_PREFIX = "SUITE-NOTE:"


def _suite_notes(out: str) -> list[str]:
    return [line.split(SUITE_NOTE_PREFIX, 1)[1].strip()
            for line in out.splitlines() if line.startswith(SUITE_NOTE_PREFIX)]


# --- the hermeticity gate -------------------------------------------------------------------------
# Every suite must call `lib.hermetic.seal()` (read that module for why). This gate is here rather
# than in the seal itself because the failure it prevents is a suite that never calls it: since Task
# 1b, a suite invoking plainkeep with no PLAINKEEP_HOME resolves through the marker walk-up and the
# registry default to the developer's REAL vault, and writes into it with exit 0. Nothing about that
# run looks wrong.
#
# The gate is STATIC, and deliberately so: it catches the next suite someone adds, on the run that
# adds it, instead of on the run where it silently files a note into real notes. It runs BEFORE any
# suite, and it is fatal — a batch that skipped it would be a batch whose greenness means less.
#
# It does NOT make a direct `python3 test/run_foo.py` hermetic; the seal in the suite does that, and
# that is the point of putting it there. This only proves the seal is present everywhere.
#
# Matched by pattern rather than by an exact line, because a suite may legitimately import more from
# the module than `seal` (`scratch_root` is the other lever) — a gate that fails on the import LIST
# would be teaching people to write the import a particular way instead of to be hermetic.
SEAL_CALL = "seal()"
SEAL_IMPORT = "from lib.hermetic import seal"
SEAL_IMPORT_RE = re.compile(r"^from lib\.hermetic import .*\bseal\b", re.M)
SEAL_CALL_RE = re.compile(r"^seal\(\)", re.M)


def _unsealed() -> list[str]:
    """The suites in SUITES that do not carry the seal.

    Iterates the list that actually RUNS, not `HERE.glob("run_*.py")`. The glob answered a
    neighbouring question and got both directions wrong: a suite registered in SUITES under any
    other name was never checked (so the gate could pass while the batch ran an unsealed suite), and
    a `run_*.py` helper that is not a suite would have failed the whole batch for not being one.
    Neither case exists today; the point of a gate is the case that does not exist yet."""
    out = []
    for _, script, *_rest in SUITES:
        f = HERE / script
        if not f.is_file():
            out.append(f"{script} (listed in SUITES but not on disk)")
            continue
        src = f.read_text(encoding="utf-8")
        if not SEAL_IMPORT_RE.search(src) or not SEAL_CALL_RE.search(src):
            out.append(script)
    return out


def _timeout_for(spec_timeout: float | None) -> float:
    env = os.environ.get("PLAINKEEP_TEST_TIMEOUT")
    if env:
        return float(env)
    return spec_timeout if spec_timeout is not None else DEFAULT_TIMEOUT


def _group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False        # macOS answers EPERM for a group that is only unreaped zombies


def _kill_group(pgid: int) -> None:
    """TERM the whole group, wait up to KILL_GRACE for it to empty, then KILL."""
    try:
        os.killpg(pgid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return
    deadline = time.monotonic() + KILL_GRACE
    while time.monotonic() < deadline and _group_alive(pgid):
        time.sleep(0.1)
    if _group_alive(pgid):
        try:
            os.killpg(pgid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass


_current_pgid: int | None = None


def _on_signal(signum, _frame):
    if _current_pgid is not None:
        _kill_group(_current_pgid)
    print(f"\n{RED}run_all interrupted (signal {signum}); suite process group killed\033[0m", flush=True)
    raise SystemExit(128 + signum)


def _run_suite(script: Path, timeout: float) -> tuple[bool, str, str, list[str]]:
    """Run one suite in its own session. Returns (ok, stdout, stderr, extra summary notes)."""
    global _current_pgid
    t0 = time.monotonic()
    proc = subprocess.Popen([sys.executable, str(script)], stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, start_new_session=True)
    pgid = proc.pid                       # start_new_session: the child leads its own group
    _current_pgid = pgid
    notes: list[str] = []
    try:
        # A grandchild that inherited the stdout/stderr pipe keeps communicate() blocked until the
        # timeout, so such a leak is reported as TIMEOUT (the group is still killed), not SUITE-NOTE.
        try:
            out, err = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            elapsed = time.monotonic() - t0
            _kill_group(pgid)
            try:
                out, err = proc.communicate(timeout=KILL_GRACE)
            except subprocess.TimeoutExpired:
                out, err = "", ""
            notes.append(f"TIMEOUT: {script.name} killed after {elapsed:.1f}s (limit {timeout:g}s)")
            return False, out or "", err or "", notes
        if _group_alive(pgid):
            _kill_group(pgid)
            notes.append(f"SUITE-NOTE: {script.name} leaked a process; its group was killed")
        return proc.returncode == 0, out, err, notes
    finally:
        _current_pgid = None


def _acquire_lock(path: Path):
    """Non-blocking exclusive flock; returns the open file (hold it) or None if another run has it."""
    fd = open(path, "a+")
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fd.seek(0)
        holder = fd.read().strip() or "unknown"
        print(f"run_all: another run holds {path} (PID {holder}); refusing to start a second one "
              "in this checkout.", file=sys.stderr)
        fd.close()
        return None
    fd.seek(0)
    fd.truncate()
    fd.write(f"{os.getpid()}\n")
    fd.flush()
    return fd


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--suite", action="append", default=[], metavar="PATH",
                    help="run only these suite scripts (repeatable); skips the seal gate")
    ap.add_argument("--lock", default=str(LOCK_FILE), metavar="PATH")
    args = ap.parse_args()

    spec = os.environ.get("PLAINKEEP_TEST_TIMEOUT")
    if spec:
        try:
            float(spec)
        except ValueError:
            print(f"run_all: PLAINKEEP_TEST_TIMEOUT={spec!r} is not a number of seconds", file=sys.stderr)
            return 2

    lock = _acquire_lock(Path(args.lock))
    if lock is None:
        return 2
    signal.signal(signal.SIGINT, _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)

    if args.suite:
        suites = [(Path(p).name, Path(p).resolve(), None) for p in args.suite]
    else:
        suites = [(label, HERE / script, (rest[0] if rest else None))
                  for label, script, *rest in SUITES]
        unsealed = _unsealed()
        if unsealed:
            print(f"{BOLD}{RED}NOT HERMETIC\033[0m — these suites never call lib.hermetic.seal(), so a "
                  "plainkeep invocation in them with no PLAINKEEP_HOME resolves to the developer's REAL "
                  "vault:")
            for name in unsealed:
                print(f"  - {name}")
            print("\nAdd, after the imports:\n"
                  f"    {SEAL_IMPORT}\n"
                  f"    {SEAL_CALL}   # hermetic: an empty throwaway registry, never the developer's "
                  "real vault\n")
            return 1

    print(f"\033[1m{'='*60}\nOffline design-validation suites\n{'='*60}\033[0m\n")
    statuses = []
    for label, script, spec_timeout in suites:
        ok, out, err, extra = _run_suite(script, _timeout_for(spec_timeout))
        sys.stdout.write(out)
        if err:
            sys.stdout.write(err)
        # Timeouts are FAIL lines; a leak note rides along like any SUITE-NOTE.
        tmo = [n for n in extra if n.startswith("TIMEOUT:")]
        notes = _suite_notes(out) + [n.split(SUITE_NOTE_PREFIX, 1)[1].strip()
                                     for n in extra if n.startswith(SUITE_NOTE_PREFIX)]
        statuses.append((label, ok, notes, tmo))
        print(f"\033[2m{'-'*60}\033[0m\n")

    print("\033[1mSUMMARY\033[0m")
    allok = True
    for label, ok, notes, tmo in statuses:
        allok = allok and ok
        mark = f"{GREEN}PASS\033[0m" if ok else f"{RED}FAIL\033[0m"
        print(f"  {mark}  {label}")
        for t in tmo:
            print(f"        {RED}{t}\033[0m")
        for note in notes:
            print(f"        \033[33m! {note}\033[0m")
    print()
    return 0 if allok else 1


if __name__ == "__main__":
    raise SystemExit(main())
