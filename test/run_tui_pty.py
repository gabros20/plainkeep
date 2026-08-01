#!/usr/bin/env python3
"""
run_tui_pty.py — the automated gate for the TUI absorbed into the core binary (hybrid-core Phase 1,
Task 6). Drives a REAL terminal with Python's stdlib `pty` (run.md D10: no napi, so no node-pty) and
proves, end to end:

  * bare `plainkeep` on a terminal opens the TUI, and bare `plainkeep` WITHOUT one still prints help
    (the predicate must never widen — scripts and agents depend on the default verb);
  * `plainkeep ui` opens the same TUI;
  * re-entry self-execs the binary's own path (run.md D8): the TUI drives verbs with NO `plainkeep`
    on PATH at all, and an explicit $PLAINKEEP_BIN still overrides it;
  * ONE action is driven to completion: the note it writes lands in the vault, the terminal shows the
    result, and the gate appends EXACTLY ONE audit line for that verb — the "one door" property;
  * exit codes propagate through the interception.

WHAT THIS SUITE REFUSES TO DO, because a test that asserts agreement instead of the outcome hides
bugs: nothing here asserts merely "it did not crash". Every render check names a string the TUI
actually paints, and the action check reads the written note and the audit log rather than trusting
the exit code.

SYNCHRONIZATION: no fixed sleeps. Every wait is "read until this string appears, or fail with the
transcript" (Session.expect). Two things were measured the hard way and are load-bearing:
  * the pty's window size must be set BEFORE the fork, or the child renders ONE CHARACTER PER LINE
    (a zero-column terminal) and every needle silently fails to match;
  * a reaper must KEEP READING while it waits. A reaper that stops reading fills the pty buffer, the
    child blocks in write(), and the suite reports a hang that is entirely its own doing (measured:
    STILL-ALIVE at 6s with a non-reading reaper vs EXITED code=0 in 0.00s with a reading one).

Binary discovery mirrors run_core_parity.py: $PLAINKEEP_CORE_BIN else <repo>/.local/bin/plainkeep-core.
Absent (or not executable) => one LOUD SKIP line and exit 0, UNLESS PLAINKEEP_REQUIRE_CORE=1, in
which case it is an error and the suite exits 1. SKIP is visible, never a silent PASS. A pinned floor
run (PLAINKEEP_CORE=off) also SKIPs: the in-core TUI does not exist on the floor, where `plainkeep ui`
is served by bin/ui/run.py and the separately-built plainkeep-ui binary.
"""
from __future__ import annotations
import fcntl
import json
import os
import re
import select
import shutil
import signal
import struct
import subprocess
import sys
import tempfile
import termios
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
GREEN, RED, YELLOW, DIM, BOLD, RESET = (
    "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[1m", "\033[0m",
)

SKIP_LINE = "SKIP tui-pty: no core binary (build with: cd cli && bun run build)"
SKIP_FLOOR = "SKIP tui-pty: PLAINKEEP_CORE=off pins the bash floor — the in-core TUI is not present"

# Strings the TUI actually paints (cli/src/tui/app.ts). Stable, human-visible copy — if any of these
# changes, this suite SHOULD go red and be updated deliberately.
MENU = "What do you want to do?"
# The capture prompt's placeholder. It must be a string that appears ONLY on that prompt: the menu
# row for the same entry reads "＋ Capture a thought (quick note → inbox)", so the obvious needle
# "a thought" matches the MENU and the drive then types into a select that ignores it. Measured —
# the first version of this suite failed exactly that way.
CAPTURE_PLACEHOLDER = "(triage later)"
CAPTURED = "captured"                       # the spinner's stop message
SPINNER = "capturing"                       # the spinner's START message (the action is in flight)
OUTRO = "bye — everything you did went through"
NO_TTY_REFUSAL = "is an interactive terminal UI"
# app.ts's manifest-load failure message — the ordinary way `plainkeep ui` fails on a real vault.
NO_MANIFEST = "did not return a verb manifest"
HELP_MARKER = "the personal OS command surface"

ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07]*\x07|\x1b[()][A-B0-9]|\x1b[=>]")

