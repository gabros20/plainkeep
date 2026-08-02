#!/usr/bin/env python3
"""
run_search.py — measure retrieval quality and produce a data-driven verdict on whether the
system needs vector embeddings (gbrain stage-2). Offline, no LLM.

It runs three retrievers (keyword / keyword+graph / semantic-proxy) over the wiki corpus and
the labeled query set, reports recall@k + MRR overall and split by lexical/semantic bucket, and
prints a verdict using the design's own stage-2 trigger: "add vectors ONLY when FTS5 demonstrably
misses things you ask for."

Usage:  python3 test/run_search.py
"""
from __future__ import annotations
import argparse
import json
import sys
from pathlib import Path
from lib.hermetic import seal
seal()   # hermetic: an empty throwaway registry, never the developer's real vault

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib.retrieval import Index, metrics, lexical_overlap, rank_of, get_embedder  # noqa: E402
from lib.wiki import parse_note  # noqa: E402

WIKI = Path(__file__).resolve().parent / "world" / "wiki_corpus.json"
QRELS = Path(__file__).resolve().parent / "world" / "search_qrels.json"
GREEN, RED, YEL, DIM, BOLD, RESET = "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[1m", "\033[0m"


def build_link_map(notes: dict) -> dict[str, list[str]]:
    base_to_key = {k.split("/")[-1]: k for k in notes}
    links = {}
    for k, text in notes.items():
        p = parse_note(text)
        outs = []
        if p:
            for t in p["links"]:
                tk = base_to_key.get(t)
                if tk:
                    outs.append(tk)
        links[k] = outs
    return links


def avg(rows, key):
    return sum(r[key] for r in rows) / len(rows) if rows else 0.0


def load_corpus_dir(d: Path) -> dict:
    """Load a real content tree: {relpath-without-.md: text} for every markdown file."""
    return {str(p.relative_to(d).with_suffix("")): p.read_text(encoding="utf-8")
            for p in sorted(Path(d).rglob("*.md"))}


