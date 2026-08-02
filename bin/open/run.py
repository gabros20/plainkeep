#!/usr/bin/env python3
"""
plainkeep open <target> [--edit|--reveal|--obsidian] [--json] — one read-class resolver for every
addressable thing (proposal Part 3.4). Resolution order: task id → wiki slug → files asset
(shadow-note `path:` field) → search top hit. Default prints the resolved path (never surprise-
launches a GUI); `--edit` opens $EDITOR, `--reveal` reveals in Finder (macOS `open -R`), `--obsidian`
delegates to `plainkeep wiki open --obsidian`. Bare `plainkeep open` with a tty + fzf → fuzzy picker with preview.
Keeps `wiki open`/`files open`/`task show` untouched (never removes a spelling).
"""
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lib import enginetree, output, paths, render  # noqa: E402
from lib.indexlib import search  # noqa: E402

FLAGS = ("--edit", "--reveal", "--obsidian")


def _find_task(tid: str):
    for st in paths.TASK_STATUSES:
        f = paths.TASKS / st / f"{tid}.md"
        if f.exists():
            return f
    return None


def _wiki_notes() -> dict:
    """Wiki notes by stem, EXCLUDING wiki/files/ shadow notes (those are the files tier)."""
    if not paths.WIKI.exists():
        return {}
    files_dir = paths.WIKI / "files"
    out = {}
    for p in sorted(paths.WIKI.rglob("*.md")):
        if files_dir in p.parents:
            continue
        out.setdefault(p.stem, p)
    return out


def _resolve(target: str):
    """Return a resolution dict {kind, slug, path, edit, reveal, wiki_slug} or None."""
    f = _find_task(target)
    if f:
        return {"kind": "task", "slug": target, "path": f, "edit": f, "reveal": f, "wiki_slug": None}
    notes = _wiki_notes()
    if target in notes:
        p = notes[target]
        return {"kind": "wiki", "slug": target, "path": p, "edit": p, "reveal": p, "wiki_slug": target}
    shadow = paths.WIKI / "files" / f"{target}.md"
    if shadow.exists():
        binp = paths.fm_field(shadow, "path")
        primary = Path(binp) if binp else shadow
        return {"kind": "files", "slug": target, "path": primary, "edit": shadow,
                "reveal": primary, "wiki_slug": target}
    hits = search(target)
    if hits:
        rel = hits[0][0]
        p = paths.WIKI / rel
        stem = Path(rel).stem
        return {"kind": "search", "slug": stem, "path": p, "edit": p, "reveal": p, "wiki_slug": stem}
    return None


def _addressable() -> list[str]:
    ids = [f.stem for st in paths.TASK_STATUSES for f in sorted((paths.TASKS / st).glob("T-*.md"))] \
        if paths.TASKS.exists() else []
    slugs = sorted({p.stem for p in paths.WIKI.rglob("*.md")}) if paths.WIKI.exists() else []
    return ids + slugs


def _pick() -> str | None:
    items = _addressable()
    if not items:
        return None
    pk = enginetree.launcher()   # engine-owned launcher, not a vault-local shim (Task 2)
    preview = (f'p="$("{pk}" open {{}} 2>/dev/null)"; [ -f "$p" ] && '
               f'(command -v glow >/dev/null && PLAINKEEP_RENDER=plain glow -p "$p" || cat "$p") || echo "$p"')
    return render.fzf_pick(items, preview=preview, prompt="open> ")


def main(argv):
    _, argv = output.parse_argv(argv)
    edit, reveal, obsidian = (fl in argv for fl in FLAGS)
    argv = [a for a in argv if a not in FLAGS]
    target = argv[0] if argv else ""

    if not target:
        target = _pick() or ""
        if not target:
            output.fail(output.EXIT_USAGE,
                        "usage: plainkeep open <task-id|slug|query> [--edit|--reveal|--obsidian]", verb="open")

    res = _resolve(target)
    if not res:
        output.fail(output.EXIT_NOT_FOUND,
                    f"nothing resolves for '{target}' (not a task id, wiki slug, files asset, or search hit)",
                    verb="open")

    if obsidian:
        if not res["wiki_slug"]:
            output.fail(output.EXIT_USAGE,
                        f"--obsidian opens a note; '{target}' resolved to a {res['kind']}", verb="open")
        cmd = [str(enginetree.launcher()), "wiki", "open", res["wiki_slug"], "--obsidian"]
        if output.json_mode():
            cmd.append("--json")
        return subprocess.run(cmd).returncode

    if edit:
        editor = os.environ.get("PLAINKEEP_EDITOR") or os.environ.get("VISUAL") or os.environ.get("EDITOR") or "vi"
        try:
            return subprocess.run([*editor.split(), str(res["edit"])]).returncode
        except FileNotFoundError:
            print(f"editor not found: {editor} (set $EDITOR)", file=sys.stderr)
            return 1

    data = {"target": target, "kind": res["kind"], "slug": res["slug"], "path": str(res["path"])}

    if reveal:
        def do_reveal(_):
            p = res["reveal"]
            print(str(p))
            if not os.environ.get("PLAINKEEP_NO_OPEN") and sys.platform == "darwin" and Path(p).exists():
                subprocess.run(["open", "-R", str(p)], check=False)
        return output.emit(data, "open", human=do_reveal)

    return output.emit(data, "open", human=lambda _: str(res["path"]))


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