results: list[tuple[str, bool, str]] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    results.append((name, bool(cond), detail))


class Timeout(Exception):
    pass


class Session:
    """One TUI process on its own pty, driven pexpect-style.

    `expect` searches only FORWARD from the last match. That matters more than it looks: clack
    repaints the whole frame on every keystroke, so the menu string is in the transcript dozens of
    times, and a whole-buffer search would match a stale frame and "pass" a step that never happened.
    """

    def __init__(self, argv: list[str], env: dict, cwd: Path, rows: int = 40, cols: int = 120):
        master, slave = os.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
        pid = os.fork()
        if pid == 0:                                    # child
            os.setsid()
            fcntl.ioctl(slave, termios.TIOCSCTTY, 0)
            for target in (0, 1, 2):
                os.dup2(slave, target)
            if slave > 2:
                os.close(slave)
            os.close(master)
            os.chdir(str(cwd))
            try:
                os.execve(argv[0], argv, env)
            except Exception:
                os._exit(127)
        os.close(slave)
        self.pid = pid
        self.fd = master
        self.raw = ""
        self.pos = 0
        self.closed = False

    # -- reading -------------------------------------------------------------------------------
    def _pump(self, timeout: float) -> bool:
        r, _, _ = select.select([self.fd], [], [], timeout)
        if not r:
            return False
        try:
            chunk = os.read(self.fd, 65536)
        except OSError:                                 # EIO: the child closed the slave side
            self.closed = True
            return False
        if not chunk:
            self.closed = True
            return False
        self.raw += chunk.decode("utf-8", "replace")
        return True

    @property
    def clean(self) -> str:
        return ANSI.sub("", self.raw)

    def expect(self, needle: str, timeout: float = 90.0) -> None:
        """Read until `needle` appears after the previous match. Raises Timeout with the transcript."""
        end = time.time() + timeout
        while True:
            idx = self.clean.find(needle, self.pos)
            if idx >= 0:
                self.pos = idx + len(needle)
                return
            if time.time() >= end or self.closed:
                tail = self.clean[-800:]
                raise Timeout(f"never saw {needle!r} (pos={self.pos}); tail={tail!r}")
            self._pump(0.2)

    def send(self, data: bytes) -> None:
        os.write(self.fd, data)

    # -- finishing -----------------------------------------------------------------------------
    def wait(self, timeout: float = 30.0) -> tuple[str, int]:
        """Reap the child while STILL READING the pty. Returns ("exit"|"signal", n)."""
        end = time.time() + timeout
        while time.time() < end:
            self._pump(0.1)
            pid, st = os.waitpid(self.pid, os.WNOHANG)
            if pid == self.pid:
                if os.WIFSIGNALED(st):
                    return ("signal", os.WTERMSIG(st))
                return ("exit", os.WEXITSTATUS(st))
        return ("alive", -1)

    def kill(self) -> None:
        try:
            os.kill(self.pid, signal.SIGKILL)
            os.waitpid(self.pid, 0)
        except (OSError, ChildProcessError):
            pass
        try:
            os.close(self.fd)
        except OSError:
            pass


# --------------------------------------------------------------------------------------------------
# Fixture vault: a copy of the working tree, so the dispatcher finds bin/ under the test
# PLAINKEEP_HOME (the same shape run_mcp.py uses) and nothing touches the real vault.
# --------------------------------------------------------------------------------------------------

def build_vault(td: str) -> Path:
    vault = Path(td) / "plainkeep"
    shutil.copytree(REPO, vault, ignore=shutil.ignore_patterns(
        ".git", ".index", ".logs", "__pycache__", "*.pyc", "node_modules"))
    return vault


