"""paths.py — shared filesystem roots + small helpers for the plainkeep verbs."""
from __future__ import annotations
import os
import re
import sys
from datetime import date, datetime
from pathlib import Path

try:
    from . import enginetree, vaultroot  # type: ignore  # (namespace siblings)
except ImportError:      # imported top-level by a verb that put bin/ on sys.path
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import enginetree  # type: ignore
    import vaultroot  # type: ignore

# The SELECTED data root — from PLAINKEEP_HOME, with NO engine-relative fallback (ADR-014 D2/D3,
# Phase 2 Task 1b). The `Path(__file__).resolve().parents[2]` that used to sit here is the "the
# engine lives in the vault" assumption in code; because the write path does not consult the wall
# for every write, its failure mode was a silent, successful write to the wrong root. The dispatcher
# selects and validates a root and exports it; a verb reached any other way refuses (exit 2).
PLAINKEEP_HOME = vaultroot.active_root()
INBOX = PLAINKEEP_HOME / "inbox"
TASKS = PLAINKEEP_HOME / "tasks"
JOURNAL = PLAINKEEP_HOME / "journal"
WIKI = PLAINKEEP_HOME / "wiki"
TASK_STATUSES = ("inbox", "active", "waiting", "done")

# The ENGINE tree and its bin/ — where the CODE is (Phase 2 Task 2). `BIN` read
# `PLAINKEEP_HOME / "bin"` through Phase 1, which was true only while the engine lived inside the
# vault; its one consumer (`setuplib`) reads engine-owned files through it — `bin/ui/version.txt`,
# the pin `plainkeep setup ui` downloads against — and would have looked for them in the user's
# notes. The vault has no `bin/` of its own to name here: an engine-owned path resolves from the
# engine, a data path from the data root, and nothing resolves from the other one.
ENGINE = enginetree.ENGINE_ROOT
BIN = enginetree.engine_bin()
VERB_TEMPLATES = ENGINE / "templates" / "verb"
SKILLS = ENGINE / "skills"

# The sibling roots (§2) — never inside ~/plainkeep. PLAINKEEP_ROOTS_HOME lets tests redirect them
# off the real ~/.
#
# The precedence MIRRORS `lib/wall.py`'s HOME, PLAINKEEP_TEST_HOME included, and that is load-bearing
# rather than tidy: `wall.HOME` anchors the write wall's ~/files segment and this anchors where the
# verbs actually put files, so the two disagreeing does not produce a wrong path — it produces a
# DENY on a correct one. Harmless while `files ingest` sat outside the wall (Task 1c put it behind
# the seam); a real divergence for anyone with PLAINKEEP_TEST_HOME exported, from that point on.
ROOTS_HOME = Path(os.environ.get("PLAINKEEP_TEST_HOME")
                  or os.environ.get("PLAINKEEP_ROOTS_HOME")
                  or os.environ.get("HOME") or Path.home())
WORK_ROOT = ROOTS_HOME / "work"
FILES_ROOT = ROOTS_HOME / "files"
WORK_KINDS = ("products", "labs", "tools")   # clients/ is nested <client>/<project>; archive/ is special


def today() -> str:
    return date.today().isoformat()


def now_stamp() -> str:
    # millisecond precision so rapid captures don't collide on filename
    n = datetime.now()
    return n.strftime("%Y%m%d-%H%M%S-") + f"{n.microsecond // 1000:03d}"


def journal_path(d: date | None = None) -> Path:
    d = d or date.today()
    return JOURNAL / f"{d.year:04d}" / f"{d.month:02d}" / f"{d.isoformat()}.md"


def _io():
    """lib.vaultio, imported lazily: vaultio imports guardrail, guardrail's CLI is exec'd standalone,
    and paths is imported by everything — a module-level import here would be a cycle. Journalling is
    guarded from inside paths so every caller inherits the wall, including plugins reaching it
    through the frozen SDK's `append_journal` re-export (lib/api.py)."""
    from . import vaultio  # type: ignore  # (namespace sibling)
    return vaultio


