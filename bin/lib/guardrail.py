"""
guardrail.py — the §5 safety layer, ENFORCED (not just modeled). Mirrors the validated spec model
in test/lib/guardrail.py (kept in lock-step by test/run_guardrail.py's parity check). Two jobs:

  1. classify(action) — the path-wall + risk decision for a concrete action (write/read/transmit/
     delete/verb). Reusable: a write-verb calls this on the path IT computes (Iron Law — the verb
     owns placement), so the wall holds where the path is actually known.
  2. gate(verb, args, risk) + the CLI — the dispatcher's per-verb risk gate: deny is refused,
     confirm needs an explicit --yes, new/undeclared verbs default to confirm. Logs every call.

The dispatcher runs `guardrail.py <verb> <args...>` before exec; nonzero exit blocks the verb.
"""
from __future__ import annotations
import json
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

# bin/lib on sys.path FIRST: this file is exec'd standalone by the bash floor and by the parity
# oracle, where the sibling modules below are not importable any other way.
_LIB = os.path.dirname(os.path.abspath(__file__))
if _LIB not in sys.path:
    sys.path.insert(0, _LIB)

# Exit-code protocol (Part 0.3) — single source of truth is lib/output.py; imported so the guardrail
# CLI and dispatcher speak the same codes. Falls back to the literals when loaded in isolation.
try:
    from output import EXIT_OK, EXIT_USAGE, EXIT_CONFIRM, EXIT_NOT_FOUND, EXIT_DENY  # noqa: F401
except Exception:
    EXIT_OK, EXIT_USAGE, EXIT_CONFIRM, EXIT_NOT_FOUND, EXIT_DENY = 0, 2, 3, 4, 5

# The LOCATION policy (wall.py) and the SELECTED data root (vaultroot.py). Neither import is guarded:
# a missing sibling means a shredded engine tree, and the whole point of Task 1b is that a guardrail
# which cannot establish where the vault is must refuse rather than guess.
import vaultroot  # noqa: E402
from wall import HOME, SYNC_DIR_MARKERS, WALLED_OFF_MARKERS, under_sync_dir  # noqa: E402,F401
from wall import is_walled as wall_is_walled  # noqa: E402

# Verb resolution (Part 2.1): the resolver is the single source of truth for the verb set and
# cmd.json lookup, so plugin verbs (plugins/<pack>/, $PLAINKEEP_PATH) are gated identically to engine ones.
# Falls back to a bin/-only glob if loaded in isolation (the parity test execs this file standalone
# with only stdlib on the path — resolver still imports there, but keep the guard defensive).
try:
    import resolver as _resolver  # noqa: E402
except Exception:
    _resolver = None

# The SELECTED data root. No `parents[2]` fallback: deriving the vault from where the engine happens
# to sit is the assumption ADR-014 deletes, and its failure mode is a successful write to the wrong
# root with exit 0. `active_root()` refuses (exit 2) when nothing selected one.
PLAINKEEP_HOME = vaultroot.active_root()
BIN = Path(__file__).resolve().parents[1]

VAULT = f"{HOME}/plainkeep"
WORK = f"{HOME}/work"
FILES = f"{HOME}/files"
DOTFILES = f"{HOME}/dotfiles"


def _with_real(*roots: str) -> list[str]:
    """A root plus its resolved form. `classify()` re-runs `_write_verdict` on the realpath and takes
    the STRICTER verdict — on macOS a root under `/tmp` or `/var/folders` resolves through a symlink
    (`/private/…`), so without the resolved form every legitimate write there reads as an escape."""
    out: list[str] = []
    for r in roots:
        for v in (os.path.abspath(os.path.normpath(r)), os.path.realpath(r)):
            if v not in out:
                out.append(v)
    return out


# Every path that counts as "inside the vault" for the write-wall.
#
# `VAULT` alone is the CONVENTIONAL location, built from `$HOME` — it does not move with
# `PLAINKEEP_HOME`, so a vault anywhere else had every guarded write denied. That was invisible
# while nothing called `classify()` on a write path (see lib/vaultio.py); the moment the seam is
# live it would deny the vault's own verbs, so the wall follows the ACTIVE data root as well.
#
# NARROWING TO ONE ROOT is the next commit (ADR-014 D5): the root is validated as of this commit,
# but the wall still carries the conventional location too, so the 51 validated cases and their
# harnesses can move together rather than half a change at a time.
VAULT_ROOTS = _with_real(VAULT, str(PLAINKEEP_HOME))
FILES_ROOTS = _with_real(FILES)
WORK_ROOTS = _with_real(WORK)
WORKTREE_ROOTS = _with_real(f"{WORK}/.worktrees")
DOTFILES_ROOTS = _with_real(DOTFILES)

