#!/usr/bin/env python3
"""
plainkeep enrich <slug> [--reenrich] | --all [--reenrich] — generate {description, keywords} for a note's
derived text (search-enrichment proposal §5) and write it to the note's frontmatter.

Source text: wiki/files/<slug>.extract.md body if it exists (the durable working-memory buffer from
`plainkeep files extract`), else the note's own body (bookmarks/wiki notes). Meta lands on the PRIMARY
note — wiki/files/<slug>.md for a file, the note itself otherwise — NEVER on the .extract.md sibling
(--reextract rewrites it from a fixed template every run and would clobber meta written there).

Idempotent: skipped when the note's `enrich_key:` already matches `enrichlib.idem_key(text)`;
--reenrich forces a re-run. --all walks every note lacking a current key, sequentially, under one
warm PLAINKEEP_ENRICH_KEEP_ALIVE for the batch (a multi-GB model shouldn't reload per note).

`enrich_note()` is also the wiring point `files extract`/`bookmark` call after writing their own
note — best-effort, non-fatal, same idempotency/guards as this CLI path.
"""
from __future__ import annotations
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lib import enrichlib, output, paths, vaultio  # noqa: E402

GREEN, YEL, DIM, RESET = "\033[32m", "\033[33m", "\033[2m", "\033[0m"
BATCH_KEEP_ALIVE = "5m"  # a multi-GB model shouldn't reload per note on `--all` (proposal §5 QA R6)


def _target_note(slug: str) -> Path | None:
    """The PRIMARY note that owns enrich meta: the files shadow if this slug is a file, else
    wherever else in the wiki the slug's note lives (bookmark/wiki notes) — never the .extract.md."""
    shadow = paths.WIKI / "files" / f"{slug}.md"
    if shadow.exists():
        return shadow
    hits = [p for p in paths.WIKI.rglob(f"{slug}.md")] if paths.WIKI.exists() else []
    return hits[0] if hits else None


def _strip_frontmatter(text: str) -> str:
    if text.startswith("---"):
        e = text.find("\n---", 3)
        if e != -1:
            return text[e + 4:].strip()
    return text.strip()


def _source_text(slug: str, note: Path) -> str:
    """The .extract.md body (the derived working-memory buffer) if one exists, else the note's own
    body — the only two planes enrich ever reads from."""
    extract = paths.WIKI / "files" / f"{slug}.extract.md"
    if extract.exists():
        return _strip_frontmatter(extract.read_text(encoding="utf-8"))
    return _strip_frontmatter(note.read_text(encoding="utf-8"))


def _stamp(note: Path, meta: dict) -> None:
    """Splice/replace description:/keywords:/enrich_key: into the note's frontmatter (mirrors
    share/run.py:_stamp_frontmatter). keywords is ALWAYS a YAML block list — an inline `[a, b]` is
    exactly what `plainkeep doctor`'s frontmatter-churn check flags (wiki/conventions.md)."""
    lines = note.read_text(encoding="utf-8").splitlines()
    if not lines or lines[0].strip() != "---":
        return
    end = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
    if end is None:
        return
    body, skip = [], False
    for ln in lines[1:end]:
        if skip:
            if ln.lstrip().startswith("- "):
                continue
            skip = False
        if ln.startswith("description:") or ln.startswith("enrich_key:"):
            continue
        if ln.startswith("keywords:"):
            skip = True
            continue
        body.append(ln)
    body.append(f"description: {meta['description']}")
    body.append("keywords:")
    body += [f"- {kw}" for kw in meta["keywords"]]
    body.append(f"enrich_key: {meta['key']}")
    vaultio.write_text(note, "\n".join(["---", *body, "---", *lines[end + 1:]]) + "\n", encoding="utf-8")


