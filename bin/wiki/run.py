#!/usr/bin/env python3
"""
plainkeep wiki open <slug> [--obsidian] | edit <slug> | new <type> <name> | backlinks <slug> |
  stale [days] | orphans | canvas <hub|#tag> [--depth N] [--stdout] | list  [--dry-run] [--json]
— navigate and grow the knowledge wiki (§10). Slugs resolve by basename ([[wikilinks]] style).
`open --obsidian` (or PLAINKEEP_OPEN=obsidian) prints an obsidian:// URI and opens it on macOS (Part 3.1);
`canvas` emits a deterministic JSON Canvas over the wikilink graph (Part 3.2).
"""
import json
import math
import os
import subprocess
import sys
from datetime import date
from pathlib import Path
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lib import notetype, output, paths, render, vaultio  # noqa: E402

CANVAS_DIR = "canvas"  # emitted under wiki/canvas/


def _notes():
    if not paths.WIKI.exists():
        return {}
    return {p.stem: p for p in sorted(paths.WIKI.rglob("*.md")) if p.suffix == ".md"}


def _obsidian_uri(p: Path) -> str:
    """obsidian://open URI for a note — local IPC to OPEN only, never a write (Part 3.1)."""
    rel = p.relative_to(paths.PLAINKEEP_HOME).as_posix()
    if rel.endswith(".md"):
        rel = rel[:-3]
    vault = os.environ.get("PLAINKEEP_OBSIDIAN_VAULT") or paths.PLAINKEEP_HOME.name
    return f"obsidian://open?vault={quote(vault, safe='')}&file={quote(rel, safe='/')}"


def _adjacency(notes):
    """Outbound + inbound wikilink edges among notes (targets outside the vault are dropped)."""
    out = {s: set() for s in notes}
    for slug, p in notes.items():
        for t in paths.link_targets(p.read_text(encoding="utf-8")):
            if t in notes and t != slug:
                out[slug].add(t)
    inbound = {s: set() for s in notes}
    for s, tgts in out.items():
        for t in tgts:
            inbound[t].add(s)
    return out, inbound


def _canvas_model(notes, hub, tagset, depth):
    """Deterministic (dist, ordered-slugs, edges) around a hub (1-hop default) or a tag set."""
    out, inbound = _adjacency(notes)
    if hub is not None:
        dist, frontier = {hub: 0}, {hub}
        for d in range(1, depth + 1):
            nxt = set()
            for s in frontier:
                for nb in out[s] | inbound[s]:
                    if nb not in dist:
                        dist[nb] = d
                        nxt.add(nb)
            frontier = nxt
        nodeset = set(dist)
    else:
        nodeset = set(tagset)
        dist = {s: 1 for s in nodeset}
    edges = sorted({(s, t) for s in nodeset for t in out[s] if t in nodeset})
    return dist, sorted(nodeset), edges


