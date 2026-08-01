#!/usr/bin/env python3
"""
plainkeep files ingest [<path>] [--client|--project|--area <slug> | --research] [--extract]
        | extract <slug> [--reextract] [--heavy] [--lang <x>] [--diarize] [--describe]
        | link <file> <hub> | list [--hub <slug>] | open <slug>  — the binary-assets plane (§9).

ingest routes a binary out of inbox/ into ~/files and writes a wiki SHADOW NOTE (wiki/files/<slug>.md)
that points at it — the graph references material it never stores. Routing (the §9 MAP):
  --client X / --project X / --area X  → ~/files/<kind>/X/in/  and the file is LINKED from that hub
  --research                           → ~/files/research/
  (none)                               → ~/files/inbox/        (unrouted; link later with `files link`)
When routed to a hub, the shadow note is added under the hub's `## Files` section (bidirectional link).
Re-ingesting the same bytes is de-duped by sha256 (no name-2 copies) and just (re)links the existing
shadow note. Personal/legal/family docs are only PROPOSED for iCloud and left in place — the §5 wall.

extract  (proposal Part 4.1) emits a SIBLING derived note wiki/files/<slug>.extract.md from the source
  bytes — the original stays byte-for-byte in ~/files, the shadow stays the pointer, and the extract
  NEVER masquerades as source truth (frontmatter: type extract|transcript, derived_from, source_sha256,
  tool). Tiered per media type, each tier an auto-detected OPTIONAL dep (try/except import or
  shutil.which; deterministic degrade with a one-line install hint); .txt/.md extract with stdlib.
  Same-bytes + same-tool re-run is an idempotent no-op; --reextract forces. `ingest --extract` chains.

distill  (proposal Part 4.2) reads a derived extract note (wiki/files/<slug>.extract.md) and produces
  1-N interlinked concept notes in wiki/notes/ (the Iron Law shape). WITH an agent (PLAINKEEP_AGENT) the
  model returns a TYPED JSON payload [{title, summary, wikilinks[]}] — never file text; the verb
  validates (unique slugs vs the whole vault, resolvable wikilinks — unresolvable dropped with a
  warning) and writes deterministically via the notetype templates with provenance frontmatter
  author: agent, source: "[[<slug>.extract]]", status: draft. WITHOUT one (PLAINKEEP_AGENT=none): a
  deterministic heading-outline fallback (split the extract by ## headings). `plainkeep triage drafts`
  then pages the agent-drafted notes as a promotion queue (accept -> active, reject -> delete).

link  attaches an already-ingested asset to a hub note after the fact.
list  shows the asset catalogue (optionally one hub's files).
open  reveals a file in Finder via its shadow note.
"""
import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lib import agent, imagelib, notetype, output, paths, vaultio  # noqa: E402
from enrich import run as enrichverb  # noqa: E402

YEL, GREEN, DIM, CYAN, RESET = "\033[33m", "\033[32m", "\033[2m", "\033[36m", "\033[0m"
PERSONAL = ("tax", "szja", "nav", "ado", "adó", "medical", "orvos", "contract", "szerzod", "szerződ",
            "legal", "passport", "utlevel", "útlevel", "birth", "szulet", "szület", "insurance",
            "biztosit", "biztosít", "marriage", "hazas", "házas", "will", "vegrendel")
TEXT_SUFFIXES = {".md", ".txt"}
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".bmp", ".tiff", ".heic"}
HUB_KIND = {"--client": "clients", "--project": "projects", "--area": "areas"}

# Extraction media types (proposal Part 4.1). Detection is by extension; a URL source is video/URL.
PDF_SUFFIXES = {".pdf"}
AUDIO_SUFFIXES = {".mp3", ".wav", ".m4a", ".flac", ".ogg", ".oga", ".opus", ".aac", ".aiff", ".wma"}
VIDEO_SUFFIXES = {".mp4", ".mov", ".webm", ".mkv", ".avi", ".m4v", ".flv"}
EXTRACT_TEXT_SUFFIXES = {".txt", ".md", ".markdown", ".text", ".vtt", ".srt"}


def _have_mod(name: str) -> bool:
    """Optional dep present? importlib probe only — never imports at detection time (Part 4.1)."""
    try:
        return importlib.util.find_spec(name) is not None
    except Exception:
        return False


def _mod_version(name: str) -> str:
    try:
        from importlib.metadata import version
        return version(name)
    except Exception:
        return "?"


def _extract_note_path(slug: str) -> Path:
    return paths.WIKI / "files" / f"{slug}.extract.md"


def _tier_text(src: Path, opts: dict):
    def run() -> str:
        raw = src.read_text(encoding="utf-8", errors="replace")
        body = "\n".join(ln.rstrip() for ln in raw.splitlines()).strip()
        return re.sub(r"\n{3,}", "\n\n", body)
    return ("stdlib-text 1.0", "extract", run)