# HOME / WALLED_OFF_MARKERS / SYNC_DIR_MARKERS / under_sync_dir are re-exported from wall.py (see the
# import above). They moved because vault SELECTION has to ask the same location question before a
# root exists, and this module cannot be imported that early any more.
ALLOW, CONFIRM, DENY = "allow", "confirm", "deny"
_ORDER = {ALLOW: 0, CONFIRM: 1, DENY: 2}
SCHEDULABLE = {"read", "safe_write"}

TRANSMIT_PATTERNS = [
    (r"\bgit\s+push\b", "git push"), (r"\b(npm|pnpm|yarn|bun)\s+publish\b", "package publish"),
    (r"\b(vercel|netlify|flyctl|fly|wrangler|gcloud|heroku|render|railway)\b.*\bdeploy", "deploy"),
    (r"\baws\s+s3\b", "aws s3"), (r"\bgsutil\b", "gsutil"), (r"\bscp\b", "scp"),
    (r"\brsync\b.*(::|\S+@\S+:)", "rsync remote"),
    (r"\bgh\s+(pr\s+merge|release\s+create|api\b.*-X\s*(POST|PUT|PATCH|DELETE))", "gh write"),
    (r"\bcurl\b.*(-X\s*(POST|PUT|PATCH|DELETE)|--data\b|--data-\w+|-d\b|--upload-file\b|-T\b)", "curl write"),
    (r"\bwget\b.*--post", "wget post"),
]
DANGER_RM = [r"\brm\s+-[a-z]*r[a-z]*f", r"\brm\s+-[a-z]*f[a-z]*r",
             r"\brm\s+-r\b.*\s-f\b", r"\brm\s+-f\b.*\s-r\b",
             r"\brm\s+--recursive\b.*--force\b", r"\brm\s+--force\b.*--recursive\b"]


@dataclass
class Decision:
    verdict: str
    reason: str
    risk_class: str

    def __str__(self) -> str:
        return f"{self.verdict.upper()} [{self.risk_class}] — {self.reason}"


def _norm(path):
    if not path:
        return None
    p = path.strip()
    if p.startswith("~"):
        p = HOME + p[1:]
    return os.path.normpath(p)


def _under(path, root):
    pl, rl = path.lower(), root.lower()
    return pl == rl or pl.startswith(rl + "/")


def _walled(path):
    return wall_is_walled(path)


def _under_any(path, roots) -> bool:
    return any(_under(path, r) for r in roots)


def _in_originals(path):
    return _under_any(path, FILES_ROOTS) and re.search(r"/in(/|$)", path, re.IGNORECASE) is not None


def _write_verdict(path, action):
    if _walled(path):
        return Decision(DENY, "iCloud/family path is walled off — propose, never write", "deny")
    if _in_originals(path):
        return Decision(DENY, "~/files/**/in/ originals are read-only evidence", "deny")
    if _under_any(path, VAULT_ROOTS):
        return Decision(ALLOW, "write inside ~/plainkeep is a revertible git diff", "safe_write")
    if _under_any(path, FILES_ROOTS):
        return Decision(ALLOW, "write inside ~/files (out/work/research)", "safe_write")
    if _under_any(path, WORKTREE_ROOTS):
        return Decision(ALLOW, "write inside ~/work/.worktrees (sanctioned agent worktree)", "safe_write")
    if _under_any(path, WORK_ROOTS):
        task_repo = _norm(action.get("task_repo"))
        repo = _norm(action.get("repo")) or path
        if task_repo and (repo == task_repo or _under(path, task_repo)):
            return Decision(ALLOW, "write inside the task's ~/work repo", "safe_write")
        return Decision(DENY, "write to a ~/work repo that is not the current task's repo", "deny")
    if _under_any(path, DOTFILES_ROOTS):
        return Decision(CONFIRM, "~/dotfiles: inspect, don't change without being asked", "confirm")
    return Decision(DENY, f"path escapes the three roots: {path}", "deny")


