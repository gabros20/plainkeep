#!/usr/bin/env python3
"""run_image_backend.py — offline suite for the image-reading backend abstraction (bin/lib/imagelib.py).
NEVER loads a real model: PLAINKEEP_IMAGE_FAKE short-circuits read_text/describe, and the OCR/VLM fallback
chains are exercised by monkeypatching the module's `_has_*` probes and `_run_*` runners — the runners
are `# pragma: no cover` and asserted to be UNCALLED whenever their backend is reported unavailable."""
from __future__ import annotations
import base64
import importlib.util
import os
import sys
import tempfile
from pathlib import Path
from lib.hermetic import seal
seal()   # hermetic: an empty throwaway registry, never the developer's real vault

REPO = Path(__file__).resolve().parents[1]
GREEN, RED, DIM, BOLD, RESET = "\033[32m", "\033[31m", "\033[2m", "\033[1m", "\033[0m"
results = []

# a real, minimal 1x1 PNG — gives image_metadata a genuine file even without Pillow
PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=")


def check(name, cond, detail=""):
    results.append((name, bool(cond), detail))


def _load_imagelib():
    """Load bin/lib/imagelib.py by file path (bin/lib namespace loses to test/lib on sys.path)."""
    spec = importlib.util.spec_from_file_location("plainkeep_imagelib", REPO / "bin" / "lib" / "imagelib.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["plainkeep_imagelib"] = mod
    spec.loader.exec_module(mod)
    return mod


def _boom(*a, **kw):
    raise AssertionError("unavailable backend runner was called")


def main() -> int:
    il = _load_imagelib()
    for k in ("PLAINKEEP_IMAGE_FAKE", "PLAINKEEP_OCR", "PLAINKEEP_VLM", "PLAINKEEP_VLM_FALLBACK", "PLAINKEEP_MLX", "PLAINKEEP_VLM_KEEP_ALIVE"):
        os.environ.pop(k, None)

    with tempfile.TemporaryDirectory() as td:
        png = Path(td) / "pic.png"
        png.write_bytes(PNG_1X1)

        # ---------- image_metadata: stdlib basics always present ----------
        meta = il.image_metadata(png)
        check("metadata: format present", meta.get("format") == "png", str(meta))
        check("metadata: bytes present", meta.get("bytes") == len(PNG_1X1), str(meta))
        check("metadata: no GPS key ever", "gps" not in {k.lower() for k in meta}, str(meta))
        if il.have_pillow():
            check("metadata: width/height present (Pillow available)",
                  meta.get("width") == 1 and meta.get("height") == 1, str(meta))
            check("metadata: mode present (Pillow available)", "mode" in meta, str(meta))
        else:
            check("metadata: width/height absent (no Pillow) — doesn't crash",
                  "width" not in meta and "height" not in meta, str(meta))

        # non-image, non-existent-content path still gets format+bytes from suffix/stat alone
        txt = Path(td) / "note.txt"; txt.write_text("hi")
        meta2 = il.image_metadata(txt)
        check("metadata: works for any suffix (format from extension)", meta2.get("format") == "txt", str(meta2))

        # ---------- PLAINKEEP_IMAGE_FAKE: deterministic no-model seam ----------
        os.environ["PLAINKEEP_IMAGE_FAKE"] = "1"
        text, backend = il.read_text(png)
        check("fake: read_text label", backend == "fake", backend)
        check("fake: read_text text", text == f"[fake-ocr] {png.name}", text)
        cap, desc, vbackend = il.describe(png)
        check("fake: describe", (cap, desc, vbackend) == ("fake caption", "fake description", "fake"),
              str((cap, desc, vbackend)))
        os.environ.pop("PLAINKEEP_IMAGE_FAKE")

        # ---------- OCR dispatch: all probes false → none, real runners never called ----------
        orig = (il._has_mlx, il._has_ollama, il._has_ocrmac, il._has_tesseract,
                il._run_mlx_ocr, il._run_ollama_ocr, il._run_ocrmac, il._run_tesseract)
        try:
            il._has_mlx = lambda: False
            il._has_ollama = lambda: False
            il._has_ocrmac = lambda: False
            il._has_tesseract = lambda: False
            il._run_mlx_ocr = _boom
            il._run_ollama_ocr = _boom
            il._run_ocrmac = _boom
            il._run_tesseract = _boom
            text, backend = il.read_text(png)
            check("OCR auto: all unavailable → (\"\", \"none\")", (text, backend) == ("", "none"),
                  str((text, backend)))

            # only ocrmac available → picked, others' runners never invoked
            il._has_ocrmac = lambda: True
            il._run_ocrmac = lambda p: "ocr text"
            text, backend = il.read_text(png)
            check("OCR auto: only ocrmac available → label starts with ocrmac",
                  backend.startswith("ocrmac") and text == "ocr text", str((text, backend)))
        finally:
            (il._has_mlx, il._has_ollama, il._has_ocrmac, il._has_tesseract,
             il._run_mlx_ocr, il._run_ollama_ocr, il._run_ocrmac, il._run_tesseract) = orig

        # PLAINKEEP_OCR=none short-circuits before any probing — force a probe True + a raising runner to prove it
        orig_mlx, orig_run = il._has_mlx, il._run_mlx_ocr
        try:
            il._has_mlx = lambda: True
            il._run_mlx_ocr = _boom
            os.environ["PLAINKEEP_OCR"] = "none"
            text, backend = il.read_text(png)
            check("OCR PLAINKEEP_OCR=none short-circuits (no probing/calls)", (text, backend) == ("", "none"),
                  str((text, backend)))
        finally:
            os.environ.pop("PLAINKEEP_OCR", None)
            il._has_mlx, il._run_mlx_ocr = orig_mlx, orig_run

        # explicit backend requested but unavailable → ("", "none"), no chaining to another tier
        orig = (il._has_mlx, il._has_ollama, il._run_mlx_ocr, il._run_ollama_ocr)
        try:
            il._has_mlx = lambda: False
            il._has_ollama = lambda: True  # ollama IS available, but glm-ocr explicitly requests mlx
            il._run_mlx_ocr = _boom
            il._run_ollama_ocr = _boom
            os.environ["PLAINKEEP_OCR"] = "glm-ocr"
            text, backend = il.read_text(png)
            check("OCR explicit backend unavailable → (\"\", \"none\"), no chaining",
                  (text, backend) == ("", "none"), str((text, backend)))
        finally:
            os.environ.pop("PLAINKEEP_OCR", None)
            il._has_mlx, il._has_ollama, il._run_mlx_ocr, il._run_ollama_ocr = orig

        # ---------- ocr_backend_label: probe-only prediction of read_text's pick, NO model run ----------
        orig = (il._has_mlx, il._has_ollama, il._has_ocrmac, il._has_tesseract,
                il._run_mlx_ocr, il._run_ollama_ocr, il._run_ocrmac, il._run_tesseract)
        try:
            # all runners raise — a passing label prediction with no exception proves zero model calls
            il._run_mlx_ocr = _boom
            il._run_ollama_ocr = _boom
            il._run_ocrmac = _boom
            il._run_tesseract = _boom

            il._has_mlx = lambda: False
            il._has_ollama = lambda: False
            il._has_ocrmac = lambda: False
            il._has_tesseract = lambda: False
            check("ocr_backend_label auto: all unavailable → None", il.ocr_backend_label() is None)

            il._has_ocrmac = lambda: True
            check("ocr_backend_label auto: only ocrmac → 'ocrmac'", il.ocr_backend_label() == "ocrmac")

            il._has_mlx = lambda: True
            check("ocr_backend_label auto: mlx available → 'mlx-vlm:glm-ocr' (matches read_text's pick)",
                  il.ocr_backend_label() == "mlx-vlm:glm-ocr")

            # explicit backend: label only when its probe passes, else None (no chaining)
            il._has_mlx, il._has_ollama = (lambda: False), (lambda: True)
            os.environ["PLAINKEEP_OCR"] = "glm-ocr"
            check("ocr_backend_label explicit unavailable → None",
                  il.ocr_backend_label() is None)
            os.environ["PLAINKEEP_OCR"] = "deepseek-ocr"
            check("ocr_backend_label explicit available → matching label",
                  il.ocr_backend_label() == "ollama:deepseek-ocr")

            # PLAINKEEP_OCR=none → None, regardless of probes
            os.environ["PLAINKEEP_OCR"] = "none"
            check("ocr_backend_label PLAINKEEP_OCR=none → None", il.ocr_backend_label() is None)
            os.environ.pop("PLAINKEEP_OCR", None)

            # fake seam
            os.environ["PLAINKEEP_IMAGE_FAKE"] = "1"
            check("ocr_backend_label fake seam → 'fake'", il.ocr_backend_label() == "fake")
            os.environ.pop("PLAINKEEP_IMAGE_FAKE", None)

            # cross-check against read_text's actual pick under the same (mocked) probes, with the
            # mlx runner given a canned no-op result so read_text can be called safely
            il._has_mlx = lambda: True
            il._has_ollama = lambda: False
            il._has_ocrmac = lambda: False
            il._has_tesseract = lambda: False
            il._run_mlx_ocr = lambda p, model: "irrelevant"
            _, actual = il.read_text(png)
            check("ocr_backend_label matches read_text's actual selection",
                  il.ocr_backend_label() == actual, f"predicted={il.ocr_backend_label()!r} actual={actual!r}")
        finally:
            os.environ.pop("PLAINKEEP_OCR", None); os.environ.pop("PLAINKEEP_IMAGE_FAKE", None)
            (il._has_mlx, il._has_ollama, il._has_ocrmac, il._has_tesseract,
             il._run_mlx_ocr, il._run_ollama_ocr, il._run_ocrmac, il._run_tesseract) = orig

        # ---------- VLM dispatch: primary unavailable (fails), fallback available → uses fallback ----------
        orig = (il._has_mlx, il._has_ollama, il._run_mlx_vlm, il._run_ollama_vlm)
        try:
            os.environ["PLAINKEEP_VLM"] = "primary-model"
            os.environ["PLAINKEEP_VLM_FALLBACK"] = "fallback-model"
            il._has_mlx = lambda: False
            il._has_ollama = lambda: True

            def _ollama_vlm(path, model, keep_alive):
                if model == "primary-model":
                    raise RuntimeError("model not pulled")
                return "cap", "desc"
            il._run_mlx_vlm = _boom
            il._run_ollama_vlm = _ollama_vlm
            cap, desc, backend = il.describe(png)
            check("VLM: primary fails, fallback available → uses fallback",
                  (cap, desc, backend) == ("cap", "desc", "ollama:fallback-model"),
                  str((cap, desc, backend)))

            # both unavailable → none, mlx runner never called
            il._has_ollama = lambda: False
            cap, desc, backend = il.describe(png)
            check("VLM: both unavailable → (\"\",\"\",\"none\")",
                  (cap, desc, backend) == ("", "", "none"), str((cap, desc, backend)))
        finally:
            os.environ.pop("PLAINKEEP_VLM", None); os.environ.pop("PLAINKEEP_VLM_FALLBACK", None)
            il._has_mlx, il._has_ollama, il._run_mlx_vlm, il._run_ollama_vlm = orig

        # PLAINKEEP_VLM=none short-circuits before any probing/calls
        orig_ollama, orig_run = il._has_ollama, il._run_ollama_vlm
        try:
            il._has_ollama = lambda: True
            il._run_ollama_vlm = _boom
            os.environ["PLAINKEEP_VLM"] = "none"
            cap, desc, backend = il.describe(png)
            check("VLM PLAINKEEP_VLM=none short-circuits (no probing/calls)",
                  (cap, desc, backend) == ("", "", "none"), str((cap, desc, backend)))
        finally:
            os.environ.pop("PLAINKEEP_VLM", None)
            il._has_ollama, il._run_ollama_vlm = orig_ollama, orig_run

        # ---------- backends_status: pure probes, all expected keys, no model load ----------
        status = il.backends_status()
        expected = {"arch", "mlx_vlm", "ollama", "ocrmac", "tesseract", "pillow", "ocr_selected", "vlm_selected"}
        check("backends_status: all expected keys present", expected.issubset(status.keys()), str(status))
        check("backends_status: pillow flag matches have_pillow()", status["pillow"] == il.have_pillow(), str(status))

    print(f"\n{BOLD}Image-reading backend (bin/lib/imagelib.py) — {len(results)} checks{RESET}\n")
    passed = sum(1 for _, ok, _ in results if ok)
    for name, ok, detail in results:
        mark = f"{GREEN}PASS{RESET}" if ok else f"{RED}FAIL{RESET}"
        print(f"  {mark} {name:<58}" + (f" {DIM}{detail.strip()[:90]}{RESET}" if (detail and not ok) else ""))
    failed = len(results) - passed
    print(f"\n{BOLD}Result:{RESET} {GREEN}{passed} passed{RESET}, "
          f"{(RED if failed else DIM)}{failed} failed{RESET}, {len(results)} checks")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
