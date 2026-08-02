#!/usr/bin/env python3
"""run_completion.py — Tier-1 terminal ergonomics: the __complete helper (tab-completion brain),
the zsh completion file, the built-in Markdown renderer, and `wiki open`/`wiki edit`. Offline."""
from __future__ import annotations
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from lib.hermetic import seal
from lib.vaultfx import mark_engine_vault
seal()   # hermetic: an empty throwaway registry, never the developer's real vault

REPO = Path(__file__).resolve().parents[1]
GREEN, RED, DIM, BOLD, RESET = "\033[32m", "\033[31m", "\033[2m", "\033[1m", "\033[0m"
results = []


def check(name, cond, detail=""):
    results.append((name, cond, bool(cond) and "" or detail))


def run(home, verb, *args, env_extra=None, stdin=None):
    env = {**os.environ, "PLAINKEEP_HOME": str(home)}
    if env_extra:
        env.update(env_extra)
    return subprocess.run([sys.executable, str(REPO / "bin" / verb / "run.py"), *args],
                          input=stdin, capture_output=True, text=True, env=env)


def comp(home, *prior, env_extra=None):
    """Run __complete; return the list of candidate values (before the ':' description)."""
    r = run(home, "__complete", *prior, env_extra=env_extra)
    vals = [ln.split(":", 1)[0] for ln in r.stdout.splitlines() if ln.strip()]
    return vals, r


def note(home, rel, typ, title, body=""):
    p = home / "wiki" / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(f"---\ntype: {typ}\ntitle: {title}\nstatus: active\ncreated: 2026-01-01\n"
                 f"updated: 2026-01-01\ntags: []\n---\n# {title}\n\n{body}\n", encoding="utf-8")


def task(home, tid, status, title):
    p = home / "tasks" / status / f"{tid}.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(f"---\ntype: task\nid: {tid}\nstatus: {status}\n---\n# {title}\n", encoding="utf-8")