def _canvas_layout(dist, ordered, hub, w, h):
    """Slug-ordered ring-per-distance (with a hub) or grid (tag mode) — byte-deterministic."""
    pos = {}
    if hub is not None:
        rings = {}
        for s in ordered:
            rings.setdefault(dist[s], []).append(s)
        for d, members in rings.items():
            if d == 0:
                pos[members[0]] = (0, 0)
                continue
            r, n = 420 * d, len(members)
            for i, s in enumerate(sorted(members)):
                theta = 2 * math.pi * i / n
                pos[s] = (round(r * math.cos(theta)), round(r * math.sin(theta)))
    else:
        cols = max(1, math.ceil(math.sqrt(len(ordered))))
        for i, s in enumerate(ordered):
            pos[s] = ((i % cols) * (w + 60), (i // cols) * (h + 80))
    return pos


def _canvas_doc(notes, ordered, edges, pos, w, h):
    nodes = [{"id": s, "type": "file",
              "file": notes[s].relative_to(paths.PLAINKEEP_HOME).as_posix(),
              "x": pos[s][0], "y": pos[s][1], "width": w, "height": h} for s in ordered]
    eds = [{"id": f"{a}--{b}", "fromNode": a, "toNode": b} for a, b in edges]
    return {"nodes": nodes, "edges": eds}


def _choose(notes, label):
    """No slug given: fuzzy-pick with fzf (live preview), else list what's available. §Tier-2."""
    items = sorted(notes)
    if not items:
        print("no notes yet — capture and triage, or: plainkeep wiki new note \"…\""); return None
    pk_bin = paths.PLAINKEEP_HOME / "plainkeep"
    preview = f'PLAINKEEP_RENDER=plain "{pk_bin}" wiki open {{}}'
    sel = render.fzf_pick(items, preview=preview, prompt=f"{label} note> ")
    if sel and sel in notes:
        return sel
    print(f"{len(items)} note(s) — pass a slug (e.g. `plainkeep wiki {label} <slug>`):")
    for s in items:
        print(f"  {s}")
    return None


def _graph(notes):
    inbound = {s: set() for s in notes}
    for slug, p in notes.items():
        for tgt in paths.link_targets(p.read_text(encoding="utf-8")):
            if tgt in inbound and tgt != slug:
                inbound[tgt].add(slug)
    return inbound


def main(argv):
    _, argv = output.parse_argv(argv)
    dry = "--dry-run" in argv
    obsidian = ("--obsidian" in argv) or (os.environ.get("PLAINKEEP_OPEN") == "obsidian")
    to_stdout = "--stdout" in argv
    depth = 1
    if "--depth" in argv:
        i = argv.index("--depth")
        if i + 1 < len(argv) and argv[i + 1].isdigit():
            depth = max(1, int(argv[i + 1]))
            del argv[i + 1]
    argv = [a for a in argv if a not in ("--dry-run", "--obsidian", "--stdout", "--depth")]
    action = argv[0] if argv else "list"
    notes = _notes()

    if action == "open":
        slug = argv[1] if len(argv) > 1 else ""
        if not slug:
            slug = _choose(notes, "open")
            if not slug:
                return 0
        p = notes.get(slug)
        if not p:
            output.fail(output.EXIT_NOT_FOUND, f"no note '{slug}'", verb="wiki")
        if obsidian:
            uri = _obsidian_uri(p)
            data = {"slug": slug, "path": str(p.relative_to(paths.PLAINKEEP_HOME)), "uri": uri}

            def open_obsidian(_):
                if not os.environ.get("PLAINKEEP_NO_OPEN") and sys.platform == "darwin":
                    subprocess.run(["open", uri], check=False)  # local IPC: OPEN only, never write
                return uri
            return output.emit(data, "wiki", human=open_obsidian)
        data = {"slug": slug, "path": str(p.relative_to(paths.PLAINKEEP_HOME))}
        return output.emit(data, "wiki", human=lambda _: render.open_note(p))

    elif action == "edit":
        slug = argv[1] if len(argv) > 1 else ""
        if not slug:
            slug = _choose(notes, "edit")
            if not slug:
                return 0
        p = notes.get(slug)
        if not p:
            output.fail(output.EXIT_NOT_FOUND, f"no note '{slug}'", verb="wiki")
        editor = os.environ.get("PLAINKEEP_EDITOR") or os.environ.get("VISUAL") or os.environ.get("EDITOR") or "vi"
        try:
            rc = subprocess.run([*editor.split(), str(p)]).returncode
        except FileNotFoundError:
            print(f"editor not found: {editor} (set $EDITOR)", file=sys.stderr); return 1
        return rc

    elif action == "new":
        if len(argv) < 3:
            output.fail(output.EXIT_USAGE, "usage: plainkeep wiki new <type> <name>", verb="wiki")
        typ, name = argv[1], " ".join(argv[2:])
        if not notetype.is_type(typ):
            output.fail(output.EXIT_USAGE, f"type must be one of {sorted(notetype.load_types())}", verb="wiki")
        slug = paths.slugify(name)
        if slug in notes:
            output.fail(output.EXIT_UNEXPECTED,
                        f"slug '{slug}' already exists ({notes[slug].relative_to(paths.PLAINKEEP_HOME)}) — slugs are unique",
                        verb="wiki")
        rel = (paths.WIKI / notetype.type_dir(typ) / f"{slug}.md").relative_to(paths.PLAINKEEP_HOME)
        if dry:
            data = {"dry_run": True, "would_create": str(rel), "type": typ, "slug": slug}
            return output.emit(data, "wiki",
                               human=lambda _: f"would create -> {rel}  (dry run — nothing written)")
        d = paths.WIKI / notetype.type_dir(typ)
        vaultio.mkdir(d)
        f = d / f"{slug}.md"
        vaultio.write_text(f, notetype.render(typ, title=name, slug=slug), encoding="utf-8")
        paths.append_journal(f"wiki new {typ}: {slug}")
        data = {"path": str(rel), "type": typ, "slug": slug}
        return output.emit(data, "wiki", human=lambda _: f"created -> {rel}")

    elif action == "backlinks":
        slug = argv[1] if len(argv) > 1 else ""
        if slug not in notes:
            output.fail(output.EXIT_NOT_FOUND, f"no note '{slug}'", verb="wiki")
        ins = sorted(_graph(notes)[slug])
        rows = [{"slug": s, "path": str(notes[s].relative_to(paths.PLAINKEEP_HOME))} for s in ins]

        def render_bl(rs):
            return "\n".join([f"{len(rs)} backlink(s) to [[{slug}]]:", *[f"  {r['path']}" for r in rs]])
        return output.emit_rows(rows, "wiki", human=render_bl, header={"slug": slug})

    elif action == "stale":
        days = int(argv[1]) if len(argv) > 1 and argv[1].isdigit() else 180
        cutoff = date.today().toordinal() - days
        pairs = []
        for slug, p in notes.items():
            u = paths.fm_field(p, "updated")
            try:
                if date.fromisoformat(u[:10]).toordinal() < cutoff:
                    pairs.append((u, slug))
            except Exception:
                pass
        rows = [{"updated": u, "slug": slug} for u, slug in sorted(pairs)]

        def render_stale(rs):
            return "\n".join([f"{len(rs)} note(s) not updated in {days}+ days:",
                              *[f"  {r['updated']}  {r['slug']}" for r in rs]])
        return output.emit_rows(rows, "wiki", human=render_stale, header={"days": days})

    elif action == "orphans":
        inbound = _graph(notes)
        orphans = sorted(s for s, p in notes.items()
                         if not inbound[s] and paths.fm_field(p, "type") == "note"
                         and s not in ("index", "conventions"))
        rows = [{"slug": s} for s in orphans]

        def render_orph(rs):
            return "\n".join([f"{len(rs)} orphan note(s) (type note, no inbound links):",
                              *[f"  {r['slug']}" for r in rs]])
        return output.emit_rows(rows, "wiki", human=render_orph)

    elif action == "canvas":
        target = argv[1] if len(argv) > 1 else ""
        if not target:
            output.fail(output.EXIT_USAGE,
                        "usage: plainkeep wiki canvas <hub-slug|#tag> [--depth N] [--stdout]", verb="wiki")
        hub = target if target in notes else None
        tagset = set()
        if hub is None:
            tag = target[1:] if target.startswith("#") else target
            tagset = {s for s, p in notes.items() if tag in paths.fm_list(p, "tags")}
            if not tagset:
                output.fail(output.EXIT_NOT_FOUND, f"no note or tag '{target}'", verb="wiki")
            base = paths.slugify(tag)
        else:
            base = hub
        W, H = 260, 120
        dist, ordered, edges = _canvas_model(notes, hub, tagset, depth)
        pos = _canvas_layout(dist, ordered, hub, W, H)
        doc = _canvas_doc(notes, ordered, edges, pos, W, H)
        text = json.dumps(doc, indent=2, ensure_ascii=False) + "\n"
        if to_stdout:
            data = {"slug": base, "nodes": len(ordered), "edges": len(edges), "canvas": doc}
            return output.emit(data, "wiki", human=lambda _: sys.stdout.write(text) and None)
        rel = Path("wiki") / CANVAS_DIR / f"{base}.canvas"
        if dry:
            data = {"dry_run": True, "would_create": str(rel), "nodes": len(ordered), "edges": len(edges)}
            return output.emit(data, "wiki",
                               human=lambda _: f"would create -> {rel}  ({len(ordered)} nodes, {len(edges)} edges)  (dry run — nothing written)")
        outp = paths.PLAINKEEP_HOME / rel
        vaultio.mkdir(outp.parent)
        vaultio.write_text(outp, text, encoding="utf-8")
        paths.append_journal(f"wiki canvas: {base} ({len(ordered)} nodes)")
        data = {"path": str(rel), "slug": base, "nodes": len(ordered), "edges": len(edges)}
        return output.emit(data, "wiki",
                           human=lambda _: f"created -> {rel}  ({len(ordered)} nodes, {len(edges)} edges)")

    elif action == "list":
        by_type = {}
        for p in notes.values():
            t = paths.fm_field(p, "type") or "?"
            by_type[t] = by_type.get(t, 0) + 1
        total = len(notes)
        rows = [{"type": t, "count": n} for t, n in sorted(by_type.items())]

        def render_list(rs):
            return "\n".join([f"wiki: {total} note(s)", *[f"  {r['type']}: {r['count']}" for r in rs]])
        return output.emit_rows(rows, "wiki", human=render_list, header={"notes": total})
    else:
        output.fail(output.EXIT_USAGE,
                    "usage: plainkeep wiki open <slug> [--obsidian]|edit <slug>|new <type> <name>|"
                    "backlinks <slug>|stale [days]|orphans|canvas <hub|#tag> [--depth N] [--stdout]|list",
                    verb="wiki")


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