def vault_env(vault: Path, binary: str, **extra) -> dict:
    env = {
        **os.environ,
        "PLAINKEEP_HOME": str(vault),
        "PLAINKEEP_CORE": "require",         # this suite is about the core; never silently the floor
        "PLAINKEEP_CORE_BIN": binary,
        "TERM": "xterm-256color",
        "HOME": str(vault / "_home"),
    }
    # PATH WITHOUT any `plainkeep`. The self-exec property (run.md D8) is only proven if a PATH
    # lookup could not have worked: measured, a PATH-resolving TUI in this environment fails to load
    # the manifest at all. Anything the engine's verbs need (python3, git) still resolves.
    env["PATH"] = "/usr/bin:/bin:/usr/sbin:/sbin"
    env.pop("PLAINKEEP_BIN", None)
    env.pop("PLAINKEEP_UI_BIN", None)
    env.pop("PLAINKEEP_REQUIRE_CORE", None)
    env.update(extra)
    return env


def gate_lines(vault: Path) -> list[list[str]]:
    p = vault / ".logs" / "plainkeep.log"
    if not p.exists():
        return []
    return [ln.split("\t") for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()]


def lines_for_verb(vault: Path, verb: str) -> list[list[str]]:
    """Audit lines whose VERB field is exactly `verb`. Field 1 is "<verb> <args joined by spaces>"
    (guardrail.ts log()), so the verb is its first whitespace-delimited token."""
    out = []
    for f in gate_lines(vault):
        if len(f) >= 4 and f[1].split(" ")[0] == verb:
            out.append(f)
    return out


# --------------------------------------------------------------------------------------------------
# The checks
# --------------------------------------------------------------------------------------------------

def check_renders(binary: str, label: str, argv_tail: list[str]) -> None:
    """bare `plainkeep` / `plainkeep ui` on a terminal renders the menu, and exits 0 when cancelled."""
    with tempfile.TemporaryDirectory() as td:
        vault = build_vault(td)
        s = Session([str(vault / "plainkeep"), *argv_tail], vault_env(vault, binary), vault)
        try:
            s.expect(MENU)
            check(f"{label}: renders the TUI menu on a pty", True)
            # Ctrl-C at a clack prompt is the BYTE 0x03, not a signal: the prompt puts the tty in raw
            # mode, so ISIG is off. clack turns it into a cancel, the loop breaks, and the outro runs.
            s.send(b"\x03")
            s.expect(OUTRO)
            kind, code = s.wait()
            check(f"{label}: cancelling exits 0 through the interception", (kind, code) == ("exit", 0),
                  f"{kind}={code}")
            # The verb that RAN is the verb the audit log must name.
            ui_lines = lines_for_verb(vault, "ui")
            check(f"{label}: appends exactly ONE `ui` audit line, allowed as read-class",
                  len(ui_lines) == 1 and ui_lines[0][2:4] == ["allow", "read"],
                  f"{ui_lines!r}")
        except Timeout as e:
            check(f"{label}: renders the TUI menu on a pty", False, str(e))
        finally:
            s.kill()


def check_non_tty_bare(binary: str) -> None:
    """The predicate must NOT widen: with no terminal, bare `plainkeep` is still the default verb."""
    with tempfile.TemporaryDirectory() as td:
        vault = build_vault(td)
        env = vault_env(vault, binary)
        p = subprocess.run([str(vault / "plainkeep")], capture_output=True, text=True, env=env)
        check("non-tty bare `plainkeep` prints help, not the TUI",
              p.returncode == 0 and HELP_MARKER in p.stdout and MENU not in p.stdout,
              f"rc={p.returncode} head={p.stdout[:120]!r}")
        # Positive control on the log: the verb that ran was `help`, and `ui` never was.
        check("non-tty bare `plainkeep` logs `help`, never `ui`",
              len(lines_for_verb(vault, "help")) == 1 and lines_for_verb(vault, "ui") == [],
              f"help={lines_for_verb(vault, 'help')!r} ui={lines_for_verb(vault, 'ui')!r}")


