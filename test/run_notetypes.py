#!/usr/bin/env python3
"""run_notetypes.py — issue #1 gaps D+F: data-driven note types/templates (lib/notetype, `plainkeep wiki
new`) and the `plainkeep bookmark` verb that rides on them. Offline (bookmark fetch uses a local fixture)."""
from __future__ import annotations
import importlib.util
import os
import subprocess
import sys
import tempfile
import types
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

# The engine modules loaded IN-PROCESS below resolve the data root at import and have no
# engine-relative fallback since ADR-014 Task 1b, so a root has to be selected before the
# first import. Only pure functions are exercised in-process (no path is written through
# it); every subprocess invocation sets its own PLAINKEEP_HOME per call.
os.environ.setdefault("PLAINKEEP_HOME", str(Path(__file__).resolve().parents[1]))
GREEN, RED, DIM, BOLD, RESET = "\033[32m", "\033[31m", "\033[2m", "\033[1m", "\033[0m"
results = []

# Load bin/lib modules under a synthetic package so `from . import paths` resolves
# (test/lib/ shadows the bare name 'lib' on sys.path, so we can't just import it).
_BINLIB = REPO / "bin" / "lib"
_pkg = types.ModuleType("opslib")
_pkg.__path__ = [str(_BINLIB)]
sys.modules["opslib"] = _pkg


def check(name, cond, detail=""):
    results.append((name, bool(cond), "" if cond else detail))


def load(mod, rel):
    spec = importlib.util.spec_from_file_location(f"opslib.{Path(rel).stem}", _BINLIB / rel)
    m = importlib.util.module_from_spec(spec)
    sys.modules[f"opslib.{Path(rel).stem}"] = m
    spec.loader.exec_module(m)
    return m


def run(home, verb, *args, env_extra=None):
    env = {**os.environ, "PLAINKEEP_HOME": str(home)}
    if env_extra:
        env.update(env_extra)
    return subprocess.run([sys.executable, str(REPO / "bin" / verb / "run.py"), *args],
                          capture_output=True, text=True, env=env)


def read(p):
    return p.read_text(encoding="utf-8")