def classify(action: dict) -> Decision:
    kind = action.get("kind", "verb")
    flags = action.get("flags", {}) or {}
    yes, force = bool(flags.get("yes")), bool(flags.get("force"))
    path, realpath = _norm(action.get("path")), _norm(action.get("realpath"))
    command = (action.get("command") or "").strip()

    if command:
        if any(re.search(p, command) for p in DANGER_RM):
            return Decision(DENY, f"recursive forced delete is denied: '{command}'", "deny")
        if re.search(r"git\s+push\b.*(--force\b|--force-with-lease\b|\s-f\b)", command):
            return Decision(DENY, f"force push is denied: '{command}'", "deny")
        for pat, label in TRANSMIT_PATTERNS:
            if re.search(pat, command, re.IGNORECASE):
                if force:
                    return Decision(DENY, f"forced external transmit ({label}) is denied", "deny")
                return Decision(ALLOW if yes else CONFIRM,
                                f"external transmit ({label})" + (" authorized by --yes" if yes else " needs --yes"),
                                "confirm")
    if kind == "read_secret" or (path and re.search(r"(^|/)\.env($|\.|/)", path)):
        return Decision(DENY, "reading .env / secret values is denied", "deny")
    if kind == "resolve_secret":
        return Decision(DENY, "resolving an op:// secret to its value is denied", "deny")
    if kind == "read":
        for p in (path, realpath):
            if p and _walled(p):
                return Decision(DENY, "reading the iCloud/family tree is off every command path", "deny")
        return Decision(ALLOW, "read-only", "read")
    if kind == "transmit":
        if force:
            return Decision(DENY, "forced external transmit is denied", "deny")
        return Decision(ALLOW if yes else CONFIRM, "external transmit" + (" via --yes" if yes else " needs --yes"), "confirm")
    if kind == "delete":
        return Decision(DENY if force else CONFIRM, "delete is irreversible" + (" (forced=deny)" if force else " — needs --yes"), "confirm")
    if kind == "draft":
        return Decision(ALLOW, "draft produced; a human transmits", "draft_only")
    if kind in ("propose", "ask"):
        return Decision(ALLOW, f"{kind} has no side effect", "read")
    if kind == "write":
        if not path:
            return Decision(DENY, "write with no resolvable path", "deny")
        primary = _write_verdict(path, action)
        if realpath and realpath != path:
            secondary = _write_verdict(realpath, action)
            if _ORDER[secondary.verdict] > _ORDER[primary.verdict]:
                return Decision(secondary.verdict, f"symlink resolves to {realpath}: {secondary.reason}", secondary.risk_class)
        return primary
    if kind == "verb":
        v = (action.get("verb") or "").strip()
        if v.startswith("plainkeep "):
            verb = v[10:].split()[0] if len(v) > 10 else ""
            if verb and verb not in _known_verbs():
                return Decision(DENY, f"unknown/invented plainkeep verb: '{verb}'", "deny")
            return Decision(ALLOW, "known plainkeep verb", "read")
        return Decision(ALLOW, "raw shell command (path wall + adapter scoping govern it)", "read")
    return Decision(DENY, f"unrecognized action kind: {kind}", "deny")


def _known_verbs() -> set:
    if _resolver is not None:
        return _resolver.known_verbs()
    return {p.parent.name for p in BIN.glob("*/cmd.json")} | {p.parent.name for p in BIN.glob("*/run.py")}


def _cmd_field(verb: str, key: str):
    """Read one field from a verb's RESOLVED cmd.json (engine wins over plugins); None on failure."""
    f = _resolver.cmd_json_path(verb) if _resolver is not None else (BIN / verb / "cmd.json")
    if not f or not f.exists():
        return None
    try:
        return json.loads(f.read_text(encoding="utf-8")).get(key)
    except Exception:
        return None


def risk_of(verb: str) -> str | None:
    """Read a verb's declared risk from its cmd.json; None if undeclared (→ default confirm)."""
    return _cmd_field(verb, "risk")


def _declares_dry_run(verb: str) -> bool:
    """True if the verb's cmd.json declares `"dry_run": true` (proposal Part 0.5)."""
    return bool(_cmd_field(verb, "dry_run"))


def _plugin_lock() -> dict:
    """The `{pack: entry}` map from plugins/plugins.lock.json (empty on any failure)."""
    try:
        f = PLAINKEEP_HOME / "plugins" / "plugins.lock.json"
        return json.loads(f.read_text(encoding="utf-8")).get("plugins", {})
    except Exception:
        return {}


