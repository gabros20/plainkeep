#!/usr/bin/env python3
"""
run_search_vec.py — tests the REAL stage-2 hybrid (LanceDB vectors + EmbeddingGemma) through the
production engine (bin/lib/indexlib.py with PLAINKEEP_VECTORS=1). Skips cleanly if the embedder or
lancedb isn't available (so run_all stays green on machines without them).

Proves: (1) `plainkeep index` embeds note-level vectors into LanceDB; (2) a zero-lexical-overlap semantic
query that keyword MISSES is recovered by the vector arm; (3) lexical queries don't regress;
(4) enabling vectors back-embeds an already-keyword-indexed corpus; (5) incremental re-index is a no-op.

Usage:  python3 test/run_search_vec.py
"""
from __future__ import annotations
import os
import sys
import tempfile
from pathlib import Path
from lib.hermetic import seal
seal()   # hermetic: an empty throwaway registry, never the developer's real vault

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(REPO / "bin" / "lib"))

GREEN, RED, YEL, DIM, BOLD, RESET = "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[1m", "\033[0m"

NOTES = {
    "notes/exponential-backoff.md": "---\ntype: note\ntitle: Exponential backoff\ntags: [reliability]\n---\n"
    "# Exponential backoff\nRetry delay doubles each attempt; add jitter to prevent a thundering herd "
    "hammering a recovering backend after an outage.",
    "notes/stripe-webhook-retries.md": "---\ntype: note\ntitle: Stripe webhook retries\ntags: [stripe]\n---\n"
    "# Stripe webhook retries\nStripe retries failed webhooks with exponential backoff over three days.",
    "notes/chunking.md": "---\ntype: note\ntitle: Chunking\ntags: [rag]\n---\n"
    "# Chunking\nSplit documents into overlapping windows for embedding; 512-token chunks, 50-token overlap.",
    "notes/rrf.md": "---\ntype: note\ntitle: Hybrid search\ntags: [rag]\n---\n"
    "# Hybrid search\nReciprocal rank fusion (RRF) merges BM25 and vector rankings without score scaling.",
}
SEMANTIC_Q = "avoid overwhelming a server with simultaneous reconnect storms"   # 0 lexical overlap
SEMANTIC_TARGET = "notes/exponential-backoff.md"
LEXICAL_Q = "reciprocal rank fusion"
LEXICAL_TARGET = "notes/rrf.md"


def main() -> int:
    # availability gate
    try:
        import embed
        import lancedb  # noqa: F401
        if not embed.available():
            raise RuntimeError("no embedder")
    except Exception as e:
        print(f"{YEL}SKIP{RESET} stage-2 vector test — embedder/lancedb unavailable ({e}). "
              f"Stage-1 covers keyword; this needs Ollama + lancedb.")
        return 0

    results = []
    with tempfile.TemporaryDirectory() as td:
        home = Path(td)
        content = home / "content"
        for k, v in NOTES.items():
            f = content / k
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(v, encoding="utf-8")
        os.environ["PLAINKEEP_HOME"] = str(home)
        os.environ["PLAINKEEP_CONTENT"] = str(content)
        import indexlib
        import vectorstore

        def topk(q, k=3):
            return [p for p, _h, _s in indexlib.search(q, k=k)]

        # 1) keyword-only first (vectors off) — establishes the miss
        os.environ["PLAINKEEP_VECTORS"] = "0"
        indexlib.index(content, verbose=False)
        kw_sem = topk(SEMANTIC_Q)
        results.append(("keyword misses the semantic query", SEMANTIC_TARGET not in kw_sem[:1], f"kw top3={kw_sem}"))

        # 2) enable vectors → back-embeds the already-indexed corpus (no content change)
        os.environ["PLAINKEEP_VECTORS"] = "1"
        indexlib.index(content, verbose=False)
        results.append(("enabling vectors back-embeds corpus", vectorstore.count() == len(NOTES),
                        f"lance rows={vectorstore.count()}"))

        # 3) semantic query now recovered by the vector arm
        v_sem = topk(SEMANTIC_Q)
        results.append(("vectors recover the semantic query (top-3)", SEMANTIC_TARGET in v_sem,
                        f"hybrid top3={v_sem}"))

        # 4) lexical query not regressed
        v_lex = topk(LEXICAL_Q)
        results.append(("lexical query still correct (top-1)", v_lex[:1] == [LEXICAL_TARGET],
                        f"top3={v_lex}"))

        # 5) incremental re-index is a no-op (nothing changed, nothing re-embedded)
        import io
        import contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            indexlib.index(content, verbose=True)
        results.append(("incremental re-index skips unchanged", "0 notes embedded" in buf.getvalue()
                        or "(0 (re)indexed)" in buf.getvalue(), buf.getvalue().strip()))

        # 6) stage-3: cross-encoder rerank (if a backend is installed) doesn't break the answers
        import rerank
        if rerank.available():
            os.environ["PLAINKEEP_RERANK"] = "1"
            rr_sem, rr_lex = topk(SEMANTIC_Q), topk(LEXICAL_Q)
            os.environ["PLAINKEEP_RERANK"] = "0"
            results.append((f"rerank [{rerank.backend()}] keeps semantic (top-3)", SEMANTIC_TARGET in rr_sem,
                            f"top3={rr_sem}"))
            results.append(("rerank keeps lexical (top-1)", rr_lex[:1] == [LEXICAL_TARGET], f"top3={rr_lex}"))
        else:
            print(f"{YEL}note{RESET} stage-3 rerank backend unavailable — `pip install fastembed` to enable it")

    print(f"{BOLD}Stage-2 hybrid (LanceDB + EmbeddingGemma) — {len(results)} checks{RESET}\n")
    passed = 0
    for name, ok, detail in results:
        mark = f"{GREEN}PASS{RESET}" if ok else f"{RED}FAIL{RESET}"
        passed += 1 if ok else 0
        print(f"  {mark} {name:<42}" + (f" {DIM}{detail}{RESET}" if (detail and not ok) else ""))
    failed = len(results) - passed
    print(f"\n{BOLD}Result:{RESET} {GREEN}{passed} passed{RESET}, "
          f"{(RED if failed else DIM)}{failed} failed{RESET}, {len(results)} checks")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