def load_qrels_file(p: Path) -> list[dict]:
    """Load a real query log / qrels: JSONL of {"q":..., "relevant": "slug-path"}.
    Lines with relevant null/missing are skipped (unlabeled — mark which note answered first)."""
    out = []
    for line in Path(p).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        rec = json.loads(line)
        if rec.get("relevant"):
            out.append({"q": rec["q"], "relevant": rec["relevant"], "intent": rec.get("intent", "?")})
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="Searchability analysis + vector decision (ADR-002)")
    ap.add_argument("--corpus", help="a real content directory (default: the fixture wiki)")
    ap.add_argument("--qrels", help="a real labeled query log JSONL (default: the fixture qrels)")
    args = ap.parse_args()

    if args.corpus:
        notes = load_corpus_dir(Path(args.corpus))
        print(f"{DIM}corpus: {args.corpus} ({len(notes)} notes){RESET}")
    else:
        notes = json.loads(WIKI.read_text(encoding="utf-8"))["notes"]
    if args.qrels:
        qrels = load_qrels_file(Path(args.qrels))
        print(f"{DIM}qrels: {args.qrels} ({len(qrels)} labeled queries){RESET}")
        if not qrels:
            print(f"{RED}No labeled queries (every line needs a non-null \"relevant\"). "
                  f"Mark which note answered each logged query first.{RESET}")
            return 2
    else:
        qrels = json.loads(QRELS.read_text(encoding="utf-8"))["queries"]
    idx = Index(notes, build_link_map(notes))

    methods = {
        "keyword (BM25)": idx.bm25,
        "keyword+graph": idx.keyword_graph,
    }
    emb_name, emb_fn = get_embedder()
    real_vectors = emb_fn is not None
    if real_vectors:
        print(f"{DIM}real embedder: {emb_name} — embedding {len(notes)} notes (local, offline)...{RESET}")
        idx.build_vectors(emb_fn)
        methods[f"vector [{emb_name}]"] = lambda q: idx.vector(q, emb_fn)
        methods["hybrid (BM25+vec)"] = lambda q: idx.hybrid_rrf(q, emb_fn)
        methods["hybrid (graph+vec)"] = lambda q: idx.hybrid_graph_vec(q, emb_fn)
    else:
        print(f"{DIM}no local embedder reachable — using trigram semantic-proxy (conservative estimate){RESET}")
        methods["semantic-proxy"] = idx.semantic_proxy

    # auto-bucket each query
    for entry in qrels:
        rel_text = notes[entry["relevant"]]
        entry["bucket"] = "lexical" if lexical_overlap(entry["q"], rel_text) else "semantic"

    n_lex = sum(1 for e in qrels if e["bucket"] == "lexical")
    n_sem = len(qrels) - n_lex
    print(f"{BOLD}Searchability analysis{RESET} — {len(notes)} notes, {len(qrels)} queries "
          f"({n_lex} lexical, {n_sem} semantic)\n")

    summary = {}
    for mname, fn in methods.items():
        rows = []
        for e in qrels:
            res = fn(e["q"])
            m = metrics(res, e["relevant"])
            m["bucket"] = e["bucket"]
            rows.append(m)
        lex = [r for r in rows if r["bucket"] == "lexical"]
        sem = [r for r in rows if r["bucket"] == "semantic"]
        summary[mname] = {
            "overall": {k: avg(rows, k) for k in ("recall@1", "recall@3", "recall@5", "rr")},
            "lexical": {k: avg(lex, k) for k in ("recall@1", "recall@3", "recall@5", "rr")},
            "semantic": {k: avg(sem, k) for k in ("recall@1", "recall@3", "recall@5", "rr")},
        }

    # table
    hdr = f"{'method':<18} {'bucket':<9} {'R@1':>6} {'R@3':>6} {'R@5':>6} {'MRR':>6}"
    print(BOLD + hdr + RESET)
    for mname, s in summary.items():
        for bucket in ("overall", "lexical", "semantic"):
            b = s[bucket]
            print(f"{mname:<18} {bucket:<9} {b['recall@1']:>6.2f} {b['recall@3']:>6.2f} "
                  f"{b['recall@5']:>6.2f} {b['rr']:>6.2f}")
        print()

    # per-query keyword+graph misses (the candidates vectors would rescue)
    print(f"{BOLD}Queries keyword+graph misses at rank 5 (vector candidates):{RESET}")
    misses = []
    for e in qrels:
        r = rank_of(idx.keyword_graph(e["q"]), e["relevant"])
        if r is None or r > 5:
            misses.append(e)
            print(f"  {RED}MISS{RESET} [{e['bucket']}] {DIM}{e['q']}{RESET}  → {e['relevant']} (rank={r})")
    if not misses:
        print(f"  {GREEN}none — keyword+graph found every relevant note in top-5{RESET}")

    # verdict — compare keyword+graph against the best semantic/hybrid method available
    kg = summary["keyword+graph"]
    if real_vectors:
        cmp_key = "hybrid (graph+vec)"
        cmp_label = f"hybrid graph+vec (real {emb_name})"
    else:
        cmp_key = "semantic-proxy"
        cmp_label = "semantic-proxy (trigram floor)"
    sp = summary[cmp_key]
    sem_recall_kg = kg["semantic"]["recall@5"]
    sem_lift = sp["semantic"]["recall@5"] - kg["semantic"]["recall@5"]
    print(f"\n{BOLD}Vector decision (design's stage-2 trigger: add ONLY when FTS5 demonstrably misses):{RESET}")
    print(f"  keyword+graph recall@5: overall {kg['overall']['recall@5']:.2f}, "
          f"lexical {kg['lexical']['recall@5']:.2f}, semantic {sem_recall_kg:.2f}")
    print(f"  {cmp_label} recovers {sem_lift:+.2f} recall@5 on the semantic bucket"
          + ("" if real_vectors else " (conservative floor for real embeddings)"))
    sem_share = n_sem / len(qrels)
    if sem_recall_kg >= 0.8 or sem_share < 0.25:
        verdict = ("NOT YET. keyword+graph covers the lexical bulk and the semantic gap is small. "
                   "Defer vectors per principle 6; revisit if real queries shift semantic.")
        color = GREEN
    elif sem_lift > 0.3 and sem_share >= 0.25:
        verdict = ("EARNED. A meaningful share of queries are semantic AND keyword+graph misses them "
                   "while even a conservative semantic method recovers them. Vectors (stage-2) are justified.")
        color = YEL
    else:
        verdict = ("BORDERLINE. Gather real query logs before committing; the synthetic signal is mixed.")
        color = YEL
    print(f"  {color}{BOLD}VERDICT: {verdict}{RESET}")
    caveat = ("real local embeddings used; remaining unknown is the SHARE of YOUR real queries that\n"
              "  are semantic — re-run on your actual query log to settle the magnitude."
              if real_vectors else
              "synthetic corpus + conservative proxy; set PLAINKEEP_EMBED_CMD or run ollama for real vectors.")
    print(f"{DIM}  NOTE: {caveat}\n"
          f"  Mechanism is philosophy-safe: vectors live in .index (gitignored, rebuildable from md),\n"
          f"  embedded local model (offline, free) — 'one SQLite file over a server', not a new source of truth.{RESET}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