def _tier_pdf(src: Path, opts: dict):
    if _have_mod("pymupdf4llm"):
        def run() -> str:
            import pymupdf4llm
            return (pymupdf4llm.to_markdown(str(src)) or "").strip()
        return (f"pymupdf4llm {_mod_version('pymupdf4llm')}", "extract", run)
    if opts.get("heavy") and _have_mod("docling"):
        def run() -> str:
            from docling.document_converter import DocumentConverter
            return DocumentConverter().convert(str(src)).document.export_to_markdown().strip()
        return (f"docling {_mod_version('docling')}", "extract", run)
    return (None, "pdf", "no PDF extractor — install: pip install pymupdf4llm (or --heavy for docling)")


STT_RUNTIMES = ("parakeet", "mlx-whisper", "faster-whisper", "whisper-cli")


def _tier_audio(src: Path, opts: dict):
    """ASR cascade. PLAINKEEP_STT_MODEL overrides the hardcoded model id of whichever backend runs
    (kept as its default); PLAINKEEP_STT_RUNTIME pins one backend instead of cascading through all
    (search-enrichment proposal §2, S1 — the one real hardcoding retrofit)."""
    if not shutil.which("ffmpeg"):
        return (None, "audio", "audio extraction needs ffmpeg — install: brew install ffmpeg")
    lang = opts.get("lang")
    model = os.environ.get("PLAINKEEP_STT_MODEL")
    runtime = os.environ.get("PLAINKEEP_STT_RUNTIME", "auto").strip().lower()
    if runtime not in ("auto", *STT_RUNTIMES):
        runtime = "auto"

    def want(name: str) -> bool:
        return runtime in ("auto", name)

    if want("parakeet") and _have_mod("parakeet_mlx"):
        mid = model or "mlx-community/parakeet-tdt-0.6b-v2"

        def run() -> str:
            from parakeet_mlx import from_pretrained
            transcriber = from_pretrained(mid)
            return (transcriber.transcribe(str(src)).text or "").strip()
        return (f"parakeet-mlx {_mod_version('parakeet-mlx')}", "transcript", run)
    if want("mlx-whisper") and _have_mod("mlx_whisper"):
        def run() -> str:
            import mlx_whisper
            kw = {"language": lang} if lang else {}
            return (mlx_whisper.transcribe(str(src), **kw).get("text") or "").strip()
        return (f"mlx-whisper {_mod_version('mlx-whisper')}", "transcript", run)
    if want("faster-whisper") and _have_mod("faster_whisper"):
        mid = model or "base"

        def run() -> str:
            from faster_whisper import WhisperModel
            segs, _ = WhisperModel(mid).transcribe(str(src), language=lang)
            return "\n".join(s.text.strip() for s in segs).strip()
        return (f"faster-whisper {_mod_version('faster-whisper')}", "transcript", run)
    if want("whisper-cli") and (shutil.which("whisper-cli") or shutil.which("whisper.cpp")):
        binname = "whisper-cli" if shutil.which("whisper-cli") else "whisper.cpp"

        def run() -> str:
            out = subprocess.run([binname, "-otxt", "-f", str(src)], capture_output=True, text=True)
            return out.stdout.strip()
        return (f"{binname} (system)", "transcript", run)
    if runtime != "auto":
        return (None, "audio", f"PLAINKEEP_STT_RUNTIME={runtime} pinned but that backend isn't available — "
                "check its optional dep is installed, or unset PLAINKEEP_STT_RUNTIME for auto-detect")
    return (None, "audio",
            "no ASR backend — install: pip install parakeet-mlx (Apple Silicon) or mlx-whisper/faster-whisper")


def _tier_image(src: Path, opts: dict):
    # imagelib owns the full OCR cascade (mlx-vlm/GLM-OCR/DeepSeek-OCR -> ollama -> ocrmac -> tesseract).
    # ocr_backend_label() is a cheap probe (no model run) matching _tier_pdf/_tier_audio's contract:
    # the label is known upfront, the actual OCR is deferred to run() — dry-run/unchanged stay free.
    label = imagelib.ocr_backend_label(opts)
    if not label:
        return (None, "image", "no OCR backend — install: pip install ocrmac (Apple Vision) or brew install tesseract")
    return (label, "extract", lambda: imagelib.read_text(src, opts)[0])


def _tier_video(src: Path, is_url: bool, opts: dict):
    if shutil.which("yt-dlp"):
        target = str(src) if is_url else str(src)

        def run() -> str:
            # captions-first: ask yt-dlp for subs/auto-subs as VTT, then flatten to plain lines. Audio
            # download + ASR is the fallback only when no captions exist (kept out of the zero-dep path).
            import tempfile
            with tempfile.TemporaryDirectory() as td:
                subprocess.run(["yt-dlp", "--skip-download", "--write-subs", "--write-auto-subs",
                                "--sub-format", "vtt", "-o", str(Path(td) / "%(id)s.%(ext)s"), target],
                               capture_output=True, text=True)
                vtts = sorted(Path(td).glob("*.vtt"))
                if vtts:
                    return _vtt_to_text(vtts[0].read_text(encoding="utf-8", errors="replace"))
            return ""
        return ("yt-dlp (captions)", "transcript", run)
    return (None, "video", "no video/URL extractor — install: pip install yt-dlp (captions-first)")


