"""
vectorstore.py — the scale-grade vector plane (ADR-006): LanceDB, embedded and file-based, with
disk IVF-PQ ANN that scales to billions on one node. NOT sqlite-vec (brute-force, dies past ~1M).

Lives at .index/vectors.lance/ — gitignored, rebuilt from markdown like every other index. No
server. One table of per-chunk vectors; delete-by-path keeps it incremental alongside the FTS index.
"""
from __future__ import annotations
import os
from pathlib import Path

import sys as _sys
_sys.path.insert(0, str(Path(__file__).resolve().parent))  # importable as `lib.vectorstore` AND top-level

# The write seam (lib/vaultio.py) — imported both ways for the same reason as the line above:
# indexlib does a bare `import vectorstore` (indexlib.py:51) and test/run_search_vec.py:65 too.
try:
    from . import vaultio, vaultroot  # type: ignore  # (namespace siblings)
except ImportError:
    import vaultio  # type: ignore
    import vaultroot  # type: ignore

# The SELECTED data root — no engine-relative fallback (ADR-014 D2, Phase 2 Task 1b).
PLAINKEEP_HOME = vaultroot.active_root()
LANCE_DIR = PLAINKEEP_HOME / ".index" / "vectors.lance"
TABLE = "chunks"
# Build an ANN index once the table is big enough to need it; below this, flat scan is exact + fast.
ANN_THRESHOLD = 50_000


def _lancedb():
    import lancedb  # imported lazily so stage-1 works without the dependency
    return lancedb


def available() -> bool:
    """True iff the LanceDB backend can actually be imported — a real probe, not a lazy one.
    `import vectorstore` always succeeds (lancedb is imported lazily in _lancedb), so callers that
    need to know whether the vector plane will WORK must ask this, not just import the module."""
    try:
        _lancedb()
        return True
    except Exception:
        return False


def connect():
    vaultio.mkdir(LANCE_DIR.parent)
    return _lancedb().connect(str(LANCE_DIR))


def _schema(dim: int):
    import pyarrow as pa
    return pa.schema([
        pa.field("id", pa.string()),
        pa.field("path", pa.string()),
        pa.field("heading", pa.string()),
        pa.field("vector", pa.list_(pa.float32(), dim)),
    ])


def _table(db, dim: int):
    if TABLE in db.table_names():
        return db.open_table(TABLE)
    return db.create_table(TABLE, schema=_schema(dim))


def upsert_path(path: str, records: list[dict], dim: int) -> None:
    """Replace all vectors for one file (delete-then-add) — keeps indexing incremental."""
    db = connect()
    tbl = _table(db, dim)
    tbl.delete(f"path = {_q(path)}")
    if records:
        tbl.add(records)


def delete_path(path: str) -> None:
    db = connect()
    if TABLE not in db.table_names():
        return
    db.open_table(TABLE).delete(f"path = {_q(path)}")


def delete_paths(paths: list[str]) -> None:
    """Bulk delete (chunked predicates) — used by the batched backfill for idempotent resume."""
    db = connect()
    if TABLE not in db.table_names() or not paths:
        return
    tbl = db.open_table(TABLE)
    for i in range(0, len(paths), 500):
        pred = " OR ".join(f"path = {_q(p)}" for p in paths[i:i + 500])
        tbl.delete(pred)


def add_batch(records: list[dict], dim: int) -> None:
    """Add many vectors in one call (batched backfill throughput)."""
    if not records:
        return
    db = connect()
    _table(db, dim).add(records)


def count() -> int:
    db = connect()
    if TABLE not in db.table_names():
        return 0
    return db.open_table(TABLE).count_rows()


def maybe_build_ann() -> None:
    """Create an IVF-PQ index when the corpus is large enough to need ANN (no-op when small)."""
    db = connect()
    if TABLE not in db.table_names():
        return
    tbl = db.open_table(TABLE)
    if tbl.count_rows() >= ANN_THRESHOLD:
        try:
            tbl.create_index(metric="cosine", vector_column_name="vector")
        except Exception:
            pass  # already indexed / will retry next run


def search(query_vec: list[float], k: int = 20) -> list[tuple[str, str, float]]:
    """Return [(path, heading, score)] best-first. score = 1 - cosine_distance."""
    db = connect()
    if TABLE not in db.table_names():
        return []
    tbl = db.open_table(TABLE)
    rows = tbl.search(query_vec).metric("cosine").limit(k).to_list()
    out = []
    for r in rows:
        dist = r.get("_distance", 1.0)
        out.append((r["path"], r.get("heading", "(top)"), 1.0 - float(dist)))
    return out


def _q(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"
