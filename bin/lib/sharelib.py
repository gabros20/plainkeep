"""
sharelib.py — pure, dependency-light helpers for `plainkeep share` (proposal Part 5.2). Kept free of any
`from lib import …` so a test can load it by file path (the bin/lib-namespace-vs-test/lib gotcha),
and network-free except for the one lazily-imported fetch helper, so bundling / HTML rendering are
unit-testable offline.

The capability-URL model: the note renders to ONE self-contained HTML blob plus its raw markdown,
those are packed into a plaintext OPSX bundle, and the bundle is stored AS-IS on the worker under a
long unguessable token. Secrecy = the unguessable link + a TTL; there is no encryption. A SINGLE link
serves both audiences: `https://<worker>/<token>` returns HTML to browsers, and `<worker>/<token>.md`
returns the raw markdown to agents. Anyone with the link can read it, so treat the link as the secret.
"""
from __future__ import annotations
import base64
import html
import re
import struct

try:
    from . import wikilink  # type: ignore
except ImportError:      # loaded by file path (tests) — sibling is not a package then
    import sys as _s, os as _o
    _s.path.insert(0, _o.path.dirname(_o.path.abspath(__file__)))
    import wikilink  # type: ignore

_LINK = wikilink.LINK_RE
_IMG = re.compile(r"!\[([^\]]*)\]\(([^)]+)\)")
_BOLD = re.compile(r"\*\*([^*]+)\*\*")
_ICODE = re.compile(r"`([^`]+)`")
_SEP_CELL = re.compile(r"^:?-{1,}:?$")


def _split_table_row(line: str) -> list[str]:
    """GFM pipe row → cell strings (outer pipes optional)."""
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|"):
        s = s[:-1]
    return [c.strip() for c in re.split(r"(?<!\\)\|", s)]   # an escaped `\|` stays inside its cell


def _is_table_separator(line: str) -> bool:
    cells = _split_table_row(line)
    return bool(cells) and all(_SEP_CELL.match(c.replace(" ", "")) for c in cells)


def _is_table_row(line: str) -> bool:
    if "|" not in line:
        return False
    return len(_split_table_row(line)) >= 2


def _render_table(header: list[str], body: list[list[str]], inl) -> str:
    def row(cells: list[str], tag: str) -> str:
        return "<tr>" + "".join(f"<{tag}>{inl(c)}</{tag}>" for c in cells) + "</tr>"
    parts = ['<div class="table-wrap"><table>']
    parts.append("<thead>" + row(header, "th") + "</thead>")
    if body:
        parts.append("<tbody>" + "".join(row(r, "td") for r in body) + "</tbody>")
    parts.append("</table></div>")
    return "".join(parts)

_MDLINK = re.compile(r"(?<!\!)\[([^\]]+)\]\(([^)]+)\)")