def main() -> int:
    notetype = load("ops_notetype", "notetype.py")

    # ---- D: the registry ----
    reg = notetype.load_types()
    check("load_types has the core types", {"note", "client", "bookmark", "decision"} <= set(reg), str(sorted(reg)))
    check("type_dir routes (decision→notes, bookmark→bookmarks)",
          notetype.type_dir("decision") == "notes" and notetype.type_dir("bookmark") == "bookmarks")
    check("is_hub true for entities, false for notes", notetype.is_hub("client") and not notetype.is_hub("note"))
    check("render fills frontmatter + heading",
          "type: decision" in notetype.render("decision", title="Pick X") and "# Pick X" in notetype.render("decision", title="Pick X"))
    check("hub render includes a Timeline", "## Timeline" in notetype.render("client", title="Acme"))
    check("bookmark render carries the url", "url: https://a.b" in notetype.render("bookmark", title="T", url="https://a.b"))
    check("unfilled placeholders are dropped", "{{" not in notetype.render("note", title="T"))

    # ---- D: `plainkeep wiki new` is data-driven ----
    with tempfile.TemporaryDirectory() as td:
        h = Path(td)
        r = run(h, "wiki", "new", "decision", "Pick libSQL")
        check("wiki new decision → routed to notes/", (h / "wiki" / "notes" / "pick-libsql.md").exists(), r.stdout + r.stderr)
        r = run(h, "wiki", "new", "bookmark", "Manual Bookmark")
        check("wiki new bookmark → routed to bookmarks/", (h / "wiki" / "bookmarks" / "manual-bookmark.md").exists(), r.stdout + r.stderr)
        r = run(h, "wiki", "new", "notatype", "X")
        check("wiki new rejects an unknown type", r.returncode == 2 and "type must be one of" in r.stderr, r.stderr)

    # ---- D: extensibility — a new type via templates/wiki/, NO code change ----
    with tempfile.TemporaryDirectory() as td:
        h = Path(td)
        tw = h / "templates" / "wiki"
        tw.mkdir(parents=True)
        (tw / "types.json").write_text('{"recipe": {"dir": "recipes"}}', encoding="utf-8")
        (tw / "recipe.md").write_text("---\ntype: recipe\ntitle: {{title}}\nserves: 2\n---\n# {{title}}\n\n## Ingredients\n", encoding="utf-8")
        r = run(h, "wiki", "new", "recipe", "Carbonara")
        rp = h / "wiki" / "recipes" / "carbonara.md"
        check("new type 'recipe' works from data alone", rp.exists(), r.stdout + r.stderr)
        check("custom template body is used", rp.exists() and "## Ingredients" in read(rp) and "serves: 2" in read(rp), r.stdout)
        # completion reflects the added type
        c = run(h, "__complete", "wiki", "new")
        check("__complete wiki new includes the custom type", "recipe:" in c.stdout, c.stdout)

    # ---- F: the bookmark verb (fetch via a local fixture; fully offline) ----
    with tempfile.TemporaryDirectory() as td:
        h = Path(td)
        roots = h / "roots"
        fx = h / "page.html"
        fx.write_text('<html><head><title>RRF beats naive hybrid</title>'
                      '<meta property="og:description" content="Why reciprocal rank fusion wins.">'
                      '</head><body><script>x()</script><p>RRF is simple and robust.</p></body></html>', encoding="utf-8")
        env = {"PLAINKEEP_ROOTS_HOME": str(roots), "PLAINKEEP_BOOKMARK_FIXTURE": str(fx)}

        r = run(h, "bookmark", "https://example.com/rrf", "--archive", env_extra=env)
        note = h / "wiki" / "bookmarks" / "rrf-beats-naive-hybrid.md"
        check("bookmark saves a note titled from <title>", note.exists() and r.returncode == 0, r.stdout + r.stderr)
        if note.exists():
            body = read(note)
            check("bookmark note has url + source frontmatter", "url: https://example.com/rrf" in body and "source: https://example.com/rrf" in body, body)
            check("bookmark note carries the meta description", "Why reciprocal rank fusion wins." in body, body)
            check("bookmark note has a readable Extract (script stripped)", "## Extract" in body and "RRF is simple and robust." in body and "x()" not in body, body)
        check("bookmark --archive snapshots html into ~/files", (roots / "files" / "bookmarks" / "rrf-beats-naive-hybrid.html").exists())

        # de-dup: same title again → -2 slug, original untouched
        r = run(h, "bookmark", "https://example.com/rrf", env_extra=env)
        check("bookmark de-dups a repeat title", (h / "wiki" / "bookmarks" / "rrf-beats-naive-hybrid-2.md").exists(), r.stdout)

        # --no-fetch: offline, title from URL, custom note as body, no extract
        r = run(h, "bookmark", "https://example.com/some-article", "--no-fetch", "--note", "read later")
        na = h / "wiki" / "bookmarks" / "some-article.md"
        check("bookmark --no-fetch titles from the URL", na.exists() and "title: some article" in read(na), r.stdout + r.stderr)
        check("bookmark --no-fetch skips network extract, keeps --note", na.exists() and "## Extract" not in read(na) and "read later" in read(na), r.stdout)

        # validation
        r = run(h, "bookmark", "ftp://nope/x")
        check("bookmark rejects a non-http(s) url", r.returncode == 2, r.stderr)
        r = run(h, "bookmark")
        check("bookmark with no url → usage", r.returncode == 2, r.stderr)

    # ---- F: extraction auto-upgrades to trafilatura when installed (else crude strip) ----
    try:
        import trafilatura  # noqa: F401
        have_traf = True
    except Exception:
        have_traf = False
    if have_traf:
        with tempfile.TemporaryDirectory() as td:
            h = Path(td)
            fx = h / "article.html"
            fx.write_text(
                "<html><head><title>Ranking Fusion</title></head><body>"
                "<nav>Home About Contact SUBSCRIBE-NOW-JUNK</nav>"
                "<article><h1>Ranking Fusion</h1>"
                "<p>Reciprocal rank fusion combines multiple ranked lists without tuning weights.</p>"
                "<p>It is robust because it depends only on rank position, not raw scores.</p>"
                "</article><footer>COPYRIGHT-FOOTER-JUNK 2026</footer></body></html>", encoding="utf-8")
            r = run(h, "bookmark", "https://example.com/fusion", env_extra={"PLAINKEEP_BOOKMARK_FIXTURE": str(fx)})
            body = read(h / "wiki" / "bookmarks" / "ranking-fusion.md") if (h / "wiki" / "bookmarks" / "ranking-fusion.md").exists() else ""
            check("trafilatura extracts the article body", "Reciprocal rank fusion combines" in body, r.stdout + body[:120])
            check("trafilatura drops nav/footer boilerplate",
                  "SUBSCRIBE-NOW-JUNK" not in body and "COPYRIGHT-FOOTER-JUNK" not in body, body)
    else:
        check("trafilatura extraction (skipped: not installed)", True)

    # ---- F: the guardrail admits bookmark (the gate the dispatcher runs before exec) ----
    g = subprocess.run([sys.executable, str(_BINLIB / "guardrail.py"), "bookmark", "https://example.com/x", "--no-fetch"],
                       capture_output=True, text=True)
    check("guardrail admits bookmark (safe_write, no --yes needed)", g.returncode == 0, g.stdout + g.stderr)

    print(f"{BOLD}Data-driven note types + bookmarks (issue #1 D+F) — {len(results)} checks{RESET}\n")
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