def check_non_tty_ui_exit_code(binary: str) -> None:
    """A NON-zero code from the interception must reach the shell unchanged."""
    with tempfile.TemporaryDirectory() as td:
        vault = build_vault(td)
        env = vault_env(vault, binary)
        p = subprocess.run([str(vault / "plainkeep"), "ui"], capture_output=True, text=True, env=env)
        check("`plainkeep ui` with no terminal refuses with exit 2 (code propagates)",
              p.returncode == 2 and NO_TTY_REFUSAL in (p.stdout + p.stderr),
              f"rc={p.returncode} err={p.stderr[:120]!r}")


def check_version_probe(binary: str) -> None:
    """`plainkeep ui --version` is answered headlessly — the setup layer's staleness probe shape."""
    with tempfile.TemporaryDirectory() as td:
        vault = build_vault(td)
        env = vault_env(vault, binary)
        expected = (REPO / "bin" / "ui" / "version.txt").read_text().strip()
        p = subprocess.run([str(vault / "plainkeep"), "ui", "--version"],
                           capture_output=True, text=True, env=env)
        check("`plainkeep ui --version` answers headlessly with the engine-pinned version",
              p.returncode == 0 and p.stdout.strip() == expected,
              f"rc={p.returncode} out={p.stdout.strip()!r} want={expected!r}")


def check_plainkeep_bin_override(binary: str) -> None:
    """$PLAINKEEP_BIN wins over the self-exec path — proven by a tracer that RECORDS the re-entry."""
    with tempfile.TemporaryDirectory() as td:
        vault = build_vault(td)
        marker = Path(td) / "tracer.log"
        tracer = Path(td) / "plainkeep-tracer"
        # Records each invocation, then forwards to the real dispatcher so the TUI still works.
        tracer.write_text(
            f'#!/bin/sh\nprintf \'%s\\n\' "$*" >> "{marker}"\nexec "{vault / "plainkeep"}" "$@"\n',
            encoding="utf-8")
        tracer.chmod(0o755)
        env = vault_env(vault, binary, PLAINKEEP_BIN=str(tracer))
        s = Session([str(vault / "plainkeep")], env, vault)
        try:
            s.expect(MENU)
            s.send(b"\x03")
            s.expect(OUTRO)
            s.wait()
        except Timeout as e:
            check("$PLAINKEEP_BIN override: the TUI re-enters through it", False, str(e))
            return
        finally:
            s.kill()
        seen = marker.read_text(encoding="utf-8").splitlines() if marker.exists() else []
        # The manifest bootstrap (`plainkeep help --json`) is the TUI's first re-entry.
        check("$PLAINKEEP_BIN override: the TUI re-enters through it, not through execPath",
              any(ln.startswith("help") for ln in seen), f"tracer saw {seen!r}")