# Self-contained reading stylesheet (Flexoki palette, kepano-minimal). Pure CSS — no JS, no external
# refs (the note renders inside a scriptless sandboxed iframe, and the bundle must stay portable).
# Light/dark auto via prefers-color-scheme; code blocks scroll instead of blowing out the layout.
_CSS = (
    ":root{color-scheme:light dark;"
    "--bg:#FFFCF0;--tx:#100F0F;--tx2:#6F6E69;--line:#DAD8CE;--code:#F2F0E5;--accent:#24837B;--sel:#DAD8CE}"
    "@media (prefers-color-scheme:dark){:root{"
    "--bg:#100F0F;--tx:#CECDC3;--tx2:#878580;--line:#282726;--code:#1C1B1A;--accent:#3AA99F;--sel:#282726}}"
    "*{box-sizing:border-box}html{-webkit-text-size-adjust:100%}"
    "body{margin:0 auto;max-width:44rem;"
    "padding:calc(3.5rem + env(safe-area-inset-top)) max(1.25rem,env(safe-area-inset-right)) "
    "calc(6rem + env(safe-area-inset-bottom)) max(1.25rem,env(safe-area-inset-left));"
    "background:var(--bg);color:var(--tx);overflow-wrap:break-word;-webkit-font-smoothing:antialiased;"
    "font:1.0625rem/1.7 -apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif}"
    "::selection{background:var(--sel)}"
    "h1,h2,h3,h4,h5,h6{line-height:1.25;font-weight:650;letter-spacing:-.01em;margin:2.4rem 0 .8rem}"
    "h1{font-size:1.9rem;font-weight:700;margin-top:0}h2{font-size:1.4rem}h3{font-size:1.17rem}"
    "h4,h5,h6{font-size:1.02rem}"
    "p{margin:0 0 1.05rem}strong{font-weight:650}em{font-style:italic}"
    "a{color:var(--tx);text-decoration:underline;text-underline-offset:2px;"
    "text-decoration-color:var(--line);text-decoration-thickness:1px}"
    "a:hover{color:var(--accent);text-decoration-color:var(--accent)}"
    "ul,ol{margin:0 0 1.05rem;padding-left:1.5rem}li{margin:.3rem 0}li::marker{color:var(--tx2)}"
    "blockquote{margin:1.5rem 0;padding:.15rem 0 .15rem 1rem;border-left:3px solid var(--line);color:var(--tx2)}"
    "blockquote p{margin:.45rem 0}"
    "hr{border:0;border-top:1px solid var(--line);margin:2.8rem 0}"
    "img{max-width:100%;height:auto;display:block;margin:1.6rem 0;border-radius:8px}"
    "code{font-family:ui-monospace,SFMono-Regular,'SF Mono',Menlo,Consolas,monospace;"
    "font-size:.88em;background:var(--code);padding:.15em .38em;border-radius:5px}"
    "pre{margin:1.5rem 0;padding:1rem 1.1rem;background:var(--code);border:1px solid var(--line);"
    "border-radius:10px;overflow-x:auto;-webkit-overflow-scrolling:touch}"
    "pre code{background:none;padding:0;border-radius:0;font-size:.86rem;line-height:1.55;"
    "display:block;white-space:pre}"
    ".table-wrap{margin:1.25rem 0;overflow-x:auto;-webkit-overflow-scrolling:touch;max-width:100%}"
    "table{width:max-content;min-width:100%;border-collapse:collapse;font-size:.94rem;line-height:1.45}"
    "th,td{border:1px solid var(--line);padding:.45rem .65rem;text-align:left;vertical-align:top}"
    "th{font-weight:650;background:var(--code);white-space:nowrap}"
    "td code{white-space:nowrap}"
    ".plainkeep-note{margin-bottom:2rem}"
)


# --------------------------------------------------------------------------- expiry

def parse_expires(spec: str) -> int:
    """'7d' / '12h' / '30m' / '3600' → seconds. Raises ValueError on garbage. Feeds KV expirationTtl."""
    spec = (spec or "").strip().lower()
    m = re.fullmatch(r"(\d+)\s*([smhdw]?)", spec)
    if not m:
        raise ValueError(f"bad --expires {spec!r} (use e.g. 7d, 12h, 30m)")
    n = int(m.group(1))
    return n * {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}[m.group(2)]


# --------------------------------------------------------------------------- markdown → self-contained HTML

def _md_inline(text: str, resolvable: set[str]) -> str:
    """Inline markdown → HTML on already-escaped text. Wikilinks resolve WITHIN the shared set to an
    intra-doc anchor; targets outside the set degrade to plain text (proposal Part 5.2)."""
    def link(m):
        tgt, label = wikilink.split_link(m.group(1))
        if tgt in resolvable:
            return f'<a href="#note-{html.escape(tgt)}">{html.escape(label)}</a>'
        return html.escape(label)
    text = _LINK.sub(link, text)
    text = _BOLD.sub(lambda m: f"<strong>{m.group(1)}</strong>", text)
    text = _ICODE.sub(lambda m: f"<code>{m.group(1)}</code>", text)
    text = _MDLINK.sub(lambda m: f'<a href="{html.escape(m.group(2))}">{m.group(1)}</a>', text)
    return text


def _strip_frontmatter(md: str) -> str:
    lines = md.splitlines()
    if lines and lines[0].strip() == "---":
        for i in range(1, len(lines)):
            if lines[i].strip() == "---":
                return "\n".join(lines[i + 1:])
    return md


