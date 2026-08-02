#!/usr/bin/env python3
"""run_mcp.py — drives `plainkeep mcp` (proposal Part 2.4) as a subprocess over stdin/stdout pipes: the
JSON-RPC/MCP handshake (initialize · tools/list · tools/call), tool generation from the plainkeep surface,
subprocess re-entry through the dispatcher, and the exit-3 → structured needs-`--yes` mapping.

Fully offline: the working tree is copied into a throwaway vault (so the dispatcher finds bin/ under
the test PLAINKEEP_HOME, including this un-committed verb) and seeded + indexed, so `tools/call search`
re-enters through the real `plainkeep` and returns real rows."""
from __future__ import annotations
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from lib.hermetic import seal
from lib.vaultfx import mark_engine_vault
seal()   # hermetic: an empty throwaway registry, never the developer's real vault

REPO = Path(__file__).resolve().parents[1]
GREEN, RED, DIM, BOLD, RESET = "\033[32m", "\033[31m", "\033[2m", "\033[1m", "\033[0m"
results = []


def check(name, cond, detail=""):
    results.append((name, cond, detail))


def drive(ops, env, messages) -> dict:
    """Send each message as one JSON line; return {id: response} parsed from the server's output."""
    proc = subprocess.run([str(ops), "mcp"], input="".join(json.dumps(m) + "\n" for m in messages),
                          capture_output=True, text=True, env=env)
    out = {}
    for ln in proc.stdout.splitlines():
        ln = ln.strip()
        if not ln:
            continue
        m = json.loads(ln)
        out[m.get("id")] = m
    return out


def main() -> int:
    with tempfile.TemporaryDirectory() as td:
        vault = Path(td) / "plainkeep"
        shutil.copytree(REPO, vault,
                        ignore=shutil.ignore_patterns(".git", ".index", ".logs", "__pycache__", "*.pyc"))
        ops = vault / "plainkeep"
        (vault / "wiki" / "notes" / "widget.md").write_text(
            "---\ntype: note\ntitle: Widget design\nstatus: active\ntags: [demo]\n---\n"
            "# Widget design\n\nThe widget subsystem is the heart of the demo.\n", encoding="utf-8")
        env = {**os.environ, "PLAINKEEP_HOME": str(vault)}
        subprocess.run([str(ops), "index"], capture_output=True, text=True, env=env)

        resp = drive(ops, env, [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize",
             "params": {"protocolVersion": "2024-11-05", "capabilities": {}}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},   # notification → no response
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
             "params": {"name": "search", "arguments": {"query": "widget"}}},
            {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
             "params": {"name": "plugin", "arguments": {"action": "add", "target": "local/x"}}},
            {"jsonrpc": "2.0", "id": 5, "method": "no/such/method"},
        ])

    # initialize handshake
    init = resp.get(1, {}).get("result", {})
    check("initialize returns serverInfo 'plainkeep'", init.get("serverInfo", {}).get("name") == "plainkeep")
    check("initialize echoes protocolVersion", init.get("protocolVersion") == "2024-11-05")
    check("initialize advertises tools capability", "tools" in init.get("capabilities", {}))

    # notification produced no response line
    check("notifications/initialized draws no reply", None not in resp)

    # tools/list is generated from the plainkeep surface
    tools = {t["name"]: t for t in resp.get(2, {}).get("result", {}).get("tools", [])}
    check("tools/list includes search + capture + task", {"search", "capture", "task"} <= set(tools))
    check("hidden verbs (mcp, __complete) are NOT exposed", "mcp" not in tools and "__complete" not in tools)
    s = tools.get("search", {})
    check("search tool has an inputSchema with a required query",
          s.get("inputSchema", {}).get("required") == ["query"]
          and "query" in s.get("inputSchema", {}).get("properties", {}))
    check("search tool description carries its hints", "keyword" in s.get("description", "").lower())

    # tools/call search → real rows (NDJSON envelope in the text content)
    r3 = resp.get(3, {}).get("result", {})
    check("tools/call search is not an error", r3.get("isError") is not True, str(r3)[:120])
    body = r3.get("content", [{}])[0].get("text", "")
    lines = [ln for ln in body.splitlines() if ln.strip()]
    hdr = json.loads(lines[0]) if lines else {}
    check("search result is the NDJSON envelope", hdr.get("ops_json") == 1 and hdr.get("ok") is True)
    check("search returned at least one row", hdr.get("count", 0) >= 1 and len(lines) > 1, body[:120])

    # tools/call on a confirm-class call WITHOUT --yes → structured needs-yes, never an execution
    r4 = resp.get(4, {}).get("result", {})
    check("confirm call is flagged isError", r4.get("isError") is True)
    payload = json.loads(r4.get("content", [{}])[0].get("text", "{}"))
    check("confirm call returns ops_confirm_needed", payload.get("ops_confirm_needed") is True, str(payload)[:120])
    check("confirm call gives the exact --yes re-run",
          payload.get("rerun") == "plainkeep plugin add local/x --yes", payload.get("rerun", ""))

    # unknown method → JSON-RPC method-not-found, gracefully
    check("unknown method → -32601", resp.get(5, {}).get("error", {}).get("code") == -32601)

    # --setup prints the install line. Against a throwaway engine-carrying vault rather than the
    # checkout: this runs the REAL dispatcher, whose gate appends to `<root>/.logs/plainkeep.log`,
    # and with the checkout as the root that line landed in the developer's own vault.
    with tempfile.TemporaryDirectory() as td:
        sh = Path(td)
        mark_engine_vault(sh, REPO)
        setup = subprocess.run([str(sh / "plainkeep"), "mcp", "--setup"], capture_output=True,
                               text=True, env={**os.environ, "PLAINKEEP_HOME": str(sh)})
    check("--setup prints the `claude mcp add plainkeep` line",
          "claude mcp add plainkeep --" in setup.stdout and setup.stdout.rstrip().endswith("plainkeep mcp"),
          setup.stdout.strip())

    print(f"{BOLD}plainkeep mcp — stateless stdio MCP server (Part 2.4) — {len(results)} checks{RESET}\n")
    passed = sum(1 for _, ok, _ in results if ok)
    for name, ok, detail in results:
        mark = f"{GREEN}PASS{RESET}" if ok else f"{RED}FAIL{RESET}"
        print(f"  {mark} {name:<52}" + (f" {DIM}{str(detail).strip()[:80]}{RESET}" if (detail and not ok) else ""))
    failed = len(results) - passed
    print(f"\n{BOLD}Result:{RESET} {GREEN}{passed} passed{RESET}, "
          f"{(RED if failed else DIM)}{failed} failed{RESET}, {len(results)} checks")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
