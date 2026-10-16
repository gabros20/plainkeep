"""
enrichlib.py — the ONE new stage that turns any extracted text into {description, keywords}
(search-enrichment proposal §2/§4). Every modality (image OCR/caption, voice transcript, video
captions, PDF markdown, article text) funnels through `enrich()`; the caller writes the result to
the shadow note's frontmatter.

- Model calls go over stdlib `urllib` to the local Ollama daemon (`embed.py`'s pattern) — never
  `import ollama` (not a dependency).
- Determinism: temperature=0 + a fixed seed, so a re-run on unchanged text is a true no-op; an
  idempotency key (`idem_key`) lets the caller skip work entirely when nothing changed.
- Honest floor: a tiny stdlib frequency+stopword extractor, no pip deps — YAKE/RAKE fragment
  agglutinative Hungarian and are pip deps anyway, so the *true* floor is stdlib-only (QA R3).
- Guards: empty/sentinel/short text never reaches the model (hallucinated keywords on garbage in);
  `PLAINKEEP_ENRICH=off` and `PLAINKEEP_ENRICH_FAKE` are per-stage seams (no global PLAINKEEP_MODELS_FAKE, QA §4).

Pure stdlib (urllib, hashlib, re). No server beyond the local Ollama daemon.
"""
from __future__ import annotations
import hashlib
import json
import os
import re
import urllib.request

try:
    from . import hostconfig  # type: ignore  # (namespace sibling)
except ImportError:                      # loaded by file path with no package (test suites do)
    import sys as _sys
    _sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import hostconfig  # type: ignore

OLLAMA_HOST = hostconfig.ollama_host()
DEFAULT_MODEL = hostconfig.get("models.enrich", "gemma4:e4b")
MIN_CHARS = 40                          # below this, there's nothing worth summarizing
SENTINEL = "_(no text extracted)_"      # files/run.py's empty-extraction marker
SEED = 7                                # fixed — pinned determinism, not a tuning knob (QA R5)

# stdlib frequency floor: min word length 3, drop function words. Not exhaustive — a floor, not a
# tagger. HU forms are surface-only (no stemming), which is exactly the honesty QA R3 asks for.
_STOPWORDS = {
    "the", "a", "an", "and", "or", "but", "if", "of", "to", "in", "on", "for", "with", "as", "by",
    "at", "is", "are", "was", "were", "be", "been", "being", "this", "that", "these", "those", "it",
    "its", "from", "not", "no", "so", "we", "you", "he", "she", "they", "them", "his", "her", "their",
    "our", "your", "my", "me", "us", "do", "does", "did", "have", "has", "had", "will", "would",
    "can", "could", "should", "may", "might", "than", "then", "there", "here", "which", "who", "whom",
    "what", "when", "where", "why", "how", "all", "any", "some", "such", "only", "also", "into",
    "about", "over", "under", "up", "down", "out", "off", "again", "further", "once", "just",
    "az", "és", "vagy", "de", "ha", "nem", "is", "hogy", "mint", "mert", "meg", "el", "fel", "le",
    "be", "ki", "van", "volt", "lesz", "egy", "ez", "ezt", "azt", "ennek", "annak", "ami", "aki",
    "mikor", "hol", "miért", "hogyan", "minden", "semmi", "csak", "még", "már", "ide", "oda", "itt",
    "ott", "így", "úgy", "én", "te", "ő", "mi", "ti", "ők", "ezek", "azok",
}
_WORD = re.compile(r"[a-záéíóöőúüű]{3,}", re.IGNORECASE)
_SENT_END = re.compile(r"(?<=[.!?])\s+")


def _fake() -> bool:
    return os.environ.get("PLAINKEEP_ENRICH_FAKE", "").lower() in ("1", "true", "yes")


