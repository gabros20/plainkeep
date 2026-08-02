#!/usr/bin/env python3
"""run_extract.py — tiered extraction + provenance planes (proposal Parts 4.1 + 4.3), offline.

No optional dep is installed (they are genuinely absent here) so this asserts the ZERO-DEP path:
  - .txt/.md sources extract with stdlib into a SIBLING derived note wiki/files/<slug>.extract.md
    (frontmatter completeness: type, derived_from, source_sha256, tool, created/updated; sha matches),
  - same-bytes + same-tool re-run is an idempotent no-op; --reextract forces a rewrite,
  - a media tier whose dep is absent (PDF) degrades with an install hint and writes nothing,
  - `ingest --extract` chains extraction onto a freshly-filed source,
  - `plainkeep doctor` flags the three provenance-plane violations (Part 4.3),
  - `plainkeep search --author human|agent` filters the provenance planes.
"""
from __future__ import annotations
import hashlib
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
results: list[tuple[str, bool, str]] = []


def check(name, cond, detail=""):
    results.append((name, cond, bool(cond) and "" or str(detail)))


def _run(script, ops, *args, roots=None, extra=None):
    env = {**os.environ, "PLAINKEEP_HOME": str(ops), **(extra or {})}
    if roots is not None:
        env["PLAINKEEP_ROOTS_HOME"] = str(roots)
    return subprocess.run([sys.executable, str(REPO / "bin" / script / "run.py"), *args],
                          capture_output=True, text=True, env=env)


def files(ops, roots, *args, extra=None):
    return _run("files", ops, *args, roots=roots, extra=extra)


def _sha256(p: Path) -> str:
    h = hashlib.sha256()
    h.update(p.read_bytes())
    return h.hexdigest()


def _fm(text: str, key: str) -> str:
    for ln in text.splitlines():
        if ln.startswith(f"{key}:"):
            return ln.split(":", 1)[1].strip().strip('"').strip("'")
    return ""


