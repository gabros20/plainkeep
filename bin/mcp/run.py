#!/usr/bin/env python3
"""
plainkeep mcp — the agent transport (proposal Part 2.4). A STATELESS stdio MCP server: the agent host
spawns it per session, it dies on stdin EOF. No daemon, no HTTP, no resident state (the daemon
verdict — a resident process accumulates state plaintext can't rebuild).

STDLIB ONLY: the JSON-RPC 2.0 / MCP handshake (initialize · notifications/initialized · tools/list ·
tools/call) is implemented by hand over newline-delimited JSON on stdin/stdout — no mcp/fastmcp dep.

  • The tool list is GENERATED from the plainkeep surface (manifest.load_cmds → the same data as plainkeep.json/3:
    summaries + hints become descriptions, args become inputSchema). Plugins appear automatically.
  • A tools/call SHELLS OUT: subprocess [<abs>/plainkeep, verb, ...args, --json]. Execution RE-ENTERS through
    the dispatcher, so the guardrail + .logs stay the single enforcement path (anti-roadmap #2 — never
    import verb/lib code to skip the dispatcher).
  • Exit 3 (confirm-needed) becomes a structured needs-`--yes` result carrying the exact re-run; the
    server NEVER auto-appends --yes.

`plainkeep mcp --setup` prints the `claude mcp add plainkeep -- <abs>/plainkeep mcp` line. Hidden/read-class (like
__complete), so the guardrail runs it freely.
"""
from __future__ import annotations
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lib import enginetree, manifest, output, paths  # noqa: E402

PROTOCOL_VERSION = "2024-11-05"          # echoed back to the client if it doesn't pin its own
SERVER_NAME = "plainkeep"


def _dispatcher_bin() -> str:
    """The dispatcher a tool call re-enters through — the ENGINE's launcher (Task 2).

    It was `$PLAINKEEP_HOME/plainkeep`, i.e. the vault-local shim. A data vault has no
    launcher of its own, and deriving one from the data root would mean an MCP client's
    tool call re-enters through whatever executable happens to sit in the notes it is
    acting on.

    The STABLE spelling, because this value is written into somebody else's config file and read back
    on every launch for as long as that client is installed. The version-pinned path `launcher()`
    returns would keep an MCP client on the engine that happened to be active the day `--setup` ran,
    and break outright once that version is pruned."""
    return str(enginetree.stable_launcher())


# ── tool generation (from the plainkeep surface = plainkeep.json/3) ────────────────────────────
def _tool(cmd: dict) -> dict:
    """One MCP tool from a cmd.json dict: description = summary (+hints), inputSchema from args."""
    desc = cmd.get("summary", "")
    if cmd.get("hints"):
        desc = f"{desc}\n\n{cmd['hints']}" if desc else cmd["hints"]
    props: dict = {}
    required: list[str] = []
    for a in cmd.get("args", []):
        name = a["name"]
        d = "" if a.get("default") is None else f" (default: {a['default']})"
        props[name] = {"type": "string", "description": f"positional arg{d}"}
        if a.get("required"):
            required.append(name)
    props["args"] = {"type": "array", "items": {"type": "string"},
                     "description": "additional positional args / flags (e.g. sub-action tokens, --yes)"}
    schema = {"type": "object", "properties": props}
    if required:
        schema["required"] = required
    return {"name": cmd["verb"], "description": desc or cmd["verb"], "inputSchema": schema}


def _tools() -> list[dict]:
    return [_tool(c) for c in manifest.load_cmds()]


def _argv_from(cmd: dict | None, arguments: dict) -> list[str]:
    """Build the positional argv (WITHOUT --json/--yes) from the tool arguments, declared args first
    in order, then the free-form `args` passthrough."""
    argv: list[str] = []
    for a in (cmd.get("args", []) if cmd else []):
        v = arguments.get(a["name"])
        if v is not None:
            argv.append(str(v))
    extra = arguments.get("args")
    if isinstance(extra, list):
        argv += [str(x) for x in extra]
    return argv


# ── JSON-RPC plumbing ───────────────────────────────────────────────────────────────────────────
def _result(mid, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def _error(mid, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}


def _text_result(text: str, is_error: bool = False) -> dict:
    return {"content": [{"type": "text", "text": text}], "isError": is_error}


def _call(mid, params: dict) -> dict:
    name = params.get("name") or ""
    arguments = params.get("arguments") or {}
    cmds = {c["verb"]: c for c in manifest.load_cmds()}
    cmd = cmds.get(name)
    argv = _argv_from(cmd, arguments)
    proc = subprocess.run([_dispatcher_bin(), name, *argv, "--json"], capture_output=True, text=True)
    out, err = proc.stdout.strip(), proc.stderr.strip()
    if proc.returncode == output.EXIT_CONFIRM:
        rerun = " ".join(["plainkeep", name, *argv, "--yes"])
        payload = {"ops_confirm_needed": True, "verb": name, "rerun": rerun,
                   "detail": out or err or "this call is confirm-class — re-run with --yes"}
        return _result(mid, _text_result(json.dumps(payload, ensure_ascii=False), is_error=True))
    if proc.returncode == output.EXIT_OK:
        return _result(mid, _text_result(out or "{}"))
    return _result(mid, _text_result(out or err or f"exit {proc.returncode}", is_error=True))


def handle(msg: dict) -> dict | None:
    """Return a JSON-RPC response dict, or None for a notification (no id / notifications/*)."""
    method = msg.get("method")
    mid = msg.get("id")
    is_notification = "id" not in msg          # JSON-RPC: a request with no id is a notification
    if method == "initialize":
        params = msg.get("params") or {}
        return _result(mid, {
            "protocolVersion": params.get("protocolVersion") or PROTOCOL_VERSION,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": manifest._engine_version()},
        })
    if method and method.startswith("notifications/"):
        return None
    if method == "ping":
        return _result(mid, {})
    if method == "tools/list":
        return _result(mid, {"tools": _tools()})
    if method == "tools/call":
        return _call(mid, msg.get("params") or {})
    if is_notification:
        return None
    return _error(mid, -32601, f"method not found: {method}")


def serve() -> int:
    """Read newline-delimited JSON-RPC from stdin, write one response line per request. EOF = exit."""
    out = sys.stdout
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except Exception:
            out.write(json.dumps(_error(None, -32700, "parse error")) + "\n")
            out.flush()
            continue
        resp = handle(msg)
        if resp is not None:
            out.write(json.dumps(resp, ensure_ascii=False) + "\n")
            out.flush()
    return 0


def _setup_line() -> str:
    return f"claude mcp add plainkeep -- {_dispatcher_bin()} mcp"


def main(argv: list[str]) -> int:
    _, argv = output.parse_argv(argv)
    if "--setup" in argv:
        print(_setup_line())
        return 0
    return serve()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