def check_action_end_to_end(binary: str) -> None:
    """The load-bearing check: drive ONE action to completion and prove it really ran and was gated.

    Quick capture is chosen because it is the FIRST menu entry (so the drive needs no navigation,
    which would make the test depend on menu ORDER) and it writes a file this suite can read back.
    """
    with tempfile.TemporaryDirectory() as td:
        vault = build_vault(td)
        s = Session([str(vault / "plainkeep")], vault_env(vault, binary), vault)
        text = "pty probe thought"
        try:
            s.expect(MENU)
            s.send(b"\r")                                # first option: ＋ Capture a thought
            s.expect(CAPTURE_PLACEHOLDER)
            s.send(text.encode() + b"\r")
            s.expect(CAPTURED)                           # the spinner stopped: the verb returned
            s.expect(MENU)                               # and the loop came back for more
            s.send(b"\x03")
            s.expect(OUTRO)
            kind, code = s.wait()
        except Timeout as e:
            check("action: quick capture runs to completion in the TUI", False, str(e))
            s.kill()
            return
        finally:
            s.kill()

        check("action: the TUI session exits 0 after the action", (kind, code) == ("exit", 0),
              f"{kind}={code}")

        # 1. The action REALLY RAN: capture writes inbox/cap-*.md containing the text.
        notes = sorted((vault / "inbox").glob("cap-*.md"))
        bodies = [n.read_text(encoding="utf-8") for n in notes]
        check("action: the verb really ran — the captured note is in the vault",
              len(notes) == 1 and any(text in b for b in bodies),
              f"{[n.name for n in notes]!r}")

        # 2. It went through THE ONE DOOR: exactly one gate line, for `capture`, allowed.
        cap = lines_for_verb(vault, "capture")
        check("action: EXACTLY ONE gate audit line for the verb the action ran",
              len(cap) == 1, f"{cap!r}")
        check("action: that line records the real argv and the allow verdict",
              len(cap) == 1 and cap[0][1] == f"capture {text} --json"
              and cap[0][2:4] == ["allow", "safe_write"],
              f"{cap!r}")

        # 3. The re-entry was the TUI's, not a stray: `ui` itself is logged exactly once, and the
        #    manifest bootstrap `help --json` is there too — the whole session through one gate.
        check("action: the session logged `ui` once and bootstrapped through `help --json`",
              len(lines_for_verb(vault, "ui")) == 1
              and any(f[1] == "help --json" for f in lines_for_verb(vault, "help")),
              f"{[f[1] for f in gate_lines(vault)]!r}")


def check_signal_disposition(binary: str) -> None:
    """PIN the signal behavior decision 2 rests on, because that decision is delegated entirely to a
    DEPENDENCY and no comment can keep it true.

    `@clack/prompts` 0.7.0 registers SIGINT/SIGTERM listeners in every spinner() and never removes
    them (zero removeListener calls in the package), which is what makes plainkeep-core survive both
    signals for the rest of a session once any action has run. That is a leak, not a design — exactly
    the sort of thing a version bump fixes — so these three rows exist to go RED the day it changes
    rather than letting the behavior drift silently.

    Costs no crash-report noise: SIGINT and SIGTERM are not fault signals, so unlike the parity
    suite's signal matrix these produce no macOS crash reports.
    """
    def session(vault: Path) -> Session:
        return Session([str(vault / "plainkeep")], vault_env(vault, binary), vault)

    def run_to_menu_then(after_action: bool, stimulus: int | None) -> tuple[str, int] | None:
        with tempfile.TemporaryDirectory() as td:
            vault = build_vault(td)
            s = session(vault)
            try:
                s.expect(MENU)
                if after_action:
                    s.send(b"\r")
                    s.expect(CAPTURE_PLACEHOLDER)
                    s.send(b"signal probe\r")
                    s.expect(CAPTURED)
                    s.expect(MENU)          # a spinner has now existed in this process
                if stimulus is not None:
                    os.kill(s.pid, stimulus)
                return s.wait(timeout=8.0)
            except Timeout:
                return None
            finally:
                s.kill()

    # ROW 1 — before any spinner exists there is no listener, so bun's default disposition applies
    # and the process dies by the signal (WIFSIGNALED, subprocess returncode -2). This is the row
    # that makes `plainkeep ui` behave like every other verb under Ctrl-C.
    got = run_to_menu_then(False, signal.SIGINT)
    check("signal: SIGINT at the menu (no spinner yet) KILLS the process by SIGINT",
          got == ("signal", int(signal.SIGINT)), f"{got!r}")

    # ROW 3 — after one action, clack's leaked SIGINT listener has removed the default disposition.
    got = run_to_menu_then(True, signal.SIGINT)
    check("signal: SIGINT after one action is SWALLOWED (clack's leaked listener)",
          got == ("alive", -1), f"{got!r}")

    # ROW 3b — and so is SIGTERM, which is the operationally interesting one: plainkeep-core cannot
    # be stopped by a supervisor or a plain `kill` for the rest of the session, only by SIGKILL.
    # Disclosed rather than fixed (floor parity: the same clack runs inside plainkeep-ui).
    got = run_to_menu_then(True, signal.SIGTERM)
    check("signal: SIGTERM after one action is SWALLOWED TOO — only SIGKILL ends the session",
          got == ("alive", -1), f"{got!r}")