def main() -> int:
    # ---- __complete: the completion brain ----
    with tempfile.TemporaryDirectory() as td:
        h = Path(td)
        note(h, "notes/alpha.md", "note", "Alpha", "see [[beta]]")
        note(h, "notes/beta.md", "note", "Beta")
        task(h, "T-20260101-01", "active", "Fix the webhook")
        task(h, "T-20260101-02", "waiting", "Await reply")

        verbs, r = comp(h)
        check("__complete (no args) lists verbs", "wiki" in verbs and "task" in verbs and "search" in verbs, r.stdout)
        check("__complete hides itself (__complete not a candidate)", "__complete" not in verbs, str(verbs))
        # no drift: every visible verb in plainkeep.json is offered
        manifest = json.loads((REPO / "plainkeep.json").read_text())
        surface = {v["verb"] for v in manifest["verbs"]}
        check("__complete covers every verb in plainkeep.json", surface.issubset(set(verbs)),
              str(sorted(surface - set(verbs))))
        check("plainkeep.json itself excludes __complete", "__complete" not in surface, str(surface))

        subs, _ = comp(h, "wiki")
        check("__complete wiki → subcommands", {"open", "edit", "new", "backlinks"} <= set(subs), str(subs))
        slugs, _ = comp(h, "wiki", "open")
        check("__complete wiki open → live note slugs", {"alpha", "beta"} <= set(slugs), str(slugs))
        eslugs, _ = comp(h, "wiki", "edit")
        check("__complete wiki edit → live note slugs", "alpha" in eslugs, str(eslugs))
        types, _ = comp(h, "wiki", "new")
        check("__complete wiki new → note types", {"note", "project", "client"} <= set(types), str(types))
        tsubs, _ = comp(h, "task")
        check("__complete task → subcommands", {"add", "done", "show", "move"} <= set(tsubs), str(tsubs))
        tids, _ = comp(h, "task", "done")
        check("__complete task done → live task ids", "T-20260101-01" in tids, str(tids))
        mvst, _ = comp(h, "task", "move", "T-20260101-01")
        check("__complete task move <id> → statuses", {"active", "waiting", "done"} <= set(mvst), str(mvst))
        unknown, _ = comp(h, "definitelynotaverb")
        check("__complete unknown verb → no candidates", unknown == [], str(unknown))

        # ---- plainkeep.json/3 (Wave 2): completion is DERIVED from actions[] — no table can drift ----
        surface_doc = json.loads((REPO / "plainkeep.json").read_text())
        drift = []
        for v in surface_doc["verbs"]:
            acts = v.get("actions")
            if not acts:
                continue
            expect = {a["name"] for a in acts if not a.get("default")}  # keyworded subactions
            subs, _ = comp(h, v["verb"])
            if not expect.issubset(set(subs)):
                drift.append((v["verb"], sorted(expect - set(subs))))
        check("__complete subactions == every compound verb's actions[] names (no drift)",
              not drift, str(drift))
        # wave-2 verbs specifically (they had no hardcoded table before)
        ssub, _ = comp(h, "share")
        check("__complete share → keyworded subactions + publish's note slugs (tokenless default)",
              {"collection", "list", "pull", "revoke", "init"} <= set(ssub)
              and "publish" not in ssub and "alpha" in ssub, str(ssub))
        bsub, _ = comp(h, "backup")
        check("__complete backup → subactions, no tokenless 'nag' keyword",
              {"status", "run", "drill", "bundle", "init"} <= set(bsub) and "nag" not in bsub, str(bsub))
        msub, _ = comp(h, "models")
        check("__complete models → subactions", {"list", "status", "stop", "pull", "test"} <= set(msub), str(msub))
        kv, _ = comp(h, "repo", "clone", "--kind")
        check("__complete repo clone --kind → enum values", {"products", "labs", "tools"} <= set(kv), str(kv))
        # files asset args use the narrow asset-slug provider (wiki/files/*), NOT every note
        note(h, "files/q3-report.md", "file", "Q3 Report")
        fopen, _ = comp(h, "files", "open")
        check("__complete files open → asset slugs only (not arbitrary notes)",
              "q3-report" in fopen and "alpha" not in fopen, str(fopen))

        # ---- plainkeep complete --json: the structured completion contract (Wave 2) ----
        def cjson(*prior):
            r = run(h, "complete", *prior, "--json")
            objs = [json.loads(ln) for ln in r.stdout.splitlines() if ln.strip()]
            return objs, r

        objs, r = cjson("task", "move", "T-20260101-01")
        head, rows = (objs[0] if objs else {}), objs[1:]
        check("plainkeep complete --json: rows header (verb=complete, count matches)",
              head.get("ops_json") == 1 and head.get("ok") is True and head.get("verb") == "complete"
              and head.get("count") == len(rows), r.stdout[:160])
        check("plainkeep complete --json: rows carry value/description/kind",
              bool(rows) and all({"value", "description", "kind"} <= set(row) for row in rows), str(rows[:2]))
        # move's status arg is declared `type: enum` (a closed set) → kind "enum" (inline enum wins
        # over the redundant `complete: status` hint, which yields the same values)
        check("plainkeep complete --json task move <id> → enum-kind status rows",
              {row["value"] for row in rows} >= {"active", "waiting", "done"}
              and all(row["kind"] == "enum" for row in rows), str(rows))
        vobjs, _ = cjson()
        check("plainkeep complete --json (no words) → verb-kind rows incl. task",
              len(vobjs) > 1 and all(row["kind"] == "verb" for row in vobjs[1:])
              and any(row["value"] == "task" for row in vobjs[1:]), str(vobjs[:2]))
        # human (non-json) mode still prints candidates, no envelope
        rh = run(h, "complete", "task")
        check("plainkeep complete (human) prints candidates, no envelope",
              "add" in rh.stdout and "ops_json" not in rh.stdout, rh.stdout[:120])

    # ---- guardrail lets __complete through the real dispatcher (risk: read) ----
    # This is the one check here that runs the REAL dispatcher, so it is the one that has to name a
    # vault. It used to inherit the environment and pass no root at all: before Task 1b that meant
    # the engine-relative fallback (the repo), and after it it means the marker walk-up out of the
    # repo — i.e. the developer's own registered vault, whose audit log this check then appended to
    # on every green run. Measured: `.logs/plainkeep.log` in the real vault gained a line per run.
    #
    # `mark_engine_vault` and not a bare marked directory: both dispatchers still look for the
    # engine under the selected root (report §6.3). See test/lib/vaultfx.py.
    with tempfile.TemporaryDirectory() as td:
        dh = Path(td)
        mark_engine_vault(dh, REPO)
        d = subprocess.run([str(REPO / "plainkeep"), "__complete", "wiki"],
                           capture_output=True, text=True,
                           env={**os.environ, "PLAINKEEP_HOME": str(dh)})
    check("dispatcher runs __complete (guardrail: read)", d.returncode == 0 and "open" in d.stdout, d.stdout + d.stderr)

    # ---- the built-in Markdown renderer ----
    # load bin/lib/render.py by path (test/lib/ shadows the name 'lib' on sys.path)
    import importlib.util
    spec = importlib.util.spec_from_file_location("plainkeep_render", REPO / "bin" / "lib" / "render.py")
    render = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(render)
    out = render.render_markdown("---\ntype: note\n---\n# Title\n\n**bold** [[link]] `code`\n")
    check("renderer emits ANSI for a heading", "\033[1m" in out and "Title" in out and "#" not in out.split("Title")[0][-3:], out[:60])
    check("renderer dims frontmatter", "\033[2mtype: note\033[0m" in out, out[:80])
    check("renderer accents [[wikilinks]]", "[[link]]" in out and "\033[36m" in out, out)
    check("fzf_pick returns None when non-interactive/no fzf", render.fzf_pick(["a", "b"]) is None)
    check("renderer shows image embeds as a terminal ref", "🖼" in render.render_markdown("![alt](/x/y.png)"))

    # ---- wiki open: raw when piped (tests + pipelines get plain Markdown) ----
    with tempfile.TemporaryDirectory() as td:
        h = Path(td)
        note(h, "notes/beta.md", "note", "Beta", "body text")
        r = run(h, "wiki", "open", "beta")
        check("wiki open (piped) is raw Markdown", "# Beta" in r.stdout and "\033[" not in r.stdout, r.stdout)
        r = run(h, "wiki", "open", "beta", env_extra={"PLAINKEEP_RENDER": "raw"})
        check("wiki open PLAINKEEP_RENDER=raw stays raw", "# Beta" in r.stdout, r.stdout)
        r = run(h, "wiki", "open", "beta", env_extra={"PLAINKEEP_RENDER": "plain"})
        check("wiki open PLAINKEEP_RENDER=plain renders ANSI even piped (fzf preview)",
              "\033[" in r.stdout and "Beta" in r.stdout, r.stdout)
        r = run(h, "wiki", "open", "nope")
        check("wiki open unknown slug → not-found (4)", r.returncode == 4 and "no note" in r.stderr, r.stderr)
        # ---- Tier-2: no-slug falls back to a plain listing (fzf picker needs an interactive tty) ----
        note(h, "notes/alpha.md", "note", "Alpha")
        r = run(h, "wiki", "open")
        check("wiki open (no slug) lists notes, exit 0", r.returncode == 0
              and "alpha" in r.stdout and "beta" in r.stdout and "note(s)" in r.stdout, r.stdout)
        r = run(h, "wiki", "edit")
        check("wiki edit (no slug) lists notes, exit 0", r.returncode == 0 and "beta" in r.stdout, r.stdout)
        # wiki edit shells out to $EDITOR on the right file
        r = run(h, "wiki", "edit", "beta", env_extra={"PLAINKEEP_EDITOR": "echo EDIT"})
        check("wiki edit opens $EDITOR on the note", r.returncode == 0
              and "EDIT" in r.stdout and str(h / "wiki" / "notes" / "beta.md") in r.stdout, r.stdout + r.stderr)
        r = run(h, "wiki", "edit", "nope")
        check("wiki edit unknown slug → not-found (4)", r.returncode == 4, r.stderr)
    with tempfile.TemporaryDirectory() as td:
        empty = Path(td)
        (empty / "wiki").mkdir()
        r = run(empty, "wiki", "open")
        check("wiki open (no slug, empty wiki) is graceful", r.returncode == 0 and "no notes yet" in r.stdout, r.stdout)

    # ---- the shipped zsh completion file ----
    comp_file = REPO / "script" / "completions" / "_plainkeep"
    check("zsh completion file exists", comp_file.exists())
    if comp_file.exists():
        txt = comp_file.read_text()
        check("completion declares #compdef plainkeep", txt.splitlines()[0].strip() == "#compdef plainkeep", txt[:40])
        check("completion delegates to `plainkeep __complete`", "plainkeep __complete" in txt)
        if shutil.which("zsh"):
            z = subprocess.run(["zsh", "-n", str(comp_file)], capture_output=True, text=True)
            check("zsh parses the completion file", z.returncode == 0, z.stderr)
        else:
            check("zsh parses the completion file (skipped: no zsh)", True)

    print(f"{BOLD}Terminal ergonomics (completion, renderer, wiki open/edit) — {len(results)} checks{RESET}\n")
    passed = sum(1 for _, ok, _ in results if ok)
    for name, ok, detail in results:
        mark = f"{GREEN}PASS{RESET}" if ok else f"{RED}FAIL{RESET}"
        print(f"  {mark} {name:<52}" + (f" {DIM}{detail.strip()[:70]}{RESET}" if (detail and not ok) else ""))
    failed = len(results) - passed
    print(f"\n{BOLD}Result:{RESET} {GREEN}{passed} passed{RESET}, "
          f"{(RED if failed else DIM)}{failed} failed{RESET}, {len(results)} checks")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