def test_extract() -> None:
    with tempfile.TemporaryDirectory() as td:
        ops, roots = Path(td) / "ops", Path(td) / "home"
        (ops / "wiki").mkdir(parents=True)
        (ops / "journal").mkdir()
        (ops / "inbox").mkdir()
        src = Path(td) / "src"
        src.mkdir()
        transcript = src / "meeting-notes.txt"
        transcript.write_text("Line one.  \n\n\n\nLine two.\ttrailing\n", encoding="utf-8")

        # ingest an explicit text path so it gets a shadow note (bare inbox-scan ignores text)
        files(ops, roots, "ingest", str(transcript), "--research")
        shadow = ops / "wiki" / "files" / "meeting-notes.md"
        check("ingest of an explicit text path writes a shadow note", shadow.exists())
        source_file = roots / "files" / "research" / "meeting-notes.txt"
        check("source filed byte-for-byte into ~/files", source_file.exists())

        # --- text extraction end-to-end ---
        r = files(ops, roots, "extract", "meeting-notes")
        deriv = ops / "wiki" / "files" / "meeting-notes.extract.md"
        check("extract emits a sibling derived note", deriv.exists() and r.returncode == 0,
              r.stdout + r.stderr)
        dt = deriv.read_text() if deriv.exists() else ""
        check("derived note type is extract", _fm(dt, "type") == "extract", dt[:200])
        check("derived note derived_from points at the shadow", _fm(dt, "derived_from") == "[[meeting-notes]]", dt[:200])
        check("derived note records source_sha256 matching the source bytes",
              _fm(dt, "source_sha256") == _sha256(source_file), dt[:200])
        check("derived note records the tool + version", _fm(dt, "tool") == "stdlib-text 1.0", dt[:200])
        check("derived note has created + updated", bool(_fm(dt, "created")) and bool(_fm(dt, "updated")), dt[:200])
        check("derived note normalizes the body (collapsed blank lines)", "Line one." in dt
              and "Line two." in dt and "\n\n\n" not in dt.split("---", 2)[-1], dt)
        check("original source is untouched by extraction",
              source_file.read_text() == "Line one.  \n\n\n\nLine two.\ttrailing\n")

        # --- idempotence: same bytes + same tool = no-op ---
        before = deriv.read_bytes()
        r = files(ops, roots, "extract", "meeting-notes")
        check("re-extract is an idempotent no-op", "unchanged" in (r.stdout + r.stderr).lower()
              and deriv.read_bytes() == before, r.stdout + r.stderr)

        # --- --reextract forces a rewrite ---
        r = files(ops, roots, "extract", "meeting-notes", "--reextract")
        check("--reextract forces re-extraction", "extracted" in (r.stdout + r.stderr).lower(),
              r.stdout + r.stderr)

        # --- --json envelope shape ---
        r = files(ops, roots, "extract", "meeting-notes", "--json")
        try:
            env = json.loads(r.stdout)
            ok_env = env.get("ops_json") == 1 and env["ok"] and env["data"]["tool"] == "stdlib-text 1.0"
        except Exception:
            ok_env = False
        check("extract emits a valid --json envelope", ok_env, r.stdout)

        # --- missing-dep tier degrades with an install hint, writes nothing ---
        fake_pdf = src / "report.pdf"
        fake_pdf.write_bytes(b"%PDF-1.4 not a real pdf")
        files(ops, roots, "ingest", str(fake_pdf), "--research")
        r = files(ops, roots, "extract", "report")
        pdf_deriv = ops / "wiki" / "files" / "report.extract.md"
        check("absent PDF tier degrades gracefully (exit 0, no note written)",
              r.returncode == 0 and not pdf_deriv.exists(), r.stdout + r.stderr)
        check("absent PDF tier prints an install hint", "pymupdf4llm" in (r.stdout + r.stderr),
              r.stdout + r.stderr)

        # --- extract of an unknown slug is not-found (exit 4) ---
        r = files(ops, roots, "extract", "does-not-exist")
        check("extract of a missing shadow is not-found (exit 4)", r.returncode == 4, r.stdout + r.stderr)

    # --- ingest --extract chains extraction onto the freshly-filed source ---
    with tempfile.TemporaryDirectory() as td:
        ops, roots = Path(td) / "ops", Path(td) / "home"
        (ops / "wiki").mkdir(parents=True)
        (ops / "journal").mkdir()
        (ops / "inbox").mkdir()
        src = Path(td) / "src"
        src.mkdir()
        cap = src / "voice-memo.txt"
        cap.write_text("captured thought", encoding="utf-8")
        r = files(ops, roots, "ingest", str(cap), "--research", "--extract")
        deriv = ops / "wiki" / "files" / "voice-memo.extract.md"
        check("ingest --extract chains extraction", deriv.exists()
              and "extracted" in r.stdout, r.stdout + r.stderr)


def test_doctor_provenance() -> None:
    with tempfile.TemporaryDirectory() as td:
        ops = Path(td) / "ops"
        for d in ("wiki/notes", "wiki/files", "tasks/inbox", "tasks/active", "tasks/waiting",
                  "tasks/done", "journal", "inbox", "templates", "jobs", "skills", "bin"):
            (ops / d).mkdir(parents=True, exist_ok=True)
        w = ops / "wiki"
        # violation 1: tool/source_sha256 but no derived_from
        (w / "notes" / "orphan-derived.md").write_text(
            "---\ntype: note\ntitle: Orphan\ntool: pymupdf4llm 1.0\nsource_sha256: abc\n---\n# Orphan\n")
        # violation 2: author: agent with no status
        (w / "notes" / "agent-nostatus.md").write_text(
            "---\ntype: note\ntitle: Agent\nauthor: agent\n---\n# Agent\n")
        # violation 3: derived_from note outside wiki/files/
        (w / "notes" / "stray-derived.md").write_text(
            "---\ntype: extract\ntitle: Stray\nderived_from: \"[[foo]]\"\nsource_sha256: x\ntool: t 1\n---\n# Stray\n")
        # well-formed: a real derived note in wiki/files/ (must NOT be flagged)
        (w / "files" / "good.extract.md").write_text(
            "---\ntype: extract\ntitle: Good (extract)\nstatus: derived\nderived_from: \"[[good]]\"\n"
            "source_sha256: deadbeef\ntool: stdlib-text 1.0\ncreated: 2026-07-01\nupdated: 2026-07-01\ntags: []\n---\n# Good\n")
        # well-formed agent note (has status) + a plain human note (must NOT be flagged)
        (w / "notes" / "good-agent.md").write_text(
            "---\ntype: note\ntitle: Good Agent\nauthor: agent\nstatus: draft\n---\n# Good Agent\n")
        (w / "notes" / "human.md").write_text("---\ntype: note\ntitle: Human\nstatus: active\n---\n# Human\n")

        r = _run("doctor", ops)
        out = r.stdout + r.stderr
        prov = [ln for ln in out.splitlines() if "provenance:" in ln]
        joined = "\n".join(prov)
        check("doctor flags tool/source_sha256 without derived_from",
              "orphan-derived.md" in joined and "no derived_from" in joined, joined)
        check("doctor flags author: agent without a status", "agent-nostatus.md" in joined, joined)
        check("doctor flags a derived note outside wiki/files/", "stray-derived.md" in joined, joined)
        check("doctor does NOT flag a well-formed derived note", "good.extract.md" not in joined, joined)
        check("doctor does NOT flag a well-formed agent note", "good-agent.md" not in joined, joined)
        check("doctor does NOT flag a plain human note", "wiki/notes/human.md" not in joined, joined)
        check("well-formed derived note wikilink in frontmatter is not lint-flagged",
              "fm-link: wiki/files/good.extract.md" not in out, out)


