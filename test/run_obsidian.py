#!/usr/bin/env python3
"""
run_obsidian.py — Obsidian as Frontend Zero + plaintext views (proposal Part 3.1/3.2). Offline,
stdlib only (PyYAML used only if present). Covers:
  1. `plainkeep wiki canvas` — byte-deterministic re-runs + valid JSON Canvas 1.0 shape; hub 1-hop/depth-2
     and tag mode;
  2. `plainkeep index --changed` — the external-edit fast path (only files touched since the last build
     reindex; .obsidian/.trash are ignored either way);
  3. frontmatter reader tolerates Obsidian Properties normalization (key reorder, flow ↔ block lists);
  4. doctor Obsidian lints fire on fixtures (non-lowercase tag, wikilink in frontmatter) + --init
     seeds .obsidian/ from templates/obsidian/ (refuse-don't-overwrite);
  5. `templates/obsidian/*.json` and `bases/*.base` parse (valid JSON / YAML).
"""
from __future__ import annotations
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from lib.hermetic import scratch_root, seal
seal()   # hermetic: an empty throwaway registry, never the developer's real vault

REPO = Path(__file__).resolve().parents[1]
BIN = REPO / "bin"
PY = sys.executable
GREEN, RED, DIM, BOLD, RESET = "\033[32m", "\033[31m", "\033[2m", "\033[1m", "\033[0m"
results = []


def check(name, cond, detail=""):
    results.append((name, bool(cond), detail))


def run(env, verb, *args):
    return subprocess.run([PY, str(BIN / verb / "run.py"), *args],
                          capture_output=True, text=True, env=env)


def note(home: Path, rel: str, title: str, body: str = "", tags: str = "[demo]", typ: str = "note"):
    p = home / "wiki" / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(f"---\ntype: {typ}\ntitle: {title}\nstatus: active\ncreated: 2026-01-01\n"
                 f"updated: 2026-01-01\ntags: {tags}\naliases: []\n---\n# {title}\n\n{body}\n",
                 encoding="utf-8")


CANVAS_NODE_KEYS = {"id", "type", "file", "x", "y", "width", "height"}
CANVAS_EDGE_KEYS = {"id", "fromNode", "toNode"}


def valid_canvas(doc) -> bool:
    if not (isinstance(doc, dict) and isinstance(doc.get("nodes"), list) and isinstance(doc.get("edges"), list)):
        return False
    ids = set()
    for n in doc["nodes"]:
        if not CANVAS_NODE_KEYS.issubset(n) or n["type"] != "file":
            return False
        if not all(isinstance(n[k], int) for k in ("x", "y", "width", "height")):
            return False
        ids.add(n["id"])
    for e in doc["edges"]:
        if not CANVAS_EDGE_KEYS.issubset(e):
            return False
        if e["fromNode"] not in ids or e["toNode"] not in ids:
            return False
    return True