def _vtt_to_text(vtt: str) -> str:
    """VTT/SRT → deduped plain text (drop cue timings, WEBVTT header, and repeated rolling lines)."""
    out: list[str] = []
    for ln in vtt.splitlines():
        s = ln.strip()
        if not s or s == "WEBVTT" or "-->" in s or s.isdigit():
            continue
        s = re.sub(r"<[^>]+>", "", s)
        if not out or out[-1] != s:
            out.append(s)
    return "\n".join(out).strip()


def _select_tier(src: Path, is_url: bool, opts: dict):
    """First available extraction tier for the source's media type.
    Returns (tool_str, note_type, run_callable) when a tier is available, else (None, media, hint)."""
    suf = src.suffix.lower()
    if is_url or suf in VIDEO_SUFFIXES:
        return _tier_video(src, is_url, opts)
    if suf in PDF_SUFFIXES:
        return _tier_pdf(src, opts)
    if suf in AUDIO_SUFFIXES:
        return _tier_audio(src, opts)
    if suf in IMAGE_SUFFIXES:
        return _tier_image(src, opts)
    if suf in EXTRACT_TEXT_SUFFIXES:
        return _tier_text(src, opts)
    return (None, f"'{suf or 'n/a'}'", "unsupported media — supported: pdf, audio, image, video/url, txt/md")


def _augment_warnings(opts: dict) -> list[str]:
    """--diarize/--describe are explicit opt-ins that NO-OP with a clear message when deps absent.
    --describe on an IMAGE is handled in _extract_one (real imagelib.describe() cascade); this covers
    only the non-image case, where no VLM applies but the flag was still passed."""
    w = []
    if opts.get("diarize") and not _have_mod("pyannote.audio"):
        w.append("--diarize requested but pyannote.audio absent — no speaker labels "
                 "(pip install pyannote.audio; needs a gated HF token)")
    if opts.get("describe") and not opts.get("_is_image") and not shutil.which("ollama"):
        w.append("--describe requested but ollama absent — no VLM scene captions (install ollama)")
    return w


def _write_extract(slug: str, sha: str, tool: str, ntype: str, text: str, shadow: Path,
                   vlm: tuple[str, str, str] | None = None) -> Path:
    dest = _extract_note_path(slug)
    title = paths.fm_field(shadow, "title") or slug
    today = paths.today()
    created = (paths.fm_field(dest, "created") if dest.exists() else "") or today
    body = text.strip() or "_(no text extracted)_"
    vlm_fm = ""
    if vlm:
        cap, desc, vbackend = vlm
        vlm_fm = f"vlm_caption: {cap}\nvlm_backend: {vbackend}\n"
        if desc.strip():
            body += f"\n\n## Description\n\n{desc.strip()}\n"
    vaultio.mkdir(dest.parent)
    vaultio.write_text(dest,
        f"---\ntype: {ntype}\ntitle: {title} (extract)\nstatus: derived\n"
        f"derived_from: \"[[{slug}]]\"\nsource_sha256: {sha}\ntool: {tool}\n{vlm_fm}"
        f"created: {created}\nupdated: {today}\ntags: []\n---\n"
        f"# {title} — {ntype}\n\n{body}\n", encoding="utf-8")
    return dest


