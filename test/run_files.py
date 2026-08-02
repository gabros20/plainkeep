#!/usr/bin/env python3
"""run_files.py — exercises `plainkeep files ingest` (work material filed + shadow note; personal/legal
proposed-not-moved) and `plainkeep files open`, against temp ~/plainkeep + ~/files."""
from __future__ import annotations
import hashlib
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


def check(name, cond, detail=""):
    results.append((name, cond, detail))


def hashes(root: Path) -> dict[str, str]:
    """Every file under `root` -> sha256. The append-only claim is about BYTES, so it is asserted by
    walking and hashing, not by an exit code or a mtime."""
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob("*")) if p.is_file()}


def run(ops, roots, *args, extra=None):
    env = {**os.environ, "PLAINKEEP_HOME": str(ops), "PLAINKEEP_ROOTS_HOME": str(roots), **(extra or {})}
    # Ingest goes through the write seam since Task 1c, and the wall's ~/files anchor and
    # `paths.FILES_ROOT` both read PLAINKEEP_TEST_HOME ahead of PLAINKEEP_ROOTS_HOME. An inherited
    # one would relocate this fixture out from under the assertions below (run_pathwall.py pops it
    # for the same reason).
    env.pop("PLAINKEEP_TEST_HOME", None)
    return subprocess.run([sys.executable, str(REPO / "bin" / "files" / "run.py"), *args],
                          capture_output=True, text=True, env=env)


