"""
imagelib.py — cross-architecture image reading: metadata (stdlib + optional Pillow), OCR text
extraction, and VLM captioning/description. Local-first, no `from lib import …` (loadable by file
path, same bin/lib-namespace-vs-test/lib gotcha as sharelib.py).

Every real model/tool call sits behind a tiny probe (`_has_mlx()`, `_has_ollama()`, `_has_ocrmac()`,
`_has_tesseract()`, `_arch()`) so the fallback chain is unit-testable by monkeypatching the probes and
the runner functions — the runners themselves are marked `# pragma: no cover` and must never run in
tests. This mirrors bin/files/run.py's `_tier_*` graceful-degrade style.

Test seam: PLAINKEEP_IMAGE_FAKE (mirrors bin/share/run.py's `_fake()`) short-circuits read_text/describe to
canned output so the offline suite can exercise dispatch logic with zero models installed.
"""
from __future__ import annotations
import base64
import importlib.util
import json
import os
import platform
import shutil
import subprocess
import urllib.request
from pathlib import Path

# Ollama is talked to over its HTTP API via stdlib urllib (mirrors bin/lib/embed.py) — never `import
# ollama` (a pip package NOT in requirements.txt; `_has_ollama()` only probes the CLI, so importing
# the pip package here would let a host with the CLI but not the package pass the probe then
# ImportError at runtime).
try:
    from . import hostconfig  # type: ignore  # (namespace sibling)
except ImportError:                      # loaded by file path with no package (test suites do)
    import sys as _sys
    _sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import hostconfig  # type: ignore

OLLAMA_HOST = hostconfig.ollama_host()

try:
    from PIL import Image, ExifTags  # type: ignore
    HAVE_PILLOW = True
except Exception:  # pragma: no cover - exercised only on the zero-install path
    Image = None  # type: ignore
    ExifTags = None  # type: ignore
    HAVE_PILLOW = False


def have_pillow() -> bool:
    return HAVE_PILLOW


def _fake() -> bool:
    return os.environ.get("PLAINKEEP_IMAGE_FAKE", "").strip().lower() in ("1", "true", "yes")


def _have_mod(name: str) -> bool:
    """Optional dep present? importlib probe only — never imports (mirrors bin/files/run.py)."""
    try:
        return importlib.util.find_spec(name) is not None
    except Exception:
        return False


# --------------------------------------------------------------------------- probes (monkeypatched in tests)

def _arch() -> str:
    m = platform.machine().lower()
    if m in ("arm64", "aarch64"):
        return "arm64"
    if m in ("x86_64", "amd64"):
        return "x86_64"
    return m


def _has_mlx() -> bool:
    return _arch() == "arm64" and _have_mod("mlx_vlm")


def _has_ollama() -> bool:
    return shutil.which("ollama") is not None


def _has_ocrmac() -> bool:
    return _have_mod("ocrmac")


def _has_tesseract() -> bool:
    return shutil.which("tesseract") is not None


def _mlx_enabled() -> bool:
    """PLAINKEEP_MLX=off forces the ollama runtime for VLM even on Apple Silicon (describe() only — the OCR
    auto chain has no such override, per the tiered-extraction contract)."""
    return (os.environ.get("PLAINKEEP_MLX", "auto") or "auto").strip().lower() != "off"


# --------------------------------------------------------------------------- metadata (stdlib + optional Pillow)

def image_metadata(path) -> dict:
    """format + bytes always work on stdlib alone. width/height/mode/EXIF only when Pillow is
    importable — never crash without it. GPS is NEVER surfaced, even if Pillow exposes it (privacy)."""
    p = Path(path)
    out: dict = {"format": p.suffix.lstrip(".").lower(), "bytes": os.stat(p).st_size}
    if not HAVE_PILLOW:
        return out
    try:
        with Image.open(p) as im:
            out["width"], out["height"] = im.size
            out["mode"] = im.mode
            exif = im.getexif()
            if exif:
                tags = {ExifTags.TAGS.get(k, k): v for k, v in exif.items()}
                tags.pop("GPSInfo", None)  # privacy rule: drop location even if Pillow exposes it
                taken = tags.get("DateTimeOriginal") or tags.get("DateTime")
                if taken:
                    out["taken"] = str(taken)
                if "Model" in tags:
                    out["camera"] = str(tags["Model"])
                if "Orientation" in tags:
                    out["orientation"] = tags["Orientation"]
    except Exception:
        pass
    return out


