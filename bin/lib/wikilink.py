"""wikilink.py — the one parser for `[[target#heading|label]]` (dependency-free, safe to import anywhere).

Inside a markdown table a literal pipe must be escaped, so a link there reads `[[slug\\|label]]`; the
trailing backslash belongs to the table syntax, not to the target.
"""
from __future__ import annotations
import re

LINK_RE = re.compile(r"\[\[([^\]]+)\]\]")


def split_link(inner: str) -> tuple[str, str]:
    """`slug#heading|label` (or `slug\\|label`) → (target, label); label defaults to the target."""
    head, sep, label = inner.partition("|")
    target = head.rstrip("\\").split("#", 1)[0].strip()
    return target, (label.strip() if sep and label.strip() else target)


def norm_link(inner: str) -> str:
    """Normalized target of one link body: no #heading, no |alias, no table-escape backslash."""
    return split_link(inner)[0]
