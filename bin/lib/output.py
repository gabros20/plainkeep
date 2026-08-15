"""
output.py — the single `--json` implementation (proposal Part 1.1). One frozen, versioned
envelope, so any agent reads a stable machine contract; the human rendering stays byte-identical
to what a verb printed before whenever `--json` is absent.

Envelope (ok):     {"ops_json": 1, "ok": true,  "verb": "...", "data": {...}}
Envelope (error):  {"ops_json": 1, "ok": false, "verb": "...", "error": {"code": N, "message": "...", "hint": "..."}}

Multi-row verbs stream NDJSON under `--json`: ONE header object (ok/verb/count) then one JSON
object per row. The envelope only ever changes with an explicit `PLAINKEEP_JSON_VERSION` bump (the `jc`
lesson: an unstable machine schema is worse than none).

Exit-code protocol (proposal Part 0.3), shared with the guardrail/dispatcher:
    0 ok · 1 unexpected · 2 usage · 3 guardrail-confirm-needed · 4 not-found · 5 guardrail-deny

Verbs adopt this mechanically:

    import output
    js, argv = output.parse_argv(sys.argv[1:])   # strip --json so arg parsing ignores it
    data = ...                                    # the structured dict the verb already computes
    return output.emit(data, "status", human=render)   # render(data) prints exactly as before

`emit`/`emit_rows`/`fail` decide mode themselves via `json_mode()` (which reads the real argv +
`PLAINKEEP_JSON`), so `--json` need only be stripped from the verb's own parsing, not from the process.
"""
from __future__ import annotations
import json
import os
import re
import sys

PLAINKEEP_JSON_VERSION = 1

# A REFUSAL IS ONE STRING WITH TWO AUDIENCES, and only one of them has a terminal.
#
# `fail(message)` renders the same text to stderr for a human and into `error.message` for a program,
# and the house idiom for a refusal is `f"{RED}refusing to …{RESET}"` — so every coloured refusal was
# shipping `\033[31m` inside the machine envelope. Measured at `ceff52f`: three such calls, all in
# `bin/job/run.py`, invisible to a line-based `grep` because the colour sits on a continuation line.
#
# Stripped HERE rather than at the five call sites, because the property wanted is "no envelope ever
# carries escapes", which a per-call fix cannot provide for the sixth call. Callers keep writing
# colour and keep getting it on the channel that can render it. `test/run_json.py` polices the
# result over every verb it can reach.
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def strip_ansi(text: str) -> str:
    """`text` with terminal escape sequences removed — for anything crossing the machine channel."""
    return _ANSI_RE.sub("", text)

# Exit-code protocol (single source of truth for the whole surface).
EXIT_OK = 0
EXIT_UNEXPECTED = 1
EXIT_USAGE = 2
EXIT_CONFIRM = 3
EXIT_NOT_FOUND = 4
EXIT_DENY = 5

_TRUTHY = ("1", "true", "yes", "on")


def _env_json() -> bool:
    return os.environ.get("PLAINKEEP_JSON", "").lower() in _TRUTHY


def json_mode(argv=None) -> bool:
    """True when JSON output is requested: `--json` anywhere in argv, or PLAINKEEP_JSON=1 in the env.
    Defaults to the real process argv so a verb that already stripped `--json` locally still emits
    JSON."""
    args = sys.argv[1:] if argv is None else argv
    return ("--json" in args) or _env_json()


def parse_argv(argv=None):
    """Return (json_on, argv_without_json). Detects and strips every `--json`, and honours PLAINKEEP_JSON=1
    so a verb can parse its own flags without tripping over the global one."""
    args = list(sys.argv[1:] if argv is None else argv)
    stripped = [a for a in args if a != "--json"]
    return (len(stripped) != len(args)) or _env_json(), stripped


def _write(stream, text: str) -> None:
    stream.write(text if text.endswith("\n") else text + "\n")


def _render_human(human, payload, stream) -> None:
    """`human` is a str, a callable(payload)->str|None (may print directly), or None."""
    if human is None:
        return
    if callable(human):
        out = human(payload)
        if out is not None:
            _write(stream, out)
    else:
        _write(stream, str(human))


def emit(data: dict, verb: str, human=None) -> int:
    """Scalar verb: print the JSON envelope under `--json`, else the human rendering. Returns EXIT_OK
    so a verb can `return output.emit(...)`."""
    if json_mode():
        env = {"ops_json": PLAINKEEP_JSON_VERSION, "ok": True, "verb": verb, "data": data}
        sys.stdout.write(json.dumps(env, ensure_ascii=False) + "\n")
    else:
        _render_human(human, data, sys.stdout)
    return EXIT_OK


def emit_rows(rows, verb: str, human=None, header: dict | None = None) -> int:
    """Multi-row verb: NDJSON (one header object, then one object per row) under `--json`, else the
    human rendering. `rows` is an iterable of dicts; `header` merges extra fields into the header."""
    rows = list(rows)
    if json_mode():
        head = {"ops_json": PLAINKEEP_JSON_VERSION, "ok": True, "verb": verb, "count": len(rows)}
        if header:
            head.update(header)
        lines = [json.dumps(head, ensure_ascii=False)]
        lines += [json.dumps(r, ensure_ascii=False) for r in rows]
        sys.stdout.write("\n".join(lines) + "\n")
    else:
        _render_human(human, rows, sys.stdout)
    return EXIT_OK


def fail(code: int, message: str, hint: str | None = None, verb: str | None = None) -> None:
    """Render the error envelope (JSON, on stdout) under `--json`, else a human message on stderr,
    then exit with `code` from the protocol. Does not return."""
    if json_mode():
        err = {"code": code, "message": strip_ansi(message)}
        if hint:
            err["hint"] = strip_ansi(hint)
        env = {"ops_json": PLAINKEEP_JSON_VERSION, "ok": False, "verb": verb, "error": err}
        sys.stdout.write(json.dumps(env, ensure_ascii=False) + "\n")
    else:
        sys.stderr.write(message + (f" ({hint})" if hint else "") + "\n")
    sys.exit(code)