def check_off_protocol_never_escapes(binary: str) -> None:
    """A TUI that cannot load the manifest must not reach the shell with exit 1.

    cli/src/tui/app.ts returns 1 when `plainkeep help --json` gives it no verb manifest, and 1 is off
    the frozen protocol (0/2/3/4/5). Driven here through the REAL failure the user hits — a
    $PLAINKEEP_BIN that exists and is executable but is not a dispatcher — rather than by injecting a
    return value, so it stays true regardless of which internal path produces the failure.
    """
    with tempfile.TemporaryDirectory() as td:
        vault = build_vault(td)
        env = vault_env(vault, binary, PLAINKEEP_BIN="/bin/echo")
        s = Session([str(vault / "plainkeep")], env, vault)
        try:
            s.expect(NO_MANIFEST)
            kind, code = s.wait(timeout=20.0)
        except Timeout as e:
            check("off-protocol: a manifest failure exits on the frozen protocol, never 1", False, str(e))
            s.kill()
            return
        finally:
            s.kill()
        check("off-protocol: a manifest failure exits on the frozen protocol, never 1",
              (kind, code) == ("exit", 5), f"{kind}={code} (1 would be off-protocol)")
        # The gate still ran and recorded the verb — a failing interception must not suppress the log.
        check("off-protocol: the `ui` audit line is written even when the TUI fails",
              len(lines_for_verb(vault, "ui")) == 1, f"{lines_for_verb(vault, 'ui')!r}")


def check_cancel_during_action(binary: str) -> None:
    """Ctrl-C typed WHILE AN ACTION IS RUNNING is a deliberate quit, and must exit 0 like the floor.

    This is the path @clack/core's block() owns: while a spinner is up it installs a keypress handler
    that calls process.exit(0) directly, so the interception never resumes. interception.ts guards
    that window, and the guard has to tell a deliberate quit apart from an anomaly — 0 stays 0, an
    off-protocol code becomes 5.

    Pinned HERE rather than argued in a comment because "0 means the user quit" is a property of the
    DEPENDENCY SET (measured: the shipped bundle's only in-window process.exit is clack's, and it
    passes 0), and a version bump can change it. If some future dependency exits 0 on an error path,
    this row is what will have to be revisited.
    """
    with tempfile.TemporaryDirectory() as td:
        vault = build_vault(td)
        # Make the action slow so the spinner is reliably still up when the 0x03 byte lands. Without
        # this the capture finishes first and the test would be driving the menu instead.
        runpy = vault / "bin" / "capture" / "run.py"
        runpy.write_text("import time\ntime.sleep(8)\n" + runpy.read_text(), encoding="utf-8")
        s = Session([str(vault / "plainkeep")], vault_env(vault, binary), vault)
        try:
            s.expect(MENU)
            s.send(b"\r")
            s.expect(CAPTURE_PLACEHOLDER)
            s.send(b"cancelled capture\r")
            s.expect(SPINNER)                    # the action is in flight
            s.send(b"\x03")                      # deliberate quit, mid-action
            kind, code = s.wait(timeout=20.0)
        except Timeout as e:
            check("cancel: Ctrl-C during a running action exits 0 (floor parity)", False, str(e))
            s.kill()
            return
        finally:
            s.kill()
        check("cancel: Ctrl-C during a running action exits 0 (floor parity, not an anomaly)",
              (kind, code) == ("exit", 0), f"{kind}={code}")
        # A deliberate quit is not an event worth narrating: the guard must stay silent here. If it
        # ever starts reporting, the user gets a scary line for having pressed Ctrl-C.
        check("cancel: the guard says nothing about a deliberate quit",
              "ended early" not in s.clean, s.clean[-200:])
        # ...and clack still renders its own "Canceled", not the "Something went wrong" it prints for
        # any code > 1. That string is the user-visible half of this decision.
        check("cancel: clack still renders 'Canceled', not 'Something went wrong'",
              "Canceled" in s.clean and "Something went wrong" not in s.clean, s.clean[-200:])


