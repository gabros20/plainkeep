"""
embed.py — local embedding via Ollama, with the per-model PROMPT PROFILES that the A/B proved
are mandatory (ADR-005). Without the doc/query prompts, EmbeddingGemma collapses; with them it
behaves. So prompts are a first-class part of the embedder, not an afterthought.

- Default model: embeddinggemma (ADR-005). Override with PLAINKEEP_EMBED_MODEL.
- Asymmetric prompts: documents and queries get different prefixes.
- Batch path (/api/embed) with single-call fallback (/api/embeddings), so bulk indexing of a
  large vault is one request per batch, not per chunk.

Pure stdlib (urllib). No server beyond the local Ollama daemon.
"""
from __future__ import annotations
import json
import os
import urllib.request

try:
    from . import hostconfig  # type: ignore  # (namespace sibling)
except ImportError:                      # loaded by file path with no package (test suites do)
    import sys as _sys
    _sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import hostconfig  # type: ignore

OLLAMA_HOST = hostconfig.ollama_host()

# (doc_prefix, query_prefix, dim). dim is the model's native output (Matryoshka can truncate later).
PROFILES = {
    "embeddinggemma":        ("title: none | text: ", "task: search result | query: ", 768),
    "multilingual-e5-large": ("passage: ", "query: ", 1024),
    "bge-m3":                ("", "", 1024),
    "qwen3-embedding":       ("", "Instruct: Given a search query, retrieve relevant passages\nQuery: ", 1024),
    "mxbai-embed-large":     ("", "Represent this sentence for searching relevant passages: ", 1024),
}
DEFAULT_MODEL = os.environ.get("PLAINKEEP_EMBED_MODEL", "embeddinggemma")


def _profile(model: str) -> tuple[str, str, int]:
    key = model.split(":")[0]
    return PROFILES.get(key, ("", "", 0))


def model_name() -> str:
    return DEFAULT_MODEL


def dim(model: str | None = None) -> int:
    d = _profile(model or DEFAULT_MODEL)[2]
    env = os.environ.get("PLAINKEEP_EMBED_DIM")
    return int(env) if env else d


def _post(path: str, payload: dict) -> dict:
    req = urllib.request.Request(OLLAMA_HOST + path,
                                 data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=300) as r:
        return json.loads(r.read())


def _truncate(vec: list[float], model: str) -> list[float]:
    d = dim(model)
    return vec[:d] if (d and len(vec) > d) else vec


def embed_batch(texts: list[str], model: str | None = None, prefix: str = "") -> list[list[float]]:
    """Embed many texts in one call where possible. Applies `prefix` to each."""
    model = model or DEFAULT_MODEL
    inputs = [prefix + t for t in texts]
    try:  # newer Ollama: /api/embed accepts a list and returns {"embeddings":[...]}
        out = _post("/api/embed", {"model": model, "input": inputs})
        if "embeddings" in out:
            return [_truncate(v, model) for v in out["embeddings"]]
    except Exception:
        pass
    # fallback: one /api/embeddings call per text
    vecs = []
    for t in inputs:
        out = _post("/api/embeddings", {"model": model, "prompt": t})
        vecs.append(_truncate(out["embedding"], model))
    return vecs


def embed_docs(texts: list[str], model: str | None = None) -> list[list[float]]:
    model = model or DEFAULT_MODEL
    return embed_batch(texts, model, prefix=_profile(model)[0])


def embed_query(text: str, model: str | None = None) -> list[float]:
    model = model or DEFAULT_MODEL
    return embed_batch([text], model, prefix=_profile(model)[1])[0]


def available(model: str | None = None) -> bool:
    try:
        embed_query("ping", model)
        return True
    except Exception:
        return False
