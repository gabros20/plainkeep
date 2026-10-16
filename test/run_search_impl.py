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
import unicodedata
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

        # Unicode query tokens: a Hungarian note must be reachable with and without diacritics,
        # and punctuation-only queries must not raise.
        hu_name = "hu-ugyfel-szerzodes"
        (content / f"{hu_name}.md").write_text(
            "---\ntitle: Ügyfél szerződés\n---\n\n# Ügyfél szerződés\n\n"
            "Az ügyfél szerződését a jogász átnézte, a szerződés aláírása következik.\n",
            encoding="utf-8")
        index(content, verbose=False)
        hu_want = hu_name + ".md"
        hu_q = {"ügyfél szerződés": "accented phrase", "ugyfel": "diacritic-free",
                "szerződés": "accented single word",
                unicodedata.normalize("NFD", "ügyfél szerződés"): "decomposed (NFD) phrase"}
        for q, label in hu_q.items():
            hp = [p for p, _h, _s in search(q, k=5)]
            check(f"unicode: {label} ({q})", hu_want in hp, f"want {hu_want}, got {hp[:3]}")
        # 0.2 inflection prefix arm: an inflected form reaches the base form, and the exact form still wins
        for q in ("ügyfelek", "ügyfélnek"):
            hp = [p for p, _h, _s in search(q, k=5)]
            check(f"inflection: {q} finds ügyfél note", hu_want in hp, f"want {hu_want}, got {hp[:3]}")
        (content / "hu-ugyfelkapu.md").write_text(
            "---\ntitle: Ügyfélkapu\n---\n\n# Ügyfélkapu\n\nAz ügyfélkapu bejelentkezés lépései.\n",
            encoding="utf-8")
        index(content, verbose=False)
        top = [p for p, _h, _s in search("ügyfél", k=5)]
        check("inflection: exact-form query still ranks the exact note first",
              top[:1] == [hu_want], f"got {top[:3]}")
        try:
            junk = [p for p, _h, _s in search("__ ... !!", k=5)]
            check("unicode: punctuation-only query has no error", True)
            check("unicode: punctuation-only query returns no hits", junk == [], f"{junk[:3]}")
        except Exception as e:  # noqa: BLE001
            check("unicode: punctuation-only query has no error", False, f"{type(e).__name__}: {e}")

        # the punctuation guard only matters when the vector arm is on, so force it on with a stub
        # (no lancedb, no embedder): punctuation must stay out of it, a single letter must reach it
        class _StubEmbed:
            @staticmethod
            def embed_query(q):
                return [0.0]

        class _StubVectors:
            @staticmethod
            def count():
                return 1

            @staticmethod
            def search(qv, k):
                return [("exponential-backoff.md", "(top)", 1.0)]

        saved = (indexlib._vectors_on, indexlib._vec_modules, indexlib._rerank_on)
        indexlib._vectors_on = lambda: True
        indexlib._vec_modules = lambda: (_StubEmbed, _StubVectors)
        indexlib._rerank_on = lambda: False
        try:
            punct = [p for p, _h, _s in search("__ ... !!", k=5)]
            check("vectors on: punctuation-only query returns no hits", punct == [], f"{punct[:3]}")
            single = [p for p, _h, _s in search("C", k=5)]
            check("vectors on: single-letter query reaches vector arm",
                  "exponential-backoff.md" in single, f"{single[:3]}")
        finally:
            indexlib._vectors_on, indexlib._vec_modules, indexlib._rerank_on = saved

        # 0.3 every collection: a vault-root index, `collection` on each hit, `--collection` before top-k
        from lib import vaultfx
        with tempfile.TemporaryDirectory() as vtd:
            vault = Path(vtd) / "v"
            for rel, body in {
                "wiki/notes/zebra-wiki.md": "---\ntitle: Zebra wiki\n---\n\nZebrafish husbandry notes.\n",
                "tasks/active/T-20261001-01.md": "---\nid: T-20261001-01\ntitle: Zebrafish tank\n---\n\nClean the zebrafish tank.\n",
                "journal/2026/10/2026-10-01.md": "---\ntype: journal\n---\n\nWatched the zebrafish today.\n",
                "inbox/cap-20261001-0900.md": "---\ntype: capture\n---\n\nbuy zebrafish food\n",
                "files/skipme.md": "zebrafish outside every collection\n",
                ".obsidian/x.md": "zebrafish config\n",
            }.items():
                f = vault / rel
                f.parent.mkdir(parents=True, exist_ok=True)
                f.write_text(body, encoding="utf-8")
            _vid, pk = vaultfx.dispatchable_vault(vault, REPO)
            venv = {k: v for k, v in os.environ.items() if k not in ("PLAINKEEP_CONTENT", "PLAINKEEP_VECTORS", "PLAINKEEP_RERANK")}
            venv["PLAINKEEP_HOME"] = str(vault)

            def pkrun(*a):
                r = subprocess.run([str(pk), *a], capture_output=True, text=True, env=venv)
                return r.returncode, r.stdout

            pkrun("index")
            def jrows(out):
                """`--json` is JSONL: an envelope line (ops_json), then one object per row."""
                objs = [json.loads(ln) for ln in out.splitlines() if ln.strip()]
                return [o for o in objs if "ops_json" not in o]

            rc, out = pkrun("search", "zebrafish", "--json")
            rows = jrows(out)
            got = {r["path"]: r.get("collection") for r in rows}
            check("collections: all four indexed with their collection",
                  got == {"wiki/notes/zebra-wiki.md": "wiki", "tasks/active/T-20261001-01.md": "tasks",
                          "journal/2026/10/2026-10-01.md": "journal", "inbox/cap-20261001-0900.md": "inbox"},
                  f"rc={rc} {got}")
            for coll, want in (("tasks", "tasks/active/T-20261001-01.md"), ("journal", "journal/2026/10/2026-10-01.md"),
                               ("wiki", "wiki/notes/zebra-wiki.md")):
                rc, out = pkrun("search", "zebrafish", "--collection", coll, "--json")
                paths_ = [r["path"] for r in jrows(out)]
                check(f"collections: --collection {coll} returns only that collection", paths_ == [want], f"{paths_}")
            rc, _ = pkrun("search", "zebrafish", "--collection", "nope")
            check("collections: unknown --collection is a usage error", rc == 2, f"rc={rc}")

            # schema bump: an index from the previous schema is dropped whole and rebuilt by the next index
            import sqlite3
            dbp = vault / ".index" / "plainkeep.sqlite"
            c2 = sqlite3.connect(dbp)
            c2.execute("DELETE FROM meta WHERE key='schema'")
            c2.execute("DROP TABLE chunks")
            c2.execute("CREATE VIRTUAL TABLE chunks USING fts5(path, heading, body, slug UNINDEXED)")
            c2.execute("DELETE FROM files")
            c2.commit()
            c2.close()
            pkrun("index")
            rc, out = pkrun("search", "zebrafish", "--json")
            check("collections: old-schema index is rebuilt on the next index", "zebra-wiki" in out and "T-20261001-01" in out,
                  out[:120])

        # 0.4 index on write: a verb's note is searchable with no `plainkeep index` in between
        with tempfile.TemporaryDirectory() as vtd:
            vault = Path(vtd) / "v"
            vault.mkdir()
            _vid, pk = vaultfx.dispatchable_vault(vault, REPO)
            venv = {k: v for k, v in os.environ.items() if k not in ("PLAINKEEP_CONTENT", "PLAINKEEP_VECTORS", "PLAINKEEP_RERANK")}
            venv["PLAINKEEP_HOME"] = str(vault)

            def pkrun2(*a):
                r = subprocess.run([str(pk), *a], capture_output=True, text=True, env=venv)
                return r.returncode, r.stdout, r.stderr

            def found(q):
                _rc, out, _e = pkrun2("search", q, "--json")
                return [json.loads(ln).get("path") for ln in out.splitlines()
                        if ln.strip() and "ops_json" not in ln]

            pkrun2("capture", "quokkas are marsupials")
            hit = found("quokkas")
            check("index-on-write: capture is searchable immediately",
                  any(h.startswith("inbox/cap-") for h in hit), f"{hit}")
            check("index-on-write: the capture's journal line is searchable (journal collection)",
                  any(h.startswith("journal/") for h in found("quokkas")), f"{found('quokkas')}")
            pkrun2("task", "add", "feed the axolotl")
            check("index-on-write: task add is searchable", any(h.startswith("tasks/active/") for h in found("axolotl")),
                  f"{found('axolotl')}")
            tid = next((Path(h).stem for h in found("axolotl") if h.startswith("tasks/")), None)
            if tid:
                pkrun2("task", "done", tid)
                hs = found("axolotl")
                check("index-on-write: moved task is re-indexed at its new path, old path gone",
                      any(h.startswith("tasks/done/") for h in hs) and not any(h.startswith("tasks/active/") for h in hs),
                      f"{hs}")
            pkrun2("wiki", "new", "note", "Platypus burrows")
            check("index-on-write: wiki new is searchable", any(h.startswith("wiki/") for h in found("platypus")),
                  f"{found('platypus')}")
            # a broken index must warn, never fail the write
            (vault / ".index" / "plainkeep.sqlite").write_bytes(b"not a database")
            rc, out, err = pkrun2("capture", "wombats too")
            check("index-on-write: an unusable index warns on stderr and the capture still lands",
                  rc == 0 and "warning" in err and len(list((vault / "inbox").glob("cap-*.md"))) == 2,
                  f"rc={rc} err={err[:100]}")

            # cost per write (measured, reported): index_paths on one small note, in a fresh interpreter
            fx = vault / "wiki" / "notes"
            fx.mkdir(parents=True, exist_ok=True)
            (vault / ".index" / "plainkeep.sqlite").unlink()
            prog = (
                "import sys, time\nfrom pathlib import Path\nsys.path.insert(0, %r)\nimport indexlib\n"
                "fx = Path(%r)\ns = []\nfor i in range(20):\n"
                "    f = fx / f'cost-{i}.md'\n"
                "    f.write_text('---\\ntitle: cost %%d\\n---\\n\\n# cost\\n\\n' %% i + 'lorem ipsum dolor sit amet ' * 40 + f'[[cost-{i+1}]]\\n')\n"
                "    t = time.perf_counter(); indexlib.index_paths([f]); s.append((time.perf_counter() - t) * 1000)\n"
                "s.sort(); print(s[len(s)//2], s[-1])\n"
            ) % (str(REPO / "bin" / "lib"), str(fx))
            r = subprocess.run([sys.executable, "-c", prog], capture_output=True, text=True, env=venv)
            try:
                med, mx = (float(x) for x in r.stdout.split())
            except ValueError:
                med, mx = 9999.0, 9999.0
            print(f"  [measure] index_paths per write: median {med:.1f} ms, max {mx:.1f} ms {r.stderr[-200:]}")
            check("index-on-write: median cost per write < 50 ms", med < 50, f"{med:.1f} ms")

        # 0.15 cap disclosure + exhaustive tag listing: 35 tagged notes, prose-only mentions, look-alike tags
        with tempfile.TemporaryDirectory() as vtd:
            vault = Path(vtd) / "v"
            for i in range(35):
                tags = "[Coll-X, extra]" if i % 2 else "[#coll-x]"
                (vault / "wiki" / "notes").mkdir(parents=True, exist_ok=True)
                (vault / "wiki" / "notes" / f"member-{i:02d}.md").write_text(
                    f"---\ntype: note\ntitle: Member {i:02d}\ntags: {tags}\n---\n# Member {i:02d}\n\nquasar entry {i}\n",
                    encoding="utf-8")
            others = {
                "prose-only": "---\ntype: note\ntitle: Prose only\ntags: [misc]\n---\n# Prose only\n\nAbout coll-x and #coll-x and quasar.\n",
                "linker": "---\ntype: note\ntitle: Linker\ntags: []\n---\n# Linker\n\nSee [[member-00]] and coll-x quasar.\n",
                "prefix-tag": "---\ntype: note\ntitle: Prefix tag\ntags: [coll-x-extra]\n---\n# Prefix tag\n\nquasar\n",
                "suffix-tag": "---\ntype: note\ntitle: Suffix tag\ntags: [old-coll-x]\n---\n# Suffix tag\n\nquasar\n",
                "split-tag": "---\ntype: note\ntitle: Split tag\ntags: [coll, x]\n---\n# Split tag\n\nquasar\n",
            }
            for slug, body in others.items():
                (vault / "wiki" / "notes" / f"{slug}.md").write_text(body, encoding="utf-8")
            _vid, pk = vaultfx.dispatchable_vault(vault, REPO)
            venv = {k: v for k, v in os.environ.items() if k not in ("PLAINKEEP_CONTENT", "PLAINKEEP_VECTORS", "PLAINKEEP_RERANK")}
            venv["PLAINKEEP_HOME"] = str(vault)

            def pk3(*a):
                r = subprocess.run([str(pk), *a], capture_output=True, text=True, env=venv)
                return r.returncode, r.stdout, r.stderr

            def jl(out):
                objs = [json.loads(ln) for ln in out.splitlines() if ln.strip()]
                return objs[0], objs[1:]

            pk3("index")
            members = [f"member-{i:02d}" for i in range(35)]
            rc, out, _e = pk3("wiki", "list", "--tag", "coll-x", "--json")
            head, rows = jl(out)
            check("tag list: exactly the 35 tagged notes, sorted by slug",
                  [r["slug"] for r in rows] == members, f"{[r['slug'] for r in rows][:5]}... n={len(rows)}")
            check("tag list: row has title, slug, type and path",
                  rows and set(rows[0]) >= {"title", "slug", "type", "path"} and rows[0]["path"] == "wiki/notes/member-00.md"
                  and rows[0]["type"] == "note" and rows[0]["title"] == "Member 00", f"{rows[:1]}")
            for variant in ("#coll-x", "COLL-X", "#Coll-X"):
                _rc, o2, _e2 = pk3("wiki", "list", "--tag", variant, "--json")
                check(f"tag list: {variant} is the same set (case + leading # normalized)",
                      [r["slug"] for r in jl(o2)[1]] == members)
            check("tag list: prose, wikilink, prefix/suffix and split tags never match",
                  not {r["slug"] for r in rows} & set(others))
            _rc, o3, _e3 = pk3("wiki", "list", "--tag", "no-such-tag", "--json")
            h3, r3 = jl(o3)
            check("tag list: an unused tag is an empty list, not an error", _rc == 0 and r3 == [] and h3["count"] == 0)
            rc, _o, _e = pk3("wiki", "list", "--tag")
            check("tag list: --tag without a value is a usage error", rc == 2, f"rc={rc}")

            rc, out, _e = pk3("search", "quasar", "--json")
            head, rows = jl(out)
            check("cap: default returns 10 and says it was cut",
                  head["returned"] == 10 and head["limit"] == 10 and head["truncated"] is True and len(rows) == 10, f"{head}")
            check("cap: lexical_total is the exact FTS match count (40 notes say quasar)",
                  head["lexical_total"] == 40, f"{head['lexical_total']}")
            rc, out, _e = pk3("search", "quasar")
            check("cap: human output ends with the showing-of footer",
                  out.rstrip().splitlines()[-1] == "showing 10 of \u226540 \u2014 use --limit/--all", out.rstrip().splitlines()[-1:])
            rc, out, _e = pk3("search", "quasar", "--limit", "4", "--json")
            head, rows = jl(out)
            check("cap: --limit 4 returns 4, truncated", head["returned"] == 4 and head["limit"] == 4 and head["truncated"] and len(rows) == 4)
            rc, out, _e = pk3("search", "quasar", "--all", "--json")
            head, rows = jl(out)
            check("cap: --all returns every FTS match, not truncated",
                  head["returned"] == 40 and head["truncated"] is False and head["limit"] is None
                  and head["lexical_total"] == 40 and len({r["path"] for r in rows}) == 40, f"{head}")
            rc, out, _e = pk3("search", "quasar", "--all")
            check("cap: no footer when nothing was cut", "showing" not in out, out.rstrip().splitlines()[-1:])
            rc, out, _e = pk3("search", "member 07", "--json")
            head, _rows = jl(out)
            check("cap: a small result is not marked truncated",
                  head["truncated"] is False or head["returned"] == 10, f"{head}")
            for bad in (("--limit", "0"), ("--limit", "x"), ("--limit",), ("--limit", "3", "--all")):
                rc, _o, _e = pk3("search", "quasar", *bad)
                check(f"cap: bad flags {' '.join(bad)} are a usage error", rc == 2, f"rc={rc}")

        # --- review r1: open handoff, migration, honest freshness, vector prune, snippets ---
        with tempfile.TemporaryDirectory() as vtd:
            vault = Path(vtd) / "v"
            for rel, body in {
                "wiki/notes/flow.md": "---\ntype: note\ntitle: Flow A\ntags: [shared-tag]\n---\n# Flow A\n\nplatypusbrook ügyfél\n",
                "wiki/clients/flow.md": "---\ntype: note\ntitle: Flow B\ntags: [shared-tag]\n---\n# Flow B\n\nbody\n",
                "journal/2026/10/2026-10-02.md": "---\ntype: journal\n---\n\nsaw a platypusbrook\n",
                "wiki/notes/dates.md": "---\ntype: note\ntitle: Dates\n---\n# Dates\n\n2026-10-02 platypusbrook\n",
            }.items():
                f = vault / rel
                f.parent.mkdir(parents=True, exist_ok=True)
                f.write_text(body, encoding="utf-8")
            _vid, pk = vaultfx.dispatchable_vault(vault, REPO)
            venv = {k: v for k, v in os.environ.items() if k not in ("PLAINKEEP_CONTENT", "PLAINKEEP_VECTORS", "PLAINKEEP_RERANK")}
            venv["PLAINKEEP_HOME"] = str(vault)

            def pk4(*a):
                r = subprocess.run([str(pk), *a], capture_output=True, text=True, env=venv)
                return r.returncode, r.stdout, r.stderr

            def rows4(out):
                objs = [json.loads(ln) for ln in out.splitlines() if ln.strip()]
                return objs[0], objs[1:]

            # M3: a vault with no full index is "partial" after a write, not "built"
            pk4("capture", "zebracorn first")
            _rc, out, _e = pk4("status", "--json")
            st = json.loads(out)["data"]
            check("freshness: a one-note index created by a write is not reported as built",
                  st["index_age_min"] is None, f"{st['index_age_min']}")
            _rc, out, _e = pk4("status")
            check("freshness: status says partial", "partial" in out, out[-160:])
            pk4("index")
            _rc, out, _e = pk4("status", "--json")
            check("freshness: a finished plainkeep index reads as built", json.loads(out)["data"]["index_age_min"] is not None)
            pk4("capture", "zebracorn second")
            _rc, out, _e = pk4("status", "--json")
            check("freshness: later writes do not make the index look fresher", json.loads(out)["data"]["index_age_min"] is not None)

            # M1: search --open / open of a journal hit lands on that journal file
            _rc, out, _e = pk4("search", "platypusbrook", "--collection", "journal", "--open")
            check("open: search --open on a journal hit resolves the journal file, not a wiki lookalike",
                  "journal/2026/10/2026-10-02.md" in out, out.strip()[:120])
            _rc, out, _e = pk4("open", "journal/2026/10/2026-10-02.md", "--json")
            check("open: a vault-relative path from a hit opens that file",
                  json.loads(out)["data"]["kind"] == "journal", out[:120])

            # m2: wiki list --tag lists both notes that share a stem
            _rc, out, _e = pk4("wiki", "list", "--tag", "shared-tag", "--json")
            tagged = sorted(r["path"] for r in rows4(out)[1])
            check("tag list: two notes sharing a stem are both listed",
                  tagged == ["wiki/clients/flow.md", "wiki/notes/flow.md"], f"{tagged}")
            # N2: --tag on another action is a usage error
            rc, _o, _e = pk4("wiki", "orphans", "--tag", "x")
            check("tag list: --tag with another action is a usage error", rc == 2, f"rc={rc}")

            # m6: a prefix-only hit gets a snippet
            _rc, out, _e = pk4("search", "ügyfelek", "--json")
            sn = [r.get("snippet") for r in rows4(out)[1] if r["path"] == "wiki/notes/flow.md"]
            check("snippet: a prefix-only (inflected) hit shows an excerpt", bool(sn and sn[0]), f"{sn}")

            # m4: triage moves an inbox item; the index follows without a full index
            pk4("capture", "quokkafarm needs feeding")
            inbox = sorted((vault / "inbox").glob("cap-*quokkafarm*.md")) or \
                [f for f in sorted((vault / "inbox").glob("cap-*.md")) if "quokkafarm" in f.read_text(encoding="utf-8")]
            if inbox:
                pk4("triage", "decide", inbox[0].name, "note")
                _rc, out, _e = pk4("search", "quokkafarm", "--json")
                ps = [r["path"] for r in rows4(out)[1]]
                check("triage: the filed note is searchable and the inbox item is gone from the index",
                      any(x.startswith("wiki/") for x in ps) and not any(x.startswith("inbox/") for x in ps), f"{ps}")

            # m3: SystemExit from the index must not escape a write
            prog = ("import sys\nsys.path.insert(0, %r)\nimport indexlib, paths\n"
                    "def boom(_): raise SystemExit(5)\nindexlib.index_paths = boom\n"
                    "paths.index_written('x.md')\nprint('survived')\n") % str(REPO / "bin" / "lib")
            r = subprocess.run([sys.executable, "-c", prog], capture_output=True, text=True, env=venv)
            check("write-safety: a SystemExit inside the index step does not abort the verb",
                  r.returncode == 0 and "survived" in r.stdout and "warning" in r.stderr, f"rc={r.returncode} {r.stderr[-120:]}")

        # M2 + m5: an index from the previous schema migrates in place (no re-embed, --changed still works)
        import sqlite3 as _sq
        with tempfile.TemporaryDirectory() as vtd:
            vault = Path(vtd) / "v"
            (vault / "wiki" / "notes").mkdir(parents=True)
            (vault / "tasks" / "active").mkdir(parents=True)
            (vault / "wiki" / "notes" / "old.md").write_text("---\ntitle: Old\n---\n# Old\n\nmigratedword here\n", encoding="utf-8")
            (vault / "tasks" / "active" / "T-20260101-01.md").write_text("---\ntitle: T\n---\n# T\n\ntaskword migratedword\n", encoding="utf-8")
            _vid, pk = vaultfx.dispatchable_vault(vault, REPO)
            venv = {k: v for k, v in os.environ.items() if k not in ("PLAINKEEP_CONTENT", "PLAINKEEP_VECTORS", "PLAINKEEP_RERANK")}
            venv["PLAINKEEP_HOME"] = str(vault)

            def mk_v1(drop_links: bool):
                db = vault / ".index" / "plainkeep.sqlite"
                db.parent.mkdir(exist_ok=True)
                if db.exists():
                    db.unlink()
                c = _sq.connect(db)
                c.executescript("""
                    CREATE TABLE files(path TEXT PRIMARY KEY, hash TEXT);
                    CREATE VIRTUAL TABLE chunks USING fts5(path, heading, body, slug UNINDEXED);
                    CREATE TABLE links(src TEXT, dst TEXT);
                    CREATE TABLE embedded(path TEXT PRIMARY KEY, model TEXT);
                    CREATE TABLE notemeta(path TEXT PRIMARY KEY, author TEXT, derived_from TEXT);
                    CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);
                """)
                c.execute("INSERT INTO files VALUES('notes/old.md','abc')")
                c.execute("INSERT INTO chunks VALUES('notes/old.md','(top)','migratedword here','old')")
                c.execute("INSERT INTO embedded VALUES('notes/old.md','m')")
                c.execute("INSERT INTO meta VALUES('last_index_ts','9999999999')")   # far future: --changed would skip everything
                if drop_links:
                    c.execute("DROP TABLE links")   # makes the in-place migration fail -> falls back to the drop
                c.commit()
                c.close()

            def hits_for(q):
                r = subprocess.run([str(pk), "search", q, "--json"], capture_output=True, text=True, env=venv)
                return [json.loads(ln)["path"] for ln in r.stdout.splitlines() if ln.strip() and "ops_json" not in ln]

            mk_v1(False)
            before = hits_for("migratedword")   # first touch triggers the migration
            check("migration: old wiki rows survive in place under the wiki/ prefix", before == ["wiki/notes/old.md"], f"{before}")
            c = _sq.connect(vault / ".index" / "plainkeep.sqlite")
            emb = c.execute("SELECT path FROM embedded").fetchall()
            hasstamp = c.execute("SELECT 1 FROM meta WHERE key='last_index_ts'").fetchone()
            c.close()
            check("migration: embedding records keep their (prefixed) paths, so nothing is re-embedded",
                  emb == [("wiki/notes/old.md",)], f"{emb}")
            check("migration: the stale build stamp is gone", hasstamp is None)
            subprocess.run([str(pk), "index", "--changed"], capture_output=True, text=True, env=venv)
            after = hits_for("taskword")
            check("migration: index --changed after the bump finds the other collections",
                  after == ["tasks/active/T-20260101-01.md"], f"{after}")
            mk_v1(True)
            subprocess.run([str(pk), "index", "--changed"], capture_output=True, text=True, env=venv)
            both = sorted(hits_for("migratedword"))
            check("migration: when the in-place path fails the index is dropped and --changed still rebuilds it",
                  both == ["tasks/active/T-20260101-01.md", "wiki/notes/old.md"], f"{both}")

        # M4 + N4: stubbed vector plane (no lancedb needed)
        class _VS:
            deleted: list = []

            @staticmethod
            def delete_path(p):
                _VS.deleted.append(p)

        saved = indexlib._vectors_on, indexlib._vec_modules
        try:
            indexlib._vectors_on = lambda: True
            indexlib._vec_modules = lambda: (object(), _VS)
            gone = content / "ephemeral-note.md"
            gone.write_text("---\ntitle: E\n---\n\nephemeralword\n", encoding="utf-8")
            indexlib.index_paths([gone])
            gone.unlink()
            indexlib.index_paths([gone])
            check("vector prune: a note pruned by index_paths also loses its vector", "ephemeral-note.md" in _VS.deleted, f"{_VS.deleted}")
            indexlib._vec_modules = lambda: (None, None)
            check("pending: vectors on but no usable embedder reports 0, not every note", indexlib.pending_embeddings() == 0)
        finally:
            indexlib._vectors_on, indexlib._vec_modules = saved

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
