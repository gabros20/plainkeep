"""
indexlib.py — stage-1 search engine for the Personal OS (§10.2).

Implements exactly what the validated spec calls stage 1:
  - SQLite FTS5 over the content tree (wiki/tasks/journal), chunked by markdown heading,
    incremental by file content hash,
  - the wikilink graph (one-hop expansion) fused with keyword via reciprocal-rank fusion,
  - one rebuildable file at .index/plainkeep.sqlite (the rebuild rule: rm -rf .index && plainkeep index).

No vectors here — that's stage 2 (sqlite-vec + local Ollama), added only when a real query log
shows FTS5+graph missing (ADR-002). Pure stdlib (sqlite3); FTS5 ships with SQLite.
"""
from __future__ import annotations
import hashlib
import json
import os
import re
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

import sys as _sys0
_sys0.path.insert(0, str(Path(__file__).resolve().parent))  # importable as `lib.indexlib` AND top-level
try:
    from . import vaultroot  # type: ignore  # (namespace sibling)
except ImportError:
    import vaultroot  # type: ignore

# The SELECTED data root — no engine-relative fallback (ADR-014 D2, Phase 2 Task 1b). An index built
# against a guessed root is the quiet half of the same failure a wrong-root write is: it indexes the
# wrong notes and answers searches from them.
PLAINKEEP_HOME = vaultroot.active_root()
CONTENT = Path(os.environ.get("PLAINKEEP_CONTENT", PLAINKEEP_HOME / "wiki"))
INDEX_DIR = PLAINKEEP_HOME / ".index"
DB = INDEX_DIR / "plainkeep.sqlite"

LINK_RE = re.compile(r"\[\[([^\]]+)\]\]")
HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")

# Paths an external editor scatters that are NEVER content (proposal Part 3.1): Obsidian config +
# its local trash. Any file whose relative path has one of these components is skipped by the
# indexer (they live at the vault root, outside `wiki/`, but the guard is explicit + cheap).
IGNORE_PARTS = {".obsidian", ".trash", ".smart-env"}

import sys as _sys
_sys.path.insert(0, str(Path(__file__).resolve().parent))  # let sibling `embed`/`vectorstore` import in both contexts

# The write seam (lib/vaultio.py). This module is imported BOTH as `lib.indexlib` (verbs) and
# top-level as `indexlib` (test/run_search_impl.py:27 puts bin/lib on sys.path), so the import has
# to work either way — same reason the line above exists.
try:
    from . import vaultio  # type: ignore  # (namespace sibling)
except ImportError:
    import vaultio  # type: ignore


def _vectors_on() -> bool:
    return os.environ.get("PLAINKEEP_VECTORS", "").lower() in ("1", "true", "yes", "on")


def _vec_modules():
    """Lazily load the (optional) vector plane; returns (None, None) if unavailable.
    `import vectorstore` succeeds even without lancedb (it imports lancedb lazily), so we must
    deep-probe with vectorstore.available() — otherwise index() thinks vectors are on and crashes in
    pass 2 at the first connect()."""
    try:
        import embed
        import vectorstore
        if not vectorstore.available():
            return None, None
        return embed, vectorstore
    except Exception:
        return None, None


def _embed_stale(con, rel: str, model: str) -> bool:
    """True if this file has no current-model embedding yet (newly enabled, or model switched)."""
    r = con.execute("SELECT model FROM embedded WHERE path=?", (rel,)).fetchone()
    return (r is None) or (r[0] != model)


def _rerank_on() -> bool:
    return os.environ.get("PLAINKEEP_RERANK", "").lower() in ("1", "true", "yes", "on")


def _candidate_texts(paths: list[str], limit: int = 1200) -> dict:
    """Reconstruct each candidate note's text (from its FTS chunks) for the cross-encoder."""
    con = connect()
    out = {}
    for p in paths:
        rows = con.execute("SELECT heading, body FROM chunks WHERE path=?", (p,)).fetchall()
        out[p] = (" ".join(f"{h} {b}" for h, b in rows))[:limit]
    con.close()
    return out


