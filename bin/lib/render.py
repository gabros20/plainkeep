"""
render.py — how a note looks when you read it in the terminal (Tier-1 ergonomics).

Zero-dependency baseline, auto-upgrading: if `glow` or `bat` is installed we hand the file to it;
otherwise a tiny built-in ANSI renderer makes Markdown legible (bold headings, dim frontmatter,
accented [[wikilinks]]). When stdout is NOT a TTY (piped, captured by a test, redirected to a file)
or PLAINKEEP_RENDER=raw, we print the raw Markdown untouched — piping should always yield plain text.

  PLAINKEEP_RENDER=raw   force raw Markdown (also the default when stdout is not a terminal)
  PLAINKEEP_RENDER=plain force the built-in ANSI renderer (even when piped — used for the fzf preview)

fzf_pick() is the Tier-2 fuzzy picker: when a verb is called with no target and we're interactive
and `fzf` is installed, let the user fuzzy-select from a list (with a live preview). Absent fzf or a
non-interactive shell, it returns None so the caller can fall back to a plain listing.
"""
from __future__ import annotations
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

try:
    from . import wikilink  # type: ignore
except ImportError:      # loaded by file path (tests) — sibling is not a package then
    import sys as _s, os as _o
    _s.path.insert(0, _o.path.dirname(_o.path.abspath(__file__)))
    import wikilink  # type: ignore

# ANSI
B, DIM, RESET = "\033[1m", "\033[2m", "\033[0m"
ACCENT = "\033[38;5;173m"   # muted terracotta — matches the docs palette
LINK = "\033[36m"
CODE = "\033[38;5;108m"

_H = re.compile(r"^(#{1,6})\s+(.*)$")
_IMG = re.compile(r"!\[([^\]]*)\]\(([^)]+)\)")   # ![alt](path) — terminal can't show pixels; show a ref
_LINK = wikilink.LINK_RE
_BOLD = re.compile(r"\*\*([^*]+)\*\*")
_ICODE = re.compile(r"`([^`]+)`")


def _tty() -> bool:
    return sys.stdout.isatty() and os.environ.get("PLAINKEEP_RENDER") != "raw"


def render_markdown(text: str) -> str:
    """Minimal, dependency-free ANSI rendering of a Markdown note."""
    out, lines, i = [], text.splitlines(), 0
    # dim a leading YAML frontmatter block
    if lines and lines[0].strip() == "---":
        out.append(f"{DIM}---{RESET}")
        i = 1
        while i < len(lines) and lines[i].strip() != "---":
            out.append(f"{DIM}{lines[i]}{RESET}")
            i += 1
        if i < len(lines):
            out.append(f"{DIM}---{RESET}")
            i += 1
    fence = False
    for line in lines[i:]:
        if line.startswith("```"):
            fence = not fence
            out.append(f"{DIM}{line}{RESET}")
            continue
        if fence:
            out.append(f"{CODE}{line}{RESET}")
            continue
        m = _H.match(line)
        if m:
            level, txt = len(m.group(1)), m.group(2)
            style = f"{B}{ACCENT}" if level <= 2 else B
            out.append(f"{style}{txt}{RESET}")
            continue
        line = _IMG.sub(lambda x: f"{DIM}🖼 {x.group(1) or 'image'} → {x.group(2)}{RESET}", line)
        line = _LINK.sub(lambda x: f"{LINK}[[{x.group(1)}]]{RESET}", line)
        line = _BOLD.sub(lambda x: f"{B}{x.group(1)}{RESET}", line)
        line = _ICODE.sub(lambda x: f"{CODE}{x.group(1)}{RESET}", line)
        out.append(line)
    return "\n".join(out) + "\n"


def open_note(path: Path) -> None:
    """Show a note the best way available; raw Markdown when piped/redirected."""
    text = path.read_text(encoding="utf-8")
    mode = os.environ.get("PLAINKEEP_RENDER")
    if mode == "raw":
        sys.stdout.write(text if text.endswith("\n") else text + "\n")
        return
    if mode == "plain":  # forced built-in render — even when piped (the fzf preview relies on this)
        sys.stdout.write(render_markdown(text))
        return
    if not sys.stdout.isatty():
        sys.stdout.write(text if text.endswith("\n") else text + "\n")
        return
    if shutil.which("glow"):
        subprocess.run(["glow", "-p", str(path)]); return
    if shutil.which("bat"):
        subprocess.run(["bat", "--style=plain", "-l", "md", "--paging=auto", str(path)]); return
    sys.stdout.write(render_markdown(text))


def fzf_pick(items, preview: str | None = None, prompt: str = "> "):
    """Fuzzy-select one of `items` with fzf; return the choice, or None to fall back.
    Returns None when not interactive or fzf is absent — the caller then lists plainly.
    `preview` is a shell command; fzf substitutes {} with the focused line."""
    if not items or not sys.stdin.isatty() or not sys.stdout.isatty() or not shutil.which("fzf"):
        return None
    argv = ["fzf", "--ansi", "--reverse", "--height", "50%", "--prompt", prompt]
    if preview:
        argv += ["--preview", preview, "--preview-window", "right:62%:wrap"]
    r = subprocess.run(argv, input="\n".join(items), capture_output=True, text=True)
    choice = r.stdout.strip()
    return choice or None
