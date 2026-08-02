#!/usr/bin/env python3
"""run_enrich.py — offline suite for the search-enrichment engine (bin/lib/enrichlib.py). NEVER
contacts Ollama: PLAINKEEP_ENRICH_FAKE short-circuits enrich(), and every guard/off path is asserted to
never reach `_call_model` (monkeypatched to raise if invoked)."""
from __future__ import annotations
import importlib.util
import os
import sys
from pathlib import Path
from lib.hermetic import seal
seal()   # hermetic: an empty throwaway registry, never the developer's real vault

REPO = Path(__file__).resolve().parents[1]
GREEN, RED, DIM, BOLD, RESET = "\033[32m", "\033[31m", "\033[2m", "\033[1m", "\033[0m"
results = []


def check(name, cond, detail=""):
    results.append((name, bool(cond), detail))


def _load_enrichlib():
    """Load bin/lib/enrichlib.py by file path (bin/lib namespace loses to test/lib on sys.path)."""
    spec = importlib.util.spec_from_file_location("plainkeep_enrichlib", REPO / "bin" / "lib" / "enrichlib.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["plainkeep_enrichlib"] = mod
    spec.loader.exec_module(mod)
    return mod


def _boom(*a, **kw):
    raise AssertionError("_call_model was invoked on a guarded/fake path — a real model call would fire")


def main() -> int:
    el = _load_enrichlib()
    for k in ("PLAINKEEP_ENRICH_FAKE", "PLAINKEEP_ENRICH", "PLAINKEEP_ENRICH_MODEL", "PLAINKEEP_ENRICH_KEEP_ALIVE"):
        os.environ.pop(k, None)

    # every check below runs with _call_model rigged to raise — a passing suite with no exception
    # proves zero network/model calls happened anywhere except the (absent) real-model test.
    orig_call = el._call_model
    el._call_model = _boom
    try:
        # ---------- PLAINKEEP_ENRICH_FAKE: deterministic no-model seam ----------
        os.environ["PLAINKEEP_ENRICH_FAKE"] = "1"
        r = el.enrich("some text")
        check("fake: backend", r["backend"] == "fake", str(r))
        check("fake: canned description", r["description"] == "[fake] some text", str(r))
        check("fake: canned keywords", r["keywords"] == ["fake", "enrich"], str(r))
        check("fake: non-empty key", bool(r.get("key")), str(r))
        os.environ.pop("PLAINKEEP_ENRICH_FAKE")

        # ---------- guards: empty / sentinel / too-short text never reaches the model ----------
        r = el.enrich("")
        check("guard: empty text → backend none", r["backend"] == "none", str(r))
        check("guard: empty text → description empty", r["description"] == "", str(r))
        check("guard: empty text → keywords list", r["keywords"] == [], str(r))

        r = el.enrich(el.SENTINEL)
        check("guard: sentinel → model skipped", r["backend"] in ("none", "floor"), str(r))

        r = el.enrich("short text")  # 10 chars, well under MIN_CHARS
        check("guard: 10-char text → model skipped", r["backend"] in ("none", "floor"), str(r))

        # ---------- PLAINKEEP_ENRICH=off: no model regardless of text length ----------
        os.environ["PLAINKEEP_ENRICH"] = "off"
        long_text = "The quick brown fox jumps over the lazy dog. " * 10
        r = el.enrich(long_text)
        check("off: backend none/floor", r["backend"] in ("none", "floor"), str(r))
        check("off: description empty", r["description"] == "", str(r))
        os.environ.pop("PLAINKEEP_ENRICH")

        # ---------- keyword_floor: stdlib frequency+stopword extractor ----------
        kws = el.keyword_floor("the quick brown fox jumps over the lazy dog near the quick river")
        check("keyword_floor: returns a list", isinstance(kws, list), str(kws))
        check("keyword_floor: lowercased", all(k == k.lower() for k in kws), str(kws))
        check("keyword_floor: drops stopwords", "the" not in kws and "over" not in kws, str(kws))
        check("keyword_floor: keeps content words", "quick" in kws and "fox" in kws, str(kws))
        check("keyword_floor: deterministic", el.keyword_floor("alpha beta alpha gamma beta alpha")
              == el.keyword_floor("alpha beta alpha gamma beta alpha"))
        check("keyword_floor: most frequent word ranks first",
              el.keyword_floor("alpha beta alpha gamma beta alpha")[0] == "alpha")
        check("keyword_floor: empty text → empty list", el.keyword_floor("") == [])

        # ---------- idem_key: stable for same input, differs on change ----------
        k1 = el.idem_key("hello world", "gemma4:e4b")
        k2 = el.idem_key("hello world", "gemma4:e4b")
        k3 = el.idem_key("hello there", "gemma4:e4b")
        k4 = el.idem_key("hello world", "other-model")
        check("idem_key: stable for identical (text, model)", k1 == k2, f"{k1} {k2}")
        check("idem_key: differs when text changes", k1 != k3, f"{k1} {k3}")
        check("idem_key: differs when model changes", k1 != k4, f"{k1} {k4}")
        check("idem_key: non-empty string", isinstance(k1, str) and len(k1) > 0)
    finally:
        el._call_model = orig_call

    # ---------- available(): bool, never raises even with no daemon present ----------
    try:
        avail = el.available()
        ok = isinstance(avail, bool)
    except Exception as e:
        ok, avail = False, e
    check("available: returns a bool without raising", ok, str(avail))

    print(f"\n{BOLD}Search enrichment engine (bin/lib/enrichlib.py) — {len(results)} checks{RESET}\n")
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