def connect() -> sqlite3.Connection:
    vaultio.mkdir(INDEX_DIR)
    con = sqlite3.connect(DB)
    con.execute("PRAGMA journal_mode=WAL")
    con.executescript(
        """
        CREATE TABLE IF NOT EXISTS files(path TEXT PRIMARY KEY, hash TEXT);
        CREATE VIRTUAL TABLE IF NOT EXISTS chunks USING fts5(path, heading, body, slug UNINDEXED);
        CREATE TABLE IF NOT EXISTS links(src TEXT, dst TEXT);
        CREATE TABLE IF NOT EXISTS embedded(path TEXT PRIMARY KEY, model TEXT);
        CREATE TABLE IF NOT EXISTS notemeta(path TEXT PRIMARY KEY, author TEXT, derived_from TEXT);
        CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
        """
    )
    return con


def _meta_get(con, key: str) -> str | None:
    r = con.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return r[0] if r else None


def _meta_set(con, key: str, value: str) -> None:
    con.execute("INSERT OR REPLACE INTO meta(key, value) VALUES(?,?)", (key, value))


def _ignored(rel: str) -> bool:
    return any(part in IGNORE_PARTS for part in Path(rel).parts)


def _chunks(text: str) -> list[tuple[str, str]]:
    """Split markdown into (heading, body) sections; preamble under '(top)'."""
    out, head, buf = [], "(top)", []
    for ln in text.splitlines():
        m = HEADING_RE.match(ln)
        if m:
            if "".join(buf).strip():
                out.append((head, "\n".join(buf)))
            head, buf = m.group(2).strip(), []
        else:
            buf.append(ln)
    if "".join(buf).strip():
        out.append((head, "\n".join(buf)))
    return out or [("(top)", text)]


def _norm_link(target: str) -> str:
    return target.split("#", 1)[0].split("|", 1)[0].strip()


