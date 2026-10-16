"""
completion.py — the ONE completion brain (plainkeep.json/3). Both `plainkeep __complete` (the zsh tab-completion
helper) and `plainkeep complete --json` (the structured contract for the future TUI + agents) derive their
candidates from here, and this derives everything from a single source: the verb surface + each
compound verb's `actions[]` grammar (via `manifest.load_cmds()` → the cmd.json sidecars) plus live
content providers (note slugs, task ids, hubs, note types, statuses). There are NO hardcoded subaction
tables — the grammar lives in cmd.json, so completion can never drift from the real surface (the
anti-drift payoff of the plainkeep.json/3 refactor).

`candidates(prior)` takes the words already typed after `plainkeep` and returns the candidates for the NEXT
word as `(value, description, kind)` triples. `kind` is `verb` | `action` | `enum` | a provider name
(`note-slug`/`asset-slug`/`task-id`/`hub`/`note-type`/`status`/`tag`/`layer`).
"""
from __future__ import annotations

from . import filing, manifest, notetype, paths  # type: ignore  # (namespace siblings)


# ------------------------------------------------------------------ live content providers (kind -> rows)

def _note_slugs():
    if not paths.WIKI.exists():
        return []
    return [(p.stem, paths.fm_field(p, "type") or "note")
            for p in sorted(paths.WIKI.rglob("*.md")) if p.suffix == ".md"]


def _tags():
    """Every distinct normalized frontmatter tag in the wiki (for `wiki list --tag`)."""
    seen = {}
    if paths.WIKI.exists():
        for p in paths.WIKI.rglob("*.md"):
            for t in paths.fm_list(p, "tags"):
                n = t.strip().lstrip("#").strip().casefold()
                if n:
                    seen[n] = seen.get(n, 0) + 1
    return [(t, f"{seen[t]} note(s)") for t in sorted(seen)]


def _asset_slugs():
    """Just the binary-asset shadow notes (wiki/files/*.md) — a narrow subset of note-slug, so
    `files open/link/extract/distill` complete assets, not every note in the vault."""
    d = paths.WIKI / "files"
    if not d.exists():
        return []
    return [(p.stem, paths.fm_field(p, "title") or p.stem) for p in sorted(d.glob("*.md"))]


def _task_ids():
    rows = []
    for st in ("active", "waiting", "inbox", "done"):
        d = paths.TASKS / st
        if not d.exists():
            continue
        for p in sorted(d.glob("T-*.md")):
            title = paths.title_of(p)
            rows.append((p.stem, f"{st} - {title}" if title else st))
    return rows


def _hubs():
    rows = []
    for folder in ("clients", "projects", "areas"):
        d = paths.WIKI / folder
        if d.exists():
            rows += [(p.stem, folder[:-1]) for p in sorted(d.glob("*.md"))]
    return rows


def _note_types():
    reg = notetype.load_types()
    return [(t, "hub" if reg[t].get("hub") else reg[t].get("dir", "")) for t in sorted(reg)]


def _statuses():
    return [(s, "") for s in filing.STATUSES]


# The closed provider set the cmd.json `complete` field may name (mirrors the machine-contract spec).
# `layer` is reserved for a future setup-layer completion (Wave 3) — no verb consumes it yet.
PROVIDERS = {
    "note-slug": _note_slugs,
    "asset-slug": _asset_slugs,
    "task-id": _task_ids,
    "hub": _hubs,
    "note-type": _note_types,
    "status": _statuses,
    "tag": _tags,
    "layer": lambda: [],
}


# ------------------------------------------------------------------------------------ derivation core

def _cmds() -> dict:
    """verb -> cmd.json dict, hidden verbs already filtered (manifest.load_cmds)."""
    return {c["verb"]: c for c in manifest.load_cmds()}


def _verb_rows(cmds) -> list:
    return [(v, cmds[v].get("summary", ""), "verb") for v in sorted(cmds)]


def _positionals(action) -> list:
    return [a for a in action.get("args", []) if not a["name"].startswith("-")]


def _arg_candidates(arg) -> list:
    """The completion values for one arg: its enum, else its `complete` provider, else nothing."""
    if arg.get("enum"):
        return [(v, "", "enum") for v in arg["enum"]]
    prov = arg.get("complete")
    if prov and prov in PROVIDERS:
        return [(v, d, prov) for v, d in PROVIDERS[prov]()]
    return []


def _walk(action, arg_toks):
    """Replay the tokens already typed for an action → (positionals_consumed, pending_value_flag).
    A value-flag (a `--x` whose type isn't `flag`) swallows the following token as its value; if it is
    the last token, the NEXT word is its value (pending)."""
    value_flags = {a["name"] for a in action.get("args", [])
                   if a["name"].startswith("-") and a.get("type") != "flag"}
    pos, i, pending = 0, 0, None
    while i < len(arg_toks):
        t = arg_toks[i]
        if t in value_flags:
            if i + 1 < len(arg_toks):
                i += 2
            else:
                pending = t
                i += 1
        elif t.startswith("-"):
            i += 1
        else:
            pos += 1
            i += 1
    return pos, pending


def candidates(prior: list[str]) -> list:
    """Candidates for the next word after the already-typed `prior` words (everything after `plainkeep`).
    Returns (value, description, kind) triples."""
    cmds = _cmds()
    if not prior:
        return _verb_rows(cmds)
    verb = prior[0]
    if verb == "help":                       # `plainkeep help <verb>` completes to the verb list
        return _verb_rows(cmds)
    c = cmds.get(verb)
    if not c:
        return []
    actions = c.get("actions")
    if not actions:                          # a scalar/uncompounded verb: no subaction grammar
        return []
    by_name = {a["name"]: a for a in actions}
    default_action = next((a for a in actions if a.get("default")), None)
    toks = prior[1:]

    # slot 1 — the subcommand token itself: offer every keyworded action, plus (for a tokenless
    # default action like `share <slug>`) that action's first positional's values.
    if not toks:
        out = [(a["name"], a.get("summary", ""), "action") for a in actions if not a.get("default")]
        if default_action:
            pos = _positionals(default_action)
            if pos:
                out += _arg_candidates(pos[0])
        return out

    # within an action: resolve which action, then which arg the next word fills.
    head = toks[0]
    if head in by_name and not by_name[head].get("default"):
        action, arg_toks = by_name[head], toks[1:]
    elif default_action:
        action, arg_toks = default_action, toks
    else:
        return []

    consumed, pending = _walk(action, arg_toks)
    if pending is not None:                  # the next word is a value-flag's value
        return _arg_candidates(next(a for a in action["args"] if a["name"] == pending))
    positionals = _positionals(action)
    if consumed < len(positionals):
        return _arg_candidates(positionals[consumed])
    return []
