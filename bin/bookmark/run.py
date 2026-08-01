#!/usr/bin/env python3
"""
plainkeep bookmark <url> [--note "<text>"] [--no-fetch] [--archive] — save a link as a wiki note (issue #1
gap F). Local-first: the note is created from the URL immediately and always works offline. Title +
readable text are a BEST-EFFORT external GET (an external *read*, which the §5 wall allows — it never
transmits); --no-fetch skips the network, degrading to the URL as the title. --archive additionally
snapshots the fetched HTML into ~/files/bookmarks/ (via the same shadow-asset idea as `plainkeep files`),
so the bookmark survives link-rot. The note is a data-driven `type: bookmark` (see lib/notetype).

Testability: PLAINKEEP_BOOKMARK_FIXTURE=<file> feeds local HTML instead of the network, so the fetch/parse/
archive paths are exercised offline and deterministically.
"""
from __future__ import annotations
import os
import re
import sys
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lib import notetype, output, paths, vaultio  # noqa: E402
from enrich import run as enrichverb  # noqa: E402

GREEN, DIM, YEL, RESET = "\033[32m", "\033[2m", "\033[33m", "\033[0m"
_TITLE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_META = re.compile(r'<meta[^>]+(?:name|property)=["\'](?:og:)?description["\'][^>]+content=["\'](.*?)["\']',
                   re.IGNORECASE | re.DOTALL)
_TAGS = re.compile(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>")
_ANGLE = re.compile(r"(?s)<[^>]+>")
_WS = re.compile(r"[ \t\r\f\v]+")


def _unescape(s: str) -> str:
    for a, b in (("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"), ("&quot;", '"'),
                 ("&#39;", "'"), ("&apos;", "'"), ("&nbsp;", " ")):
        s = s.replace(a, b)
    return s.strip()


def _fetch(url: str):
    """Return the page HTML, or None on any failure/offline. Honors PLAINKEEP_BOOKMARK_FIXTURE for tests."""
    fixture = os.environ.get("PLAINKEEP_BOOKMARK_FIXTURE")
    if fixture:
        try:
            return Path(fixture).read_text(encoding="utf-8", errors="replace")
        except Exception:
            return None
    try:
        import urllib.request
        req = urllib.request.Request(url, headers={"User-Agent": "plainkeep-bookmark/1.0"})
        with urllib.request.urlopen(req, timeout=8) as r:  # noqa: S310 (http/https validated below)
            return r.read(600_000).decode("utf-8", "replace")
    except Exception:
        return None


def _readable(html: str, limit: int = 1200) -> str:
    """Zero-dependency fallback: crude tag-strip, capped (noisy — keeps nav/boilerplate)."""
    text = _ANGLE.sub(" ", _TAGS.sub(" ", html))
    text = _WS.sub(" ", _unescape(text))
    lines = [ln.strip() for ln in text.split("\n") if ln.strip()]
    joined = " ".join(lines)
    return (joined[:limit] + "…") if len(joined) > limit else joined


def _extract(html: str, url: str) -> str:
    """Main-content extraction → Markdown. Auto-upgrades to trafilatura (local, pip) if importable —
    real article extraction that drops nav/ads and keeps the full body; else the crude strip above.
    (Cloud readers like Jina are deliberately NOT a default — they'd send the URL to a third party.)"""
    try:
        import trafilatura  # optional local enhancer; `pip install trafilatura`
        md = trafilatura.extract(html, url=url, output_format="markdown",
                                 include_links=True, include_comments=False)
        if md and md.strip():
            return md.strip()
    except Exception:
        pass
    return _readable(html)


def _title_from_url(url: str) -> str:
    p = urlparse(url)
    tail = (p.path.rstrip("/").rsplit("/", 1)[-1] or p.netloc)
    return _unescape(tail.replace("-", " ").replace("_", " ")) or p.netloc or url


def main(argv: list[str]) -> int:
    _, argv = output.parse_argv(argv)
    dry = "--dry-run" in argv
    args = [a for a in argv if not a.startswith("-")]
    no_fetch = "--no-fetch" in argv
    archive = "--archive" in argv
    note_extra = ""
    if "--note" in argv:
        i = argv.index("--note")
        note_extra = argv[i + 1] if i + 1 < len(argv) else ""

    if not args:
        output.fail(output.EXIT_USAGE,
                    "usage: plainkeep bookmark <url> [--note \"<text>\"] [--no-fetch] [--archive]", verb="bookmark")
    url = args[0]
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        output.fail(output.EXIT_USAGE, f"not a http(s) url: {url}", verb="bookmark")

    title, desc, html = "", "", None
    if not no_fetch:
        html = _fetch(url)
        if html:
            m = _TITLE.search(html)
            if m:
                title = _unescape(m.group(1))
            md = _META.search(html)
            if md:
                desc = _unescape(md.group(1))
    if not title:
        title = _title_from_url(url)
        if not no_fetch and html is None:
            print(f"{YEL}note{RESET}  fetch failed/offline — titled from the URL (edit later)", file=sys.stderr)

    slug = paths.slugify(title) or paths.slugify(parsed.netloc)
    existing = {p.stem for p in paths.WIKI.rglob("*.md")} if paths.WIKI.exists() else set()
    base, i = slug, 2
    while slug in existing:
        slug, i = f"{base}-{i}", i + 1

    f = paths.WIKI / notetype.type_dir("bookmark") / f"{slug}.md"
    rel = f.relative_to(paths.PLAINKEEP_HOME)
    if dry:
        data = {"dry_run": True, "url": url, "title": title, "slug": slug, "would_write": str(rel)}
        return output.emit(data, "bookmark",
                           human=lambda _: f"would save -> {rel}  {DIM}({title}){RESET}  (dry run — nothing written)")

    body_parts = []
    if desc:
        body_parts.append(f"> {desc}")
    if note_extra:
        body_parts.append(note_extra)
    if html and not no_fetch:
        body_parts.append("## Extract\n" + _extract(html, url))
    body = "\n\n".join(body_parts)

    d = paths.WIKI / notetype.type_dir("bookmark")
    vaultio.mkdir(d)
    vaultio.write_text(f, notetype.render("bookmark", title=title, url=url, body=body, slug=slug), encoding="utf-8")

    if os.environ.get("PLAINKEEP_ENRICH", "").strip().lower() != "off":
        try:
            enrichverb.enrich_note(slug)  # best-effort — an enrich failure must never fail the save
        except Exception:
            pass

    archived = None
    if archive:
        if html:
            ad = paths.FILES_ROOT / "bookmarks"
            vaultio.mkdir(ad)
            archived = ad / f"{slug}.html"
            vaultio.write_text(archived, html, encoding="utf-8")
        else:
            print(f"{YEL}note{RESET}  --archive skipped (no fetched HTML)", file=sys.stderr)

    paths.append_journal(f"bookmark: {slug} <- {url}")
    data = {"url": url, "title": title, "slug": slug, "path": str(rel),
            "archived": str(archived) if archived else None}

    def render(_):
        print(f"{GREEN}saved{RESET} -> {rel}  {DIM}({title}){RESET}")
        if archived:
            print(f"{GREEN}archived{RESET} -> {archived}")

    return output.emit(data, "bookmark", human=render)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
