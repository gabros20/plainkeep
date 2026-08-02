#!/usr/bin/env python3
"""run_files_image.py — offline suite for imagelib wired into `plainkeep files` (bin/files/run.py):
metadata on ingest, OCR via imagelib.read_text, and --describe via imagelib.describe. Runs entirely
with PLAINKEEP_IMAGE_FAKE=1 (no real models) against a temp ~/plainkeep + ~/files, reusing the 1x1 PNG fixture
bytes from test/run_image_backend.py."""
from __future__ import annotations
import base64
import importlib.util
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from lib.hermetic import seal
seal()   # hermetic: an empty throwaway registry, never the developer's real vault

REPO = Path(__file__).resolve().parents[1]
GREEN, RED, DIM, BOLD, RESET = "\033[32m", "\033[31m", "\033[2m", "\033[1m", "\033[0m"
results = []

# same fixture bytes as test/run_image_backend.py — a real, minimal 1x1 PNG
PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=")


def check(name, cond, detail=""):
    results.append((name, bool(cond), detail))


def _have_pillow() -> bool:
    spec = importlib.util.spec_from_file_location("plainkeep_imagelib_probe", REPO / "bin" / "lib" / "imagelib.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.have_pillow()


def run(ops, roots, *args, extra=None):
    env = {**os.environ, "PLAINKEEP_HOME": str(ops), "PLAINKEEP_ROOTS_HOME": str(roots),
          "PLAINKEEP_IMAGE_FAKE": "1", **(extra or {})}
    return subprocess.run([sys.executable, str(REPO / "bin" / "files" / "run.py"), *args],
                          capture_output=True, text=True, env=env)


def main() -> int:
    have_pillow = _have_pillow()

    with tempfile.TemporaryDirectory() as td:
        ops, roots = Path(td) / "ops", Path(td) / "home"
        (ops / "wiki").mkdir(parents=True); (ops / "journal").mkdir()
        inbox = ops / "inbox"; inbox.mkdir()
        (inbox / "pic.png").write_bytes(PNG_1X1)

        # ---------- Layer 1: metadata merged into the shadow note on ingest ----------
        r = run(ops, roots, "ingest")
        shadow = ops / "wiki" / "files" / "pic.md"
        check("ingest files the image", (roots / "files" / "inbox" / "pic.png").exists(), r.stdout + r.stderr)
        fm = shadow.read_text() if shadow.exists() else ""
        check("shadow note has kind: image", "kind: image" in fm, fm)
        check("shadow note has format: png", "format: png" in fm, fm)
        check("shadow note has bytes:", f"bytes: {len(PNG_1X1)}" in fm, fm)
        if have_pillow:
            check("shadow note has width/height (Pillow available)",
                  "width: 1" in fm and "height: 1" in fm, fm)
        else:
            check("shadow note has no width/height (no Pillow) — doesn't crash",
                  "width:" not in fm and "height:" not in fm, fm)

        # ---------- laziness: --dry-run is a cheap preview, never runs the OCR backend ----------
        extract = ops / "wiki" / "files" / "pic.extract.md"
        r = run(ops, roots, "extract", "pic", "--dry-run")
        check("extract --dry-run exits 0", r.returncode == 0, r.stdout + r.stderr)
        check("extract --dry-run reports would-extract", '"status": "would-extract"' in r.stdout
              or "would extract" in r.stdout, r.stdout)
        check("extract --dry-run writes NO .extract.md (run() never called)", not extract.exists(), r.stdout)

        # ---------- Layer 2: OCR via imagelib (fake seam) ----------
        r = run(ops, roots, "extract", "pic")
        check("extract exits 0", r.returncode == 0, r.stdout + r.stderr)
        etext = extract.read_text() if extract.exists() else ""
        check("extract note is type: extract, derived_from pic", "type: extract" in etext
              and 'derived_from: "[[pic]]"' in etext, etext)
        check("extract note has the fake OCR text", "[fake-ocr] pic.png" in etext, etext)
        check("extract note records the fake backend as tool", "tool: fake" in etext, etext)

        # re-extract without --reextract is an idempotent no-op (same bytes + same tool)
        r = run(ops, roots, "extract", "pic")
        check("re-extract is unchanged (idempotent)", '"status": "unchanged"' in r.stdout
              or "unchanged" in r.stdout, r.stdout)

        # ---------- Layer 3: --describe wires the real VLM cascade (fake seam) ----------
        r = run(ops, roots, "extract", "pic", "--reextract", "--describe")
        check("extract --describe exits 0", r.returncode == 0, r.stdout + r.stderr)
        dtext = extract.read_text() if extract.exists() else ""
        check("extract note has vlm_caption: fake caption", "vlm_caption: fake caption" in dtext, dtext)
        check("extract note has vlm_backend: fake", "vlm_backend: fake" in dtext, dtext)
        check("extract note has a ## Description section with the fake description",
              "## Description" in dtext and "fake description" in dtext, dtext)

    print(f"{BOLD}files: imagelib wiring (metadata + OCR + describe) — {len(results)} checks{RESET}\n")
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