def render_note_html(md: str, resolvable: set[str], image_resolver=None) -> str:
    """Render ONE note body (frontmatter stripped) to an HTML fragment. `image_resolver(path)` returns
    a data: URI (or None to drop the image — over the size cap or outside the files root)."""
    out, lines = [], _strip_frontmatter(md).splitlines()
    in_code = False
    list_type = None  # 'ul' | 'ol' | None — the currently open list
    in_quote = False

    def close_list():
        nonlocal list_type
        if list_type:
            out.append(f"</{list_type}>")
            list_type = None

    def close_quote():
        nonlocal in_quote
        if in_quote:
            out.append("</blockquote>")
            in_quote = False

    def inl(s):
        return _md_inline(html.escape(s), resolvable)

    i = 0
    while i < len(lines):
        raw = lines[i]
        if raw.strip().startswith("```"):
            close_list(); close_quote()
            out.append("</code></pre>" if in_code else "<pre><code>")
            in_code = not in_code
            i += 1
            continue
        if in_code:
            out.append(html.escape(raw))
            i += 1
            continue

        stripped = raw.strip()
        if stripped == "":
            close_list(); close_quote()
            out.append("")
            i += 1
            continue

        # images (whole-line) first — before escaping — so data URIs inline
        img = _IMG.match(stripped)
        if img:
            close_list(); close_quote()
            alt, path = img.group(1), img.group(2).strip()
            uri = image_resolver(path) if image_resolver else None
            out.append(f'<img alt="{html.escape(alt)}" src="{uri}">' if uri
                       else f"<p><em>{html.escape(alt or path)}</em></p>")
            i += 1
            continue

        # GFM pipe tables: header row + |---| separator + body rows
        if (_is_table_row(stripped) and i + 1 < len(lines)
                and _is_table_separator(lines[i + 1].strip())):
            close_list(); close_quote()
            header = _split_table_row(stripped)
            i += 2
            body: list[list[str]] = []
            while i < len(lines):
                row_st = lines[i].strip()
                if row_st == "" or not _is_table_row(row_st):
                    break
                body.append(_split_table_row(row_st))
                i += 1
            out.append(_render_table(header, body, inl))
            continue

        hm = re.match(r"^(#{1,6})\s+(.*)$", raw)
        if hm:
            close_list(); close_quote()
            lvl = len(hm.group(1))
            out.append(f"<h{lvl}>{inl(hm.group(2))}</h{lvl}>")
            i += 1
            continue

        um = re.match(r"^\s*[-*+]\s+(.*)$", raw)
        if um:
            close_quote()
            if list_type != "ul":
                close_list(); out.append("<ul>"); list_type = "ul"
            out.append(f"<li>{inl(um.group(1))}</li>")
            i += 1
            continue

        om = re.match(r"^\s*\d+[.)]\s+(.*)$", raw)
        if om:
            close_quote()
            if list_type != "ol":
                close_list(); out.append("<ol>"); list_type = "ol"
            out.append(f"<li>{inl(om.group(1))}</li>")
            i += 1
            continue

        qm = re.match(r"^\s*>\s?(.*)$", raw)
        if qm:
            close_list()
            if not in_quote:
                out.append("<blockquote>"); in_quote = True
            out.append(f"<p>{inl(qm.group(1))}</p>")
            i += 1
            continue

        close_list(); close_quote()
        out.append(f"<p>{inl(raw)}</p>")
        i += 1

    close_list(); close_quote()
    if in_code:  # defensive: unterminated fence
        out.append("</code></pre>")
    return "\n".join(out)


def render_bundle(notes: list[dict], image_resolver=None) -> str:
    """`notes` = [{slug, title, md}] → ONE self-contained HTML document (inline CSS, intra-set
    wikilinks). A single note and a collection use the same path (collections coalesce to one blob)."""
    resolvable = {n["slug"] for n in notes}
    title = notes[0]["title"] if len(notes) == 1 else f"{len(notes)} notes"
    parts = [
        "<!doctype html>", '<html lang="en"><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">',
        '<meta name="color-scheme" content="light dark">',
        '<meta name="theme-color" media="(prefers-color-scheme:light)" content="#FFFCF0">',
        '<meta name="theme-color" media="(prefers-color-scheme:dark)" content="#100F0F">',
        f"<title>{html.escape(title)}</title><style>{_CSS}</style></head><body>",
    ]
    for i, n in enumerate(notes):
        if i:
            parts.append("<hr>")
        parts.append(f'<section class="plainkeep-note" id="note-{html.escape(n["slug"])}">')
        parts.append(render_note_html(n["md"], resolvable, image_resolver))
        parts.append("</section>")
    parts.append("</body></html>")
    return "\n".join(parts)


def data_uri(data: bytes, mime: str) -> str:
    return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"