def test_search_author() -> None:
    with tempfile.TemporaryDirectory() as td:
        ops = Path(td) / "ops"
        (ops / "wiki" / "notes").mkdir(parents=True)
        (ops / "wiki" / "files").mkdir(parents=True)
        (ops / "journal").mkdir()
        (ops / "wiki" / "notes" / "human-k8s.md").write_text(
            "---\ntype: note\ntitle: Human K8s\nstatus: active\n---\n# Human K8s\n\nkubernetes operator notes.\n")
        (ops / "wiki" / "notes" / "agent-k8s.md").write_text(
            "---\ntype: note\ntitle: Agent K8s\nauthor: agent\nstatus: draft\n---\n# Agent K8s\n\nkubernetes agent draft.\n")
        (ops / "wiki" / "files" / "derived-k8s.extract.md").write_text(
            "---\ntype: transcript\ntitle: Derived K8s (extract)\nstatus: derived\nderived_from: \"[[derived-k8s]]\"\n"
            "source_sha256: abc\ntool: stdlib-text 1.0\ncreated: 2026-07-01\nupdated: 2026-07-01\ntags: []\n---\n"
            "# Derived K8s\n\nkubernetes transcript body.\n")

        _run("index", ops)

        def paths_for(*args):
            r = _run("search", ops, "kubernetes", *args, "--json")
            hits = []
            for ln in r.stdout.splitlines():
                try:
                    obj = json.loads(ln)
                except Exception:
                    continue
                if "path" in obj:
                    hits.append(obj["path"])
            return hits, r

        allhits, r = paths_for()
        check("unfiltered search returns all three planes", len(allhits) == 3, r.stdout)
        human, r = paths_for("--author", "human")
        check("--author human keeps only the human note",
              any("human-k8s" in p for p in human)
              and not any("agent-k8s" in p for p in human)
              and not any("derived-k8s" in p for p in human), human)
        agent, r = paths_for("--author", "agent")
        check("--author agent keeps only the agent note",
              agent == [p for p in agent if "agent-k8s" in p] and any("agent-k8s" in p for p in agent),
              agent)
        r = _run("search", ops, "kubernetes", "--author", "bogus")
        check("--author with a bad value is a usage error (exit 2)", r.returncode == 2, r.stdout + r.stderr)


def main() -> int:
    test_extract()
    test_doctor_provenance()
    test_search_author()
    print(f"{BOLD}tiered extraction + provenance planes (4.1/4.3) — {len(results)} checks{RESET}\n")
    passed = sum(1 for _, ok, _ in results if ok)
    for name, ok, detail in results:
        mark = f"{GREEN}PASS{RESET}" if ok else f"{RED}FAIL{RESET}"
        print(f"  {mark} {name:<52}" + (f" {DIM}{detail.strip()[:80]}{RESET}" if (detail and not ok) else ""))
    failed = len(results) - passed
    print(f"\n{BOLD}Result:{RESET} {GREEN}{passed} passed{RESET}, "
          f"{(RED if failed else DIM)}{failed} failed{RESET}, {len(results)} checks")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
