"""
agent.py — §6 agent indirection: the ONE swappable point where a verb borrows a model's judgment.

A verb calls run_agent(prompt, scope); whatever agent is configured via PLAINKEEP_AGENT answers, or — if
none is set or it fails — the call returns None and the verb uses its deterministic shell fallback.
The verb never knows which agent (or none) ran: identical surface either way. This is how `plainkeep triage`
/ `plainkeep close` can be smarter WITH a model and still fully functional WITHOUT one.

Configure in your dotfiles:
    export PLAINKEEP_AGENT="claude"        # or "codex", "grok", "ollama", or "none" (default)
    export PLAINKEEP_AGENT_MODEL="gemma4:e2b"   # for PLAINKEEP_AGENT=ollama (triage; enrich has its own model)
    export PLAINKEEP_AGENT_CMD="my-agent --print"   # escape hatch: any command; the prompt is appended
"""
import os
import shlex
import subprocess

# The triage/classify default: small and fast, kept separate from the enrich default (enrichlib, gemma4:e4b).
DEFAULT_TRIAGE_MODEL = "gemma4:e2b"


def triage_model() -> str:
    """env PLAINKEEP_AGENT_MODEL → config `models.triage` → default (loaded lazily: the test suite
    imports this file standalone, with no package around it)."""
    try:
        try:
            from . import hostconfig
        except ImportError:
            import sys
            sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
            import hostconfig
        return hostconfig.get("models.triage", DEFAULT_TRIAGE_MODEL)
    except Exception:
        return os.environ.get("PLAINKEEP_AGENT_MODEL") or DEFAULT_TRIAGE_MODEL


def _agent() -> str:
    return (os.environ.get("PLAINKEEP_AGENT", "none") or "none").strip().lower()


def available() -> bool:
    """True if some agent is configured (a verb can decide whether to even build a prompt)."""
    return bool(os.environ.get("PLAINKEEP_AGENT_CMD")) or _agent() not in ("", "none")


def _command(prompt: str, scope: str):
    custom = os.environ.get("PLAINKEEP_AGENT_CMD")
    if custom:                                  # escape hatch: any command, prompt appended as one arg
        return shlex.split(custom) + [prompt]
    a = _agent()
    if a == "claude":
        cmd = ["claude", "-p", prompt, "--output-format", "text"]
        cmd += ["--allowedTools", "Read,Bash(plainkeep search:*)" if scope == "read" else "Read,Write,Edit,Bash(plainkeep:*)"]
        return cmd
    if a == "codex":
        return ["codex", "exec", prompt]
    if a == "grok":
        return ["grok", "-p", prompt]
    if a == "ollama":
        return ["ollama", "run", triage_model(), prompt]
    return None                                 # unknown agent → no command → fallback


def run_agent(prompt: str, scope: str = "read", timeout: int = 60):
    """Return the agent's text output, or None to signal 'use your fallback'. Never raises."""
    cmd = _command(prompt, scope)
    if not cmd:
        return None
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        out = (r.stdout or "").strip()
        return out or None
    except Exception:
        return None