# --------------------------------------------------------------------------- OCR dispatch

def _run_mlx_ocr(path: Path, model: str) -> str:  # pragma: no cover - real model call, never in tests
    from mlx_vlm import generate, load
    from mlx_vlm.prompt_utils import apply_chat_template
    from mlx_vlm.utils import load_config
    mpath = f"mlx-community/{model}"
    m, processor = load(mpath)
    config = load_config(mpath)
    prompt = apply_chat_template(processor, config, "Transcribe all text in this image, verbatim.",
                                 num_images=1)
    return (generate(m, processor, prompt, [str(path)], verbose=False).text or "").strip()


def _ollama_generate(model: str, prompt: str, b64_image: str, keep_alive) -> str:  # pragma: no cover
    """POST /api/generate with the image inlined as base64, non-streamed (one JSON object back)."""
    payload = {"model": model, "prompt": prompt, "images": [b64_image], "keep_alive": keep_alive,
              "stream": False}
    req = urllib.request.Request(OLLAMA_HOST + "/api/generate", data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=300) as r:
        return (json.loads(r.read()).get("response") or "").strip()


def _run_ollama_ocr(path: Path, model: str) -> str:  # pragma: no cover - real model call, never in tests
    b64 = base64.b64encode(Path(path).read_bytes()).decode("ascii")
    return _ollama_generate(model, "Transcribe all text in this image, verbatim.", b64,
                            os.environ.get("PLAINKEEP_VLM_KEEP_ALIVE", "0"))


def _run_ocrmac(path: Path) -> str:  # pragma: no cover - real model call, never in tests
    from ocrmac import ocrmac
    return "\n".join(a[0] for a in ocrmac.OCR(str(path)).recognize()).strip()


def _run_tesseract(path: Path) -> str:  # pragma: no cover - real model call, never in tests
    return subprocess.run(["tesseract", str(path), "stdout"], capture_output=True, text=True).stdout.strip()


def read_text(path, opts: dict | None = None) -> tuple[str, str]:
    """OCR dispatch. PLAINKEEP_OCR selects a backend (default auto); auto falls through mlx-vlm → ollama →
    ocrmac → tesseract → none. An explicit backend that's unavailable returns ("", "none") — no
    chaining onto the next tier (that's what auto is for)."""
    opts = opts or {}
    p = Path(path)
    if _fake():
        return f"[fake-ocr] {p.name}", "fake"
    mode = (os.environ.get("PLAINKEEP_OCR", "auto") or "auto").strip().lower()
    if mode == "none":
        return "", "none"
    if mode == "glm-ocr":
        return (_run_mlx_ocr(p, "glm-ocr"), "mlx-vlm:glm-ocr") if _has_mlx() else ("", "none")
    if mode == "deepseek-ocr":
        return (_run_ollama_ocr(p, "deepseek-ocr"), "ollama:deepseek-ocr") if _has_ollama() else ("", "none")
    if mode == "apple":
        return (_run_ocrmac(p), "ocrmac") if _has_ocrmac() else ("", "none")
    if mode == "tesseract":
        return (_run_tesseract(p), "tesseract") if _has_tesseract() else ("", "none")
    # auto
    if _has_mlx():
        return _run_mlx_ocr(p, "glm-ocr"), "mlx-vlm:glm-ocr"
    if _has_ollama():
        return _run_ollama_ocr(p, "deepseek-ocr"), "ollama:deepseek-ocr"
    if _has_ocrmac():
        return _run_ocrmac(p), "ocrmac"
    if _has_tesseract():
        return _run_tesseract(p), "tesseract"
    return "", "none"