def _plugin_ceiling(verb: str) -> str | None:
    """The trust ceiling for a verb from an EXTERNALLY-installed pack (proposal Part 2.2): 'confirm'
    if the verb belongs to a pack recorded in plugins.lock.json that has not been trusted — a pack's
    self-declared risk NEVER takes effect at install. Returns None for engine verbs, for user-placed
    packs with no lock entry (plugins/local, $PLAINKEEP_PATH — the user put them there directly), and for
    explicitly trusted packs (their declared risk then stands). The transmit-block + path-wall in
    classify() apply to every verb regardless, so a trusted pack is still walled."""
    if _resolver is None:
        return None
    src = _resolver.source_of(verb)
    if not src or not src.startswith("plugin:"):
        return None
    pack = src.split(":", 1)[1]
    entry = _plugin_lock().get(pack)
    if entry is None or entry.get("trusted"):
        return None
    return "confirm"


def gate(verb: str, args: list[str], risk: str | None = None) -> Decision:
    """Dispatcher per-verb gate: enforce the declared risk class (new verbs default to confirm).
    `risk` override is for tests; in normal use it's read from the verb's cmd.json.

    Dry-run rule (Part 0.5): a verb that declares `"dry_run": true`, invoked with `--dry-run`, is
    downgraded to a read — a true dry-run IS a read — so confirm-class verbs stay explorable without
    `--yes`."""
    if verb not in _known_verbs():
        return Decision(DENY, f"unknown/invented verb: '{verb}' (not in plainkeep.json)", "deny")
    risk = risk or risk_of(verb) or "confirm"  # §5: new/undeclared verbs default to confirm
    yes = ("--yes" in args) or ("-y" in args)
    dry = "--dry-run" in args
    if risk == "deny":
        return Decision(DENY, f"'{verb}' is deny-class — never run", "deny")
    # Trust ceiling (Part 2.2): an untrusted external pack's verb can't self-declare its way below
    # confirm, and can't use --dry-run to bypass it (we can't trust the pack to honour dry-run).
    capped = _plugin_ceiling(verb) == "confirm"
    if not capped and dry and _declares_dry_run(verb) and risk in ("confirm", "safe_write"):
        return Decision(ALLOW, f"--dry-run downgrades {risk} to read (no side effect)", "read")
    if capped and risk in ("read", "safe_write"):
        risk = "confirm"
    if risk == "confirm" and not yes:
        return Decision(CONFIRM, f"'{verb}' is confirm-class — re-run with --yes to proceed", "confirm")
    return Decision(ALLOW, f"{risk}", risk)


def _log(verb, args, d: Decision):
    try:
        logdir = PLAINKEEP_HOME / ".logs"
        logdir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with open(logdir / "plainkeep.log", "a", encoding="utf-8") as f:
            f.write(f"{ts}\t{verb} {' '.join(args)}\t{d.verdict}\t{d.reason}\n")
    except Exception:
        pass


def _remediation(verb: str, args: list[str]) -> str:
    """The exact re-run line a confirm-class refusal should print — self-teaching, not a class name."""
    parts = ["plainkeep", verb, *args]
    if "--yes" not in args and "-y" not in args:
        parts.append("--yes")
    return "re-run: " + " ".join(parts)


def exit_code_for(d: Decision) -> int:
    """Map a gate verdict to the shared exit-code protocol (Part 0.3): confirm→3, deny→5."""
    return {ALLOW: EXIT_OK, CONFIRM: EXIT_CONFIRM, DENY: EXIT_DENY}[d.verdict]


def main_cli(argv: list[str]) -> int:
    """Dispatcher-side gate: unknown verb → not-found (4) with a did-you-mean; else the risk gate,
    logged, with confirm→3 (printing the exact remediation) and deny→5."""
    import difflib
    verb = argv[0] if argv else "help"
    args = argv[1:]
    known = _known_verbs()
    if verb not in known:
        near = difflib.get_close_matches(verb, sorted(known), n=3, cutoff=0.6)
        hint = f" did you mean: {', '.join(near)}?" if near else " (run: plainkeep help)"
        print(f"plainkeep: unknown verb '{verb}'.{hint}", file=sys.stderr)
        return EXIT_NOT_FOUND
    d = gate(verb, args)
    _log(verb, args, d)
    if d.verdict == CONFIRM:
        print(f"guardrail: {d}\n  {_remediation(verb, args)}", file=sys.stderr)
    elif d.verdict == DENY:
        print(f"guardrail: {d}", file=sys.stderr)
    return exit_code_for(d)


if __name__ == "__main__":
    raise SystemExit(main_cli(sys.argv[1:]))
