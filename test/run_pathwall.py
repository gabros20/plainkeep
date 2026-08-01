#!/usr/bin/env python3
"""
run_pathwall.py — proves the path-wall is ON the write path, by FILESYSTEM SIDE EFFECT.

Why this suite exists, stated plainly because a green suite here is easy to over-read: until
`bin/lib/vaultio.py`, `guardrail.classify()` was never called by any verb. It was reachable from
the test harness and re-exported to plugins through `lib/api.py`, and that was all. The dispatcher
gate (`guardrail.gate`) admitted a verb on its DECLARED RISK CLASS and nothing looked at the path
the verb then wrote to. So "the guardrail refused it" could not be concluded from an exit code, and
a guardrail unit test could not have caught it — the failing region was never exercised.

Hence the shape of every assertion below: **walk the filesystem and count files**. An exit code is
checked too, but it is never the proof.

Offline, stdlib only.
"""
from __future__ import annotations
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
GREEN, RED, DIM, BOLD, RESET = "\033[32m", "\033[31m", "\033[2m", "\033[1m", "\033[0m"
results: list[tuple[str, bool, str]] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    results.append((name, bool(cond), detail))


def run_verb(home: Path, real_home: Path, verb: str, *args, **envextra):
    """Invoke a verb's run.py directly with PLAINKEEP_HOME=home. `real_home` becomes $HOME so the
    CONVENTIONAL `~/plainkeep` root can never accidentally satisfy the wall during a test."""
    env = {**os.environ, "PLAINKEEP_HOME": str(home), "HOME": str(real_home), **envextra}
    env.pop("PLAINKEEP_TEST_HOME", None)
    return subprocess.run([sys.executable, str(REPO / "bin" / verb / "run.py"), *args],
                          capture_output=True, text=True, env=env)


def files_under(root: Path) -> list[Path]:
    return [p for p in root.rglob("*") if p.is_file()]


# --------------------------------------------------------------------------------------------
# A. The wrong-root side-effect gate: a policy-denied data root must produce ZERO files.
# --------------------------------------------------------------------------------------------
def case_walled_root() -> None:
    with tempfile.TemporaryDirectory() as td:
        h = Path(td)
        # "Mobile Documents" is an absolute wall (guardrail.WALLED_OFF_MARKERS) — a vault synced
        # into iCloud is the one bad root the wall can recognise WITHOUT the ADR-014 root
        # validation, so it is what this suite can honestly prove today.
        root = h / "Library" / "Mobile Documents" / "com~apple~CloudDocs" / "vault"
        root.mkdir(parents=True)
        before = len(files_under(h))
        r = run_verb(root, h, "capture", "this must never reach an iCloud-synced vault")
        after = files_under(h)
        check("walled root: capture refuses with EXIT_DENY (5)", r.returncode == 5,
              f"rc={r.returncode} out={r.stdout.strip()} err={r.stderr.strip()}")
        check("walled root: capture wrote ZERO files (filesystem walk, not an exit code)",
              len(after) == before, f"created: {[str(p.relative_to(h)) for p in after]}")
        check("walled root: the refusal names the wall",
              "walled off" in (r.stdout + r.stderr).lower(), (r.stdout + r.stderr).strip())


# --------------------------------------------------------------------------------------------
# B. Regression: a correctly-resolved root still writes. A wall that denies everything is not a wall.
# --------------------------------------------------------------------------------------------
def case_good_root() -> None:
    with tempfile.TemporaryDirectory() as td:
        h = Path(td)
        root = h / "vault"
        root.mkdir()
        r = run_verb(root, h, "capture", "an ordinary thought")
        caps = list((root / "inbox").glob("cap-*.md"))
        check("good root: capture still succeeds", r.returncode == 0, r.stdout + r.stderr)
        check("good root: the inbox note exists", len(caps) == 1, f"found {caps}")
        check("good root: the journal line was appended",
              any("captured:" in p.read_text(encoding="utf-8")
                  for p in (root / "journal").rglob("*.md")))


# --------------------------------------------------------------------------------------------
# C. Symlink escape: the wall takes the STRICTER of path and realpath, so a subtree symlinked out
#    of the vault is refused even though the unresolved path looks fine.
# --------------------------------------------------------------------------------------------
def case_symlink_escape() -> None:
    with tempfile.TemporaryDirectory() as td:
        h = Path(td)
        root, outside = h / "vault", h / "outside"
        root.mkdir()
        outside.mkdir()
        (root / "inbox").symlink_to(outside, target_is_directory=True)
        r = run_verb(root, h, "capture", "escaping through a symlinked inbox")
        check("symlink escape: refused with EXIT_DENY (5)", r.returncode == 5,
              f"rc={r.returncode} out={r.stdout.strip()} err={r.stderr.strip()}")
        check("symlink escape: ZERO files landed outside the vault",
              len(files_under(outside)) == 0,
              f"created: {[str(p) for p in files_under(outside)]}")