def _note_provenance(text: str) -> tuple[str, str]:
    """(author, derived_from) from the note's frontmatter — '' when absent (proposal Part 4.3).
    Inline + stdlib so indexlib stays dependency-free and standalone-loadable; the provenance planes
    become filterable so `plainkeep search --author human` can exclude agent + derived material."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return "", ""
    author = derived = ""
    for ln in lines[1:]:
        if ln.strip() == "---":
            break
        m = re.match(r"^(author|derived_from):\s*(.+)$", ln)
        if m:
            val = m.group(2).strip().strip('"').strip("'")
            if m.group(1) == "author":
                author = val
            else:
                derived = val
    return author, derived


def _embed_text(content: str, limit: int = 2000) -> str:
    """Note-level embed text: keep the title + tags (frontmatter flattened to text, not dropped —
    that signal helps retrieval), capped at `limit` chars. Matches the validated run_search_live."""
    return content.replace("---", " ")[:limit].strip()


def index(root: Path | str = CONTENT, verbose: bool = True, changed_only: bool = False) -> int:
    """Build/refresh the index. Resumable + batched so a large vault's multi-hour embedding
    backfill survives interruption (each FTS commit and each embed batch is durable; a re-run
    skips already-done files by hash + embedded-model, and continues where it left off).

    Sharding is transparent: it recurses the tree, so `notes/<aa>/<slug>.md` filesystem fanout
    (and per-area sub-repos pointed at via the content root) index without special handling.

    `changed_only` (proposal Part 3.1 — `plainkeep index --changed`): the external-edit fast path for a
    vault Obsidian is also editing. Instead of hashing every file, skip any file whose mtime is at
    or before the last recorded build time (assumed unchanged) — the full `plainkeep index` still does the
    complete hash pass, so this is a pure accelerator. `.obsidian/`, `.trash/`, `.smart-env/` are
    ignored either way.
    """
    root = Path(root)
    con = connect()
    started = time.time()
    last_ts = None
    if changed_only:
        raw = _meta_get(con, "last_index_ts")
        last_ts = float(raw) if raw else None
    vec_on = _vectors_on()
    emb, vs = _vec_modules() if vec_on else (None, None)
    if vec_on and not (emb and vs):
        print("PLAINKEEP_VECTORS=1 but lancedb isn't importable by the python3 that runs plainkeep — "
              "indexing keyword-only (stage 1).\n"
              "  Enable vectors: pip install -r requirements.txt into THAT interpreter "
              "(verify: python3 -c 'import lancedb'), then re-run `plainkeep index`. See requirements.txt.")
        vec_on = False
    model = emb.model_name() if vec_on else None
    BATCH = int(os.environ.get("PLAINKEEP_EMBED_BATCH", "32"))
    COMMIT_EVERY = 200

    # --- pass 1: keyword/graph (fast), collect the embed worklist ---
    seen, changed, worklist = set(), 0, []
    for p in sorted(root.rglob("*.md")):
        rel = str(p.relative_to(root))
        if _ignored(rel):
            continue
        seen.add(rel)
        if changed_only and last_ts is not None:
            try:
                if p.stat().st_mtime <= last_ts:
                    continue  # unchanged since the last build — fast-path skip (still in `seen`)
            except OSError:
                pass
        content = p.read_text(encoding="utf-8")
        h = hashlib.sha1(content.encode()).hexdigest()
        row = con.execute("SELECT hash FROM files WHERE path=?", (rel,)).fetchone()
        changed_file = not (row and row[0] == h)
        need_embed = vec_on and (changed_file or _embed_stale(con, rel, model))
        if not changed_file and not need_embed:
            continue  # unchanged AND already embedded with this model — skip (resume fast-path)
        if changed_file:
            con.execute("DELETE FROM chunks WHERE path=?", (rel,))
            con.execute("DELETE FROM links WHERE src=?", (rel,))
            slug = Path(rel).stem
            for head, body in _chunks(content):
                con.execute("INSERT INTO chunks(path, heading, body, slug) VALUES(?,?,?,?)",
                            (rel, head, body, slug))
            for tgt in {_norm_link(t) for t in LINK_RE.findall(content)}:
                con.execute("INSERT INTO links(src, dst) VALUES(?,?)", (rel, tgt))
            au, dv = _note_provenance(content)
            con.execute("INSERT OR REPLACE INTO notemeta(path, author, derived_from) VALUES(?,?,?)",
                        (rel, au, dv))
            con.execute("INSERT OR REPLACE INTO files(path, hash) VALUES(?,?)", (rel, h))
            changed += 1
            if changed % COMMIT_EVERY == 0:
                con.commit()  # durable progress for the keyword pass
        if need_embed:
            worklist.append((rel, _embed_text(content)))
    for (rel,) in con.execute("SELECT path FROM files").fetchall():  # prune deleted
        if rel not in seen:
            con.execute("DELETE FROM chunks WHERE path=?", (rel,))
            con.execute("DELETE FROM links WHERE src=?", (rel,))
            con.execute("DELETE FROM files WHERE path=?", (rel,))
            con.execute("DELETE FROM embedded WHERE path=?", (rel,))
            con.execute("DELETE FROM notemeta WHERE path=?", (rel,))
            if vec_on:
                vs.delete_path(rel)
    _meta_set(con, "last_index_ts", repr(started))  # build stamp for the next --changed fast path
    con.commit()

    # --- pass 2: embed the worklist in batches (one Ollama call per batch; durable per batch) ---
    # The keyword/graph index (pass 1) is already committed and durable. The vector plane is an
    # optional accelerator, so a mid-run failure here (embedder/Ollama unreachable, lancedb import
    # that probes but fails on connect, disk) must NOT lose that work — warn and finish keyword-only.
    embedded = 0
    if vec_on and worklist:
        try:
            vs.delete_paths([rel for rel, _ in worklist])  # clear partial/old vectors (idempotent resume)
            dim = emb.dim()
            for i in range(0, len(worklist), BATCH):
                batch = worklist[i:i + BATCH]
                vecs = emb.embed_docs([t for _, t in batch])
                recs = [{"id": rel, "path": rel, "heading": "(note)", "vector": vec}
                        for (rel, _), vec in zip(batch, vecs)]
                vs.add_batch(recs, dim)
                con.executemany("INSERT OR REPLACE INTO embedded(path, model) VALUES(?,?)",
                                [(rel, model) for rel, _ in batch])
                con.commit()  # durable: a crash after this batch resumes from here, not from zero
                embedded += len(batch)
                if verbose and (embedded % (BATCH * 5) == 0 or embedded == len(worklist)):
                    print(f"  embedded {embedded}/{len(worklist)} notes")
            vs.maybe_build_ann()
        except Exception as e:
            print(f"vector pass failed ({type(e).__name__}: {e}) — keyword/graph index is complete; "
                  "vectors skipped. Check the embedder (Ollama) and lancedb, then re-run `plainkeep index`.")
            vec_on = False  # keep the summary honest: don't claim vectors were written

    n = con.execute("SELECT COUNT(*) FROM files").fetchone()[0]
    con.close()
    if verbose:
        extra = f", {embedded} notes embedded ({model}) -> vectors.lance" if vec_on else ""
        print(f"indexed {n} files ({changed} (re)indexed){extra} -> {DB}")
    return n


def _fts_query(q: str) -> str:
    toks = [t for t in re.findall(r"[A-Za-z0-9]+", q.lower()) if len(t) > 1]
    return " OR ".join(f'"{t}"' for t in toks) or '""'


LOG_DIR = PLAINKEEP_HOME / ".logs"
QUERY_LOG = LOG_DIR / "queries.jsonl"


def log_query(query: str, hits: list[tuple[str, str, float]]) -> None:
    """Append a search to .logs/queries.jsonl — the real query log that settles the vector
    question over time (ADR-002). Add the slug that actually answered as `relevant` later to
    turn a logged query into a labeled benchmark case (`plainkeep search --mark`, future)."""
    vaultio.mkdir(LOG_DIR)
    rec = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "q": query,
        "hits": [p for p, _h, _s in hits[:5]],
        "relevant": None,
    }
    with vaultio.open_append(QUERY_LOG, encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def snippets(query: str, wanted, k: int = 40) -> dict:
    """FTS5 snippet() per matched note (proposal Part 3.4) — a short highlighted excerpt so an agent
    judges relevance without opening the file. Keyed by path; '' for a hit that matched via the graph
    or vector arm (no FTS body match). One MATCH query, first snippet per path (mirrors search()'s
    keyword arm). Never raises — snippets are a display aid, not the ranking."""
    wanted = set(wanted)
    if not wanted:
        return {}
    con = connect()
    out: dict = {}
    try:
        rows = con.execute(
            "SELECT path, snippet(chunks, 2, '<', '>', '…', 12) AS s, bm25(chunks) AS r "
            "FROM chunks WHERE chunks MATCH ? ORDER BY r LIMIT ?",
            (_fts_query(query), k),
        ).fetchall()
        for path, s, _r in rows:
            if path in wanted and path not in out:
                out[path] = " ".join((s or "").split())
    except sqlite3.OperationalError:
        pass
    con.close()
    return out


def _authorship(con) -> dict:
    """{path: (author, derived_from)} from notemeta (proposal Part 4.3) — the provenance planes."""
    return {p: (a or "", d or "")
            for p, a, d in con.execute("SELECT path, author, derived_from FROM notemeta")}


def search(query: str, k: int = 10, graph: bool = True, log: bool = False,
           author: str | None = None) -> list[tuple[str, str, float]]:
    """Return [(path, heading, score)] — FTS5 keyword + one-hop wikilink-graph, fused by RRF.

    `author` filters by provenance plane (Part 4.3): 'human' excludes agent-authored AND derived
    material (any note with `derived_from`); 'agent' keeps only `author: agent` notes."""
    con = connect()
    amap = _authorship(con) if author else {}
    try:
        rows = con.execute(
            "SELECT path, heading, bm25(chunks) AS r FROM chunks WHERE chunks MATCH ? ORDER BY r LIMIT ?",
            (_fts_query(query), k * 4),
        ).fetchall()
    except sqlite3.OperationalError:
        rows = []
    best, order = {}, []
    for path, head, _r in rows:
        if path not in best:
            best[path] = head
            order.append(path)
    scores = {p: 1.0 / (60 + i) for i, p in enumerate(order, 1)}  # RRF: keyword arm
    if graph and order:
        slug_to_file = {Path(p).stem: p for (p,) in con.execute("SELECT path FROM files")}
        for i, p in enumerate(order[:k], 1):
            for (dst,) in con.execute("SELECT dst FROM links WHERE src=?", (p,)):
                fp = slug_to_file.get(dst)
                if fp:
                    scores[fp] = scores.get(fp, 0.0) + 0.5 / (60 + i)  # RRF: graph arm (discounted)
    con.close()
    # --- vector arm (stage 2): RRF-fuse LanceDB ANN with keyword+graph ---
    if _vectors_on():
        emb, vs = _vec_modules()
        if emb and vs:
            try:
                if vs.count() > 0:
                    qv = emb.embed_query(query)
                    # per-NOTE max-pool: collapse chunk hits to the best chunk per note, so a
                    # multi-chunk note contributes ONE RRF term (at its best rank), not several.
                    # Limit dense mass to the vector top-k (keeps fusion clean), but when rerank is
                    # on, pull a WIDER candidate set so the reranker has the right note to reorder
                    # (recall widening — a precise note keyword+vector ranked low can still win).
                    vcount = 40 if _rerank_on() else k
                    seen, vrank = set(), 0
                    for path, head, _s in vs.search(qv, vcount):
                        if path in seen:
                            continue
                        seen.add(path)
                        vrank += 1
                        # weight dense > sparse: on paraphrase queries keyword's confident-but-wrong
                        # #1 must lose to the vector's right #1; on lexical queries both arms agree.
                        scores[path] = scores.get(path, 0.0) + 2.0 / (60 + vrank)
                        best.setdefault(path, head)
            except Exception:
                pass  # vectors are an accelerator; never let them break keyword search
    if author:
        def _keep(p: str) -> bool:
            a, d = amap.get(p, ("", ""))
            if author == "human":
                return a != "agent" and not d
            if author == "agent":
                return a == "agent"
            return True
        scores = {p: s for p, s in scores.items() if _keep(p)}
    pool = sorted(scores.items(), key=lambda x: -x[1])
    # --- stage 3: local cross-encoder rerank of the top candidates (precision layer) ---
    if _rerank_on() and len(pool) > 1:
        try:
            import rerank as _rr
            if _rr.available():
                n = min(len(pool), max(k, 30))  # rerank a wide pool (recall widening)
                cand = [p for p, _ in pool[:n]]
                texts = _candidate_texts(cand)
                rs = _rr.rerank(query, [texts.get(p, "") for p in cand])
                order = sorted(range(len(cand)), key=lambda i: -rs[i])
                pool = [(cand[i], rs[i]) for i in order] + pool[n:]
        except Exception:
            pass  # reranker is a precision layer; never let it break search
    ranked = pool[:k]
    hits = [(p, best.get(p, "(top)"), float(sc)) for p, sc in ranked]
    if log:
        log_query(query, hits)
    return hits
