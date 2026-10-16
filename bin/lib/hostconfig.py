"""
hostconfig.py — the host config file, `<config dir>/config.toml` (v2 0.5).

Why it exists: the engine's knobs (`PLAINKEEP_VECTORS`, `PLAINKEEP_RERANK`, model tags, the Ollama
host) were environment variables only, so a non-interactive `ssh host plainkeep search …` — which
sources no shell profile — silently ran keyword-only. A file next to the vault registry is read by
every process on the machine, interactive or not.

Precedence, highest first: CLI flag → environment → this file → built-in default. A caller handles
the flag; `resolve()` answers the remaining three and says which one won, so `setup status` can
print the source.

Keys (dotted, one TOML table each):
    search.vectors  bool    ↔ PLAINKEEP_VECTORS
    search.rerank   bool    ↔ PLAINKEEP_RERANK
    models.enrich   string  ↔ PLAINKEEP_ENRICH_MODEL
    models.triage   string  ↔ PLAINKEEP_AGENT_MODEL
    ollama.host     string  ↔ OLLAMA_HOST

The file lives in `vaultreg.config_dir()` — `$PLAINKEEP_CONFIG_HOME`, else `$XDG_CONFIG_HOME/plainkeep`,
else `~/.config/plainkeep` — the same override the test seal already points at a scratch directory, so
a developer's real config never leaks into a suite and a suite never writes the real one.

Reads never raise: a missing or malformed file is an empty config. Stdlib only (`tomllib` on 3.11+;
a small reader for the flat tables above on 3.10, the project floor).
"""
from __future__ import annotations
import os
from pathlib import Path

try:
    from . import vaultreg  # type: ignore  # (namespace sibling)
except ImportError:
    import vaultreg  # type: ignore

# dotted key → (environment variable, kind)
KEYS: dict[str, tuple[str, str]] = {
    "search.vectors": ("PLAINKEEP_VECTORS", "bool"),
    "search.rerank": ("PLAINKEEP_RERANK", "bool"),
    "models.enrich": ("PLAINKEEP_ENRICH_MODEL", "str"),
    "models.triage": ("PLAINKEEP_AGENT_MODEL", "str"),
    "ollama.host": ("OLLAMA_HOST", "str"),
}
TRUE = ("1", "true", "yes", "on")


def path() -> Path:
    return vaultreg.config_dir() / "config.toml"


def _mini_toml(text: str) -> dict:
    """Flat `[table]` / `key = value` reader for Python 3.10, which has no tomllib."""
    out: dict = {}
    table = out
    for raw in text.splitlines():
        ln = raw.strip()
        if not ln or ln.startswith("#"):
            continue
        if ln.startswith("[") and ln.endswith("]"):
            table = out.setdefault(ln[1:-1].strip(), {})
            continue
        if "=" not in ln:
            continue
        k, v = (x.strip() for x in ln.split("=", 1))
        if v[:1] in ('"', "'"):
            q = v[0]
            end = v.find(q, 1)
            table[k] = v[1:end] if end > 0 else v[1:]
        else:
            v = v.split("#", 1)[0].strip()
            table[k] = True if v == "true" else False if v == "false" else v
    return out


def _parse(text: str) -> dict:
    try:
        import tomllib
        return tomllib.loads(text)
    except ImportError:
        return _mini_toml(text)


def load() -> dict:
    """The file as a flat `{"search.vectors": True, …}` dict; `{}` if absent or unreadable."""
    try:
        doc = _parse(path().read_text(encoding="utf-8"))
    except Exception:
        return {}
    flat: dict = {}
    for tbl, body in doc.items():
        if isinstance(body, dict):
            for k, v in body.items():
                flat[f"{tbl}.{k}"] = v
    return flat


def _coerce(key: str, v):
    if KEYS[key][1] == "bool":
        return v if isinstance(v, bool) else str(v).lower() in TRUE
    return str(v) if v not in (None, "") else None


def resolve(key: str, default=None) -> tuple[object, str]:
    """(effective value, source) with source one of "env", "config", "default"."""
    env_name, kind = KEYS[key]
    ev = os.environ.get(env_name)
    if ev:                                       # an exported empty string is "unset"
        return (ev.lower() in TRUE if kind == "bool" else ev), "env"
    cfg = load()
    if key in cfg:
        v = _coerce(key, cfg[key])
        if v is not None:
            return v, "config"
    return default, "default"


def get(key: str, default=None):
    return resolve(key, default)[0]


def flag(key: str) -> bool:
    return bool(get(key, False))


def ollama_host() -> str:
    return get("ollama.host", "http://localhost:11434")


def _fmt(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    return '"' + str(v).replace("\\", "\\\\").replace('"', '\\"') + '"'


def set_values(values: dict) -> Path:
    """Merge dotted `values` into the file (existing keys kept; comments are not preserved) and write it."""
    for k in values:
        if k not in KEYS:
            raise KeyError(f"unknown host-config key: {k}")
    merged = {**load(), **values}
    tables: dict[str, dict] = {}
    for k in sorted(merged):
        t, _, name = k.partition(".")
        tables.setdefault(t, {})[name] = merged[k]
    lines = ["# plainkeep host config — read by every plainkeep process on this machine.",
             "# Precedence: CLI flag > environment variable > this file > default.", ""]
    for t, body in tables.items():
        lines.append(f"[{t}]")
        lines += [f"{n} = {_fmt(v)}" for n, v in body.items()]
        lines.append("")
    p = path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("\n".join(lines), encoding="utf-8")
    return p


def redundant_env_sh() -> Path | None:
    """`env.sh` beside the config when every `export KEY=VALUE` it holds for a managed knob is already
    in the config with the same value (so it can be deleted). Never deletes anything."""
    sh = path().parent / "env.sh"
    try:
        text = sh.read_text(encoding="utf-8")
    except OSError:
        return None
    by_env = {env: (k, kind) for k, (env, kind) in KEYS.items()}
    cfg = load()
    seen = 0
    for ln in text.splitlines():
        ln = ln.strip()
        if not ln.startswith("export "):
            continue
        for part in ln[7:].split():
            name, eq, val = part.partition("=")
            if name in by_env and eq:
                k, kind = by_env[name]
                val = val.strip("\"'")
                want = val.lower() in TRUE if kind == "bool" else val
                if k not in cfg or _coerce(k, cfg[k]) != want:
                    return None
                seen += 1
    return sh if seen else None
