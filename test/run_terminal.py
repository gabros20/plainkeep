#!/usr/bin/env python3
"""
run_terminal.py — terminal ergonomics (proposal Part 3.4) + the Raycast frontend (Part 3.3).
Offline, stdlib only. Covers:
  1. `plainkeep open` resolution ORDER on a fixture vault: task id → wiki slug → files asset (shadow-note
     `path:` field) → search top-hit fallback; --json envelope; not-found (4); --reveal path;
  2. `plainkeep orient` all three renders (human dashboard, --json envelope, --line ≤60-char cached) +
     JSON envelope validity + the .cache/orient.line TTL cache;
  3. `plainkeep search` FTS5 snippet() present in human + --json rows; --open jumps via plainkeep open; bare
     non-tty search keeps the usage error (2);
  4. `frontends/raycast/*.sh` pass `bash -n` and reference only `plainkeep` (no python/lib import).
"""
from __future__ import annotations
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from lib.hermetic import seal
seal()   # hermetic: an empty throwaway registry, never the developer's real vault

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib import vaultfx  # noqa: E402
BIN = REPO / "bin"
PLAINKEEP = REPO / "plainkeep"
PY = sys.executable
GREEN, RED, DIM, BOLD, RESET = "\033[32m", "\033[31m", "\033[2m", "\033[1m", "\033[0m"
results = []


def check(name, cond, detail=""):
    results.append((name, bool(cond), detail))


def run(env, verb, *args):
    return subprocess.run([PY, str(BIN / verb / "run.py"), *args],
                          capture_output=True, text=True, env=env)


def note(home: Path, rel: str, title: str, body: str = "", typ: str = "note"):
    p = home / "wiki" / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(f"---\ntype: {typ}\ntitle: {title}\nstatus: active\ncreated: 2026-01-01\n"
                 f"updated: 2026-01-01\ntags: [demo]\naliases: []\n---\n# {title}\n\n{body}\n",
                 encoding="utf-8")


def parse_ndjson(text: str):
    return [json.loads(ln) for ln in text.splitlines() if ln.strip()]


def envelope_ok(objs, verb):
    return bool(objs) and objs[0].get("ops_json") == 1 and objs[0].get("ok") is True \
        and objs[0].get("verb") == verb