def _discover_binary() -> str | None:
    cand = os.environ.get("PLAINKEEP_CORE_BIN") or str(REPO / ".local" / "bin" / "plainkeep-core")
    p = Path(cand)
    if p.is_file() and os.access(cand, os.X_OK):
        return cand
    return None


def main() -> int:
    # A pinned floor run has no in-core TUI to test. This is a deliberate operator choice rather than
    # a missing artifact, so it is a plain SKIP even under PLAINKEEP_REQUIRE_CORE=1 (which is about
    # the BINARY being absent). The floor's own `plainkeep ui` path — bin/ui/run.py execing the
    # separately-built plainkeep-ui — is untouched by Task 6.
    if os.environ.get("PLAINKEEP_CORE") == "off":
        print(f"{YELLOW}{BOLD}{SKIP_FLOOR}{RESET}")
        return 0

    binary = _discover_binary()
    if binary is None:
        if os.environ.get("PLAINKEEP_REQUIRE_CORE") == "1":
            print(f"{RED}{BOLD}{SKIP_LINE}{RESET}", file=sys.stderr)
            print(f"{RED}PLAINKEEP_REQUIRE_CORE=1 — a missing core binary is a FAILURE.{RESET}",
                  file=sys.stderr)
            return 1
        print(f"{YELLOW}{BOLD}{SKIP_LINE}{RESET}")
        return 0

    # Preflight: a binary built before Task 6 has no `ui` interception, and every check below would
    # then fail with a confusing render timeout instead of the real reason. Ask the binary directly —
    # the same `--core-api intercepts` data run_core_parity.py reads — and say so plainly.
    probe = subprocess.run([binary, "--core-api", "intercepts"], capture_output=True, text=True)
    try:
        buckets = json.loads(probe.stdout)["verbs"]
    except Exception:
        buckets = {}
    if "ui" not in buckets.get("noncomparable", []):
        print(f"{RED}{BOLD}tui-pty: this binary does not register `ui` as a NON-comparable "
              f"interception (rebuild it: cd cli && bun run build).{RESET}", file=sys.stderr)
        print(f"{RED}  --core-api intercepts verbs = {buckets!r}{RESET}", file=sys.stderr)
        return 1

    check_renders(binary, "bare `plainkeep` on a tty", [])
    check_renders(binary, "`plainkeep ui`", ["ui"])
    check_non_tty_bare(binary)
    check_non_tty_ui_exit_code(binary)
    check_version_probe(binary)
    check_plainkeep_bin_override(binary)
    check_action_end_to_end(binary)
    check_off_protocol_never_escapes(binary)
    check_signal_disposition(binary)
    check_cancel_during_action(binary)

    print(f"{BOLD}TUI pty gate — the in-core terminal UI (Task 6) — {len(results)} checks "
          f"(binary: {binary}){RESET}\n")
    passed = sum(1 for _, ok, _ in results if ok)
    for name, ok, detail in results:
        mark = f"{GREEN}PASS{RESET}" if ok else f"{RED}FAIL{RESET}"
        print(f"  {mark} {name}" + (f"\n       {DIM}{detail}{RESET}" if (detail and not ok) else ""))
    failed = len(results) - passed
    print(f"\n{BOLD}Result:{RESET} {GREEN}{passed} passed{RESET}, "
          f"{(RED if failed else DIM)}{failed} failed{RESET}, {len(results)} checks")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