def _extract_one(slug: str, opts: dict, dry: bool = False) -> dict:
    """Extract one shadow-note's source into wiki/files/<slug>.extract.md. Pure status dict (no exit)."""
    shadow = paths.WIKI / "files" / f"{slug}.md"
    if not shadow.exists():
        return {"slug": slug, "status": "no-shadow",
                "message": f"no shadow note wiki/files/{slug}.md (see `plainkeep files list`)"}
    src_str = paths.fm_field(shadow, "path")
    if not src_str:
        return {"slug": slug, "status": "no-path", "message": "shadow note has no path:"}
    is_url = src_str.startswith(("http://", "https://"))
    src = Path(src_str)
    if not is_url and not src.exists():
        return {"slug": slug, "status": "source-missing", "message": f"source not found: {src_str}"}

    is_image = not is_url and src.suffix.lower() in IMAGE_SUFFIXES
    tool, second, third = _select_tier(src, is_url, opts)
    if tool is None:
        media, hint = second, third
        return {"slug": slug, "status": "no-extractor", "media": media, "hint": hint,
                "message": f"no extractor for {media} — {hint}"}
    ntype, run = second, third
    sha = hashlib.sha256(src_str.encode()).hexdigest() if is_url else _sha256(src)
    dest = _extract_note_path(slug)
    rel = str(dest.relative_to(paths.PLAINKEEP_HOME))
    if dest.exists() and not opts.get("reextract") \
            and paths.fm_field(dest, "source_sha256") == sha and paths.fm_field(dest, "tool") == tool:
        return {"slug": slug, "status": "unchanged", "note": rel, "tool": tool, "sha256": sha}
    if dry:
        return {"slug": slug, "status": "would-extract", "note": rel, "tool": tool, "type": ntype}
    warns = _augment_warnings({**opts, "_is_image": is_image})
    try:
        text = run()
    except Exception as e:  # a tier's runtime failure degrades loudly, never a traceback
        return {"slug": slug, "status": "extract-failed", "tool": tool, "message": f"{tool} failed: {e}"}
    vlm = None
    if opts.get("describe") and is_image:
        cap, desc, vbackend = imagelib.describe(src, opts)
        if vbackend == "none":
            warns.append("--describe requested but no VLM backend available — "
                         "no scene captions (install ollama or mlx-vlm)")
        else:
            vlm = (cap, desc, vbackend)
    _write_extract(slug, sha, tool, ntype, text, shadow, vlm=vlm)
    if os.environ.get("PLAINKEEP_ENRICH", "").strip().lower() != "off":
        try:
            enrichverb.enrich_note(slug)  # best-effort — an enrich failure must never fail the extract
        except Exception:
            pass
    paths.append_journal(f"files extract {slug} <- {tool}")
    res = {"slug": slug, "status": "extracted", "note": rel, "tool": tool, "type": ntype,
           "sha256": sha, "chars": len(text)}
    if warns:
        res["warnings"] = warns
    return res


def _pull_opt(argv: list[str], name: str):
    """Remove `name <value>` from argv (mutating) and return the value, or None if absent."""
    if name in argv:
        i = argv.index(name)
        val = argv[i + 1] if i + 1 < len(argv) else None
        del argv[i:i + 2]
        return val
    return None


def _is_personal(name: str) -> bool:
    n = name.lower()
    return any(k in n for k in PERSONAL)