def _post(path: str, payload: dict, timeout: int = 120) -> dict:
    req = urllib.request.Request(OLLAMA_HOST + path,
                                 data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:  # pragma: no cover - never run in tests
        return json.loads(r.read())


def _parse_json_object(text: str):
    """Extract the first JSON object from a model's reply, tolerating ``` fences and surrounding
    prose (mirrors files/run.py's `_parse_json_array`, same tolerant-parse shape for objects)."""
    t = re.sub(r"```(?:json)?", "", text or "").strip()
    i, j = t.find("{"), t.rfind("}")
    if i == -1 or j == -1 or j < i:
        return None
    try:
        data = json.loads(t[i:j + 1])
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def keyword_floor(text: str) -> list[str]:
    """Top ~8 content words by frequency, lowercased, stopwords dropped. Deterministic: ties break
    by first occurrence, not dict/set iteration order. The honest no-dep floor (QA R3)."""
    words = [w.lower() for w in _WORD.findall(text or "") if w.lower() not in _STOPWORDS]
    counts: dict[str, int] = {}
    first_seen: dict[str, int] = {}
    for i, w in enumerate(words):
        counts[w] = counts.get(w, 0) + 1
        first_seen.setdefault(w, i)
    ranked = sorted(counts, key=lambda w: (-counts[w], first_seen[w]))
    return ranked[:8]


def _first_sentences(text: str, max_chars: int = 200) -> str:
    """Floor description: lead sentence(s), trimmed. Same head-bias as the model path."""
    text = text.strip()
    sentences = _SENT_END.split(text)
    out = sentences[0] if sentences else text
    if len(out) < 60 and len(sentences) > 1:
        out = out + " " + sentences[1]
    return out[:max_chars].strip()


def idem_key(text: str, model: str | None = None) -> str:
    """Short stable hash of (enrich_model, text). A caller stores this in frontmatter so a plain
    re-run on unchanged text is a no-op; `--reenrich` forces past it (proposal §4 QA R5)."""
    model = model or DEFAULT_MODEL
    h = hashlib.sha256((model + "\x00" + (text or "")).encode("utf-8")).hexdigest()
    return h[:16]


def available() -> bool:
    """Probe whether the Ollama daemon answers. No model load — just liveness."""
    try:
        req = urllib.request.Request(OLLAMA_HOST + "/api/tags")
        with urllib.request.urlopen(req, timeout=2) as r:  # pragma: no cover - never run in tests
            return r.status == 200
    except Exception:
        return False


def _call_model(text: str, model: str) -> dict | None:
    """POST /api/generate with a structured-output schema; validate the reply. None on ANY failure
    (bad JSON, missing keys, connection error, model absent) — the caller falls back to the floor."""
    schema = {
        "type": "object",
        "properties": {
            "description": {"type": "string"},
            "keywords": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["description", "keywords"],
    }
    prompt = (
        "Summarize this text for a search index. Reply with ONLY a JSON object with keys "
        "\"description\" (one plain-prose sentence) and \"keywords\" (5-8 lowercase content words "
        "or short phrases). No text outside the JSON.\n\n" + text
    )
    try:
        out = _post("/api/generate", {
            "model": model, "prompt": prompt, "format": schema, "stream": False,
            "options": {"temperature": 0, "seed": SEED},
            "keep_alive": os.environ.get("PLAINKEEP_ENRICH_KEEP_ALIVE", "0"),
        })
        data = _parse_json_object(out.get("response", ""))
        if not isinstance(data, dict):
            return None
        desc = str(data.get("description", "")).strip()
        kws = [str(k).strip().lower() for k in (data.get("keywords") or []) if str(k).strip()]
        if not desc or not kws:
            return None
        return {"description": desc, "keywords": kws}
    except Exception:
        return None


def enrich(text: str, *, model: str | None = None) -> dict:
    """Return {description, keywords, backend, key}. backend in {"fake","ollama:<model>","floor",
    "none"}. Never raises and never hits the network for empty/short/off/sentinel text."""
    model = model or DEFAULT_MODEL
    key = idem_key(text, model)
    if _fake():
        return {"description": f"[fake] {text[:40].strip()}", "keywords": ["fake", "enrich"],
                "backend": "fake", "key": key}

    stripped = (text or "").strip()
    off = os.environ.get("PLAINKEEP_ENRICH", "").strip().lower() == "off"
    if off or not stripped or stripped == SENTINEL or len(stripped) < MIN_CHARS:
        backend = "none" if not stripped else "floor"
        return {"description": "", "keywords": keyword_floor(stripped), "backend": backend, "key": key}

    bounded = stripped[:8000]  # head-bias: leading text carries the headings (QA R7, files/run.py:580)
    result = _call_model(bounded, model)
    if result is not None:
        return {"description": result["description"], "keywords": result["keywords"],
                "backend": f"ollama:{model}", "key": key}
    return {"description": _first_sentences(bounded), "keywords": keyword_floor(bounded),
            "backend": "floor", "key": key}