# --------------------------------------------------------------------------------------------
# D. The wall reaches the SDK too: a plugin journalling through lib/api.py inherits it.
# --------------------------------------------------------------------------------------------
def case_sdk_journal() -> None:
    with tempfile.TemporaryDirectory() as td:
        h = Path(td)
        root = h / "Library" / "Mobile Documents" / "vault"
        root.mkdir(parents=True)
        script = h / "plug.py"
        script.write_text(
            "import sys\n"
            f"sys.path.insert(0, {str(REPO / 'bin')!r})\n"
            "from lib import api\n"
            "api.append_journal('a plugin writing through the frozen SDK')\n",
            encoding="utf-8")
        env = {**os.environ, "PLAINKEEP_HOME": str(root), "HOME": str(h)}
        env.pop("PLAINKEEP_TEST_HOME", None)
        r = subprocess.run([sys.executable, str(script)], capture_output=True, text=True, env=env)
        check("SDK journal: append_journal through api.py is refused (5)", r.returncode == 5,
              f"rc={r.returncode} err={r.stderr.strip()}")
        check("SDK journal: ZERO files under the walled root",
              len(files_under(root)) == 0, f"created: {files_under(root)}")


# --------------------------------------------------------------------------------------------
# E. The ratchet: no NEW unguarded write may appear in bin/ without joining the exemption list.
#
# Each exemption is a path that the wall as written DENIES but the verb legitimately needs — a
# launchd plist, the ~/.local/bin launcher, a ~/work tree. Those are a POLICY question (does the
# wall's model cover verb-owned writes outside the three roots?), not something to paper over by
# widening the wall. The list may shrink; anything new is a failure.
# --------------------------------------------------------------------------------------------
# `vaultio` (and the `io` alias lib/paths.py binds it to) ARE the seam — a call on them is guarded.
GUARDED_RECEIVERS = {"vaultio", "io"}
METHOD_WRITE = re.compile(r"(\w+)?\.(write_text|write_bytes|mkdir)\s*\(")
SHUTIL_WRITE = re.compile(r"\bshutil\.(?:move|copy2|copytree|copyfile)\s*\(")
PATH_REPLACE = re.compile(r"\.replace\s*\(\s*(?:target|dest|dst|out|path)\b")
OPEN_WRITE = re.compile(r"\bopen\(\s*[^)]*?,\s*[\"'](?:w|a|wb|ab)[\"']")


def _is_raw_write(code: str) -> bool:
    for m in METHOD_WRITE.finditer(code):
        if (m.group(1) or "") not in GUARDED_RECEIVERS:
            return True
    return bool(SHUTIL_WRITE.search(code) or PATH_REPLACE.search(code) or OPEN_WRITE.search(code))


