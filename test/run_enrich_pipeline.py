#!/usr/bin/env python3
"""run_enrich_pipeline.py — integration suite for `plainkeep enrich` wired into `files extract` and
`bookmark` (search-enrichment proposal §5). Drives the verbs as subprocesses under PLAINKEEP_ENRICH_FAKE=1
(no models, no network) against a temp PLAINKEEP_HOME + PLAINKEEP_ROOTS_HOME, reusing the 1x1 PNG fixture from
test/run_files_image.py."""
from __future__ import annotations
import base64
import json
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

# same fixture bytes as test/run_image_backend.py / run_files_image.py — a real, minimal 1x1 PNG
PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=")


def check(name, cond, detail=""):
    results.append((name, bool(cond), detail))


def run(verb, *args, home, roots=None, extra=None):
    env = {**os.environ, "PLAINKEEP_HOME": str(home), "PLAINKEEP_ENRICH_FAKE": "1", "PLAINKEEP_IMAGE_FAKE": "1"}
    if roots:
        env["PLAINKEEP_ROOTS_HOME"] = str(roots)
    if extra:
        env.update(extra)
    return subprocess.run([sys.executable, str(REPO / "bin" / verb / "run.py"), *args],
                          capture_output=True, text=True, env=env)


def main() -> int:
    with tempfile.TemporaryDirectory() as td:
        ops, roots = Path(td) / "ops", Path(td) / "home"
        (ops / "wiki").mkdir(parents=True); (ops / "journal").mkdir()
        inbox = ops / "inbox"; inbox.mkdir()
        (inbox / "pic.png").write_bytes(PNG_1X1)

        # ---------- ingest + extract auto-wires enrich onto the SHADOW note ----------
        run("files", "ingest", home=ops, roots=roots)
        r = run("files", "extract", "pic", home=ops, roots=roots)
        check("extract exits 0", r.returncode == 0, r.stdout + r.stderr)

        shadow = ops / "wiki" / "files" / "pic.md"
        extract = ops / "wiki" / "files" / "pic.extract.md"
        sfm = shadow.read_text()
        check("shadow note gets fake description", "description: [fake]" in sfm, sfm)
        check("shadow note gets a keywords BLOCK list, not inline",
              "keywords:\n- fake\n- enrich" in sfm and "keywords: [" not in sfm, sfm)
        check("shadow note gets enrich_key", "enrich_key:" in sfm, sfm)

        efm = extract.read_text()
        check(".extract.md note has NO enrich meta (meta lives on the shadow, not the extract)",
              "description:" not in efm and "enrich_key:" not in efm, efm)

        # ---------- `plainkeep enrich <slug>` re-run is a no-op (same key); --reenrich forces ----------
        key1 = next(ln.split(":", 1)[1].strip() for ln in sfm.splitlines() if ln.startswith("enrich_key:"))
        r = run("enrich", "pic", "--json", home=ops, roots=roots)
        check("re-run enrich pic exits 0", r.returncode == 0, r.stdout + r.stderr)
        head = json.loads(r.stdout.splitlines()[0]) if r.stdout.strip() else {}
        check("re-run enrich pic is unchanged (idempotent)",
              head.get("data", {}).get("status") == "unchanged", r.stdout)
        check("re-run leaves the enrich_key untouched",
              head.get("data", {}).get("enrich_key") == key1, r.stdout)

        r = run("enrich", "pic", "--reenrich", "--json", home=ops, roots=roots)
        check("--reenrich exits 0", r.returncode == 0, r.stdout + r.stderr)
        rhead = json.loads(r.stdout.splitlines()[0]) if r.stdout.strip() else {}
        check("--reenrich reports enriched (forced past the idempotency key)",
              rhead.get("data", {}).get("status") == "enriched", r.stdout)
        sfm2 = shadow.read_text()
        check("--reenrich rewrites the note (still fake meta)", "description: [fake]" in sfm2, sfm2)

        # unknown slug -> not-found
        r = run("enrich", "no-such-slug", home=ops, roots=roots)
        check("enrich of an unknown slug -> exit 4 (not-found)", r.returncode == 4, r.stdout + r.stderr)

        # ---------- `plainkeep bookmark --no-fetch` gets enriched meta on its own note ----------
        r = run("bookmark", "https://example.com/some-page", "--no-fetch",
                "--note", "hello world this is a note body for enrichment testing purposes here",
                home=ops, roots=roots)
        check("bookmark exits 0", r.returncode == 0, r.stdout + r.stderr)
        bmark = ops / "wiki" / "bookmarks" / "some-page.md"
        check("bookmark note written", bmark.exists(), r.stdout + r.stderr)
        bfm = bmark.read_text() if bmark.exists() else ""
        check("bookmark note gets fake description", "description: [fake]" in bfm, bfm)
        check("bookmark note gets a keywords BLOCK list",
              "keywords:\n- fake\n- enrich" in bfm and "keywords: [" not in bfm, bfm)
        check("bookmark note gets enrich_key", "enrich_key:" in bfm, bfm)

        # ---------- PLAINKEEP_ENRICH=off suppresses the auto-wiring hook ----------
        r = run("bookmark", "https://example.com/other-page", "--no-fetch",
                "--note", "another note body, long enough to matter for enrichment guards here",
                home=ops, roots=roots, extra={"PLAINKEEP_ENRICH": "off"})
        check("bookmark (PLAINKEEP_ENRICH=off) exits 0", r.returncode == 0, r.stdout + r.stderr)
        other = ops / "wiki" / "bookmarks" / "other-page.md"
        ofm = other.read_text() if other.exists() else ""
        check("PLAINKEEP_ENRICH=off: no enrich_key written by the auto-wiring hook",
              "enrich_key:" not in ofm, ofm)

        # ---------- `plainkeep enrich --all` sweeps every eligible note, including the un-enriched one ----------
        r = run("enrich", "--all", "--json", home=ops, roots=roots)
        check("enrich --all exits 0", r.returncode == 0, r.stdout + r.stderr)
        alines = r.stdout.splitlines()
        ahead = json.loads(alines[0]) if alines else {}
        arows = [json.loads(ln) for ln in alines[1:]] if len(alines) > 1 else []
        check("enrich --all is an ok NDJSON envelope", ahead.get("ok") is True, r.stdout)
        check("enrich --all picks up the previously-skipped note",
              any(row.get("slug") == "other-page" and row.get("status") == "enriched" for row in arows),
              str(arows))
        check("enrich --all leaves already-current notes unchanged",
              any(row.get("slug") == "pic" and row.get("status") == "unchanged" for row in arows),
              str(arows))
        ofm2 = other.read_text()
        check("--all wrote enrich meta onto the previously-off note", "enrich_key:" in ofm2, ofm2)

    print(f"\n{BOLD}Enrichment pipeline wiring (files extract + bookmark + plainkeep enrich) "
          f"— {len(results)} checks{RESET}\n")
    passed = sum(1 for _, ok, _ in results if ok)
    for name, ok, detail in results:
        mark = f"{GREEN}PASS{RESET}" if ok else f"{RED}FAIL{RESET}"
        print(f"  {mark} {name:<62}" + (f" {DIM}{detail.strip()[:90]}{RESET}" if (detail and not ok) else ""))
    failed = len(results) - passed
    print(f"\n{BOLD}Result:{RESET} {GREEN}{passed} passed{RESET}, "
          f"{(RED if failed else DIM)}{failed} failed{RESET}, {len(results)} checks")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
