"""
filing.py — the single factory for tasks and captured notes. The SHAPE of a task/note is guaranteed
here, in one place (Iron Law: the system owns WHERE/HOW); verbs decide only WHAT goes in it. Both
`plainkeep task add` and `plainkeep triage` create tasks through create_task(), so the frontmatter can never drift.
"""
from __future__ import annotations
import re
from datetime import date
from pathlib import Path

from . import paths  # type: ignore  # (namespace sibling)
from . import vaultio  # type: ignore  # (namespace sibling)

STATUSES = ("inbox", "active", "waiting", "done")

TASK_TEMPLATE = """\
---
type: task
id: {id}
status: {status}
created: {created}
updated: {created}
source: {source}
risk: green
why:
---
# {title}

## Intent
{intent}

## Plan

## Outcome
<!-- COMPILED TRUTH: current best state, rewritten as it changes -->

## Log
<!-- TIMELINE: append-only; commands run, files changed -->
"""


def next_task_id() -> str:
    """Next free T-YYYYMMDD-NN id for today, scanning every status folder."""
    day = date.today().strftime("%Y%m%d")
    n = 0
    for st in STATUSES:
        for f in (paths.TASKS / st).glob(f"T-{day}-*.md"):
            m = re.search(rf"T-{day}-(\d+)", f.stem)
            if m:
                n = max(n, int(m.group(1)))
    return f"T-{day}-{n + 1:02d}"


def create_task(title: str, intent: str | None = None, status: str = "active",
                source: str = "manual") -> Path:
    """Create a task file (the §7.2 two-zone shape) and return its path. intent defaults to title."""
    tid = next_task_id()
    d = paths.TASKS / status
    vaultio.mkdir(d)
    f = d / f"{tid}.md"
    vaultio.write_text(f, TASK_TEMPLATE.format(id=tid, status=status, created=paths.today(),
                                      source=source, title=title[:70],
                                      intent=(intent if intent is not None else title)),
                 encoding="utf-8")
    return f


def create_note(text: str, folder: str = "notes", typ: str = "note", status: str = "draft") -> Path:
    """Create a captured wiki note with a globally-unique slug (§10.1) and return its path."""
    title = (text.splitlines()[0][:70] if text.strip() else "note")
    base = paths.slugify(title)
    existing = {p.stem for p in paths.WIKI.rglob("*.md")} if paths.WIKI.exists() else set()
    slug, i = base, 2
    while slug in existing:
        slug, i = f"{base}-{i}", i + 1
    d = paths.WIKI / folder
    vaultio.mkdir(d)
    f = d / f"{slug}.md"
    vaultio.write_text(f, f"---\ntype: {typ}\ntitle: {title}\nstatus: {status}\ncreated: {paths.today()}\n"
                 f"updated: {paths.today()}\ntags: []\naliases: []\n---\n# {title}\n\n{text}\n",
                 encoding="utf-8")
    return f
