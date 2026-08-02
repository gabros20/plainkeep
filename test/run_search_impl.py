#!/usr/bin/env python3
"""
run_search_impl.py — tests the REAL stage-1 search implementation (bin/lib/indexlib.py + the
`plainkeep` CLI), not just the retrieval model. Offline, no LLM.

It materializes the wiki fixture into a temp content tree, runs `plainkeep index`, then asserts:
  - lexical queries return the relevant note in the top-5 (stage-1 claim: "covers most queries"),
  - incremental indexing skips unchanged files,
  - the rebuild rule works (drop the db, reindex, same results),
  - a subprocess smoke of `./plainkeep search` returns hits.

Semantic queries are intentionally NOT asserted here — keyword can't answer them by construction
(that's the stage-2/vectors case, ADR-002).

Usage:  python3 test/run_search_impl.py
"""
from __future__ import annotations
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from lib.hermetic import seal
seal()   # hermetic: an empty throwaway registry, never the developer's real vault

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(REPO / "bin" / "lib"))  # for `import indexlib` (top-level, avoids the test/lib clash)

WIKI = HERE / "world" / "wiki_corpus.json"
QRELS = HERE / "world" / "search_qrels.json"
GREEN, RED, DIM, BOLD, RESET = "\033[32m", "\033[31m", "\033[2m", "\033[1m", "\033[0m"
results: list[tuple[str, bool, str]] = []


def check(name, cond, detail=""):
    results.append((name, cond, detail))


def materialize(notes: dict, root: Path):
    for key, text in notes.items():
        if not text.lstrip().startswith("---"):
            continue  # skip the missing-frontmatter fixture
        f = root / (key + ".md")
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text, encoding="utf-8")


def main() -> int:
    notes = json.loads(WIKI.read_text())["notes"]
    qrels = json.loads(QRELS.read_text())["queries"]
    rel_text = {q["relevant"]: notes.get(q["relevant"], "") for q in qrels}

    with tempfile.TemporaryDirectory() as td:
        home = Path(td)
        content = home / "content"
        materialize(notes, content)
        os.environ["PLAINKEEP_HOME"] = str(home)
        os.environ["PLAINKEEP_CONTENT"] = str(content)
        # env is set before import, so indexlib's module-level paths resolve to the temp home
        import indexlib  # noqa: E402  (top-level; bin/lib on sys.path)
        from indexlib import index, search, DB  # noqa: E402

        n1 = index(content, verbose=False)
        check("index built over fixture", n1 >= 9, f"{n1} files")

        # lexical queries: relevant note in top-5
        from lib.retrieval import tokenize  # reuse stemmed-token overlap to classify (test/lib)
        lexicals = [q for q in qrels if set(tokenize(q["q"])) & set(tokenize(rel_text[q["relevant"]]))]
        hits_ok = 0
        for q in lexicals:
            paths = [p for p, _h, _s in search(q["q"], k=5)]
            want = q["relevant"] + ".md"
            ok = want in paths
            hits_ok += 1 if ok else 0
            if not ok:
                check(f"lexical: {q['q'][:38]}", False, f"want {want}, got {paths[:3]}")
        check(f"lexical recall@5 ({hits_ok}/{len(lexicals)})", hits_ok == len(lexicals),
              f"{hits_ok}/{len(lexicals)}")

        # incremental: re-index with no changes touches nothing
        import io
        import contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            index(content, verbose=True)
        check("incremental re-index skips unchanged", "(0 (re)indexed)" in buf.getvalue(),
              buf.getvalue().strip())

        # rebuild rule: delete db, reindex, same hit
        before = [p for p, _h, _s in search("stripe webhook retry", k=3)]
        Path(DB).unlink(missing_ok=True)
        for suf in ("-wal", "-shm"):
            Path(str(DB) + suf).unlink(missing_ok=True)
        index(content, verbose=False)
        after = [p for p, _h, _s in search("stripe webhook retry", k=3)]
        check("rebuild-from-files reproduces results", before == after and before, f"{before} vs {after}")

        # graph arm: searching the client surfaces the linked note too (one-hop)
        dpaths = [p for p, _h, _s in search("designatives", k=6)]
        check("wikilink-graph expansion surfaces linked note",
              any("stripe-webhook-retries" in p for p in dpaths), f"{dpaths}")

        # subprocess smoke of the real CLI
        env = {**os.environ}
        idx = subprocess.run(["python3", str(REPO / "bin/index/run.py")], capture_output=True, text=True, env=env)
        srch = subprocess.run(["python3", str(REPO / "bin/search/run.py"), "exponential", "backoff"],
                              capture_output=True, text=True, env=env)
        check("CLI plainkeep search returns a hit", "exponential-backoff.md" in srch.stdout,
              srch.stdout.strip()[:80] or srch.stderr.strip()[:80])

    print(f"{BOLD}Stage-1 search implementation{RESET} — {len(results)} checks\n")
    passed = 0
    for name, ok, detail in results:
        mark = f"{GREEN}PASS{RESET}" if ok else f"{RED}FAIL{RESET}"
        passed += 1 if ok else 0
        print(f"  {mark} {name:<46}" + (f" {DIM}{detail}{RESET}" if (detail and not ok) else ""))
    failed = len(results) - passed
    print(f"\n{BOLD}Result:{RESET} {GREEN}{passed} passed{RESET}, "
          f"{(RED if failed else DIM)}{failed} failed{RESET}, {len(results)} checks")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