# file -> {exact source line (stripped): why it is NOT behind the wall}.
#
# Keyed by source text, not line number, so an unrelated edit above does not fake a failure — and so
# the reason travels with the code it excuses. EVERY entry here is a write the wall as currently
# written would DENY, for a destination the verb legitimately needs. That is a POLICY gap in the
# wall's model (it was authored for agent actions, not verb-owned ones), and papering over it by
# widening the wall would cost more than it buys. ADR-014 / Phase 2 Task 1 is where it gets decided.
EXEMPT: dict[str, dict[str, str]] = {
    "bin/lib/guardrail.py": {
        'logdir.mkdir(parents=True, exist_ok=True)':
            "the guardrail's own audit log — it records the refusal, so it cannot be subject to it",
        'with open(logdir / "plainkeep.log", "a", encoding="utf-8") as f:':
            "same: the append that logs a DENY must not itself be gated on a DENY",
    },
    "bin/archive/run.py": {
        'dest_dir.mkdir(parents=True, exist_ok=True)':
            "~/work/archive/<year> — the bundle destination for an archived fleet repo",
    },
    "bin/new/run.py": {
        'repo.parent.mkdir(parents=True, exist_ok=True)':
            "~/work/<kind>/<slug> — _write_verdict denies a ~/work write unless it is the current "
            "task's repo (guardrail.py's WORK branch), and `new project` has no task context",
        'shutil.copytree(TEMPLATE, repo)': "same ~/work project tree",
        'p.write_text(t, encoding="utf-8")':
            "_fill() substituting template placeholders INSIDE the ~/work repo just created",
        '(tree / sub).mkdir(parents=True, exist_ok=True)':
            "`new client` creates in/out/work — and in/ is the walled originals directory",
    },
    # The sharpest one, and it is a CONTRADICTION rather than an omission: the wall's model says
    # "~/files/**/in/ originals are read-only evidence" (guardrail._in_originals, validated case
    # `originals-in-readonly`), while `files ingest --client` and `new client` exist precisely to
    # PUT an original into in/. Creating a new original is not modifying evidence, but the rule as
    # validated does not draw that line, and a wiring commit is the wrong place to redraw it.
    "bin/files/run.py": {
        'dest_dir.mkdir(parents=True, exist_ok=True)':
            "ingest arrival directory — may be ~/files/<hub>/in/, which the wall denies",
        'shutil.move(str(src), str(dest))':
            "the ingest move itself; the verb's own uniquifying loop is what guarantees it never "
            "overwrites an existing original",
    },
    "bin/repo/run.py": {
        'dest.parent.mkdir(parents=True, exist_ok=True)': "~/work fleet clone + adopt destination",
        'shutil.move(str(src), str(dest))': "~/work fleet adopt: moves an existing repo into the fleet",
    },
    "bin/share/run.py": {
        'Path(out).write_bytes(html_bytes)':
            "`--out <path>`: a destination the human named on the command line, deliberately "
            "outside the vault (that is the point of exporting)",
        'Path(out).write_text(md, encoding="utf-8")': "same `--out <path>`",
    },
    "bin/sweep/run.py": {
        'trash.mkdir(parents=True, exist_ok=True)':
            "~/.Trash — outside the three roots BY DESIGN: the recoverable end of the decay machine "
            "(sweep/run.py:34)",
        'shutil.move(str(item), str(tdest))': "same ~/.Trash move",
    },
}


def scan_raw_writes() -> dict[str, dict[int, str]]:
    found: dict[str, dict[int, str]] = {}
    for f in sorted((REPO / "bin").rglob("*.py")):
        rel = str(f.relative_to(REPO))
        if rel == "bin/lib/vaultio.py":       # the seam itself is where the real writes live
            continue
        for i, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
            code = line.split("#", 1)[0]
            if _is_raw_write(code):
                found.setdefault(rel, {})[i] = line.strip()
    return found


def case_ratchet() -> None:
    found = scan_raw_writes()
    unexpected = []
    for rel, lines in found.items():
        allowed = EXEMPT.get(rel, {})
        for ln, text in lines.items():
            if not any(text.startswith(k) for k in allowed):
                unexpected.append(f"{rel}:{ln}  {text}")
    check("ratchet: every raw write in bin/ is either guarded or a listed exemption",
          not unexpected, "\n        " + "\n        ".join(unexpected[:40]))

    stale = [f"{rel}: {k}" for rel, lines in EXEMPT.items() for k in lines
             if not any(t.startswith(k) for t in found.get(rel, {}).values())]
    check("ratchet: no stale exemptions (a fixed site must leave the list)", not stale, str(stale))


def main() -> int:
    case_walled_root()
    case_good_root()
    case_symlink_escape()
    case_sdk_journal()
    case_ratchet()

    print(f"{BOLD}Path-wall enforcement (bin/lib/vaultio.py) — {len(results)} checks{RESET}\n")
    passed = sum(1 for _, ok, _ in results if ok)
    for name, ok, detail in results:
        mark = f"{GREEN}PASS{RESET}" if ok else f"{RED}FAIL{RESET}"
        print(f"  {mark} {name:<62}" + (f" {DIM}{detail}{RESET}" if (detail and not ok) else ""))
    failed = len(results) - passed
    exempt_total = sum(len(v) for v in EXEMPT.values())
    if exempt_total:
        print(f"\nSUITE-NOTE: {exempt_total} raw write site(s) in bin/ are NOT behind the wall — "
              f"~/work fleet trees, ~/.Trash, a human-supplied `--out`, and the guardrail's own "
              f"audit log. The wall as written DENIES all of them; whether its model should cover "
              f"verb-owned writes outside the three roots is a policy decision, not a wiring fix.")
    print("SUITE-NOTE: a MISRESOLVED data root is still not detectable here — the wall is anchored "
          "to the root it would have to doubt. Root validation is ADR-014 / Phase 2 Task 1.")
    print(f"\n{BOLD}Result:{RESET} {GREEN}{passed} passed{RESET}, "
          f"{(RED if failed else DIM)}{failed} failed{RESET}, {len(results)} checks")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
