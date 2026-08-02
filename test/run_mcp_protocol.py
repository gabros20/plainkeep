#!/usr/bin/env python3
"""
run_mcp_protocol.py — the protocol-level gate for `plainkeep mcp` served IN-CORE (hybrid-core Phase 1,
Task 7), and the differential that proves the in-core server and bin/mcp/run.py are the same server.

WHY A SECOND MCP SUITE. test/run_mcp.py drives ONE session of happy paths and is deliberately left
untouched (it must stay green in both modes, which is a property this task cannot be allowed to
"fix"). What it cannot see is everything that makes an stdio server hard: what happens to the frame
stream under a malformed line, a notification, a signal, a peer that stops reading, a pack that
appears mid-session, or a byte written to stdout that is not a frame. That is this file.

THE ORACLE IS BYTES, NOT SHAPES. The in-core server reproduces CPython's `json.dumps` spacing
(`", "` / `": "`), so a whole session's stdout can be compared BYTE FOR BYTE between
`PLAINKEEP_CORE=off` (bin/mcp/run.py) and the core binary. That is a much stronger oracle than
comparing parsed objects, which cannot see an encoding, ordering or whitespace difference at all.

AND BYTES ARE NOT ENOUGH BY THEMSELVES, which is the lesson this run has paid for three times: two
sides agreeing proves only that they agree. So every differential case ALSO asserts the real outcome
— that the frame parsed, that the tool list contains the verbs the vault actually has, that the verb
really ran (its own stdout, and the audit line the gate appended for it), that the confirm-class call
was refused rather than executed. A case that only compared the two modes would pass just as happily
if both were broken.

WHAT IS COMPARED AND WHAT IS NOT. Every case runs in BOTH modes. Everything in DIFFERENTIAL is
byte-compared as well as asserted. Three things deliberately are NOT byte-compared, and say why at
the case:
  * SIGTERM, idle and mid-call — the two modes are DESIGNED to differ here (the floor takes CPython's
    default disposition and dies by signal, possibly mid-frame; the core shuts down gracefully). Both
    measured behaviours are asserted, so a change on EITHER side reddens a named check.
  * the second-signal escape hatch — it exists only in the core; the floor has no drain to abandon.
  * the exit CODE of a session that ends abnormally. The frames still match byte for byte, but the
    floor's status is whatever CPython produces (1 for an uncaught traceback, 120 when the failure is
    a stdout flush during interpreter shutdown) and the core's is clamped onto the frozen protocol.

Binary discovery mirrors run_core_parity.py / run_tui_pty.py: $PLAINKEEP_CORE_BIN else
<repo>/.local/bin/plainkeep-core. Absent (or not executable) => one LOUD SKIP line and exit 0, UNLESS
PLAINKEEP_REQUIRE_CORE=1, in which case it is an error and the suite exits 1. A pinned floor run
(PLAINKEEP_CORE=off) also SKIPs: there is no in-core server to differentiate against.
"""
from __future__ import annotations
import json
import os
import select
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
GREEN, RED, YELLOW, DIM, BOLD, RESET = (
    "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[1m", "\033[0m",
)

SKIP_LINE = "SKIP mcp-protocol: no core binary (build with: cd cli && bun run build)"
SKIP_FLOOR = ("SKIP mcp-protocol: PLAINKEEP_CORE=off pins the bash floor — there is no in-core MCP "
              "server to differentiate against")

results: list[tuple[str, bool, str]] = []
# EVERY session ever constructed, registered at construction rather than at teardown. The
# stdout-hygiene check (the analogue of run_tui_pty.py's render assertions, for a protocol stream)
# reads their accumulated bytes at the end.
#
# Registered at CONSTRUCTION for a reason a negative control found: an earlier version appended in
# finish(), so a case that threw — which is precisely what a broken server causes — never contributed
# its bytes, and the hygiene check passed over 2 sessions while 12 other checks failed
# (.orchestrate/raw/task7-broken-stdout-hygiene.log, first run). A corpus that shrinks when things go
# wrong is not a corpus.
sessions: list["Session"] = []


def check(name: str, cond: object, detail: str = "") -> None:
    results.append((name, bool(cond), str(detail)))


class Timeout(Exception):
    pass