def _sha256(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _shadows() -> list[Path]:
    d = paths.WIKI / "files"
    return sorted(d.glob("*.md")) if d.exists() else []


def _find_by_hash(sha: str):
    for p in _shadows():
        if paths.fm_field(p, "sha256") == sha:
            return p
    return None


def _hub_note(slug: str):
    """Resolve a hub slug to its wiki note (any folder), or None."""
    hits = [p for p in paths.WIKI.rglob(f"{slug}.md")] if paths.WIKI.exists() else []
    return hits[0] if hits else None


def _link_into_hub(hub: Path, shadow_slug: str, title: str) -> bool:
    """Add `- [[shadow_slug]] — title` under the hub's `## Files` section (idempotent)."""
    text = hub.read_text(encoding="utf-8")
    line = f"- [[{shadow_slug}]] — {title}"
    if line in text:
        return False
    if "## Files" in text:
        out, done = [], False
        for ln in text.splitlines():
            out.append(ln)
            if ln.strip() == "## Files" and not done:
                out.append(line); done = True
        text = "\n".join(out) + ("\n" if text.endswith("\n") else "")
    else:
        text = text.rstrip("\n") + f"\n\n## Files\n{line}\n"
    vaultio.write_text(hub, text, encoding="utf-8")
    return True


def _shadow(dest: Path, title: str, sha: str, hub_slug: str | None) -> Path:
    d = paths.WIKI / "files"; vaultio.mkdir(d)
    base = paths.slugify(Path(title).stem)
    existing = {p.stem for p in paths.WIKI.rglob("*.md")}
    slug, i = base, 2
    while slug in existing:
        slug, i = f"{base}-{i}", i + 1
    f = d / f"{slug}.md"
    hub_fm = f"hub: {hub_slug}\n" if hub_slug else ""
    hub_body = f"\nFiled under [[{hub_slug}]].\n" if hub_slug else ""
    is_image = dest.suffix.lower() in IMAGE_SUFFIXES
    # images stay binaries in ~/files (the wiki is plaintext-only), but the shadow note EMBEDS them so
    # an editor that resolves the path previews inline; other files get a plain reference.
    asset = (f"![{title}]({dest})\n" if is_image
             else f"Shadow note for a binary in `~/files` (not stored in git). File: `{dest}`.\n")
    kind_fm = "kind: image\n" if is_image else ""
    meta_fm = ""
    if is_image:
        meta = imagelib.image_metadata(dest)
        # only keys imagelib actually returns land in frontmatter (width/height/taken/camera need
        # Pillow; format+bytes are stdlib-only and always present) — never fabricate a missing field.
        meta_fm = "".join(f"{k}: {meta[k]}\n" for k in ("format", "bytes", "width", "height", "taken", "camera")
                          if k in meta)
    vaultio.write_text(f, f"---\ntype: file\n{kind_fm}{meta_fm}title: {title}\nstatus: active\nsource: ingest\n"
                 f"ingested: {paths.today()}\npath: {dest}\nsha256: {sha}\n{hub_fm}tags: []\n---\n# {title}\n\n"
                 f"{asset}{hub_body}", encoding="utf-8")
    return f


def _parse_route(rest):
    """Return (dest_dir, hub_slug, hub_note, leftover_args). Exits via message on a bad hub."""
    hub_kind = hub_slug = None
    research = False
    leftover = []
    i = 0
    while i < len(rest):
        a = rest[i]
        if a in HUB_KIND and i + 1 < len(rest):
            hub_kind, hub_slug = HUB_KIND[a], rest[i + 1]; i += 2; continue
        if a == "--research":
            research = True; i += 1; continue
        leftover.append(a); i += 1

    if hub_slug:
        dest_dir = paths.FILES_ROOT / hub_kind / hub_slug / "in"
        hub_note = _hub_note(hub_slug)
        return dest_dir, hub_slug, hub_note, leftover
    if research:
        return paths.FILES_ROOT / "research", None, None, leftover
    return paths.FILES_ROOT / "inbox", None, None, leftover


def cmd_ingest(argv, dry=False, extract=False):
    dest_dir, hub_slug, hub_note, rest = _parse_route(argv)
    if hub_slug and hub_note is None:
        output.fail(output.EXIT_UNEXPECTED,
                    f"{YEL}no wiki hub '{hub_slug}'{RESET} — create it first (e.g. `plainkeep new client {hub_slug}`), "
                    f"then re-run. Nothing ingested.", verb="files")

    if rest:
        sources = [Path(rest[0]).expanduser()]
    else:
        sources = [p for p in paths.INBOX.iterdir()
                   if p.is_file() and p.suffix.lower() not in TEXT_SUFFIXES and p.name != ".gitkeep"] \
            if paths.INBOX.exists() else []
    if not sources:
        return output.emit_rows([], "files",
                                human=lambda _: "nothing to ingest (drop a binary into inbox/, or pass a path).",
                                header={"filed": 0, "proposed": 0, "linked": 0, "deduped": 0, "dry_run": dry})

    events, rows = [], []
    filed = proposed = linked = deduped = 0
    for src in sources:
        if not src.exists():
            events.append(f"  not found: {src}"); continue
        if _is_personal(src.name):
            events.append(f"  {YEL}PROPOSE iCloud{RESET}: '{src.name}' looks personal/legal — move it yourself to "
                          f"iCloud (the wall forbids any verb writing there). Left in place.")
            rows.append({"action": "propose", "name": src.name})
            proposed += 1
            continue

        sha = _sha256(src)
        dup = _find_by_hash(sha)
        if dup:
            events.append(f"  {DIM}duplicate{RESET}: '{src.name}' == {dup.stem} (same bytes) — not copied again.")
            rows.append({"action": "dedup", "name": src.name, "slug": dup.stem})
            deduped += 1
            if hub_note and (dry or _link_into_hub(hub_note, dup.stem, paths.fm_field(dup, "title") or dup.stem)):
                events.append(f"    {GREEN}linked{RESET} -> [[{hub_slug}]]"); linked += 1
            continue

        where = dest_dir.relative_to(paths.FILES_ROOT)
        if dry:
            events.append(f"  {GREEN}would file{RESET}: {src.name} -> ~/files/{where}/")
            rows.append({"action": "file", "name": src.name, "dest": f"~/files/{where}/"})
            filed += 1
            if hub_note:
                events.append(f"    {GREEN}would link{RESET} -> [[{hub_slug}]]"); linked += 1
            continue

        # NOT behind the wall — see test/run_pathwall.py EXEMPT. `--client` routes here into
        # ~/files/<hub>/in/, and the wall DENIES every write under in/ ("originals are read-only
        # evidence"). Ingest is how an original ARRIVES; the uniquifying loop just below is what
        # keeps it from ever overwriting one.
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / src.name
        i = 2
        while dest.exists():
            dest = dest_dir / f"{src.stem}-{i}{src.suffix}"; i += 1
        shutil.move(str(src), str(dest))
        note = _shadow(dest, src.name, sha, hub_slug)
        paths.append_journal(f"files ingest {src.name} -> {dest}" + (f" [[{hub_slug}]]" if hub_slug else ""))
        events.append(f"  {GREEN}filed{RESET}: {src.name} -> ~/files/{where}/  ({note.relative_to(paths.PLAINKEEP_HOME)})")
        rows.append({"action": "file", "name": src.name, "dest": f"~/files/{where}/",
                     "slug": note.stem})
        filed += 1
        if hub_note and _link_into_hub(hub_note, note.stem, src.name):
            events.append(f"    {GREEN}linked{RESET} -> [[{hub_slug}]]"); linked += 1

    extracted = 0
    if extract and not dry:
        for r in rows:
            if r.get("action") == "file" and r.get("slug"):
                er = _extract_one(r["slug"], {}, dry=False)
                r["extract"] = er["status"]
                if er["status"] == "extracted":
                    extracted += 1
                    events.append(f"    {GREEN}extracted{RESET} -> {er['note']}  {DIM}({er['tool']}){RESET}")
                elif er["status"] == "no-extractor":
                    events.append(f"    {DIM}extract skipped: {er['hint']}{RESET}")

    def render(_):
        for e in events:
            print(e)
        tail = f", {linked} linked" if hub_slug else ""
        tail += f", {deduped} de-duped" if deduped else ""
        tail += f", {extracted} extracted" if extract else ""
        print(f"\nfiles ingest: {filed} filed{tail}, {proposed} proposed for iCloud (left in place)"
              + (" (dry run)" if dry else ""))

    return output.emit_rows(rows, "files", human=render,
                            header={"filed": filed, "proposed": proposed, "linked": linked,
                                    "deduped": deduped, "extracted": extracted, "dry_run": dry})


def cmd_link(argv):
    if len(argv) < 2:
        output.fail(output.EXIT_USAGE, "usage: plainkeep files link <file-slug> <hub-slug>", verb="files")
    file_slug, hub_slug = argv[0], argv[1]
    shadow = paths.WIKI / "files" / f"{file_slug}.md"
    if not shadow.exists():
        output.fail(output.EXIT_UNEXPECTED,
                    f"no shadow note: wiki/files/{file_slug}.md (see `plainkeep files list`)", verb="files")
    hub = _hub_note(hub_slug)
    if hub is None:
        output.fail(output.EXIT_UNEXPECTED, f"no wiki hub '{hub_slug}'", verb="files")
    title = paths.fm_field(shadow, "title") or file_slug
    changed = _link_into_hub(hub, file_slug, title)
    # make the back-reference explicit in the shadow note too
    stext = shadow.read_text(encoding="utf-8")
    if f"[[{hub_slug}]]" not in stext:
        vaultio.write_text(shadow, stext.rstrip("\n") + f"\n\nFiled under [[{hub_slug}]].\n", encoding="utf-8")
    data = {"file": file_slug, "hub": hub_slug, "already_linked": not changed}
    return output.emit(data, "files", human=lambda _:
                       f"{GREEN}linked{RESET} [[{file_slug}]] -> [[{hub_slug}]]"
                       + ("" if changed else " (already linked)"))


def cmd_list(argv):
    hub_filter = None
    if "--hub" in argv:
        j = argv.index("--hub")
        hub_filter = argv[j + 1] if j + 1 < len(argv) else None
    rows = []
    for p in _shadows():
        hub = paths.fm_field(p, "hub")
        if hub_filter and hub != hub_filter:
            continue
        rows.append({"slug": p.stem, "title": paths.fm_field(p, "title") or p.stem,
                     "hub": hub, "path": paths.fm_field(p, "path")})

    def render(rs):
        if not rs:
            return "no assets yet (ingest one: `plainkeep files ingest <path> --client <slug>`)."
        out = [f"{len(rs)} asset(s)" + (f" under [[{hub_filter}]]" if hub_filter else "") + ":"]
        for r in rs:
            tag = f"  {CYAN}[[{r['hub']}]]{RESET}" if r["hub"] else f"  {DIM}(unlinked){RESET}"
            out.append(f"  {r['slug']:<28} {DIM}{r['title'][:40]:<40}{RESET}{tag}")
        return "\n".join(out)

    return output.emit_rows(rows, "files", human=render, header={"hub": hub_filter})


def cmd_open(argv):
    if not argv:
        output.fail(output.EXIT_USAGE, "usage: plainkeep files open <slug>", verb="files")
    note = paths.WIKI / "files" / f"{argv[0]}.md"
    if not note.exists():
        output.fail(output.EXIT_UNEXPECTED, f"no shadow note: wiki/files/{argv[0]}.md", verb="files")
    target = paths.fm_field(note, "path")
    if not target:
        output.fail(output.EXIT_UNEXPECTED, "shadow note has no path:", verb="files")

    def render(_):
        print(target)
        if not os.environ.get("PLAINKEEP_NO_OPEN") and sys.platform == "darwin" and Path(target).exists():
            subprocess.run(["open", "-R", target], check=False)

    return output.emit({"slug": argv[0], "path": target}, "files", human=render)


def _strip_frontmatter(text: str) -> str:
    if text.startswith("---"):
        e = text.find("\n---", 3)
        if e != -1:
            return text[e + 4:].strip()
    return text.strip()


def _parse_json_array(text: str):
    """Extract the first JSON array from a model's reply, tolerating ``` fences and surrounding prose."""
    t = re.sub(r"```(?:json)?", "", text or "").strip()
    i, j = t.find("["), t.rfind("]")
    if i == -1 or j == -1 or j < i:
        return None
    try:
        data = json.loads(t[i:j + 1])
    except Exception:
        return None
    return data if isinstance(data, list) else None


def _agent_concepts(body: str):
    """Ask the configured agent (scope=read) for a TYPED payload; None -> use the outline fallback.
    The model NEVER writes files or returns note text — only [{title, summary, wikilinks[]}]."""
    prompt = ("Read this extract and distill it into 1-5 concept notes. Reply with ONLY a JSON array; "
              "each element is an object with keys \"title\" (short), \"summary\" (2-4 sentences of "
              "plain prose), and \"wikilinks\" (array of related note titles). No text outside the "
              "JSON.\n\n" + body[:8000])
    data = _parse_json_array(agent.run_agent(prompt, scope="read") or "")
    if not data:
        return None
    out = []
    for it in data:
        if isinstance(it, dict) and str(it.get("title", "")).strip():
            out.append({"title": str(it["title"]).strip(),
                        "summary": str(it.get("summary", "")).strip(),
                        "wikilinks": [str(w).strip() for w in (it.get("wikilinks") or []) if str(w).strip()]})
    return out or None


def _outline_concepts(body: str, fallback_title: str) -> list:
    """PLAINKEEP_AGENT=none deterministic path: split the extract by `## headings` into concept notes."""
    concepts, cur, buf = [], None, []
    for ln in body.splitlines():
        m = re.match(r"^#{2,6}\s+(.*)$", ln)
        if m:
            if cur is not None and "\n".join(buf).strip():
                concepts.append({"title": cur, "summary": "\n".join(buf).strip(), "wikilinks": []})
            cur, buf = m.group(1).strip(), []
        elif cur is not None:
            buf.append(ln)
    if cur is not None and "\n".join(buf).strip():
        concepts.append({"title": cur, "summary": "\n".join(buf).strip(), "wikilinks": []})
    if not concepts and body.strip():
        concepts.append({"title": fallback_title, "summary": body.strip(), "wikilinks": []})
    return concepts


def _inject_provenance(text: str, source_link: str) -> str:
    """Splice author:/source: provenance into the note's leading frontmatter (Part 4.2/4.3)."""
    inject = f"author: agent\nsource: \"[[{source_link}]]\"\n"
    m = re.search(r"(?m)^status:.*\n", text)
    if m:
        return text[:m.end()] + inject + text[m.end():]
    return text.replace("---\n", "---\n" + inject, 1)


def cmd_distill(argv, dry=False):
    rest = list(argv)
    slugs = [a for a in rest if not a.startswith("-")]
    if not slugs:
        output.fail(output.EXIT_USAGE, "usage: plainkeep files distill <slug> [--redistill]", verb="files")
    slug = slugs[0]
    extract = _extract_note_path(slug)
    if not extract.exists():
        output.fail(output.EXIT_NOT_FOUND,
                    f"no extract note wiki/files/{slug}.extract.md — run `plainkeep files extract {slug}` first",
                    verb="files")
    body = _strip_frontmatter(extract.read_text(encoding="utf-8"))
    shadow = paths.WIKI / "files" / f"{slug}.md"
    fallback_title = (paths.fm_field(shadow, "title") if shadow.exists() else "") or slug
    source_link = f"{slug}.extract"

    concepts = _agent_concepts(body) if agent.available() else None
    used_agent = concepts is not None
    if concepts is None:
        concepts = _outline_concepts(body, fallback_title)
    if not concepts:
        output.fail(output.EXIT_UNEXPECTED,
                    f"nothing to distill from {extract.relative_to(paths.PLAINKEEP_HOME)} (empty extract)",
                    verb="files")

    # unique slugs vs the WHOLE vault AND within this batch (Iron Law: the verb owns placement)
    existing = {p.stem for p in paths.WIKI.rglob("*.md")}
    used, warnings = set(existing), []
    for c in concepts:
        base = paths.slugify(c["title"])
        s, i = base, 2
        while s in used:
            s, i = f"{base}-{i}", i + 1
        c["slug"] = s
        used.add(s)
    valid = existing | {c["slug"] for c in concepts}
    for c in concepts:
        keep = []
        for w in c.get("wikilinks", []):
            tgt = w.split("#", 1)[0].split("|", 1)[0].strip().strip("[").strip("]").strip()
            if tgt and tgt in valid and tgt != c["slug"]:
                keep.append(tgt)
            elif tgt:
                warnings.append(f"dropped unresolvable link [[{tgt}]] from {c['slug']}")
        c["wikilinks"] = keep

    dest_dir = paths.WIKI / "notes"
    rows = [{"slug": c["slug"], "title": c["title"],
             "note": str((dest_dir / f"{c['slug']}.md").relative_to(paths.PLAINKEEP_HOME)),
             "links": c["wikilinks"], "status": "would-write" if dry else "written"}
            for c in concepts]
    if not dry:
        vaultio.mkdir(dest_dir)
        for c in concepts:
            md = c["summary"].strip() or "_(no summary)_"
            if c["wikilinks"]:
                md += "\n\n## Related\n" + "\n".join(f"- [[{w}]]" for w in c["wikilinks"])
            text = notetype.render("note", title=c["title"], status="draft", body=md, slug=c["slug"])
            vaultio.write_text((dest_dir / f"{c['slug']}.md"), _inject_provenance(text, source_link), encoding="utf-8")
        paths.append_journal(f"files distill {slug} -> {len(concepts)} draft note(s)"
                             + (" (agent)" if used_agent else " (outline)"))

    def render(rs):
        head = "would distill" if dry else "distilled"
        out = [f"{head} {slug} -> {len(rs)} concept note(s) "
               f"{DIM}({'agent' if used_agent else 'heading-outline fallback'}){RESET}:"]
        for r in rs:
            link = f"  {CYAN}{len(r['links'])} link(s){RESET}" if r["links"] else ""
            out.append(f"  {r['slug']:<30} {DIM}{r['title'][:34]:<34}{RESET}{link}")
        for w in warnings:
            out.append(f"  {YEL}note{RESET} {w}")
        out.append("\n  promote drafts:  plainkeep triage drafts   (accept -> active, reject -> delete)"
                   + ("  (dry run — nothing written)" if dry else ""))
        return "\n".join(out)

    return output.emit_rows(rows, "files", human=render,
                            header={"slug": slug, "agent": used_agent, "warnings": warnings,
                                    "dry_run": dry})


def cmd_extract(argv, dry=False):
    rest = list(argv)
    lang = _pull_opt(rest, "--lang")
    opts = {"reextract": "--reextract" in rest, "heavy": "--heavy" in rest,
            "diarize": "--diarize" in rest, "describe": "--describe" in rest, "lang": lang}
    slugs = [a for a in rest if not a.startswith("-")]
    if not slugs:
        output.fail(output.EXIT_USAGE,
                    "usage: plainkeep files extract <slug> [--reextract] [--heavy] [--lang <x>] "
                    "[--diarize] [--describe]", verb="files")
    res = _extract_one(slugs[0], opts, dry)
    if res["status"] in ("no-shadow", "no-path", "source-missing"):
        output.fail(output.EXIT_NOT_FOUND, res["message"], verb="files")

    def render(r):
        s = r["status"]
        if s == "extracted":
            line = f"{GREEN}extracted{RESET} -> {r['note']}  {DIM}({r['tool']}, {r['chars']} chars){RESET}"
            return line + "".join(f"\n  {YEL}note{RESET}  {w}" for w in r.get("warnings", []))
        if s == "unchanged":
            return (f"{DIM}unchanged{RESET} {r['note']} — same bytes + tool "
                    f"(idempotent no-op; --reextract to force)")
        if s == "would-extract":
            return f"{GREEN}would extract{RESET} -> {r['note']}  {DIM}({r['tool']}){RESET}  (dry run — nothing written)"
        if s == "no-extractor":
            return (f"{YEL}no extractor{RESET} for {r['media']} — {r['hint']}\n"
                    f"  {DIM}(original untouched, nothing written){RESET}")
        if s == "extract-failed":
            return f"{YEL}extract failed{RESET}: {r.get('message', '')}"
        return r.get("message", s)

    return output.emit(res, "files", human=render)


def main(argv):
    _, argv = output.parse_argv(argv)
    dry = "--dry-run" in argv
    argv = [a for a in argv if a != "--dry-run"]
    action = argv[0] if argv else "ingest"
    if action == "ingest":
        extract = "--extract" in argv
        rest = [a for a in argv[1:] if a != "--extract"]
        return cmd_ingest(rest, dry, extract=extract)
    if action == "extract":
        return cmd_extract(argv[1:], dry)
    if action == "distill":
        return cmd_distill(argv[1:], dry)
    if action == "link":
        return cmd_link(argv[1:])
    if action == "list":
        return cmd_list(argv[1:])
    if action == "open":
        return cmd_open(argv[1:])
    output.fail(output.EXIT_USAGE,
                "usage: plainkeep files ingest [<path>] [--client|--project|--area <slug> | --research] "
                "[--extract] | extract <slug> [--reextract] [--heavy] [--lang <x>] [--diarize] "
                "[--describe] | distill <slug> | link <file> <hub> | list [--hub <slug>] | "
                "open <slug>", verb="files")


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
