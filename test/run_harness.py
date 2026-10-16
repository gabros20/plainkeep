#!/usr/bin/env python3
"""
run_harness.py — meta-suite for the test harness itself (v2 0.14).

The harness promises that a run cannot pile up, hang, leak processes or read the developer's shell.
Those are promises about PROCESSES and ENVIRONMENT, so each case here is proved by what is (or is
not) left alive / written afterwards, never by an exit code alone:

  * hang      — a suite that sleeps forever is a FAIL naming the suite and elapsed seconds, within its
                timeout, and nothing in its process group survives.
  * leak      — a suite that exits 0 while a grandchild lives is flagged with a SUITE-NOTE and the
                grandchild is killed.
  * lock      — a second run_all on the same lock exits 2 naming the holder's PID; SIGTERM to the
                holder kills its suite's group and exits non-zero.
  * env       — run_search_impl passes with PLAINKEEP_VECTORS/PLAINKEEP_RERANK exported.
  * decoy     — the suites that mint a scratch vault leave an exported PLAINKEEP_HOME (here a decoy,
                never a real vault) byte-for-byte untouched.

run_all.py is driven over fixture suites through its `--suite PATH` / `--lock PATH` hooks; the outer
run owns the default lock, which is why the inner runs name their own.

Offline, stdlib only. Every subprocess has a timeout; nothing is left running.
"""
from __future__ import annotations
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from lib.hermetic import seal, ENV_SEALED_MARK
seal()   # hermetic: an empty throwaway registry, never the developer's real vault
from lib import vaultfx

HERE = Path(__file__).resolve().parent
RUN_ALL = HERE / "run_all.py"
GREEN, RED, DIM, BOLD, RESET = "\033[32m", "\033[31m", "\033[2m", "\033[1m", "\033[0m"
results: list[tuple[str, bool, str]] = []
spawned: list[subprocess.Popen] = []   # inner run_all processes, reaped in main()'s finally


def check(name: str, cond: bool, detail: str = "") -> None:
    results.append((name, bool(cond), detail))


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False


def wait_gone(check_fn, secs: float = 8.0) -> bool:
    end = time.monotonic() + secs
    while time.monotonic() < end:
        if not check_fn():
            return True
        time.sleep(0.1)
    return not check_fn()


def owner_env(**extra) -> dict:
    """The environment of a developer's shell: not sealed, engine knobs exported."""
    env = {**os.environ, **extra}
    env.pop(ENV_SEALED_MARK, None)
    return env


def inner(lock: Path, suites: list[Path], **env) -> subprocess.Popen:
    cmd = [sys.executable, str(RUN_ALL), "--lock", str(lock)]
    for s in suites:
        cmd += ["--suite", str(s)]
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                         env=owner_env(**env), start_new_session=True)
    spawned.append(p)
    return p


def snapshot(root: Path) -> list[tuple[str, int, int]]:
    out = []
    for p in sorted(root.rglob("*")):
        st = p.lstat()
        out.append((str(p.relative_to(root)), st.st_mtime_ns, st.st_size))
    return out


HANG = '''import os, sys
open(sys.argv[0] + ".pid", "w").write(str(os.getpgrp()))
import subprocess
g = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"])   # same group
open(sys.argv[0] + ".gpid", "w").write(str(g.pid))
import time
time.sleep(600)
'''
LEAK = '''import os, subprocess, sys
g = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"],
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
open(sys.argv[0] + ".gpid", "w").write(str(g.pid))
print("leaky suite done")
'''


def case_hang(td: Path) -> None:
    f = td / "hang_suite.py"
    f.write_text(HANG)
    t0 = time.monotonic()
    p = inner(td / "hang.lock", [f], PLAINKEEP_TEST_TIMEOUT="2")
    try:
        out, err = p.communicate(timeout=60)
    except subprocess.TimeoutExpired:
        os.killpg(p.pid, signal.SIGKILL)
        out, err = p.communicate()
        check("hang: run_all returned on its own", False, "run_all itself hung")
        return
    elapsed = time.monotonic() - t0
    pgid = int((td / "hang_suite.py.pid").read_text())
    gpid = int((td / "hang_suite.py.gpid").read_text())
    check("hang: suite that never returns is a FAIL", p.returncode == 1 and "FAIL" in out, f"rc={p.returncode}")
    check("hang: summary names the suite and the elapsed seconds",
          "TIMEOUT: hang_suite.py killed after 2." in out, out[-300:])
    check("hang: returned within timeout + kill grace", elapsed < 20, f"{elapsed:.1f}s")
    check("hang: whole process group is gone", wait_gone(lambda: group_alive(pgid)), f"pgid={pgid}")
    check("hang: grandchild is gone", wait_gone(lambda: alive(gpid)), f"pid={gpid}")


