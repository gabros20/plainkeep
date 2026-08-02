#!/usr/bin/env python3
"""
run_search_live.py — settle the vector question against a REAL vault, without pre-labeling.

For an existing markdown vault you usually don't know which file "should" answer each query, so
this runs UNLABELED: for every query it shows keyword+graph top-3 vs real-vector top-3 side by
side and flags each query as:
  - agree        → keyword already returns what vectors do (keyword suffices; vectors add nothing)
  - KEYWORD-EMPTY→ keyword found nothing; only vectors surfaced anything (semantic-only)
  - DISAGREE     → different top hit; eyeball it — if the vector hit is the right one, that's a
                   query keyword would have missed (the share that earns stage-2 vectors).

The summary's "vectors-might-help share" = (empty + disagree)/total is your magnitude estimate.
Then eyeball the flagged rows: confirmed-better-vector-answers / total = the real semantic share.

Read-only: it only indexes/embeds; it never writes to the vault. (If the vault is in iCloud,
reading is fine — the §5 wall is about writes.) Embeddings are local (Ollama), offline, free.

Usage:
  python3 test/run_search_live.py --corpus ~/path/to/vault --queries my-queries.txt
  python3 test/run_search_live.py --corpus ~/vault --queries -   # read queries from stdin
"""
from __future__ import annotations
import argparse
import sys
from pathlib import Path
from lib.hermetic import seal
seal()   # hermetic: an empty throwaway registry, never the developer's real vault

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib.retrieval import Index, get_embedder  # noqa: E402
from lib.wiki import parse_note  # noqa: E402

GREEN, RED, YEL, DIM, BOLD, CYAN, RESET = (
    "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[1m", "\033[36m", "\033[0m")
LEAD_CHARS = 1500  # embed each note's lead (title+intro) — fast, fits the model context


def load_corpus_dir(d: Path) -> dict:
    notes = {}
    for p in sorted(Path(d).rglob("*.md")):
        try:
            notes[str(p.relative_to(d).with_suffix(""))] = p.read_text(encoding="utf-8")
        except Exception:
            pass
    return notes


def build_link_map(notes: dict) -> dict:
    base = {k.split("/")[-1]: k for k in notes}
    out = {}
    for k, text in notes.items():
        pr = parse_note(text)
        out[k] = [base[t.split("#")[0].split("|")[0].strip()]
                  for t in (pr["links"] if pr else [])
                  if t.split("#")[0].split("|")[0].strip() in base]
    return out


def top3(ranking):
    return [p for p, _ in ranking[:3]]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--corpus", required=True, help="path to an existing markdown vault")
    ap.add_argument("--queries", required=True, help="file with one query per line ('-' = stdin)")
    ap.add_argument("--limit", type=int, default=0, help="cap notes embedded (0 = all)")
    args = ap.parse_args()

    notes = load_corpus_dir(Path(args.corpus).expanduser())
    if not notes:
        print(f"{RED}no .md files under {args.corpus}{RESET}")
        return 2
    if args.limit:
        notes = dict(list(notes.items())[:args.limit])

    raw_q = sys.stdin.read() if args.queries == "-" else Path(args.queries).expanduser().read_text()
    queries = [ln.strip() for ln in raw_q.splitlines() if ln.strip() and not ln.startswith("#")]
    if not queries:
        print(f"{RED}no queries given{RESET}")
        return 2

    emb_name, emb_fn = get_embedder()
    if not emb_fn:
        print(f"{RED}no local embedder reachable (start ollama, or set PLAINKEEP_EMBED_CMD).{RESET}")
        return 2

    # Per-model asymmetric prompt prefixes (doc, query). Required by embeddinggemma/e5/mxbai;
    # omitting them collapses quality. No-prefix models map to ("","").
    PREFIX = {
        "embeddinggemma": ("title: none | text: ", "task: search result | query: "),
        "multilingual-e5-large": ("passage: ", "query: "),
        "mxbai-embed-large": ("", "Represent this sentence for searching relevant passages: "),
    }
    model = (emb_name or "").split(":")[-1]
    doc_pref, query_pref = PREFIX.get(model, ("", ""))
    base_embed = emb_fn
    def doc_embed(t): return base_embed(doc_pref + t)
    def query_embed(t): return base_embed(query_pref + t)

    idx = Index(notes, build_link_map(notes))
    # truncate to lead for speed; embed via the same fn over a trimmed Index.raw
    for k in idx.raw:
        idx.raw[k] = idx.raw[k][:LEAD_CHARS]
    print(f"{DIM}corpus: {args.corpus} ({len(notes)} notes) | embedder: {emb_name} | prompts: {'yes' if (doc_pref or query_pref) else 'none'} | embedding...{RESET}")
    done = 0
    vecs = {}
    for k in idx.keys:
        vecs[k] = doc_embed(idx.raw[k])
        done += 1
        if done % 100 == 0:
            print(f"{DIM}  embedded {done}/{len(idx.keys)}{RESET}")
    idx.vecs = vecs

    agree = empty = disagree = 0
    print(f"\n{BOLD}{'query':<46} {'flag':<14} keyword+graph top1  |  vector top1{RESET}\n")
    for q in queries:
        kg = idx.keyword_graph(q)
        ve = idx.vector(q, query_embed)
        kg_t, ve_t = top3(kg), top3(ve)
        if not kg_t:
            flag, color = "KEYWORD-EMPTY", YEL
            empty += 1
        elif kg_t[0] == ve_t[0]:
            flag, color = "agree", DIM
            agree += 1
        else:
            flag, color = "DISAGREE", CYAN
            disagree += 1
        k1 = kg_t[0] if kg_t else "—"
        v1 = ve_t[0] if ve_t else "—"
        print(f"{q[:45]:<46} {color}{flag:<14}{RESET} {k1[:26]:<28} | {v1[:26]}")
        if flag != "agree":
            print(f"{DIM}    kw+graph: {kg_t}\n    vector:   {ve_t}{RESET}")

    n = len(queries)
    share = (empty + disagree) / n
    print(f"\n{BOLD}Summary:{RESET} {agree} agree, {YEL}{empty} keyword-empty{RESET}, {CYAN}{disagree} disagree{RESET} / {n}")
    print(f"  vectors-might-help share (empty+disagree): {BOLD}{share:.0%}{RESET}")
    print(f"{DIM}  Now eyeball the non-agree rows: count how many the VECTOR top1 actually answers correctly.\n"
          f"  confirmed-vector-wins / {n} = your real semantic share. Rule of thumb:\n"
          f"  <~15% -> defer vectors (keyword+graph is enough, principle 6/7);\n"
          f"  >~25% with clear vector wins -> stage-2 (sqlite-vec + local Ollama) is earned (ADR-002).{RESET}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