# --------------------------------------------------------------------------------------------------
# one server process, driven frame by frame
# --------------------------------------------------------------------------------------------------
class Session:
    """A `plainkeep mcp` process on pipes, driven one line at a time.

    Reading is non-blocking with an explicit deadline rather than `readline()`: half these cases are
    ABOUT the server not answering (a notification, a signalled shutdown, a stopped read), and a
    blocking readline turns "correctly silent" into a hung suite. stderr goes to a FILE, never a pipe
    — a pipe nobody drains is a deadlock the moment a traceback exceeds the buffer, and the floor
    writes tracebacks on exactly the cases this suite provokes.
    """

    def __init__(self, label: str, ops: Path, env: dict, tmp: Path):
        self.label = label
        self.errfile = tmp / f"stderr-{label.replace('/', '_').replace(' ', '_')}-{os.getpid()}-{len(sessions)}.log"
        self.errfh = open(self.errfile, "wb")
        self.proc = subprocess.Popen(
            [str(ops), "mcp"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=self.errfh, env=env,
        )
        self.fd = self.proc.stdout.fileno()
        os.set_blocking(self.fd, False)
        self.buf = b""
        self.raw = b""
        self.eof = False
        sessions.append(self)

    # -- writing ---------------------------------------------------------------------------------
    def send(self, obj: dict) -> None:
        self.send_raw(json.dumps(obj) + "\n")

    def send_raw(self, text: str) -> None:
        try:
            self.proc.stdin.write(text.encode("utf-8"))
            self.proc.stdin.flush()
        except (BrokenPipeError, ValueError):
            pass    # the server already ended the session; the case asserts that, not this write

    def close_stdin(self) -> None:
        try:
            self.proc.stdin.close()
        except (BrokenPipeError, ValueError):
            pass

    # -- reading ---------------------------------------------------------------------------------
    def _pump(self, timeout: float) -> bool:
        """Read whatever is available within `timeout`. False if stdout hit EOF."""
        if self.eof:
            return False
        r, _, _ = select.select([self.fd], [], [], timeout)
        if not r:
            return True
        chunk = os.read(self.fd, 1 << 16)
        if not chunk:
            self.eof = True
            return False
        self.buf += chunk
        self.raw += chunk
        return True

    def readline(self, timeout: float = 30.0) -> bytes:
        deadline = time.time() + timeout
        while b"\n" not in self.buf:
            left = deadline - time.time()
            if left <= 0:
                raise Timeout(f"{self.label}: no frame within {timeout}s (buffered: {self.buf[:80]!r})")
            if not self._pump(min(0.25, left)) and b"\n" not in self.buf:
                raise Timeout(f"{self.label}: stdout closed with no frame (buffered: {self.buf[:80]!r})")
        line, self.buf = self.buf.split(b"\n", 1)
        return line

    def frame(self, timeout: float = 30.0) -> dict:
        return json.loads(self.readline(timeout))

    def silent_for(self, seconds: float) -> bool:
        """True if NOTHING arrived on stdout for `seconds` — how a notification is proven silent."""
        deadline = time.time() + seconds
        while time.time() < deadline:
            self._pump(min(0.1, max(0.0, deadline - time.time())))
            if b"\n" in self.buf:
                return False
        return b"\n" not in self.buf

    def alive(self) -> bool:
        return self.proc.poll() is None

    def wait_writing(self, timeout: float = 30.0) -> bool:
        """Block until the server has STARTED writing a reply — the first byte is readable — without
        consuming it. False if it never did.

        This exists because "the reply is in flight" used to be a `time.sleep(0.05)`, and a sleep is
        an assumption about how long a tool call takes. ADR-014 Task 1b made every dispatch spawn one
        more process (root discovery, ~23 ms measured), which pushed the child PAST the 50 ms window
        — so the SIGTERM landed while the child was still running, the core killed it as designed,
        and the case saw `exit -15` instead of the frame. `select()` on the descriptor answers the
        question the case is actually asking, and it answers it by measurement."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            # `self.buf`, never `self.raw`: raw accumulates every byte the session has EVER read
            # (the initialize frame included), so testing it would answer True instantly and
            # reproduce the sleep this replaced.
            if self.buf or select.select([self.fd], [], [], 0.05)[0]:
                return True
            if self.proc.poll() is not None:
                return False
        return False

    # -- teardown --------------------------------------------------------------------------------
    def finish(self, timeout: float = 30.0, close: bool = True) -> int:
        if close:
            self.close_stdin()
        deadline = time.time() + timeout
        while time.time() < deadline:
            # `self.eof` also covers the case where the TEST closed the read end (the EPIPE case), so
            # the fd must never be select()ed again — it is not merely at EOF, it is gone.
            if self.eof:
                if self.proc.poll() is not None:
                    break
                time.sleep(0.05)
                continue
            if not self._pump(0.1) and self.proc.poll() is not None:
                break
            if self.proc.poll() is not None and not select.select([self.fd], [], [], 0)[0]:
                break
        try:
            rc = self.proc.wait(timeout=max(1.0, deadline - time.time()))
        except subprocess.TimeoutExpired:
            self.proc.kill()
            rc = self.proc.wait()
            raise Timeout(f"{self.label}: server did not exit within {timeout}s")
        finally:
            self.errfh.close()
        return rc

    def wait_without_reading(self, timeout: float = 20.0) -> int:
        """Wait for exit WITHOUT touching stdout. The escape-hatch case needs this: `finish()` pumps
        stdout, and a pumping reader is exactly what unblocks the drain the case is trying to prove
        the server can be pulled out of. (Measured: with finish() the server completed the frame and
        exited 0 — a green test of nothing.)"""
        try:
            rc = self.proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait()
            raise Timeout(f"{self.label}: server did not exit within {timeout}s")
        finally:
            self.errfh.close()
        return rc

    def stderr_text(self) -> str:
        try:
            return self.errfile.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""


# --------------------------------------------------------------------------------------------------
# the fixture vault + the two modes
# --------------------------------------------------------------------------------------------------
IGNORE = shutil.ignore_patterns(".git", ".index", ".logs", "__pycache__", "*.pyc",
                                "node_modules", ".local", ".venv")

# A pack root the vault does not ship, used by the cases that need a verb whose behaviour this suite
# controls: `echoargs` prints the argv it received (so argument ORDER is observable end to end), and
# `bigout` prints a payload far larger than a pipe buffer (so backpressure is observable at all).
BIG_BYTES = 300_000
# Past 1.25 MiB, the exact size at which the pre-fix core truncated a tool result (quality r1, Q1).
HUGE_BYTES = 3_000_000

# Non-ASCII, spread across every place a byte can reach a frame: a cmd.json summary and hints (→ the
# tool DESCRIPTION), a tool ARGUMENT, a verb's own RESULT, and a note body reached through `search`.
# The suite was previously pure ASCII end to end (the r1 spec review, F2), which meant the property
# the whole byte-oracle rests on — that both sides emit raw UTF-8, never \uXXXX — was never exercised
# and could have regressed silently. U+00A0 is in there deliberately: it is in Python's str.strip()
# set and not in JS's, so it also exercises pyStrip on the captured child streams.
UNI_SUMMARY = "tesztige\u0301 — ✅ 日本語 «guillemets» \u00a0nbsp"
UNI_HINTS = "hint: naïve café 😀"
UNI_ARG = "Árvíztűrő tükörfúrógép ✅ 日本語 😀"
UNI_BLOB = "blob: Árvíztűrő ✅ 日本語 😀 «x» \u00a0y"
UNI_NOTE_TITLE = "Kávé jegyzet — árvíztűrő ✅ 日本語 😀"

# The r1 spec review's EXACT shape for the key-order divergence, plus a JSON-NUMBER name for the
# sibling path through dictKey(). ECMAScript hoists integer-index keys to the front of a plain
# object; CPython preserves insertion order. The declared order below is the answer both modes must
# give.
KEYORDER_NAMES = ["alpha", "0", "zeta", "2", 5]
KEYORDER_EXPECTED = ["alpha", "0", "zeta", "2", "5", "args"]

PACK_VERBS = {
    "keyorder": (
        {"verb": "keyorder", "summary": "test verb: integer-like arg names", "usage": "x",
         "risk": "read", "args": [{"name": n} for n in KEYORDER_NAMES], "reads": [], "writes": []},
        "print('{}')\n",
    ),
    # A cmd.json whose `hints` is an OBJECT with integer-like keys. This one is expected to DIVERGE:
    # JSON.parse has already reordered it before mcp.ts sees it, so no serializer can recover the
    # order. Pinned so the divergence has a named cause instead of surfacing as a mystery byte.
    "parserorder": (
        {"verb": "parserorder", "summary": "", "usage": "x", "risk": "read",
         "hints": {"2": "b", "1": "a"}, "reads": [], "writes": []},
        "print('{}')\n",
    ),
    # Well past the pipe buffer AND past the 1.25 MiB at which the pre-fix core truncated. `bigout`
    # (300 KB) sat comfortably under that, which is exactly why the whole suite was green while a
    # tool result could be silently cut in half.
    "hugeout": (
        {"verb": "hugeout", "summary": "test verb: emit a result far past the truncation threshold",
         "usage": "plainkeep hugeout", "risk": "read", "reads": [], "writes": []},
        "import json\n"
        f"print(json.dumps({{'ops_json': 1, 'ok': True, 'verb': 'hugeout', 'blob': 'h' * {HUGE_BYTES}}}))\n",
    ),
    # A verb that never returns, for the signal cases: a tool call in flight must not make the server
    # unkillable.
    "sleeper": (
        {"verb": "sleeper", "summary": "test verb: never returns", "usage": "plainkeep sleeper",
         "risk": "read", "reads": [], "writes": []},
        "import time\ntime.sleep(600)\n",
    ),
    "uniecho": (
        {"verb": "uniecho", "summary": UNI_SUMMARY, "hints": UNI_HINTS,
         "usage": "plainkeep uniecho <text>", "risk": "read",
         "args": [{"name": "text", "required": True}], "reads": [], "writes": []},
        "import json, sys\n"
        "print(json.dumps({'ops_json': 1, 'ok': True, 'verb': 'uniecho', 'argv': sys.argv[1:],\n"
        f"                  'blob': {UNI_BLOB!r}}}, ensure_ascii=False))\n",
    ),
    "echoargs": (
        {"verb": "echoargs", "summary": "test verb: echo the argv the dispatcher passed",
         "usage": "plainkeep echoargs <alpha> [beta]", "risk": "read",
         "args": [{"name": "alpha", "required": True}, {"name": "beta", "default": "b0"}],
         "reads": [], "writes": []},
        "import json, sys\n"
        "print(json.dumps({'ops_json': 1, 'ok': True, 'verb': 'echoargs', 'argv': sys.argv[1:]}))\n",
    ),
    "bigout": (
        {"verb": "bigout", "summary": "test verb: emit a payload larger than a pipe buffer",
         "usage": "plainkeep bigout", "risk": "read", "reads": [], "writes": []},
        "import json\n"
        f"print(json.dumps({{'ops_json': 1, 'ok': True, 'verb': 'bigout', 'blob': 'x' * {BIG_BYTES}}}))\n",
    ),
}


def write_pack(vault: Path, pack: str, verbs: list[str]) -> None:
    for verb in verbs:
        cmd, body = PACK_VERBS[verb]
        d = vault / "plugins" / pack / verb
        d.mkdir(parents=True, exist_ok=True)
        (d / "cmd.json").write_text(json.dumps(cmd, indent=2) + "\n", encoding="utf-8")
        (d / "run.py").write_text(body, encoding="utf-8")


def remove_pack(vault: Path, pack: str) -> None:
    shutil.rmtree(vault / "plugins" / pack, ignore_errors=True)


def audit_lines(vault: Path) -> list[str]:
    log = vault / ".logs" / "plainkeep.log"
    if not log.exists():
        return []
    return [ln for ln in log.read_text(encoding="utf-8", errors="replace").splitlines() if ln.strip()]


def clear_audit(vault: Path) -> None:
    log = vault / ".logs" / "plainkeep.log"
    if log.exists():
        log.unlink()


class Mode:
    def __init__(self, name: str, vault: Path, tmp: Path, env: dict):
        self.name = name
        self.vault = vault
        self.tmp = tmp
        self.env = env

    def session(self, label: str) -> Session:
        return Session(f"{label} [{self.name}]", self.vault / "plainkeep", self.env, self.tmp)


# --------------------------------------------------------------------------------------------------
# the differential cases
#
# Each returns a comparable "transcript" — the raw stdout bytes plus whatever the case measured — and
# ALSO asserts the real outcome on the spot. run_case() then compares the two modes' transcripts.
# --------------------------------------------------------------------------------------------------
INIT = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2024-11-05", "capabilities": {}}}


def case_handshake(m: Mode) -> bytes:
    """initialize · a notification · ping · an id-less ping · tools/list."""
    s = m.session("handshake")
    s.send(INIT)
    # THE SPACING PROPERTY, named. Everything else in this suite compares the two modes to each
    # other, which cannot say WHY they agree; this pins the exact bytes one frame must have. CPython's
    # json.dumps defaults to the separators ", " and ": " — WITH the spaces — and reproducing them is
    # the whole reason whole sessions can be byte-compared at all. Without this check, a serializer
    # that dropped the spaces would redden nine differential cases with no named cause.
    raw_init = s.readline()
    expected_init = (
        b'{"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": "2024-11-05", '
        b'"capabilities": {"tools": {"listChanged": false}}, "serverInfo": {"name": "plainkeep", '
        b'"version": "' + (REPO / "VERSION").read_text().strip().encode() + b'"}}}'
    )
    check(f"[{m.name}] the initialize frame is EXACTLY CPython's json.dumps spacing, byte for byte",
          raw_init == expected_init, f"{raw_init[:200]!r}")
    init = json.loads(raw_init)
    check(f"[{m.name}] initialize returns serverInfo name+version",
          init["result"]["serverInfo"]["name"] == "plainkeep"
          and init["result"]["serverInfo"]["version"] == (REPO / "VERSION").read_text().strip(),
          init["result"]["serverInfo"])
    check(f"[{m.name}] initialize echoes the client's protocolVersion",
          init["result"]["protocolVersion"] == "2024-11-05")
    check(f"[{m.name}] initialize advertises tools.listChanged=False",
          init["result"]["capabilities"]["tools"] == {"listChanged": False})

    s.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
    check(f"[{m.name}] notifications/* draws no frame at all", s.silent_for(0.6))

    s.send({"jsonrpc": "2.0", "id": 2, "method": "ping"})
    check(f"[{m.name}] ping answers an empty result", s.frame()["result"] == {})

    # THE QUIRK, pinned deliberately: run.py's `is_notification` test sits BELOW the method table, so
    # an id-less `ping` is answered with a full frame carrying "id": null. A port that "fixed" this
    # would silently change the wire, which is why it is a case and not a footnote.
    s.send({"jsonrpc": "2.0", "method": "ping"})
    quirk = s.frame()
    check(f"[{m.name}] an id-less ping still draws a frame with id null (run.py's ordering)",
          quirk == {"jsonrpc": "2.0", "id": None, "result": {}}, quirk)

    s.send({"jsonrpc": "2.0", "id": 3, "method": "tools/list"})
    tools = {t["name"]: t for t in s.frame()["result"]["tools"]}
    check(f"[{m.name}] tools/list carries the vault's real verbs",
          {"search", "capture", "task", "help"} <= set(tools), sorted(tools)[:8])
    check(f"[{m.name}] hidden verbs (mcp, __complete) are absent from the tool list",
          "mcp" not in tools and "__complete" not in tools)
    check(f"[{m.name}] a declared arg becomes a required string property",
          tools["search"]["inputSchema"]["required"] == ["query"]
          and tools["search"]["inputSchema"]["properties"]["query"]["type"] == "string")
    check(f"[{m.name}] every tool carries the free-form args passthrough",
          all("args" in t["inputSchema"]["properties"] for t in tools.values()))

    rc = s.finish()
    check(f"[{m.name}] EOF on stdin ends the session with exit 0", rc == 0, f"rc={rc}")
    return s.raw


def case_malformed_and_unknown(m: Mode) -> bytes:
    """A parse error, a blank line, an unknown method, an unknown tool — and the session SURVIVES."""
    s = m.session("malformed")
    s.send(INIT)
    s.frame()

    s.send_raw("not json at all\n")
    parse_err = s.frame()
    check(f"[{m.name}] a malformed line answers -32700 with id null",
          parse_err == {"jsonrpc": "2.0", "id": None,
                        "error": {"code": -32700, "message": "parse error"}}, parse_err)

    s.send_raw("\n   \n")
    check(f"[{m.name}] blank lines draw no frame", s.silent_for(0.5))

    s.send({"jsonrpc": "2.0", "id": 7, "method": "no/such/method"})
    unknown = s.frame()
    check(f"[{m.name}] an unknown method answers -32601 naming it",
          unknown["error"] == {"code": -32601, "message": "method not found: no/such/method"},
          unknown)

    s.send({"jsonrpc": "2.0", "id": 8, "method": "tools/call",
            "params": {"name": "definitely-not-a-verb", "arguments": {}}})
    unknown_tool = s.frame()["result"]
    check(f"[{m.name}] an unknown tool is an isError result carrying the gate's refusal",
          unknown_tool["isError"] is True
          and "unknown verb 'definitely-not-a-verb'" in unknown_tool["content"][0]["text"],
          unknown_tool["content"][0]["text"][:90])

    # The session survived all four: a live request after them proves it, which "no crash" would not.
    s.send({"jsonrpc": "2.0", "id": 9, "method": "ping"})
    check(f"[{m.name}] the session still serves after a parse error", s.frame()["id"] == 9)
    rc = s.finish()
    check(f"[{m.name}] a session that saw malformed input still exits 0", rc == 0, f"rc={rc}")
    return s.raw


def case_tool_call_and_audit(m: Mode) -> bytes:
    """A real verb runs through the ONE DOOR: its own output comes back, and the gate logged it."""
    clear_audit(m.vault)
    s = m.session("tools/call")
    s.send(INIT)
    s.frame()
    s.send({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": "search", "arguments": {"query": "widget"}}})
    res = s.frame()["result"]
    check(f"[{m.name}] tools/call search is not an error", res["isError"] is False, str(res)[:120])
    lines = [ln for ln in res["content"][0]["text"].splitlines() if ln.strip()]
    head = json.loads(lines[0]) if lines else {}
    check(f"[{m.name}] the tool result is the verb's real NDJSON envelope",
          head.get("ops_json") == 1 and head.get("ok") is True and head.get("verb") == "search", head)
    check(f"[{m.name}] the seeded note was actually found",
          head.get("count", 0) >= 1 and len(lines) > 1, res["content"][0]["text"][:120])
    rc = s.finish()
    check(f"[{m.name}] the tool-call session exits 0", rc == 0, f"rc={rc}")

    log = audit_lines(m.vault)
    check(f"[{m.name}] the audit line was written for `mcp` itself",
          sum(1 for ln in log if ln.split("\t")[1].split(" ")[0] == "mcp") == 1,
          "\n".join(log)[:200])
    check(f"[{m.name}] the audit line was written for the verb the tool call ran",
          any(ln.split("\t")[1].startswith("search widget --json") for ln in log),
          "\n".join(log)[:200])
    return s.raw


def case_argument_ordering(m: Mode) -> bytes:
    """Declared args go in the cmd.json's order, then the free-form passthrough — never the client's
    key order, which JSON does not even promise."""
    write_pack(m.vault, "protopack", ["echoargs"])
    clear_audit(m.vault)
    try:
        s = m.session("argv-order")
        s.send(INIT)
        s.frame()
        # `beta` FIRST in the object and `alpha` second: if the port walked the client's keys instead
        # of the sidecar's args[], the argv would come out reversed.
        s.send({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                "params": {"name": "echoargs",
                           "arguments": {"beta": "B", "alpha": "A", "args": ["--flag", "C"]}}})
        res = s.frame()["result"]
        argv = json.loads(res["content"][0]["text"])["argv"]
        check(f"[{m.name}] argv is declared-order, then passthrough, then --json",
              argv == ["A", "B", "--flag", "C", "--json"], argv)
        rc = s.finish()
        check(f"[{m.name}] the argv-order session exits 0", rc == 0, f"rc={rc}")
        check(f"[{m.name}] the audit line records that exact argv",
              any(ln.split("\t")[1] == "echoargs A B --flag C --json" for ln in audit_lines(m.vault)),
              "\n".join(audit_lines(m.vault))[:200])
        return s.raw
    finally:
        remove_pack(m.vault, "protopack")


def case_confirm_class(m: Mode) -> bytes:
    """A confirm-class verb is REFUSED with the exact re-run, and is never auto-`--yes`ed."""
    clear_audit(m.vault)
    s = m.session("confirm")
    s.send(INIT)
    s.frame()
    s.send({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": "plugin", "arguments": {"action": "add", "target": "local/x"}}})
    res = s.frame()["result"]
    check(f"[{m.name}] a confirm-class call is flagged isError", res["isError"] is True)
    payload = json.loads(res["content"][0]["text"])
    check(f"[{m.name}] it returns ops_confirm_needed for the right verb",
          payload["ops_confirm_needed"] is True and payload["verb"] == "plugin", payload)
    check(f"[{m.name}] it carries the exact --yes re-run line",
          payload["rerun"] == "plainkeep plugin add local/x --yes", payload.get("rerun"))
    rc = s.finish()
    check(f"[{m.name}] the confirm session exits 0", rc == 0, f"rc={rc}")
    # WHERE THE 3 COMES FROM, measured rather than assumed: the GATE allows this invocation (bin/
    # plugin/cmd.json declares the verb `safe_write`; the confirm risk lives on the `add` ACTION and
    # is enforced by the verb itself, which is why the audit line below reads `allow`). The exit-3
    # mapping in the server is therefore about the verb's exit status, not about a gate verdict.
    #
    # The refusal has to be a REFUSAL rather than a slow yes, so the evidence is the argv that was
    # actually dispatched: it must be the caller's, with `--json` appended and `--yes` nowhere in the
    # session at all.
    log = audit_lines(m.vault)
    check(f"[{m.name}] the confirm-class verb was dispatched exactly as asked, without --yes",
          any(ln.split("\t")[1] == "plugin add local/x --json" for ln in log)
          and not any("--yes" in ln for ln in log), "\n".join(log)[:200])
    return s.raw


def case_plugin_mid_session(m: Mode) -> bytes:
    """A pack installed BETWEEN two tools/list calls in ONE session becomes visible — the property
    that fails the moment either side reads plainkeep.json instead of the live sidecars (run.md D9)."""
    remove_pack(m.vault, "latepack")
    try:
        s = m.session("plugin-mid-session")
        s.send(INIT)
        s.frame()
        s.send({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        before = {t["name"] for t in s.frame()["result"]["tools"]}
        check(f"[{m.name}] the pack's verb is absent before it is installed", "echoargs" not in before)

        write_pack(m.vault, "latepack", ["echoargs"])

        s.send({"jsonrpc": "2.0", "id": 3, "method": "tools/list"})
        after = {t["name"] for t in s.frame()["result"]["tools"]}
        check(f"[{m.name}] the pack's verb appears in the SAME session, no restart",
              "echoargs" in after, sorted(after - before))

        # And it is callable, not merely listed — a tool list that advertises an unrunnable verb is
        # the failure this would otherwise hide.
        s.send({"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                "params": {"name": "echoargs", "arguments": {"alpha": "live"}}})
        argv = json.loads(s.frame()["result"]["content"][0]["text"])["argv"]
        check(f"[{m.name}] the freshly-installed verb actually runs", argv[:1] == ["live"], argv)
        rc = s.finish()
        check(f"[{m.name}] the mid-session-plugin session exits 0", rc == 0, f"rc={rc}")
        return s.raw
    finally:
        remove_pack(m.vault, "latepack")


def case_non_object_frame(m: Mode) -> bytes:
    """A JSON array as a frame. run.py calls `.get` on whatever json.loads returned, so this ENDS the
    session on the floor — reproduced rather than papered over, because a port that kept serving would
    be a different server. Only the exit CODE differs (floor 1, core 5: see the clamp)."""
    s = m.session("non-object-frame")
    s.send(INIT)
    s.frame()
    s.send_raw("[1, 2, 3]\n")
    check(f"[{m.name}] a non-object frame draws no response", s.silent_for(1.0))
    rc = s.finish()
    check(f"[{m.name}] a non-object frame ends the session (off-protocol code never reaches the wire)",
          rc == (5 if m.name == "core" else 1), f"rc={rc}")
    check(f"[{m.name}] the death is explained on stderr, not on stdout",
          len(s.stderr_text().strip()) > 0, s.stderr_text()[:120])
    return s.raw


def case_key_order(m: Mode) -> bytes:
    """Integer-like arg names keep the sidecar's declared order in `inputSchema.properties`.

    The r1 spec review measured this diverging: ECMAScript hoists integer-index property keys to the
    front of a plain object in ascending numeric order, CPython preserves insertion order, so
    `alpha, 0, zeta, 2` rendered as `0, 2, alpha, zeta` under the core — same frame length, one
    differing byte at offset 25947 of a 26744-byte tools/list frame. mcp.ts now builds `properties`
    as a Map, which iterates in insertion order for every key shape, so the divergence class is
    ELIMINATED rather than documented. `{"name": 5}` covers the sibling path through dictKey().

    The order is asserted against the DECLARED order, not merely across the two modes: two sides that
    hoisted identically would agree with each other and still be wrong.
    """
    write_pack(m.vault, "protopack", ["keyorder"])
    try:
        s = m.session("key-order")
        s.send(INIT)
        s.frame()
        s.send({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        tools = {t["name"]: t for t in s.frame()["result"]["tools"]}
        order = list(tools["keyorder"]["inputSchema"]["properties"])
        check(f"[{m.name}] integer-like arg names keep the sidecar's declared order",
              order == KEYORDER_EXPECTED, f"{order} != {KEYORDER_EXPECTED}")
        rc = s.finish()
        check(f"[{m.name}] the key-order session exits 0", rc == 0, f"rc={rc}")
        return s.raw
    finally:
        remove_pack(m.vault, "protopack")


def case_unicode(m: Mode) -> bytes:
    """Non-ASCII through every path a byte can take into a frame.

    This exists because the byte oracle's ENABLING property — that both sides emit raw UTF-8, exactly
    as `json.dumps(…, ensure_ascii=False)` does, where a naive port would emit \\uXXXX — was asserted
    nowhere: every payload in the suite was ASCII (r1 spec review, F2), so `scalarJson` could have
    been changed to escape non-ASCII and nothing would have gone red. The byte comparison would have
    quietly degraded into a shape check.

    So the assertion is on the RAW BYTES, not just on the parsed values: the UTF-8 encoding must be
    present and the escaped spelling of each of these characters must be absent.
    """
    write_pack(m.vault, "protopack", ["uniecho"])
    try:
        s = m.session("unicode")
        s.send(INIT)
        s.frame()
        s.send({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        tools = {t["name"]: t for t in s.frame()["result"]["tools"]}
        check(f"[{m.name}] a non-ASCII summary + hints become the tool description verbatim",
              tools["uniecho"]["description"] == f"{UNI_SUMMARY}\n\n{UNI_HINTS}",
              repr(tools["uniecho"]["description"])[:120])

        s.send({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                "params": {"name": "uniecho", "arguments": {"text": UNI_ARG}}})
        payload = json.loads(s.frame()["result"]["content"][0]["text"])
        check(f"[{m.name}] a non-ASCII tool ARGUMENT survives the round trip through the dispatcher",
              payload["argv"][:1] == [UNI_ARG], repr(payload["argv"])[:120])
        check(f"[{m.name}] a non-ASCII verb RESULT survives the round trip",
              payload["blob"] == UNI_BLOB, repr(payload.get("blob"))[:120])

        s.send({"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                "params": {"name": "search", "arguments": {"query": "unicodeprobe"}}})
        body = s.frame()["result"]["content"][0]["text"]
        check(f"[{m.name}] a non-ASCII note title comes back through search",
              UNI_NOTE_TITLE in body, body[:160])
        rc = s.finish()
        check(f"[{m.name}] the unicode session exits 0", rc == 0, f"rc={rc}")

        # THE POINT OF THE CASE: raw bytes, not parsed values.
        check(f"[{m.name}] every non-ASCII character is on the wire as raw UTF-8",
              all(t.encode("utf-8") in s.raw for t in (UNI_SUMMARY, UNI_HINTS, UNI_ARG, UNI_BLOB,
                                                       UNI_NOTE_TITLE)))
        escaped = [e for e in (rb"\u00e1", rb"\u2705", rb"\ud83d", rb"\u00a0", rb"\u65e5")
                   if e in s.raw]
        check(f"[{m.name}] and NEVER as a \\uXXXX escape (the ensure_ascii=False property)",
              not escaped, f"found {escaped}")
        return s.raw
    finally:
        remove_pack(m.vault, "protopack")


def case_parser_key_order(m: Mode) -> list[str]:
    """The RESIDUAL half of the key-order class, pinned as an EXPECTED divergence.

    An object arriving through `JSON.parse` — a non-string `hints` such as `{"2":"b","1":"a"}`, which
    rides into the tool description verbatim — has already been reordered before mcp.ts sees it,
    because JSON.parse builds an ordinary object and ECMAScript hoists integer-index keys. No
    serializer can recover an order the parser discarded; only a hand-written JSON parser could, and
    the shape is not worth one. (The same class is already XFAILed by the fuzz suite for pythonStr.)

    Asserted in BOTH directions so it cannot rot silently: the core must give ['1','2'] and the floor
    ['2','1']. If either side ever changes — a bun that preserves order, a CPython that stops — this
    reddens with a named cause instead of surfacing as an unexplained byte difference elsewhere.
    """
    write_pack(m.vault, "protopack", ["parserorder"])
    try:
        s = m.session("parser-key-order")
        s.send(INIT)
        s.frame()
        s.send({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        tools = {t["name"]: t for t in s.frame()["result"]["tools"]}
        order = list(tools["parserorder"]["description"])
        expected = ["1", "2"] if m.name == "core" else ["2", "1"]
        check(f"[{m.name}] an integer-keyed object from a cmd.json orders {expected} "
              f"(DOCUMENTED divergence, JSON.parse's doing)", order == expected, str(order))
        s.finish()
        return order
    finally:
        remove_pack(m.vault, "protopack")


def case_huge_tool_result(m: Mode) -> bytes:
    """A tool result far past the size at which the core used to truncate it.

    THE DEFECT THIS PINS (quality review r1, Q1, pre-existing since Task 4): bun marks its own
    stdout/stderr O_NONBLOCK when they are pipes. The flag lives on the open file description, so
    `stdio: "inherit"` handed it to the verb, and CPython — which has no idea it is holding a
    non-blocking descriptor — raised `BlockingIOError` on the first write it could not satisfy in
    full. Through a tool call the failure was LAUNDERED: the child died, so `callTool` fell into its
    error branch and the agent was handed ~1.25 MiB of truncated JSON flagged `isError: true`, reading
    as "the verb failed" rather than "the transport cut it off". Measured before the fix at 2,000,000
    requested bytes: core `isError=True len=1310720`, floor `isError=False len=2000059`.

    `bigout` at 300 KB was under the threshold, which is why the suite was green throughout. This one
    is 3 MB, and asserts the REAL outcome — not an error, and every byte present.
    """
    write_pack(m.vault, "protopack", ["hugeout"])
    try:
        s = m.session("huge-tool-result")
        s.send(INIT)
        s.frame()
        s.send({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                "params": {"name": "hugeout", "arguments": {}}})
        res = s.frame(timeout=120.0)["result"]
        check(f"[{m.name}] a {HUGE_BYTES}-byte tool result is not reported as an error",
              res["isError"] is False, str(res)[:140])
        blob = json.loads(res["content"][0]["text"])["blob"]
        check(f"[{m.name}] and arrives whole — no silent truncation",
              len(blob) == HUGE_BYTES, f"got {len(blob)} of {HUGE_BYTES}")
        rc = s.finish()
        check(f"[{m.name}] the huge-result session exits 0", rc == 0, f"rc={rc}")
        return s.raw
    finally:
        remove_pack(m.vault, "protopack")


def case_hung_child_sigterm(m: Mode) -> None:
    """A tool call whose child never returns must not make the server unkillable.

    `spawnSync` blocked the event loop for the child's entire life, and bun runs signal handlers ON
    the event loop — so during a tool call neither the shutdown handler nor the second-signal poll
    could run at all. Measured before the fix against this same `time.sleep(600)` verb: two SIGTERMs
    AND a SIGINT left the core running, and only SIGKILL stopped it, where the floor died on the first
    SIGTERM. The graceful handler had made the server strictly LESS stoppable than the thing it
    replaced.

    Two modes, two different correct answers, both asserted: the floor dies by signal; the core kills
    the child, turns its termination into the last frame, and exits on the protocol. What makes this
    a real test rather than "it stopped" is the DEADLINE — 10 s against a child that would otherwise
    sleep for 600.
    """
    write_pack(m.vault, "protopack", ["sleeper"])
    try:
        s = m.session("hung-child")
        s.send(INIT)
        s.frame()
        s.send({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                "params": {"name": "sleeper", "arguments": {}}})
        time.sleep(1.0)
        check(f"[{m.name}] the server is alive with a tool call in flight", s.alive())
        t0 = time.time()
        s.proc.send_signal(signal.SIGTERM)
        try:
            rc = s.finish(timeout=10.0, close=False)
        except Timeout:
            check(f"[{m.name}] SIGTERM ends a session with a hung child (needed SIGKILL)", False,
                  "still running after 10s")
            return
        elapsed = time.time() - t0
        if m.name == "core":
            check("[core] SIGTERM ends a session with a hung child, on the protocol (0)",
                  rc == 0, f"rc={rc}")
            check("[core] and promptly — the 600s child did not have to finish",
                  elapsed < 5.0, f"{elapsed:.2f}s")
        else:
            check("[floor] SIGTERM with a hung child kills the server by signal",
                  rc == -signal.SIGTERM, f"rc={rc}")
        check(f"[{m.name}] stdout carries no partial frame after a hung-child SIGTERM",
              s.raw == b"" or s.raw.endswith(b"\n"), repr(s.raw[-60:]))
    finally:
        remove_pack(m.vault, "protopack")
        subprocess.run(["pkill", "-f", "sleeper/run.py"], capture_output=True)


def case_stdin_backpressure(m: Mode) -> None:
    """A peer that floods stdin while the server is parked must not grow the server without bound.

    The floor gets this free: CPython's blocking read means the KERNEL PIPE does the flow control, so
    a peer that keeps writing simply blocks. The port did not, and the gap was large — measured by the
    quality review at 13.6 GB accepted and RSS from 44 MB to 4.7 GB while the server was parked
    writing a frame nobody was reading. Two later attempts were still not enough (`pause()` while
    parked: 479 MB; `readable` + `read()`: 411 MB with ZERO lines processed, so bun was buffering on
    its own behalf) — which is why the server now reads fd 0 directly with `fs.read`.

    The assertion is the peer's WRITER BLOCKING, plus a hard ceiling on what the server absorbed. Both
    are properties of the fixed shape, not of agreement between the modes.
    """
    write_pack(m.vault, "protopack", ["bigout"])
    try:
        s = m.session("stdin-backpressure")
        s.send(INIT)
        s.frame()
        # Park the server writing a frame this test will never read.
        s.send({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                "params": {"name": "bigout", "arguments": {}}})
        time.sleep(1.5)
        os.set_blocking(s.proc.stdin.fileno(), False)
        flood = b'{"jsonrpc":"2.0","id":3,"method":"ping"}\n' * 64
        sent = 0
        blocked = False
        deadline = time.time() + 4.0
        while time.time() < deadline:
            try:
                n = s.proc.stdin.raw.write(flood)
                if n is None:
                    blocked = True
                    time.sleep(0.02)
                    continue
                sent += n
            except BlockingIOError:
                blocked = True
                time.sleep(0.02)
            except Exception:
                break
        check(f"[{m.name}] a flooding peer's writer BLOCKS while the server is parked", blocked)
        # One pipe buffer (64 KiB on macOS) is what a correctly back-pressured reader absorbs. The
        # ceiling is deliberately loose (4 MiB) so it pins the CLASS — bounded vs unbounded — rather
        # than a platform's exact buffer size; the pre-fix numbers were 400–13,000x above it.
        check(f"[{m.name}] and the server absorbed a BOUNDED amount ({sent} bytes)",
              sent <= 4 * 1024 * 1024, f"{sent} bytes accepted")
        s.proc.kill()
        s.proc.wait()
    finally:
        remove_pack(m.vault, "protopack")
        subprocess.run(["pkill", "-f", "bigout/run.py"], capture_output=True)


def case_broken_pipe(m: Mode) -> bytes:
    """The peer closes the read end and then asks for another frame.

    This is the one failure mode a stdio server cannot answer with a frame, so it is the one place the
    core's stdout-error branch is reachable at all — without this case that branch would be code
    nobody had watched run. Both sides end the session and both explain themselves on stderr; the exit
    codes differ, and the floor's is MEASURED here rather than assumed:

      * core  → 5, the clamp's EXIT_ANOMALOUS.
      * floor → 120, NOT the 1 an uncaught traceback gives. CPython raises BrokenPipeError out of
        serve(), prints the traceback, and then fails AGAIN flushing stdout during interpreter
        shutdown ("Exception ignored in: <_io.TextIOWrapper name='<stdout>'>"), which is the specific
        condition CPython reports as 120. The first version of this case asserted 1 and went red.
    """
    s = m.session("broken-pipe")
    s.send(INIT)
    s.frame()
    s.proc.stdout.close()
    s.eof = True                                 # nothing more can be read; do not try
    s.send({"jsonrpc": "2.0", "id": 2, "method": "ping"})
    rc = s.finish(timeout=20.0)
    check(f"[{m.name}] a closed read end ends the session (core 5 · floor 120, both measured)",
          rc == (5 if m.name == "core" else 120), f"rc={rc}")
    check(f"[{m.name}] and the reason is on stderr, not lost",
          len(s.stderr_text().strip()) > 0, s.stderr_text()[:120])
    return s.raw


def case_large_frame(m: Mode) -> bytes:
    """A tool result far larger than the pipe buffer, delivered to a peer that is NOT reading yet.

    This is the backpressure case. macOS pipes buffer 64 KiB; the payload is ~300 KB, so `write()`
    cannot complete in one go and the server must wait for the reader. What is asserted is the
    OUTCOME — every byte of the frame arrives, it parses, the blob is exactly the length the verb
    wrote, and the session keeps serving afterwards — because a truncated frame is precisely what a
    server that ignored `write()`'s return value would produce.
    """
    write_pack(m.vault, "protopack", ["bigout"])
    try:
        s = m.session("large-frame")
        s.send(INIT)
        s.frame()
        s.send({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                "params": {"name": "bigout", "arguments": {}}})
        # Deliberately do not read for a second: the server fills the pipe and blocks/waits.
        time.sleep(1.0)
        check(f"[{m.name}] the server is still alive with a full pipe (it waited, it did not give up)",
              s.alive())
        frame = s.frame(timeout=60.0)
        blob = json.loads(frame["result"]["content"][0]["text"])["blob"]
        check(f"[{m.name}] the whole {BIG_BYTES}-byte payload arrived intact",
              len(blob) == BIG_BYTES, f"got {len(blob)}")
        s.send({"jsonrpc": "2.0", "id": 3, "method": "ping"})
        check(f"[{m.name}] the session still serves after a large frame", s.frame()["id"] == 3)
        rc = s.finish()
        check(f"[{m.name}] the large-frame session exits 0", rc == 0, f"rc={rc}")
        return s.raw
    finally:
        remove_pack(m.vault, "protopack")


def case_large_frame_then_eof(m: Mode) -> bytes:
    """The case that can actually tell backpressure apart from the absence of it.

    case_large_frame above passes either way: an unbackpressured server queues the tail in userspace
    and node flushes it as the reader drains, so the frame still arrives. The difference only becomes
    observable when the SESSION ENDS with bytes still queued — the end-of-life drains
    (interception.ts's and main.ts's) are each bounded at 2 s, and after that the process exits and
    the tail is gone.

    So: send the big call, close stdin immediately, and do not read for 5 seconds (longer than both
    bounded drains combined). A server that honours `write()` returning false cannot even reach EOF
    handling until the frame is out, so it is still alive and the frame is whole. Demonstrated failing
    against a variant with the drain-await deleted (.orchestrate/raw/task7-broken-backpressure.log):
    stdout closed mid-frame, the case failed with `stdout closed with no frame`, and the mid-call
    SIGTERM case failed the same way — 5 failures where the real build has 0.
    """
    write_pack(m.vault, "protopack", ["bigout"])
    try:
        s = m.session("large-frame-then-eof")
        s.send(INIT)
        s.frame()
        s.send({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                "params": {"name": "bigout", "arguments": {}}})
        s.close_stdin()
        time.sleep(5.0)
        frame = s.frame(timeout=60.0)
        blob = json.loads(frame["result"]["content"][0]["text"])["blob"]
        check(f"[{m.name}] a large frame survives EOF + a peer that reads 5s late",
              len(blob) == BIG_BYTES, f"got {len(blob)} of {BIG_BYTES}")
        rc = s.finish(close=False)
        check(f"[{m.name}] and the session still exits 0", rc == 0, f"rc={rc}")
        return s.raw
    finally:
        remove_pack(m.vault, "protopack")


# --------------------------------------------------------------------------------------------------
# the signal cases — measured per mode, NOT compared (the two modes are designed to differ)
# --------------------------------------------------------------------------------------------------
def case_sigterm_idle(m: Mode) -> None:
    s = m.session("sigterm-idle")
    s.send(INIT)
    s.frame()                                   # the loop is provably running before we signal
    s.proc.send_signal(signal.SIGTERM)
    try:
        rc = s.finish(timeout=15.0, close=False)
    except Timeout:
        check(f"[{m.name}] SIGTERM on an idle session terminates it", False, "still running after 15s")
        return
    if m.name == "core":
        check("[core] SIGTERM on an idle session shuts down gracefully on the protocol (0)",
              rc == 0, f"rc={rc}")
    else:
        check("[floor] SIGTERM on an idle session kills it by signal (the disclosed divergence)",
              rc == -signal.SIGTERM, f"rc={rc}")
    check(f"[{m.name}] no partial frame was left on stdout after SIGTERM",
          s.raw == b"" or s.raw.endswith(b"\n"), repr(s.raw[-60:]))


def case_sigterm_mid_call(m: Mode) -> None:
    """SIGTERM while a tool call is in flight. The core must FINISH the in-flight frame; the floor
    dies wherever it happens to be. The timing is recorded so 'it was in flight' is a measurement
    rather than an assumption."""
    write_pack(m.vault, "protopack", ["bigout"])
    try:
        s = m.session("sigterm-mid-call")
        s.send(INIT)
        s.frame()
        s.send({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                "params": {"name": "bigout", "arguments": {}}})
        # THE TRIGGER DIFFERS BY MODE, and the asymmetry is deliberate — the two branches below
        # assert different things, and one of them is timing-sensitive in a way this task MEASURED
        # but did not change.
        #
        # CORE asserts "the COMPLETE in-flight frame is still delivered", so the frame has to be
        # provably in flight. Two measured halves: the server has STARTED writing (wait_writing —
        # the first byte is readable and we have consumed nothing), so the tool child has finished
        # and the frame is what is in flight; and the reply is ~300 KB against a 64 KiB pipe buffer
        # with nothing read, so it CANNOT have finished writing. This used to be `time.sleep(0.05)`,
        # a guess about how long a tool call takes, and it stopped being true the moment ADR-014
        # Task 1b added a process to every dispatch (root discovery, ~23 ms): the signal started
        # landing while the child still ran, the core killed it as designed, and the case saw
        # `exit -15`.
        #
        # FLOOR keeps the sleep, and here is the finding that decided it. The floor's claim is that
        # it dies by signal leaving NO PARTIAL FRAME — and that is true because CPython has not
        # begun writing yet, not because it cannot be interrupted mid-write. Driving the floor with
        # wait_writing too was tried and it REDDENS: stdout ends mid-payload
        # (b'xxxx…' with no newline), which is exactly the truncation this module's header discloses
        # ("run.py takes the default disposition and dies instantly — mid-frame if it is halfway
        # through a write()"). So the floor's assertion holds by timing rather than by construction.
        # It is left exactly as it was rather than rewritten under this task's pressure; the
        # measurement is recorded in .orchestrate/task-1b-report.md as a live concern.
        if m.name == "core":
            started_writing = s.wait_writing(timeout=60.0)
        else:
            time.sleep(0.05)
            started_writing = True
        alive_at_signal = s.alive()
        t0 = time.time()
        s.proc.send_signal(signal.SIGTERM)
        if m.name == "core":
            frame = s.frame(timeout=60.0)
            elapsed = time.time() - t0
            blob = json.loads(frame["result"]["content"][0]["text"])["blob"]
            check("[core] SIGTERM mid-call still delivers the COMPLETE in-flight frame",
                  frame["id"] == 2 and len(blob) == BIG_BYTES, f"blob={len(blob)}")
            check("[core] the frame really was in flight when the signal landed "
                  f"(alive, writing, unread, {BIG_BYTES}B > 64 KiB pipe)",
                  alive_at_signal and started_writing and BIG_BYTES > 65536,
                  f"started_writing={started_writing} finished {elapsed:.3f}s after SIGTERM")
            rc = s.finish(timeout=15.0, close=False)
            check("[core] and then shuts down on the protocol (0)", rc == 0, f"rc={rc}")
        else:
            rc = s.finish(timeout=15.0, close=False)
            check("[floor] SIGTERM mid-call kills the server by signal",
                  rc == -signal.SIGTERM, f"rc={rc}")
        check(f"[{m.name}] stdout carries no partial frame after a mid-call SIGTERM",
              s.raw == b"" or s.raw.endswith(b"\n"), repr(s.raw[-60:]))
    finally:
        remove_pack(m.vault, "protopack")


def case_second_signal_escape(m: Mode) -> None:
    """CORE ONLY, and it exists because the graceful handler has a cost worth pinning: while the
    server is parked waiting for a peer that stopped reading, the FIRST signal cannot interrupt it.
    A second one abandons the wait and returns EXIT_ANOMALOUS (5), so the process is still stoppable
    without SIGKILL — and the first signal alone provably does NOT stop it."""
    write_pack(m.vault, "protopack", ["bigout"])
    try:
        s = m.session("second-signal")
        s.send(INIT)
        s.frame()
        s.send({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                "params": {"name": "bigout", "arguments": {}}})
        time.sleep(1.5)                          # the pipe is full and nobody is reading
        s.proc.send_signal(signal.SIGTERM)
        time.sleep(1.0)
        check("[core] one signal does NOT abandon a frame the peer stopped reading", s.alive())
        s.proc.send_signal(signal.SIGTERM)
        try:
            rc = s.wait_without_reading(timeout=20.0)
        except Timeout:
            check("[core] a second signal abandons the wait and exits", False, "still running")
            return
        check("[core] a second signal abandons the wait and reports the anomaly (5)",
              rc == 5, f"rc={rc}")
        check("[core] and says so on stderr",
              "second shutdown signal" in s.stderr_text(), s.stderr_text()[:140])
    finally:
        remove_pack(m.vault, "protopack")


# --------------------------------------------------------------------------------------------------
# driver
# --------------------------------------------------------------------------------------------------
DIFFERENTIAL = [
    ("handshake · notification · ping · tools/list", case_handshake),
    ("malformed JSON · blank lines · unknown method · unknown tool", case_malformed_and_unknown),
    ("tools/call re-enters the dispatcher (+ audit lines)", case_tool_call_and_audit),
    ("argument ordering follows the sidecar, not the client", case_argument_ordering),
    ("confirm-class call → ops_confirm_needed, never --yes", case_confirm_class),
    ("a pack installed mid-session becomes visible", case_plugin_mid_session),
    ("integer-like arg names keep the sidecar's order", case_key_order),
    ("non-ASCII rides the wire as raw UTF-8, never \\uXXXX", case_unicode),
    ("a non-object frame ends the session on both sides", case_non_object_frame),
    ("a closed read end (EPIPE) ends the session on both sides", case_broken_pipe),
    ("a frame larger than the pipe buffer arrives intact", case_large_frame),
    ("a large frame survives EOF + a late reader (backpressure)", case_large_frame_then_eof),
    ("a tool result past the O_NONBLOCK truncation threshold", case_huge_tool_result),
]

MEASURED = [
    ("SIGTERM on an idle session", case_sigterm_idle),
    ("SIGTERM while a tool call is in flight", case_sigterm_mid_call),
    ("SIGTERM while a HUNG tool call is in flight", case_hung_child_sigterm),
    ("a flooding peer cannot grow a parked server", case_stdin_backpressure),
]


def discover_binary() -> str | None:
    cand = os.environ.get("PLAINKEEP_CORE_BIN") or str(REPO / ".local" / "bin" / "plainkeep-core")
    return cand if os.path.isfile(cand) and os.access(cand, os.X_OK) else None


def main() -> int:
    if os.environ.get("PLAINKEEP_CORE") == "off":
        print(f"{YELLOW}{BOLD}{SKIP_FLOOR}{RESET}")
        return 0
    binary = discover_binary()
    if binary is None:
        if os.environ.get("PLAINKEEP_REQUIRE_CORE") == "1":
            print(f"{RED}{BOLD}{SKIP_LINE}{RESET}", file=sys.stderr)
            print(f"{RED}PLAINKEEP_REQUIRE_CORE=1 — a missing core binary is a FAILURE.{RESET}",
                  file=sys.stderr)
            return 1
        print(f"{YELLOW}{BOLD}{SKIP_LINE}{RESET}")
        return 0

    # Preflight, mirroring run_tui_pty.py's: a binary built before Task 7 has no `mcp` interception,
    # and every case below would then fail with a confusing timeout instead of the real reason.
    probe = subprocess.run([binary, "--core-api", "intercepts"], capture_output=True, text=True)
    try:
        buckets = json.loads(probe.stdout)["verbs"]
    except Exception:
        buckets = {}
    if "mcp" not in buckets.get("noncomparable", []):
        print(f"{RED}{BOLD}mcp-protocol: this binary does not register `mcp` as a NON-comparable "
              f"interception (rebuild it: cd cli && bun run build).{RESET}", file=sys.stderr)
        print(f"{RED}  --core-api intercepts verbs = {buckets!r}{RESET}", file=sys.stderr)
        return 1
    check("`mcp` is registered NON-comparable (a session is not an invocation)",
          "mcp" in buckets.get("noncomparable", []) and "mcp" not in buckets.get("comparable", []))
    # The ONLY thing keeping a session server out of the TUI's menu and out of the tool list is this
    # flag, so it is asserted rather than assumed.
    check("bin/mcp/cmd.json still declares hidden:true (keeps `mcp` off its own tool list)",
          json.loads((REPO / "bin" / "mcp" / "cmd.json").read_text())["hidden"] is True)

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        vault = tmp / "plainkeep"
        shutil.copytree(REPO, vault, ignore=IGNORE)
        (vault / "wiki" / "notes" / "widget.md").write_text(
            "---\ntype: note\ntitle: Widget design\nstatus: active\ntags: [demo]\n---\n"
            "# Widget design\n\nThe widget subsystem is the heart of the demo.\n", encoding="utf-8")
        # A SECOND note, deliberately separate from the widget one so the existing tool-call case is
        # not perturbed: its keyword is ASCII (searchable without depending on how the index folds
        # non-ASCII) while its title is not, so a `search` result frame carries non-ASCII bytes.
        (vault / "wiki" / "notes" / "unicode-probe.md").write_text(
            f"---\ntype: note\ntitle: {UNI_NOTE_TITLE}\nstatus: active\ntags: [demo]\n---\n"
            f"# {UNI_NOTE_TITLE}\n\nunicodeprobe — {UNI_BLOB}\n", encoding="utf-8")
        base = {**os.environ, "PLAINKEEP_HOME": str(vault)}
        base.pop("PLAINKEEP_PATH", None)
        core = Mode("core", vault, tmp, {**base, "PLAINKEEP_CORE": "require",
                                         "PLAINKEEP_CORE_BIN": binary})
        floor = Mode("floor", vault, tmp, {**base, "PLAINKEEP_CORE": "off",
                                           "PLAINKEEP_CORE_BIN": ""})
        subprocess.run([str(vault / "plainkeep"), "index"], capture_output=True, env=core.env)

        for label, fn in DIFFERENTIAL:
            try:
                a = fn(core)
                b = fn(floor)
            except (Timeout, Exception) as e:      # noqa: BLE001 — a case that blew up is a failure
                check(f"DIFFERENTIAL {label}", False, f"{type(e).__name__}: {e}")
                continue
            check(f"DIFFERENTIAL {label}: core and floor frames are BYTE-IDENTICAL",
                  a == b, f"core={len(a)}B floor={len(b)}B")
        for label, fn in MEASURED:
            for m in (core, floor):
                try:
                    fn(m)
                except Exception as e:             # noqa: BLE001
                    check(f"[{m.name}] {label}", False, f"{type(e).__name__}: {e}")
        try:
            case_second_signal_escape(core)
        except Exception as e:                     # noqa: BLE001
            check("[core] second-signal escape hatch", False, f"{type(e).__name__}: {e}")
        # The one case that asserts the two modes DIFFER. It is deliberately outside DIFFERENTIAL,
        # which byte-compares: this divergence is JSON.parse's and is documented rather than fixed.
        try:
            a, b = case_parser_key_order(core), case_parser_key_order(floor)
            check("EXPECTED DIVERGENCE: a JSON.parse-ordered object differs across modes, "
                  "and differs in the documented direction", a == ["1", "2"] and b == ["2", "1"],
                  f"core={a} floor={b}")
        except Exception as e:                     # noqa: BLE001
            check("EXPECTED DIVERGENCE: integer-keyed object from a cmd.json", False,
                  f"{type(e).__name__}: {e}")

    # STDOUT HYGIENE — every byte this suite ever read off an mcp server, in either mode, must be part
    # of a newline-terminated JSON-RPC frame. This is the analogue of run_tui_pty.py's render
    # assertions for a protocol stream: one stray console.log, one diagnostic on the wrong fd, one
    # un-terminated write, and the peer sees a protocol error rather than a session.
    bad: list[str] = []
    frames = 0
    for s_ in sessions:
        label, raw = s_.label, s_.raw
        if raw and not raw.endswith(b"\n"):
            bad.append(f"{label}: stdout did not end on a frame boundary: {raw[-60:]!r}")
        for ln in raw.split(b"\n"):
            if not ln.strip():
                continue
            try:
                obj = json.loads(ln)
            except Exception:
                bad.append(f"{label}: stdout line is not JSON: {ln[:80]!r}")
                continue
            if not isinstance(obj, dict) or obj.get("jsonrpc") != "2.0":
                bad.append(f"{label}: stdout line is not a JSON-RPC frame: {ln[:80]!r}")
                continue
            frames += 1
    check(f"STDOUT HYGIENE: all {frames} lines across {len(sessions)} sessions are JSON-RPC frames",
          not bad, "\n".join(bad[:4]))

    print(f"{BOLD}plainkeep mcp — protocol differential (core ↔ bin/mcp/run.py) — "
          f"{len(results)} checks{RESET}\n")
    passed = sum(1 for _, ok, _ in results if ok)
    for name, ok, detail in results:
        mark = f"{GREEN}PASS{RESET}" if ok else f"{RED}FAIL{RESET}"
        print(f"  {mark} {name:<72}" + (f" {DIM}{detail.strip()[:100]}{RESET}" if (detail and not ok) else ""))
    failed = len(results) - passed
    print(f"\n{BOLD}Result:{RESET} {GREEN}{passed} passed{RESET}, "
          f"{(RED if failed else DIM)}{failed} failed{RESET}, {len(results)} checks")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