def ocr_backend_label(opts: dict | None = None) -> str | None:
    """The label read_text() WOULD select, decided by the same probes/env — no model run. Lets a
    dry-run / unchanged-file preview show its OCR tier without eagerly paying for the OCR itself
    (mirrors bin/files/run.py's cheap-label / lazy-run() split for the other extraction tiers)."""
    opts = opts or {}
    if _fake():
        return "fake"
    mode = (os.environ.get("PLAINKEEP_OCR", "auto") or "auto").strip().lower()
    if mode == "none":
        return None
    if mode == "glm-ocr":
        return "mlx-vlm:glm-ocr" if _has_mlx() else None
    if mode == "deepseek-ocr":
        return "ollama:deepseek-ocr" if _has_ollama() else None
    if mode == "apple":
        return "ocrmac" if _has_ocrmac() else None
    if mode == "tesseract":
        return "tesseract" if _has_tesseract() else None
    # auto
    if _has_mlx():
        return "mlx-vlm:glm-ocr"
    if _has_ollama():
        return "ollama:deepseek-ocr"
    if _has_ocrmac():
        return "ocrmac"
    if _has_tesseract():
        return "tesseract"
    return None


# --------------------------------------------------------------------------- VLM dispatch (caption + description)

def _run_mlx_vlm(path: Path, model: str) -> tuple[str, str]:  # pragma: no cover - real model call, never in tests
    from mlx_vlm import generate, load
    from mlx_vlm.prompt_utils import apply_chat_template
    from mlx_vlm.utils import load_config
    mpath = f"mlx-community/{model}"
    m, processor = load(mpath)
    config = load_config(mpath)
    prompt = apply_chat_template(processor, config,
                                 "Caption this image in one line, then describe it in one short paragraph.",
                                 num_images=1)
    out = (generate(m, processor, prompt, [str(path)], verbose=False).text or "").strip()
    lines = out.splitlines()
    return (lines[0].strip() if lines else ""), ("\n".join(lines[1:]).strip() if len(lines) > 1 else out)


def _run_ollama_vlm(path: Path, model: str, keep_alive: str) -> tuple[str, str]:  # pragma: no cover
    b64 = base64.b64encode(Path(path).read_bytes()).decode("ascii")
    out = _ollama_generate(model, "Caption this image in one line, then describe it in one short paragraph.",
                           b64, keep_alive)
    lines = out.splitlines()
    return (lines[0].strip() if lines else ""), ("\n".join(lines[1:]).strip() if len(lines) > 1 else out)


def _try_vlm(path: Path, model: str, keep_alive: str):
    """One model, one runtime pick (mlx if eligible else ollama). None = unavailable/failed — the
    caller moves on to the next model rather than retrying this one on the other runtime."""
    if not model or model.strip().lower() == "none":
        return None
    if _mlx_enabled() and _has_mlx():
        try:
            cap, desc = _run_mlx_vlm(path, model)
            return cap, desc, f"mlx-vlm:{model}"
        except Exception:
            return None
    if _has_ollama():
        try:
            cap, desc = _run_ollama_vlm(path, model, keep_alive)
            return cap, desc, f"ollama:{model}"
        except Exception:
            return None
    return None


def describe(path, opts: dict | None = None) -> tuple[str, str, str]:
    """VLM dispatch: primary (PLAINKEEP_VLM) then fallback (PLAINKEEP_VLM_FALLBACK), each via mlx-vlm when
    eligible else ollama. PLAINKEEP_VLM=none skips entirely; either model missing/failing falls through."""
    opts = opts or {}
    p = Path(path)
    if _fake():
        return "fake caption", "fake description", "fake"
    primary = (os.environ.get("PLAINKEEP_VLM", "qwen3-vl:4b") or "qwen3-vl:4b").strip()
    if primary.lower() == "none":
        return "", "", "none"
    fallback = (os.environ.get("PLAINKEEP_VLM_FALLBACK", "moondream") or "moondream").strip()
    keep_alive = os.environ.get("PLAINKEEP_VLM_KEEP_ALIVE", "0")
    for model in (primary, fallback):
        r = _try_vlm(p, model, keep_alive)
        if r is not None:
            return r
    return "", "", "none"


# --------------------------------------------------------------------------- status (pure probes, no model load)

def backends_status() -> dict:
    return {
        "arch": _arch(),
        "mlx_vlm": _has_mlx(),
        "ollama": _has_ollama(),
        "ocrmac": _has_ocrmac(),
        "tesseract": _has_tesseract(),
        "pillow": HAVE_PILLOW,
        "ocr_selected": (os.environ.get("PLAINKEEP_OCR", "auto") or "auto").strip().lower(),
        "vlm_selected": (os.environ.get("PLAINKEEP_VLM", "qwen3-vl:4b") or "qwen3-vl:4b").strip(),
    }