def main() -> int:
    # ---- 5. static: shipped config pack + bases parse ----
    pack = REPO / "templates" / "obsidian"
    for name in ("app.json", "appearance.json", "core-plugins.json", "hotkeys.json"):
        try:
            json.loads((pack / name).read_text(encoding="utf-8")); check(f"config: {name} valid JSON", True)
        except Exception as e:
            check(f"config: {name} valid JSON", False, str(e))
    app = json.loads((pack / "app.json").read_text(encoding="utf-8"))
    check("app.json: newLinkFormat=shortest", app.get("newLinkFormat") == "shortest", str(app))
    check("app.json: useMarkdownLinks=false", app.get("useMarkdownLinks") is False)
    check("app.json: attachments routed outside wiki/",
          "wiki" not in (app.get("attachmentFolderPath") or ""), app.get("attachmentFolderPath"))
    bases = sorted((pack / "bases").glob("*.base"))
    check("bases: 4 starter .base files", len(bases) == 4, [b.name for b in bases])
    try:
        import yaml  # optional; the zero-install path has no PyYAML
        for b in bases:
            doc = yaml.safe_load(b.read_text(encoding="utf-8"))
            check(f"base: {b.name} valid YAML with views",
                  isinstance(doc, dict) and isinstance(doc.get("views"), list), str(doc)[:80])
    except ImportError:
        for b in bases:  # fallback structural check (offline, no yaml)
            t = b.read_text(encoding="utf-8")
            check(f"base: {b.name} has filters+views", "filters:" in t and "views:" in t)

    # engine boundary: .obsidian/ must NOT be engine-owned; templates/ is user-owned too
    eng = (REPO / "script" / "engine.txt").read_text(encoding="utf-8")
    check(".obsidian NOT in engine.txt (user-owned)", ".obsidian" not in eng)
    check("templates/ NOT engine-owned (user config pack survives updates)",
          not re.search(r"(?m)^templates\b", eng))
    check("docs/obsidian-compat.md IS engine-owned", "docs/obsidian-compat.md" in eng)
    gi = (REPO / ".gitignore").read_text(encoding="utf-8")
    for pat in (".obsidian/workspace*.json", ".obsidian/cache", ".trash/", ".smart-env/"):
        check(f".gitignore has {pat}", pat in gi)

    # ---- 1. canvas: determinism + JSON Canvas shape + tag mode ----
    with tempfile.TemporaryDirectory() as td:
        h = Path(td)
        env = {**os.environ, "PLAINKEEP_HOME": str(h), "PLAINKEEP_NO_OPEN": "1"}
        note(h, "notes/alpha.md", "Alpha", "see [[beta]] and [[gamma]]")
        note(h, "notes/beta.md", "Beta", "back to [[alpha]] and [[delta]]")
        note(h, "notes/gamma.md", "Gamma", "")
        note(h, "notes/delta.md", "Delta", "", tags="[other]")

        r1 = run(env, "wiki", "canvas", "alpha", "--stdout")
        r2 = run(env, "wiki", "canvas", "alpha", "--stdout")
        check("canvas --stdout is byte-identical across runs", r1.stdout == r2.stdout and r1.stdout.strip(),
              r1.stderr[:120])
        try:
            doc = json.loads(r1.stdout)
        except Exception as e:
            doc = None
            check("canvas is valid JSON", False, str(e))
        if doc is not None:
            check("canvas is valid JSON Canvas 1.0", valid_canvas(doc), str(doc)[:120])
            ids = {n["id"] for n in doc["nodes"]}
            check("canvas 1-hop: hub + direct neighbours only", ids == {"alpha", "beta", "gamma"}, ids)
            hub = next(n for n in doc["nodes"] if n["id"] == "alpha")
            check("canvas hub is centred at (0,0)", hub["x"] == 0 and hub["y"] == 0, hub)

        # depth 2 pulls delta in (alpha->beta->delta)
        rd = run(env, "wiki", "canvas", "alpha", "--depth", "2", "--stdout")
        docd = json.loads(rd.stdout)
        check("canvas --depth 2 expands to the second ring", "delta" in {n["id"] for n in docd["nodes"]},
              {n["id"] for n in docd["nodes"]})

        # tag mode collects all tagged notes (no hub centre)
        rt = run(env, "wiki", "canvas", "#demo", "--stdout")
        doct = json.loads(rt.stdout)
        check("canvas tag mode collects the tagged set",
              {n["id"] for n in doct["nodes"]} == {"alpha", "beta", "gamma"}, {n["id"] for n in doct["nodes"]})
        check("canvas tag mode is valid JSON Canvas", valid_canvas(doct))

        # write-to-file mode + dry-run writes nothing
        rw = run(env, "wiki", "canvas", "alpha")
        cf = h / "wiki" / "canvas" / "alpha.canvas"
        check("canvas writes wiki/canvas/<slug>.canvas", cf.exists() and valid_canvas(json.loads(cf.read_text())),
              rw.stdout + rw.stderr)
        cf.unlink()
        run(env, "wiki", "canvas", "alpha", "--dry-run")
        check("canvas --dry-run writes nothing", not cf.exists())

        # unknown target -> not-found (4)
        ru = run(env, "wiki", "canvas", "nonesuch", "--json")
        check("canvas unknown target -> not-found(4)", ru.returncode == 4, ru.stdout[:120])

    # ---- 2. index --changed: mtime fast path + ignore rules ----
    with tempfile.TemporaryDirectory() as td:
        h = Path(td)
        env = {**os.environ, "PLAINKEEP_HOME": str(h)}
        note(h, "notes/a.md", "A", "alpha content")
        note(h, "notes/b.md", "B", "beta content")
        (h / "wiki" / ".trash").mkdir(parents=True, exist_ok=True)
        note(h, ".trash/ghost.md", "Ghost", "should never index")

        full = run(env, "index")
        m = re.search(r"indexed (\d+) files", full.stdout)
        check("index skips .trash/ (ignore rule)", m and int(m.group(1)) == 2, full.stdout.strip())

        time.sleep(1.05)  # ensure the touched file's mtime clears the last-build stamp
        note(h, "notes/a.md", "A", "alpha content EDITED externally")  # b untouched
        chg = run(env, "index", "--changed")
        mc = re.search(r"\((\d+) \(re\)indexed\)", chg.stdout)
        check("index --changed reindexes only the touched file", mc and int(mc.group(1)) == 1, chg.stdout.strip())

        # the edit is searchable; the untouched file's index is intact
        s = run(env, "search", "EDITED", "--json")
        check("index --changed surfaces the new content", "a.md" in s.stdout, s.stdout[:160])

    # ---- 3. frontmatter tolerance (reorder + flow ↔ block) ----
    tol = subprocess.run(
        [PY, "-c",
         "import sys; sys.path.insert(0,'bin'); from lib import paths as P;"
         "flow='---\\ntype: note\\ntags: [Alpha, beta-two]\\n---\\n# x';"
         "block='---\\ntags:\\n  - one\\n  - two\\nstatus: active\\ntype: note\\n---\\n# x';"
         "print('FLOW', P.fm_list(flow,'tags'));"
         "print('BLOCK', P.fm_list(block,'tags'));"
         "print('REORDER', P.frontmatter(block).get('type'))"],
        # PLAINKEEP_HOME is set because lib/paths.py resolves the data root at import and has no
        # engine-relative fallback since ADR-014 Task 1b. These three assertions are about
        # frontmatter PARSING and touch no path at all, so any valid root will do — and it is a
        # THROWAWAY one, not REPO. "Nothing here writes" was true of the assertions and not of the
        # variable, which every child inherits; `scratch_root()` exists for exactly this shape.
        capture_output=True, text=True, cwd=str(REPO),
        env={**os.environ, "PLAINKEEP_HOME": scratch_root()})
    out = tol.stdout
    check("fm_list reads a flow list", "FLOW ['Alpha', 'beta-two']" in out, out + tol.stderr)
    check("fm_list reads a block list (Obsidian-normalized)", "BLOCK ['one', 'two']" in out, out)
    check("frontmatter is position-independent (key reorder)", "REORDER note" in out, out)

    # ---- 4. doctor lints + --init seeds .obsidian/ ----
    with tempfile.TemporaryDirectory() as td:
        h = Path(td)
        env = {**os.environ, "PLAINKEEP_HOME": str(h)}
        shutil.copytree(pack, h / "templates" / "obsidian")
        note(h, "notes/badtag.md", "Bad Tag", "body", tags="[Not_Lowercase]")
        note(h, "notes/fmlink.md", "FM Link", "clean body",
             tags="[demo]")
        # inject a wikilink INTO the frontmatter of fmlink.md (the anti-pattern)
        fp = h / "wiki" / "notes" / "fmlink.md"
        fp.write_text(fp.read_text(encoding="utf-8").replace("aliases: []", "aliases: [[[alpha]]]"),
                      encoding="utf-8")

        d = run(env, "doctor", "--init")
        check("doctor lints non-lowercase-hyphenated tag", "tag: " in d.stdout and "Not_Lowercase" in d.stdout,
              d.stdout)
        check("doctor lints wikilink in frontmatter", "fm-link: " in d.stdout, d.stdout)
        # --init seeded .obsidian/ from the pack
        seeded = h / ".obsidian" / "app.json"
        check("doctor --init seeds .obsidian/app.json", seeded.exists(), d.stdout)
        check("doctor --init reports the config pack", "obsidian: config pack" in d.stdout, d.stdout)

        # refuse-don't-overwrite: a customized config survives a second --init
        seeded.write_text('{"custom": true}\n', encoding="utf-8")
        run(env, "doctor", "--init")
        check("doctor --init keeps a customized .obsidian/ (refuse-overwrite)",
              json.loads(seeded.read_text(encoding="utf-8")) == {"custom": True}, seeded.read_text())

        # clean vault: no false lint positives, canvas file is not flagged as a binary
        with tempfile.TemporaryDirectory() as td2:
            h2 = Path(td2)
            env2 = {**os.environ, "PLAINKEEP_HOME": str(h2)}
            note(h2, "notes/clean.md", "Clean", "body [[clean]]")
            run(env2, "doctor", "--init")
            dc = run(env2, "doctor")
            check("doctor: clean vault passes the tag lint",
                  "tags are lowercase-hyphenated" in dc.stdout, dc.stdout)
            check("doctor: clean vault passes the fm-link lint",
                  "wikilinks are in note bodies only" in dc.stdout, dc.stdout)

    print(f"{BOLD}Obsidian frontend zero + plaintext views — {len(results)} checks{RESET}\n")
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