def ensure_journal(d: date | None = None) -> tuple[Path, bool]:
    """Return (path, created). Creates today's journal note with a header if missing."""
    d = d or date.today()
    note = journal_path(d)
    created = not note.exists()
    if created:
        io = _io()
        io.mkdir(note.parent)
        io.write_text(note, f"---\ntype: journal\ndate: {d.isoformat()}\n---\n# {d.isoformat()}\n\n")
    return note, created


def append_journal(line: str) -> Path:
    """Append one timestamped line to today's journal note (the shared activity record, §8)."""
    note, _ = ensure_journal()
    _io().append_text(note, f"- {datetime.now().strftime('%H:%M')} {line}\n")
    return note


def git(*args) -> str:
    import subprocess
    try:
        return subprocess.run(["git", "-C", str(PLAINKEEP_HOME), *args],
                              capture_output=True, text=True, timeout=10).stdout
    except Exception:
        return ""


def slugify(s: str, n: int = 60) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")
    return (s[:n].rstrip("-")) or "note"


LINK_RE = re.compile(r"\[\[([^\]]+)\]\]")


def link_targets(text: str) -> list[str]:
    """Normalized [[wikilink]] targets (strip #heading / |alias)."""
    return [t.split("#", 1)[0].split("|", 1)[0].strip() for t in LINK_RE.findall(text)]


def fm_field(path: Path, key: str) -> str:
    """Read a single frontmatter `key:` value from a markdown file ('' if absent)."""
    try:
        m = re.search(rf"(?m)^{re.escape(key)}:\s*(.+)$", path.read_text(encoding="utf-8"))
        return m.group(1).strip() if m else ""
    except Exception:
        return ""


def _fm_block(text: str) -> list[str]:
    """The lines strictly INSIDE a leading `---`…`---` YAML frontmatter block ([] if none)."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return []
    end = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
    return lines[1:end] if end is not None else []


def _unquote(v: str) -> str:
    v = v.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        return v[1:-1]
    return v


def frontmatter(source) -> dict:
    """Parse the YAML frontmatter into a dict, TOLERATING Obsidian's Properties normalization
    (proposal Part 3.1, anti-roadmap #12): key reorder (position-independent), flow lists
    `key: [a, b]` AND block lists (`key:` then `  - a` lines), and quoted scalars. Scalars → str,
    lists → list[str]. Read-only — never rewrites the file. Accepts a Path or the raw text.
    Stdlib only (no PyYAML dependency, which is optional/absent on the zero-install path)."""
    text = source.read_text(encoding="utf-8") if isinstance(source, Path) else str(source)
    out: dict = {}
    cur_key = None
    for ln in _fm_block(text):
        if not ln.strip():
            continue
        stripped = ln.lstrip()
        if stripped.startswith("- ") and cur_key is not None and isinstance(out.get(cur_key), list):
            out[cur_key].append(_unquote(stripped[2:]))
            continue
        m = re.match(r"^([A-Za-z0-9_\-. ]+):\s*(.*)$", ln)
        if not m:
            continue
        key, val = m.group(1).strip(), m.group(2).strip()
        cur_key = key
        if val == "":
            out[key] = []          # tentative: a block list may follow; stays [] if not
        elif re.match(r"^\[.*\]$", val):
            inner = val[1:-1].strip()
            out[key] = [_unquote(x) for x in inner.split(",") if x.strip()] if inner else []
        else:
            out[key] = _unquote(val)
    return out


def fm_list(source, key: str) -> list[str]:
    """Read a frontmatter list field (`tags`, `aliases`, …), tolerating flow OR block form ([] if
    absent). Convenience over frontmatter()."""
    v = frontmatter(source).get(key)
    if isinstance(v, list):
        return v
    return [v] if isinstance(v, str) and v else []


def title_of(path: Path) -> str:
    """First markdown heading, else the slug."""
    try:
        for ln in path.read_text(encoding="utf-8").splitlines():
            if ln.startswith("# "):
                return ln[2:].strip()
    except Exception:
        pass
    return path.stem