def enrich_note(slug: str, reenrich: bool = False) -> dict:
    """Enrich one note by slug. Pure status dict, never raises — the shared entry point for both
    `plainkeep enrich <slug>` and the best-effort hooks in `files extract`/`bookmark`."""
    note = _target_note(slug)
    if note is None:
        return {"slug": slug, "status": "no-note", "message": f"no note for slug '{slug}'"}
    text = _source_text(slug, note)
    key = enrichlib.idem_key(text)
    if not reenrich and paths.fm_field(note, "enrich_key") == key:
        return {"slug": slug, "status": "unchanged",
                "note": str(note.relative_to(paths.PLAINKEEP_HOME)), "enrich_key": key}
    try:
        meta = enrichlib.enrich(text)
        _stamp(note, meta)
    except Exception as e:  # best-effort callers (files/bookmark) must never fail on this
        return {"slug": slug, "status": "enrich-failed", "message": str(e)}
    paths.append_journal(f"enrich {slug} <- {meta['backend']}")
    return {"slug": slug, "status": "enriched", "note": str(note.relative_to(paths.PLAINKEEP_HOME)),
            "backend": meta["backend"], "enrich_key": meta["key"]}


def _all_slugs() -> list[str]:
    """Every note's slug (files shadows, bookmarks, wiki notes) — never a .extract.md sibling,
    which is never itself an enrich target."""
    if not paths.WIKI.exists():
        return []
    return sorted({p.stem for p in paths.WIKI.rglob("*.md") if not p.name.endswith(".extract.md")})


def cmd_all(reenrich: bool) -> list[dict]:
    """Sequential batch under one warm PLAINKEEP_ENRICH_KEEP_ALIVE (proposal §5 QA R6) — left warm
    afterward rather than force-stopped."""
    prior = os.environ.get("PLAINKEEP_ENRICH_KEEP_ALIVE")
    os.environ.setdefault("PLAINKEEP_ENRICH_KEEP_ALIVE", BATCH_KEEP_ALIVE)
    try:
        return [enrich_note(slug, reenrich=reenrich) for slug in _all_slugs()]
    finally:
        if prior is None:
            os.environ.pop("PLAINKEEP_ENRICH_KEEP_ALIVE", None)
        else:
            os.environ["PLAINKEEP_ENRICH_KEEP_ALIVE"] = prior


def main(argv):
    _, argv = output.parse_argv(argv)
    reenrich = "--reenrich" in argv
    rest = [a for a in argv if a != "--reenrich"]

    if "--all" in rest:
        rows = cmd_all(reenrich)

        def render_all(rs):
            if not rs:
                return "no notes to enrich."
            n = sum(1 for r in rs if r["status"] == "enriched")
            out = [f"enriched {n}/{len(rs)} note(s):"]
            tags = {"enriched": GREEN + "enriched" + RESET, "unchanged": DIM + "unchanged" + RESET,
                    "enrich-failed": YEL + "failed" + RESET, "no-note": YEL + "no-note" + RESET}
            for r in rs:
                out.append(f"  {r['slug']:<30} {tags.get(r['status'], r['status'])}")
            return "\n".join(out)

        return output.emit_rows(rows, "enrich", human=render_all,
                                header={"enriched": sum(1 for r in rows if r["status"] == "enriched")})

    slugs = [a for a in rest if not a.startswith("-")]
    if not slugs:
        output.fail(output.EXIT_USAGE, "usage: plainkeep enrich <slug> [--reenrich] | --all [--reenrich]",
                    verb="enrich")
    res = enrich_note(slugs[0], reenrich=reenrich)
    if res["status"] == "no-note":
        output.fail(output.EXIT_NOT_FOUND, res["message"], verb="enrich")

    def render(r):
        if r["status"] == "enriched":
            return f"{GREEN}enriched{RESET} -> {r['note']}  {DIM}({r['backend']}){RESET}"
        if r["status"] == "unchanged":
            return f"{DIM}unchanged{RESET} {r['note']} — same text (idempotent no-op; --reenrich to force)"
        if r["status"] == "enrich-failed":
            return f"{YEL}enrich failed{RESET}: {r.get('message', '')}"
        return r.get("message", r["status"])

    return output.emit(res, "enrich", human=render)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