def case_leak(td: Path) -> None:
    f = td / "leak_suite.py"
    f.write_text(LEAK)
    p = inner(td / "leak.lock", [f], PLAINKEEP_TEST_TIMEOUT="30")
    out, err = p.communicate(timeout=60)
    gpid = int((td / "leak_suite.py.gpid").read_text())
    check("leak: a suite that exits 0 still PASSES", p.returncode == 0 and "PASS" in out, f"rc={p.returncode}")
    check("leak: SUITE-NOTE names the leaking suite", "leak_suite.py leaked a process" in out, out[-300:])
    check("leak: the leaked grandchild was killed", wait_gone(lambda: alive(gpid)), f"pid={gpid}")


def case_lock(td: Path) -> None:
    f = td / "lockhold_suite.py"      # its own name: case_hang's pid files must not satisfy the wait
    f.write_text(HANG)
    lock = td / "two.lock"
    a = inner(lock, [f], PLAINKEEP_TEST_TIMEOUT="120")
    pidfile = td / "lockhold_suite.py.pid"
    end = time.monotonic() + 20
    while time.monotonic() < end and not (pidfile.exists() and pidfile.read_text()):
        time.sleep(0.1)
    b = inner(lock, [f], PLAINKEEP_TEST_TIMEOUT="120")
    bout, berr = b.communicate(timeout=30)
    check("lock: a second concurrent run exits 2", b.returncode == 2, f"rc={b.returncode}")
    check("lock: it prints the holder's PID", str(a.pid) in berr, berr.strip())
    pgid = int(pidfile.read_text())
    a.send_signal(signal.SIGTERM)
    try:
        a.communicate(timeout=30)
    except subprocess.TimeoutExpired:
        os.killpg(a.pid, signal.SIGKILL)
        a.communicate()
    check("lock: SIGTERM to run_all exits non-zero", a.returncode not in (0, None) and a.returncode != 0,
          f"rc={a.returncode}")
    check("lock: SIGTERM killed the suite's process group", wait_gone(lambda: group_alive(pgid)),
          f"pgid={pgid}")
    c = inner(lock, [td / "leak_suite.py"], PLAINKEEP_TEST_TIMEOUT="30")
    c.communicate(timeout=60)
    check("lock: released when the holder dies (a new run starts)", c.returncode != 2, f"rc={c.returncode}")


def case_env() -> None:
    p = subprocess.run([sys.executable, str(HERE / "run_search_impl.py")], capture_output=True, text=True,
                       timeout=300, env=owner_env(PLAINKEEP_VECTORS="1", PLAINKEEP_RERANK="1",
                                                  PLAINKEEP_CONTENT="/nonexistent-content",
                                                  PLAINKEEP_ENRICH_MODEL="x", PLAINKEEP_AGENT_MODEL="x"))
    check("env: run_search_impl passes with VECTORS/RERANK/CONTENT/models exported",
          p.returncode == 0, (p.stdout + p.stderr)[-400:])


def case_decoy(td: Path) -> None:
    decoy = td / "decoy-vault"
    decoy.mkdir()
    vaultfx.mark_vault(decoy)
    before = snapshot(decoy)
    for suite in ("run_notetypes.py", "run_originals.py"):
        p = subprocess.run([sys.executable, str(HERE / suite)], capture_output=True, text=True, timeout=300,
                           env=owner_env(PLAINKEEP_HOME=str(decoy)))
        check(f"decoy: {suite} passes with PLAINKEEP_HOME exported", p.returncode == 0,
              (p.stdout + p.stderr)[-300:])
        after = snapshot(decoy)
        check(f"decoy: {suite} left the exported vault untouched", after == before,
              f"changed: {sorted(set(after) ^ set(before))[:5]}")


def main() -> int:
    try:
        with tempfile.TemporaryDirectory(prefix="pk-harness-") as t:
            td = Path(t)
            case_hang(td)
            case_leak(td)
            case_lock(td)
            case_env()
            case_decoy(td)
    finally:
        for p in spawned:       # an assertion that blew up must not leave a run_all (or its suite) alive
            if p.poll() is None:
                p.send_signal(signal.SIGTERM)
                try:
                    p.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    os.killpg(p.pid, signal.SIGKILL)
    passed = sum(1 for _, ok, _ in results if ok)
    failed = len(results) - passed
    for name, ok, detail in results:
        print(f"  {GREEN + 'PASS' if ok else RED + 'FAIL'}{RESET}  {name}"
              + (f"  {DIM}{detail}{RESET}" if (not ok and detail) else ""))
    print(f"\n{BOLD}Result:{RESET} {GREEN}{passed} passed{RESET}, "
          f"{(RED if failed else DIM)}{failed} failed{RESET}, {len(results)} checks")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