# --------------------------------------------------------------------------- multipart bundle (HTML + agent markdown)

BUNDLE_MAGIC = b"OPSX"
BUNDLE_VERSION = 1


def render_markdown_bundle(notes: list[dict]) -> str:
    """Wiki source bytes for agent / .md — single note is file-identical; collections join with ---."""
    if len(notes) == 1:
        return notes[0]["md"]
    parts: list[str] = []
    for i, n in enumerate(notes):
        if i:
            parts.append("\n\n---\n\n")
        parts.append(n["md"])
    return "".join(parts)


def pack_bundle(md: bytes, html: bytes) -> bytes:
    """Single publish payload: raw markdown + rendered HTML (v1 OPSX)."""
    if len(md) > 0xFFFFFFFF or len(html) > 0xFFFFFFFF:
        raise ValueError("bundle part too large")
    header = BUNDLE_MAGIC + bytes([BUNDLE_VERSION, 0, 0, 0]) + struct.pack(">II", len(md), len(html))
    return header + md + html


def unpack_bundle(data: bytes) -> tuple[bytes, bytes] | None:
    """Return (md, html) or None if legacy HTML-only blob."""
    if len(data) < 16 or data[:4] != BUNDLE_MAGIC or data[4] != BUNDLE_VERSION:
        return None
    md_len, html_len = struct.unpack(">II", data[8:16])
    off = 16
    if off + md_len + html_len > len(data):
        return None
    md = data[off : off + md_len]
    html = data[off + md_len : off + md_len + html_len]
    return md, html


# --------------------------------------------------------------------------- pull (capability link → markdown)

_SHARE_ID = re.compile(r"^[a-z0-9]{10,32}$", re.I)


def parse_share_link(url: str) -> tuple[str, str]:
    """Parse a capability share URL. Returns (origin, share_id).

    Accepts `https://host/<token>`, tolerating a trailing `.md` (agent link) and IGNORING any
    `#fragment` (old zero-knowledge links pasted from history still carry a stale key — harmless
    now). Tokens are `[a-z0-9]{10,32}` (current tokens are 24 chars; legacy ids were 10).
    """
    from urllib.parse import urlparse

    u = urlparse((url or "").strip())
    if not u.scheme or not u.netloc:
        raise ValueError("not a share URL (need https://<worker>/<token>)")
    path = (u.path or "").strip("/")
    if path.lower().endswith(".md"):
        path = path[:-3]
    if not _SHARE_ID.fullmatch(path or ""):
        raise ValueError(f"bad share token in URL (expected 10–32 char token): {path!r}")
    sid = path.lower()
    origin = f"{u.scheme}://{u.netloc}"
    return origin, sid


def markdown_from_blob(blob: bytes) -> str:
    """Plaintext OPSX bundle → wiki markdown UTF-8 (the md half). Legacy encrypted blobs no longer
    carry a key we can use, so they are rejected with a re-publish hint rather than silently failing."""
    unpacked = unpack_bundle(blob)
    if unpacked is not None:
        return unpacked[0].decode("utf-8")
    raise ValueError("legacy encrypted share — re-publish with current plainkeep share")


class SharePullError(Exception):
    """Fetch failure for pull (standalone callers)."""


def fetch_share_blob(origin: str, share_id: str, *, timeout: int = 30) -> bytes:
    """GET /<token>?raw=1 — the raw plaintext OPSX bundle the worker stored (same token the browser hits)."""
    from urllib.parse import urljoin
    import urllib.error
    import urllib.request

    url = urljoin(origin.rstrip("/") + "/", f"{share_id}?raw=1")
    req = urllib.request.Request(
        url,
        headers={"Accept": "application/octet-stream", "User-Agent": "plainkeep-share-pull/1"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read()
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise SharePullError("share expired or revoked") from e
        if e.code == 410:
            raise SharePullError("legacy encrypted share — re-publish with current plainkeep share") from e
        raise SharePullError(f"fetch failed: HTTP {e.code}") from e
    except Exception as e:
        raise SharePullError(f"fetch failed: {e}") from e


def pull_markdown_from_url(url: str) -> str:
    """Capability share URL → wiki markdown. No plainkeep ledger, no dispatcher — for any Python caller."""
    origin, sid = parse_share_link(url)
    blob = fetch_share_blob(origin, sid)
    return markdown_from_blob(blob)