def main() -> int:
    # ---- 4. raycast scripts: bash -n + reference only `plainkeep` ----
    ray = sorted((REPO / "frontends" / "raycast").glob("*.sh"))
    check("raycast: 4-6 script commands present", 4 <= len(ray) <= 6, [p.name for p in ray])
    for sh in ray:
        rc = subprocess.run(["bash", "-n", str(sh)], capture_output=True, text=True)
        check(f"raycast: {sh.name} passes bash -n", rc.returncode == 0, rc.stderr)
        text = sh.read_text(encoding="utf-8")
        code = "\n".join(ln for ln in text.splitlines() if not ln.lstrip().startswith("#"))
        check(f"raycast: {sh.name} invokes plainkeep", re.search(r'"\$PLAINKEEP"|\$\{PLAINKEEP', code) is not None, code[:120])
        check(f"raycast: {sh.name} no python/lib import",
              "python" not in code and "bin/lib" not in code and "import" not in code, code[:120])
        check(f"raycast: {sh.name} has @raycast.title", "@raycast.title" in text)
    check("frontends/ IS engine-owned (improvements flow via update)",
          re.search(r"(?m)^frontends\b", (REPO / "script" / "engine.txt").read_text(encoding="utf-8")) is not None)
    check("docs/mobile-and-capture.md engine-owned",
          "docs/mobile-and-capture.md" in (REPO / "script" / "engine.txt").read_text(encoding="utf-8"))
    check("docs/mobile-and-capture.md exists", (REPO / "docs" / "mobile-and-capture.md").exists())

    with tempfile.TemporaryDirectory() as td:
        # THE ENGINE IS A SEPARATE TREE BESIDE the vault (Phase 2 Task 2). It used to be mirrored
        # INTO the fixture vault, because the dispatcher tied `bin/` to PLAINKEEP_HOME; that is the
        # assumption this phase deletes, and a vault that CONTAINS its engine is refused with exit 5
        # — which is why the vault moved down a level too, into `<td>/vault`. `<td>` itself would
        # have contained the engine. What the fixture still needs is unchanged: a real engine to
        # re-enter through, so `plainkeep open` and `search --open` are exercised honestly.
        h = Path(td) / "vault"
        h.mkdir()
        env = {**os.environ, "PLAINKEEP_HOME": str(h), "PLAINKEEP_ROOTS_HOME": str(h), "PLAINKEEP_NO_OPEN": "1"}
        engine = Path(td) / "engine"
        shutil.copytree(BIN, engine / "bin")
        shutil.copy2(PLAINKEEP, engine / "plainkeep"); os.chmod(engine / "plainkeep", 0o755)
        (engine / "VERSION").write_text((REPO / "VERSION").read_text(encoding="utf-8"), encoding="utf-8")
        vaultfx.mark_vault(h)   # Task 1b: the dispatcher validates the root before it dispatches
        home_bin = engine / "plainkeep"
        # seed: a wiki note, a task, a files asset (shadow note), a search-only note
        note(h, "notes/alpha-widget.md", "Alpha Widget", "the alpha widget doc")
        note(h, "notes/gamma-doc.md", "Gamma Doc", "gamma content about frobnication")
        run(env, "task", "add", "Fix the beta widget")
        # a files shadow note pointing at a binary in ~/files (path: field)
        binp = h / "files" / "research" / "paper.pdf"
        binp.parent.mkdir(parents=True, exist_ok=True)
        binp.write_bytes(b"%PDF-1.4 fake")
        shadow = h / "wiki" / "files" / "paper.md"
        shadow.parent.mkdir(parents=True, exist_ok=True)
        shadow.write_text(f"---\ntype: file\ntitle: Paper\nstatus: active\npath: {binp}\n"
                          f"sha256: deadbeef\ntags: []\n---\n# Paper\n\nshadow note\n", encoding="utf-8")
        run(env, "index")
        tid = next((p.stem for p in (h / "tasks" / "active").glob("T-*.md")), None)
        check("fixture: task created", tid is not None)

        # ---- 1. open resolution order ----
        r = run(env, "open", tid, "--json")
        objs = parse_ndjson(r.stdout)
        check("open resolves a task id", envelope_ok(objs, "open") and objs[0]["data"]["kind"] == "task",
              r.stdout[:160])
        check("open task path points at tasks/", objs and "tasks/" in objs[0]["data"]["path"], r.stdout[:160])

        r = run(env, "open", "alpha-widget", "--json")
        objs = parse_ndjson(r.stdout)
        check("open resolves a wiki slug", objs and objs[0]["data"]["kind"] == "wiki", r.stdout[:160])
        check("open wiki path is the note .md", objs and objs[0]["data"]["path"].endswith("alpha-widget.md"),
              r.stdout[:160])

        r = run(env, "open", "paper", "--json")
        objs = parse_ndjson(r.stdout)
        check("open resolves a files asset (shadow note)", objs and objs[0]["data"]["kind"] == "files",
              r.stdout[:160])
        check("open files path is the BINARY (path: field), not the shadow .md",
              objs and objs[0]["data"]["path"].endswith("paper.pdf"), r.stdout[:160])

        # a query that is not a task/slug/shadow -> search fallback
        r = run(env, "open", "frobnication", "--json")
        objs = parse_ndjson(r.stdout)
        check("open falls back to search top hit", objs and objs[0]["data"]["kind"] == "search"
              and objs[0]["data"]["slug"] == "gamma-doc", r.stdout[:160])

        # default (no flags) prints the resolved path, no GUI launch
        r = run(env, "open", "alpha-widget")
        check("open default prints the resolved path", r.stdout.strip().endswith("alpha-widget.md"),
              r.stdout[:160])

        # not-found -> exit 4 + error envelope
        r = run(env, "open", "no-such-thing-xyz", "--json")
        objs = parse_ndjson(r.stdout)
        check("open unknown -> not-found(4)", r.returncode == 4 and objs and objs[0]["ok"] is False
              and objs[0]["error"]["code"] == 4, f"rc={r.returncode} {r.stdout[:160]}")

        # --reveal prints the binary path (PLAINKEEP_NO_OPEN suppresses the Finder call)
        r = run(env, "open", "paper", "--reveal")
        check("open --reveal prints the binary path", r.stdout.strip().endswith("paper.pdf"), r.stdout[:160])

        # bare `plainkeep open` non-tty (no fzf/tty) -> usage(2)
        r = run(env, "open")
        check("bare open non-tty -> usage(2)", r.returncode == 2, f"rc={r.returncode} {r.stderr[:120]}")

        # dispatcher path: guardrail classes `open` as read (runs freely, exit 0)
        d = subprocess.run([str(home_bin), "open", "alpha-widget"], capture_output=True, text=True, env=env)
        check("dispatcher runs open (guardrail: read)", d.returncode == 0
              and d.stdout.strip().endswith("alpha-widget.md"), d.stdout + d.stderr)

        # ---- 2. orient: three renders ----
        r = run(env, "orient")
        check("orient human dashboard renders", "plainkeep orient" in r.stdout and "tasks:" in r.stdout, r.stdout[:200])
        check("orient dashboard lists the active task", tid in r.stdout, r.stdout[:300])

        r = run(env, "orient", "--json")
        objs = parse_ndjson(r.stdout)
        check("orient --json is a valid envelope", envelope_ok(objs, "orient"), r.stdout[:200])
        if objs:
            data = objs[0]["data"]
            for f in ("active", "waiting", "inbox", "proposals", "index_age_min", "git_dirty",
                      "journal_tail", "top_tasks", "recent_notes", "line"):
                check(f"orient --json field: {f}", f in data, list(data))
            check("orient counts the active task", data.get("active") == 1, data.get("active"))
            check("orient recent_notes lists notes", any(n["slug"] == "alpha-widget"
                  for n in data.get("recent_notes", [])), data.get("recent_notes"))
            check("orient line is <=60 chars", isinstance(data.get("line"), str) and len(data["line"]) <= 60,
                  data.get("line"))

        r = run(env, "orient", "--line")
        line = r.stdout.strip()
        check("orient --line prints a compact string", 0 < len(line) <= 60 and "T1/0" in line, repr(line))
        cache = h / ".cache" / "orient.line"
        check("orient --line writes the cache", cache.exists() and cache.read_text().strip() == line, str(cache))
        # cache TTL: a poisoned cache value is served while fresh (proves it reads the cache, cheap)
        cache.write_text("CACHED-SENTINEL\n", encoding="utf-8")
        r2 = run({**env, "PLAINKEEP_ORIENT_TTL": "60"}, "orient", "--line")
        check("orient --line serves the fresh cache (TTL)", r2.stdout.strip() == "CACHED-SENTINEL", r2.stdout)
        # TTL=0 disables the cache -> recompute
        r3 = run({**env, "PLAINKEEP_ORIENT_TTL": "0"}, "orient", "--line")
        check("orient --line TTL=0 recomputes (ignores cache)", r3.stdout.strip() != "CACHED-SENTINEL", r3.stdout)

        # ---- 3. search snippets + --open + bare non-tty ----
        r = run(env, "search", "frobnication", "--json")
        objs = parse_ndjson(r.stdout)
        check("search --json header is a valid envelope", envelope_ok(objs, "search"), r.stdout[:160])
        rowobjs = objs[1:] if len(objs) > 1 else []
        check("search rows carry a snippet field", rowobjs and all("snippet" in r for r in rowobjs),
              r.stdout[:200])
        check("search snippet has the highlighted match",
              any("frobnication" in (r.get("snippet") or "").lower() for r in rowobjs), r.stdout[:220])

        r = run(env, "search", "frobnication")  # human
        check("search human shows the snippet excerpt", "frobnication" in r.stdout.lower(), r.stdout[:200])

        # --open jumps to the top hit via `plainkeep open` (prints its resolved path)
        r = run(env, "search", "frobnication", "--open")
        check("search --open jumps to the top hit", "gamma-doc.md" in r.stdout, r.stdout[:160])

        # bare search non-tty keeps the usage error (unchanged contract)
        r = run(env, "search")
        check("bare search non-tty -> usage(2)", r.returncode == 2, f"rc={r.returncode} {r.stderr[:120]}")

    print(f"{BOLD}Terminal ergonomics + Raycast frontend — {len(results)} checks{RESET}\n")
    passed = sum(1 for _, ok, _ in results if ok)
    for name, ok, detail in results:
        mark = f"{GREEN}PASS{RESET}" if ok else f"{RED}FAIL{RESET}"
        print(f"  {mark} {name:<56}" + (f" {DIM}{str(detail).strip()[:80]}{RESET}" if (detail and not ok) else ""))
    failed = len(results) - passed
    print(f"\n{BOLD}Result:{RESET} {GREEN}{passed} passed{RESET}, "
          f"{(RED if failed else DIM)}{failed} failed{RESET}, {len(results)} checks")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
