"""
manifest.py — the capability manifest (§4.3, proposal Part 1.2). Each verb carries a `cmd.json`
sidecar; this concatenates them into `plainkeep.json` v2 — the authoritative surface `plainkeep help`
renders from and any agent reads to negotiate capabilities. plainkeep.json is the complete I/O
contract: schema + version + detected capabilities + per-verb output block + hints, so a third party
drives plainkeep without importing lib. Nothing is persisted that isn't re-detected on every write_manifest() (find_spec).
Agents learn the surface from this — they never hardcode or invent verbs.
"""
from __future__ import annotations
import importlib.util
import json
import os
from pathlib import Path

from . import paths  # type: ignore  # (namespace sibling)
from . import resolver  # type: ignore  # multi-root verb resolution (Part 2.1)
from . import vaultio  # type: ignore  # (namespace sibling)

BIN = Path(__file__).resolve().parents[1]   # the verbs live with the CODE (bin/), not under PLAINKEEP_HOME
MANIFEST = paths.PLAINKEEP_HOME / "plainkeep.json"  # ...but plainkeep.json is written to the data root (PLAINKEEP_HOME)
VERSION_FILE = BIN.parent / "VERSION"        # engine version lives with the CODE (repo root)
SCHEMA = "plainkeep.json/3"
API_VERSION = "1.0"

# display grouping (design §4.1); verbs not listed fall under "OTHER"
GROUPS = [
    ("SYSTEM", ["help", "status", "orient", "doctor", "backup", "index", "consolidate", "plugin", "complete", "ui"]),
    ("FLOW", ["capture", "triage", "start", "close", "week"]),
    ("KNOWLEDGE", ["search", "open", "wiki", "bookmark", "organize"]),
    ("TASKS", ["task"]),
    ("WORK", ["new", "repo", "archive", "files", "sweep"]),
    ("BUSINESS", ["invoice", "share"]),
    ("JOBS", ["job"]),
]

# reverse index of GROUPS (verb -> display group); anything unlisted is "OTHER"
_GROUP_OF = {v: name for name, verbs in GROUPS for v in verbs}


def group_of(verb: str) -> str:
    """The display group a verb renders under (plainkeep.json/3 field `group`). The GROUPS table above is
    the single source; verbs not listed there (incl. plugin verbs) fall under "OTHER" — matching how
    `render()` places them. Every plainkeep.json verb entry carries this so a UI groups without
    re-encoding the table."""
    return _GROUP_OF.get(verb, "OTHER")


def load_cmds() -> list[dict]:
    """Visible verbs, from the cmd.json sidecars across every root (engine bin/ + plugins/<pack>/ +
    $PLAINKEEP_PATH — Part 2.1), each tagged with `_source` ('engine' | 'plugin:<pack>'). `"hidden": true`
    verbs (e.g. __complete, an internal shell-completion helper) are omitted from the surface,
    `plainkeep help`, and plainkeep.json — but still exist on disk, so the guardrail reads their
    risk directly.
    Shadowed plugin verbs (name reserved by the engine) are already excluded by resolver.iter_cmds."""
    cmds = []
    for cmd, source in resolver.iter_cmds():
        try:
            d = json.loads(cmd.read_text(encoding="utf-8"))
            if d.get("hidden"):
                continue
            d["_built"] = (cmd.parent / "run.py").exists()
            d["_source"] = source
            cmds.append(d)
        except Exception:
            pass
    return cmds


def _engine_version() -> str:
    try:
        return VERSION_FILE.read_text(encoding="utf-8").strip()
    except Exception:
        return "0.0.0"


def _capabilities() -> dict:
    """Live capability detection (Part 1.2) — re-run on every write, nothing persisted stale."""
    def spec(mod: str) -> bool:
        try:
            return importlib.util.find_spec(mod) is not None
        except Exception:
            return False
    return {
        "vectors": spec("lancedb"),
        "rerank": spec("fastembed"),
        "agent": os.environ.get("PLAINKEEP_AGENT") or "none",
        "plugins": resolver.plugin_names(),
    }


def write_manifest() -> Path:
    """(Re)generate plainkeep.json/3 from the cmd.json sidecars (committed; rebuilt by `plainkeep index`).
    Top level carries schema/version/capabilities; each verb gains `source` (engine) and `group` (its
    display group) — the output block, hints, and optional `actions[]` grammar ride through from
    cmd.json as-is."""
    cmds = []
    for c in load_cmds():
        src = c.get("_source", "engine")
        d = {k: v for k, v in c.items() if not k.startswith("_")}
        d["source"] = src
        d["group"] = group_of(d["verb"])
        cmds.append(d)
    doc = {
        "schema": SCHEMA,
        "ops_version": _engine_version(),
        "api_version": API_VERSION,
        "json_envelope": 1,
        "capabilities": _capabilities(),
        "verbs": cmds,
    }
    vaultio.write_text(MANIFEST, json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    return MANIFEST


def render(verb: str | None = None) -> str:
    cmds = {c["verb"]: c for c in load_cmds()}
    if verb:
        c = cmds.get(verb)
        if not c:
            return f"unknown verb: {verb} (try: plainkeep help)"
        lines = [f"plainkeep {c['verb']} — {c.get('summary','')}", f"  usage: {c.get('usage','')}",
                 f"  risk:  {c.get('risk','?')}", f"  source: {c.get('_source','engine')}"]
        if c.get("args"):
            lines.append("  args:")
            for a in c["args"]:
                req = "required" if a.get("required") else f"optional (default: {a.get('default','-')})"
                lines.append(f"    {a['name']:<12} {req}")
        if c.get("hints"):
            lines.append(f"  hints: {c['hints']}")
        out_block = c.get("output")
        if out_block:
            fields = ", ".join(out_block.get("fields", {}))
            lines.append(f"  --json: {out_block.get('mode','scalar')} · fields: {fields}")
        if not c.get("_built", True):
            lines.append("  status: DESIGNED — not built yet")
        return "\n".join(lines)
    out = ["plainkeep <verb> — the personal OS command surface", ""]
    placed = set()
    for group, verbs in GROUPS:
        rows = []
        for v in verbs:
            if v in cmds:
                placed.add(v)
                c = cmds[v]
                mark = "" if c.get("_built") else "  (designed, not built)"
                rows.append(f"  plainkeep {c['verb']:<10} {c.get('summary','')}{mark}")
        if rows:
            out.append(group)
            out += rows
            out.append("")
    extra = [c for v, c in cmds.items() if v not in placed]
    other = [c for c in extra if not str(c.get("_source", "engine")).startswith("plugin:")]
    plugins = [c for c in extra if str(c.get("_source", "engine")).startswith("plugin:")]
    if other:
        out.append("OTHER")
        out += [f"  plainkeep {c['verb']:<10} {c.get('summary','')}" for c in other]
        out.append("")
    if plugins:
        out.append("PLUGINS")
        out += [f"  plainkeep {c['verb']:<10} {c.get('summary','')}  [{c.get('_source','')}]" for c in plugins]
        out.append("")
    for v, pack in resolver.shadowed():
        out.append(f"warning: plugin '{pack}' verb '{v}' is IGNORED — '{v}' is a reserved engine verb")
    out.append("`plainkeep help <verb>` for one verb. Search stages: PLAINKEEP_VECTORS=1 (LanceDB), PLAINKEEP_RERANK=1 (rerank).")
    return "\n".join(out)