def main() -> int:
    with tempfile.TemporaryDirectory() as td:
        ops, roots = Path(td) / "ops", Path(td) / "home"
        (ops / "wiki").mkdir(parents=True); (ops / "journal").mkdir()
        inbox = ops / "inbox"; inbox.mkdir()
        (inbox / "acme-brief.pdf").write_bytes(b"%PDF work material")
        (inbox / "tax-return-2025.pdf").write_bytes(b"%PDF personal")
        (inbox / "cap-note.md").write_text("a text capture — not a binary")

        r = run(ops, roots, "ingest")
        filed = roots / "files" / "inbox" / "acme-brief.pdf"
        shadow = ops / "wiki" / "files" / "acme-brief.md"
        check("ingest files work material into ~/files", filed.exists(), r.stdout + r.stderr)
        check("ingest writes a shadow note", shadow.exists() and "path:" in shadow.read_text())
        check("ingest leaves personal/legal in place (iCloud proposed)",
              (inbox / "tax-return-2025.pdf").exists() and "PROPOSE iCloud" in r.stdout, r.stdout)
        check("ingest ignores text captures", (inbox / "cap-note.md").exists()
              and not (ops / "wiki" / "files" / "cap-note.md").exists())
        check("shadow note points at the moved file", str(filed) in shadow.read_text())

        r = run(ops, roots, "open", "acme-brief", extra={"PLAINKEEP_NO_OPEN": "1"})
        check("files open resolves the path from the shadow note", str(filed) in r.stdout, r.stdout)

    # ---- routing into a hub + bidirectional linking + de-dup + management (issue #1 gap A) ----
    with tempfile.TemporaryDirectory() as td:
        ops, roots = Path(td) / "ops", Path(td) / "home"
        (ops / "wiki" / "clients").mkdir(parents=True); (ops / "journal").mkdir()
        hub = ops / "wiki" / "clients" / "acme.md"
        hub.write_text("---\ntype: client\ntitle: Acme\nstatus: active\n---\n# Acme\n\n## Timeline\n- 2026-01-01 created\n")
        src = Path(td) / "src"; src.mkdir()
        (src / "brief.pdf").write_bytes(b"BRIEF-BYTES-v1")
        (src / "brief2.pdf").write_bytes(b"BRIEF-BYTES-v1")   # identical bytes, different name
        (src / "spec.pdf").write_bytes(b"SPEC-BYTES")

        r = run(ops, roots, "ingest", str(src / "brief.pdf"), "--client", "acme")
        into = roots / "files" / "clients" / "acme" / "in" / "brief.pdf"
        shadow = ops / "wiki" / "files" / "brief.md"
        check("ingest --client routes into ~/files/<hub>/in/", into.exists(), r.stdout + r.stderr)
        check("shadow note records hub + sha256", shadow.exists()
              and "hub: acme" in shadow.read_text() and "sha256:" in shadow.read_text(), r.stdout)
        check("hub note gets a ## Files backlink", "## Files" in hub.read_text() and "[[brief]]" in hub.read_text(), hub.read_text())

        r = run(ops, roots, "ingest", str(src / "brief2.pdf"), "--client", "acme")
        check("identical bytes are de-duped (no second copy)", "duplicate" in r.stdout
              and not (roots / "files" / "clients" / "acme" / "in" / "brief2.pdf").exists(), r.stdout)
        check("de-dup keeps a single shadow note", len(list((ops / "wiki" / "files").glob("*.md"))) == 1)

        # ---- APPEND-ONLY (Task 1c): a same-NAME, different-BYTES arrival must not touch the first --
        # The first ingest above is the create half; this is the collision half. `in/` is walled by
        # `_in_originals`, so both go through `vaultio.move_create_only` — the assertion is that the
        # already-filed original is byte-identical afterwards, hashed before and after.
        originals = roots / "files" / "clients" / "acme" / "in"
        (src / "brief.pdf").write_bytes(b"BRIEF-BYTES-v2-SAME-NAME")   # the first one was moved away
        before = hashes(originals)
        r = run(ops, roots, "ingest", str(src / "brief.pdf"), "--client", "acme")
        after = hashes(originals)
        check("collision: a same-name arrival lands beside the original as brief-2.pdf",
              (originals / "brief-2.pdf").exists()
              and (originals / "brief-2.pdf").read_bytes() == b"BRIEF-BYTES-v2-SAME-NAME",
              r.stdout + r.stderr)
        check("collision: the filed original is byte-identical (hashed before and after)",
              before.get("brief.pdf") == after.get("brief.pdf")
              and after["brief.pdf"] == hashlib.sha256(b"BRIEF-BYTES-v1").hexdigest(),
              f"before={before} after={after}")
        check("collision: nothing else under in/ changed either",
              {k: v for k, v in after.items() if k != "brief-2.pdf"} == before, f"{before} -> {after}")

        r = run(ops, roots, "ingest", str(src / "spec.pdf"), "--client", "ghost")
        check("ingest to a missing hub errors (nothing filed)", r.returncode == 1 and "no wiki hub" in r.stderr, r.stderr)

        r = run(ops, roots, "ingest", str(src / "spec.pdf"), "--research")
        check("ingest --research routes into ~/files/research/", (roots / "files" / "research" / "spec.pdf").exists(), r.stdout + r.stderr)

        r = run(ops, roots, "list")
        check("files list shows the catalogue with hub tags", "brief" in r.stdout and "[[acme]]" in r.stdout, r.stdout)
        r = run(ops, roots, "list", "--hub", "acme")
        check("files list --hub filters to one hub", "brief" in r.stdout and "spec" not in r.stdout, r.stdout)

        # link an already-ingested (research) asset to the hub after the fact
        r = run(ops, roots, "link", "spec", "acme")
        check("files link attaches an asset to a hub", r.returncode == 0
              and "[[spec]]" in hub.read_text() and "[[acme]]" in (ops / "wiki" / "files" / "spec.md").read_text(), r.stdout + r.stderr)
        r = run(ops, roots, "link", "spec", "acme")
        check("files link is idempotent", r.returncode == 0 and "already linked" in r.stdout, r.stdout)

        # ---- images: ingested as binaries (wiki stays plaintext) but EMBEDDED for preview (gap C) ----
        (src / "logo.png").write_bytes(b"\x89PNG\r\n fake image bytes")
        r = run(ops, roots, "ingest", str(src / "logo.png"), "--client", "acme")
        shadow_img = ops / "wiki" / "files" / "logo.md"
        check("image ingested to ~/files (not wiki)", (roots / "files" / "clients" / "acme" / "in" / "logo.png").exists()
              and not any((ops / "wiki").rglob("*.png")), r.stdout + r.stderr)
        check("image shadow note embeds ![](path) + kind: image", shadow_img.exists()
              and "kind: image" in shadow_img.read_text() and "![logo.png](" in shadow_img.read_text(), shadow_img.read_text() if shadow_img.exists() else "")

    print(f"{BOLD}files verb (binary-assets plane) — {len(results)} checks{RESET}\n")
    passed = sum(1 for _, ok, _ in results if ok)
    for name, ok, detail in results:
        mark = f"{GREEN}PASS{RESET}" if ok else f"{RED}FAIL{RESET}"
        print(f"  {mark} {name:<46}" + (f" {DIM}{detail.strip()[:80]}{RESET}" if (detail and not ok) else ""))
    failed = len(results) - passed
    print(f"\n{BOLD}Result:{RESET} {GREEN}{passed} passed{RESET}, "
          f"{(RED if failed else DIM)}{failed} failed{RESET}, {len(results)} checks")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
