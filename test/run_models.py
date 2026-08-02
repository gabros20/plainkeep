#!/usr/bin/env python3
"""run_models.py — offline suite for `plainkeep models` (search-enrichment proposal §2.1: the download/
offload/on-demand test surface for the model layer). NEVER touches a real ollama daemon or downloads
anything: PATH is scrubbed so `ollama` resolves to nothing (list/status must still degrade cleanly,
not crash), and pull/test are only exercised WITHOUT --yes (the confirm gate) — the real --yes paths
would pull gigabytes and are never run here, mirroring how run_backup_share.py keeps the real transport
out of the test loop via PLAINKEEP_SHARE_FAKE (here there's nothing to fake: the gate itself is the seam)."""
from __future__ import annotations
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from lib.hermetic import seal
seal()   # hermetic: an empty throwaway registry, never the developer's real vault

REPO = Path(__file__).resolve().parents[1]
GREEN, RED, DIM, BOLD, RESET = "\033[32m", "\033[31m", "\033[2m", "\033[1m", "\033[0m"
results = []

# A PATH with no `ollama` on it — deterministic "ollama absent" regardless of the host running the suite.
NO_OLLAMA_PATH = "/usr/bin:/bin"


def check(name, cond, detail=""):
    results.append((name, bool(cond), detail))


def run(*args, home):
    env = {**os.environ, "PLAINKEEP_HOME": str(home), "PATH": NO_OLLAMA_PATH}
    return subprocess.run([sys.executable, str(REPO / "bin" / "models" / "run.py"), *args],
                          capture_output=True, text=True, env=env)


def main() -> int:
    with tempfile.TemporaryDirectory() as td:
        home = Path(td) / "ops"
        (home / "wiki").mkdir(parents=True)

        # ---------- list: read-only, exits 0, never crashes without ollama ----------
        r = run("list", "--json", home=home)
        check("models list exits 0 (ollama absent)", r.returncode == 0, r.stdout + r.stderr)
        lines = r.stdout.splitlines()
        head = json.loads(lines[0]) if lines else {}
        rows = [json.loads(ln) for ln in lines[1:]] if len(lines) > 1 else []
        check("models list is an ok NDJSON envelope", head.get("ok") is True, r.stdout)
        stages = {row.get("stage") for row in rows}
        check("models list covers all 6 stages",
              stages == {"stt", "ocr", "vlm", "enrich", "embed", "rerank"}, str(stages))
        check("models list rows carry model/runtime/available",
              all({"model", "runtime", "available"}.issubset(row) for row in rows), str(rows))
        check("models list: ollama-absent stages report unavailable",
              all(not row["available"] for row in rows if row["stage"] in ("enrich", "embed")), str(rows))

        r2 = run("list", home=home)  # human rendering path also must not crash
        check("models list (human) exits 0", r2.returncode == 0, r2.stdout + r2.stderr)

        # ---------- status: read-only, degrades cleanly when ollama is absent ----------
        r = run("status", "--json", home=home)
        check("models status exits 0 (ollama absent)", r.returncode == 0, r.stdout + r.stderr)
        shead = json.loads(r.stdout.splitlines()[0]) if r.stdout.strip() else {}
        check("models status reports ollama unavailable", shead.get("ollama") is False, r.stdout)
        srows = [json.loads(ln) for ln in r.stdout.splitlines()[1:]]
        check("models status: no resident models reported (none exist)", srows == [], str(srows))

        r2 = run("status", home=home)
        check("models status (human) exits 0", r2.returncode == 0, r2.stdout + r2.stderr)

        # ---------- pull: confirm-gated, never downloads ----------
        r = run("pull", "--all", home=home)
        check("pull --all without --yes -> exit 3 (EXIT_CONFIRM)", r.returncode == 3, r.stdout + r.stderr)
        check("pull --all without --yes writes nothing to ollama's store (no --yes reached)",
              "--yes" in (r.stderr or r.stdout), r.stdout + r.stderr)

        r = run("pull", "--stage", "embed", home=home)
        check("pull --stage embed without --yes -> exit 3", r.returncode == 3, r.stdout + r.stderr)

        r = run("pull", home=home)
        check("pull with neither --stage nor --all -> exit 2 (usage)", r.returncode == 2, r.stdout + r.stderr)

        r = run("pull", "--stage", "bogus", "--yes", home=home)
        check("pull --stage <unknown> -> exit 2 (usage), even with --yes", r.returncode == 2, r.stdout + r.stderr)

        # ---------- test: confirm-gated, never runs a model ----------
        r = run("test", "embed", home=home)
        check("test embed without --yes -> exit 3", r.returncode == 3, r.stdout + r.stderr)

        r = run("test", "vlm", "--input", "pic.png", home=home)
        check("test vlm without --yes -> exit 3 (args ignored until gated)", r.returncode == 3, r.stdout + r.stderr)

        r = run("test", "bogus-stage", home=home)
        check("test unknown stage -> exit 2 (usage), no confirm needed to reject it", r.returncode == 2,
              r.stdout + r.stderr)

        r = run("test", home=home)
        check("test with no stage -> exit 2 (usage)", r.returncode == 2, r.stdout + r.stderr)

        # ---------- stop: safe_write (no --yes required), degrades cleanly without ollama ----------
        r = run("stop", "--all", home=home)
        check("stop --all without ollama -> clean error (exit 1), not a crash", r.returncode == 1,
              r.stdout + r.stderr)

        r = run("stop", home=home)
        check("stop with no target and no --all -> exit 2 (usage)", r.returncode == 2, r.stdout + r.stderr)

        # ---------- unknown action ----------
        r = run("bogus", home=home)
        check("unknown action -> exit 2 (usage)", r.returncode == 2, r.stdout + r.stderr)

        r = run(home=home)
        check("bare `plainkeep models` (no action) -> exit 2 (usage)", r.returncode == 2, r.stdout + r.stderr)

    print(f"\n{BOLD}plainkeep models (search-enrichment proposal §2.1) — {len(results)} checks{RESET}\n")
    passed = sum(1 for _, ok, _ in results if ok)
    for name, ok, detail in results:
        mark = f"{GREEN}PASS{RESET}" if ok else f"{RED}FAIL{RESET}"
        print(f"  {mark} {name:<62}" + (f" {DIM}{detail.strip()[:90]}{RESET}" if (detail and not ok) else ""))
    failed = len(results) - passed
    print(f"\n{BOLD}Result:{RESET} {GREEN}{passed} passed{RESET}, "
          f"{(RED if failed else DIM)}{failed} failed{RESET}, {len(results)} checks")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
