#!/usr/bin/env python3
"""
plainkeep plugin add <owner/repo|path>[@tag] | list | trust <name> | update <name> | remove <name>
  — the plugin distribution surface (proposal Part 2.2). A pack is a directory of verb folders
  (run.py + cmd.json, the exact engine shape) plus a `plugin.json` manifest; it installs under
  `plugins/<name>/` (user-owned, survives `script/update`) and is discovered by the resolver like any
  verb — zero new runtime.

THE TRUST MODEL (non-negotiable). A pack's self-declared risk NEVER takes effect at install: `add`
records the pack in `plugins/plugins.lock.json` as UNTRUSTED, and the guardrail caps every verb from
an untrusted pack at `confirm` (needs --yes each run). `plainkeep plugin trust <name> --yes` records the
accepted ceiling; only then does the pack's declared per-verb risk stand. Even a trusted pack keeps
the transmit-block + path-wall (classify), and a declared verb that collides with an engine name is
refused at install — the engine namespace is reserved.

add/update/remove/trust mutate external state, so each refuses without an explicit --yes (confirm);
list is read-only. `update` re-resolves the pin explicitly and refuses to cross min_ops_version — it
never auto-updates. The git-clone path exists but is not exercised by tests (local paths only).
"""
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lib import manifest, output, paths, resolver, vaultio  # noqa: E402

GREEN, RED, YEL, DIM, CYAN, RESET = "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[36m", "\033[0m"
RISK_CLASSES = ("read", "safe_write", "confirm", "deny")
RISK_ORDER = {"read": 0, "safe_write": 1, "confirm": 2, "deny": 3}
LOCK = paths.PLAINKEEP_HOME / "plugins" / "plugins.lock.json"
NAME_RE = re.compile(r"^[a-z][a-z0-9_-]*$")
REPO_RE = re.compile(r"^[\w.-]+/[\w.-]+$")


# --- version / range helpers (stdlib-only semver) ------------------------------------------------
def _semver(s) -> tuple | None:
    m = re.match(r"^(\d+)\.(\d+)\.(\d+)", str(s or ""))
    return tuple(int(x) for x in m.groups()) if m else None


def _api_ok(spec, api_version: str) -> bool:
    """Does `api_version` (e.g. '1.0') satisfy the plugin's range (e.g. '>=1,<2')?"""
    try:
        cur = float(api_version)
    except Exception:
        return False
    for part in str(spec or "").split(","):
        part = part.strip()
        m = re.match(r"^(>=|<=|==|>|<)?\s*(\d+(?:\.\d+)?)$", part)
        if not m:
            return False
        op, num = (m.group(1) or ">="), float(m.group(2))
        if not {">=": cur >= num, "<=": cur <= num, "==": cur == num,
                ">": cur > num, "<": cur < num}[op]:
            return False
    return True


# --- lockfile ------------------------------------------------------------------------------------
def _load_lock() -> dict:
    try:
        return json.loads(LOCK.read_text(encoding="utf-8"))
    except Exception:
        return {"plugins": {}}


def _save_lock(lock: dict) -> None:
    vaultio.mkdir(LOCK.parent)
    vaultio.write_text(LOCK, json.dumps(lock, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


# --- manifest validation -------------------------------------------------------------------------
def validate_manifest(data: dict, pack_dir: Path | None) -> list[str]:
    """Return a list of human-readable schema errors ([] == valid). Structural only; the
    min_ops_version + engine-collision gates are applied separately by `add`/`update`."""
    errs: list[str] = []
    if not isinstance(data, dict):
        return ["plugin.json: must be a JSON object"]
    name = data.get("name")
    if not isinstance(name, str) or not NAME_RE.match(name or ""):
        errs.append("name: required lowercase slug (^[a-z][a-z0-9_-]*$)")
    if not _semver(data.get("version")):
        errs.append("version: required semver x.y.z")
    if not _semver(data.get("min_ops_version")):
        errs.append("min_ops_version: required semver x.y.z")
    if not _api_ok(data.get("api"), manifest.API_VERSION):
        errs.append(f"api: range {data.get('api')!r} excludes this engine api {manifest.API_VERSION}")
    verbs = data.get("verbs")
    if not isinstance(verbs, list) or not verbs:
        errs.append("verbs: required non-empty list")
        verbs = []
    seen: set[str] = set()
    for i, vd in enumerate(verbs):
        if not isinstance(vd, dict):
            errs.append(f"verbs[{i}]: must be an object")
            continue
        vn = vd.get("verb")
        if not isinstance(vn, str) or not vn:
            errs.append(f"verbs[{i}].verb: required")
            continue
        if vn in seen:
            errs.append(f"verbs[{i}].verb: duplicate '{vn}'")
        seen.add(vn)
        if vd.get("risk") not in RISK_CLASSES:
            errs.append(f"verb '{vn}': risk must be one of {list(RISK_CLASSES)}")
        for fld in ("reads", "writes"):
            if fld in vd and not isinstance(vd[fld], list):
                errs.append(f"verb '{vn}': {fld} must be a list")
        if pack_dir is not None and not (pack_dir / vn / "cmd.json").exists():
            errs.append(f"verb '{vn}': declared but {vn}/cmd.json missing in the pack")
    return errs


def _max_risk(verbs: list[dict]) -> str:
    return max((v.get("risk", "read") for v in verbs), key=lambda r: RISK_ORDER.get(r, 0), default="read")


def _declared_surface(data: dict) -> list[str]:
    out = [f"  pack {CYAN}{data['name']}{RESET} v{data['version']}  (api {data.get('api')}, "
           f"needs plainkeep ≥ {data.get('min_ops_version')})"]
    for v in data["verbs"]:
        rw = ""
        if v.get("writes"):
            rw = f"  writes: {', '.join(v['writes'])}"
        out.append(f"    plainkeep {v['verb']:<14} [{v.get('risk')}]{rw}")
    return out


# --- source resolution ---------------------------------------------------------------------------
def _stage(source: str, tag: str | None):
    """Return (staging_dir, commit, is_local, err). Local path → the dir itself, commit 'local'.
    owner/repo → shallow clone into a tempdir (NOT exercised by tests; never hit in this build)."""
    local = Path(source).expanduser()
    if local.exists() and local.is_dir():
        return local.resolve(), "local", True, None
    if REPO_RE.match(source):
        tmp = Path(tempfile.mkdtemp(prefix="plainkeep-plugin-"))
        url = f"https://github.com/{source}.git"
        cmd = ["git", "clone", "--depth", "1"]
        if tag:
            cmd += ["--branch", tag]
        cmd += [url, str(tmp)]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        except Exception as e:  # pragma: no cover
            return None, None, False, f"clone failed: {e}"
        if r.returncode != 0:  # pragma: no cover
            return None, None, False, f"clone failed: {r.stderr.strip()}"
        sha = subprocess.run(["git", "-C", str(tmp), "rev-parse", "HEAD"],
                             capture_output=True, text=True).stdout.strip() or "unknown"
        return tmp, sha, False, None
    return None, None, False, f"not a local path or owner/repo: {source!r}"


def _resolve_and_validate(source: str, tag: str | None):
    """Stage + parse + schema-validate + apply the compat gates. Returns (data, staging, commit,
    is_local) or fails hard via output.fail."""
    staging, commit, is_local, err = _stage(source, tag)
    if err:
        output.fail(output.EXIT_NOT_FOUND, err, verb="plugin")
    mf = staging / "plugin.json"
    if not mf.exists():
        output.fail(output.EXIT_UNEXPECTED, f"no plugin.json in {source}", verb="plugin")
    try:
        data = json.loads(mf.read_text(encoding="utf-8"))
    except Exception as e:
        output.fail(output.EXIT_UNEXPECTED, f"plugin.json is not valid JSON: {e}", verb="plugin")
    errs = validate_manifest(data, staging)
    if errs:
        output.fail(output.EXIT_USAGE, "invalid plugin.json:\n  - " + "\n  - ".join(errs), verb="plugin")
    cur = _semver(manifest._engine_version()) or (0, 0, 0)
    if _semver(data["min_ops_version"]) > cur:
        output.fail(output.EXIT_UNEXPECTED,
                    f"pack needs plainkeep ≥ {data['min_ops_version']} but this engine is "
                    f"{manifest._engine_version()}", verb="plugin")
    collisions = [v["verb"] for v in data["verbs"] if resolver.is_engine_verb(v["verb"])]
    if collisions:
        output.fail(output.EXIT_USAGE,
                    f"declared verb(s) collide with reserved engine verbs: {', '.join(collisions)}",
                    hint="the engine namespace is reserved; a plugin can never shadow a core verb",
                    verb="plugin")
    return data, staging, commit, is_local


# --- subcommands ---------------------------------------------------------------------------------
def _needs_yes(argv, action: str) -> None:
    if "--yes" not in argv and "-y" not in argv:
        output.fail(output.EXIT_CONFIRM,
                    f"plainkeep plugin {action} is confirm-class (installs/changes external code)",
                    hint=f"re-run: plainkeep plugin {action} ... --yes", verb="plugin")


def cmd_add(argv):
    _needs_yes(argv, "add")
    positional = [a for a in argv if not a.startswith("-")]
    if not positional:
        output.fail(output.EXIT_USAGE, "usage: plainkeep plugin add <owner/repo|path>[@tag] --yes", verb="plugin")
    spec = positional[0]
    source, tag = (spec.split("@", 1) + [None])[:2] if "@" in spec else (spec, None)

    data, staging, commit, is_local = _resolve_and_validate(source, tag)
    name = data["name"]
    dest = paths.PLAINKEEP_HOME / "plugins" / name
    if dest.exists():
        output.fail(output.EXIT_USAGE,
                    f"pack '{name}' is already installed — `plainkeep plugin update {name} --yes` or "
                    f"`plainkeep plugin remove {name} --yes` first", verb="plugin")
    vaultio.mkdir(dest.parent)
    vaultio.copytree(staging, dest, ignore=shutil.ignore_patterns(".git"))
    if not is_local:  # clone tempdir is disposable once copied
        shutil.rmtree(staging, ignore_errors=True)

    lock = _load_lock()
    lock.setdefault("plugins", {})[name] = {
        "source": source, "ref": tag, "commit": commit,
        "version": data["version"], "min_ops_version": data["min_ops_version"],
        "api": data.get("api"),
        "verbs": [{"verb": v["verb"], "risk": v.get("risk"),
                   "reads": v.get("reads", []), "writes": v.get("writes", [])} for v in data["verbs"]],
        "trusted": False, "accepted_ceiling": None,
    }
    _save_lock(lock)

    def render(_):
        print(f"{GREEN}installed{RESET} pack '{name}' -> plugins/{name}/  (commit {commit})")
        print("\n".join(_declared_surface(data)))
        print(f"\n{YEL}untrusted{RESET} — every verb is capped at confirm until you run:\n"
              f"  plainkeep plugin trust {name} --yes")

    return output.emit({"name": name, "version": data["version"], "commit": commit,
                        "verbs": [v["verb"] for v in data["verbs"]], "trusted": False,
                        "source": source}, "plugin", human=render)


def cmd_list(argv):
    lock = _load_lock().get("plugins", {})
    rows = []
    for name in sorted(lock):
        e = lock[name]
        rows.append({"name": name, "version": e.get("version"), "trusted": bool(e.get("trusted")),
                     "verbs": [v["verb"] for v in e.get("verbs", [])],
                     "source": e.get("source"), "commit": e.get("commit")})

    def render(rs):
        if not rs:
            return "no plugins installed (add one: `plainkeep plugin add <path> --yes`)."
        out = [f"{len(rs)} plugin pack(s):"]
        for r in rs:
            state = f"{GREEN}trusted{RESET}" if r["trusted"] else f"{YEL}untrusted{RESET}"
            out.append(f"  {r['name']:<18} v{r['version']:<8} {state}  "
                       f"{DIM}{', '.join('plainkeep '+v for v in r['verbs'])}{RESET}")
        return "\n".join(out)

    return output.emit_rows(rows, "plugin", human=render)


def cmd_trust(argv):
    _needs_yes(argv, "trust")
    names = [a for a in argv if not a.startswith("-")]
    if not names:
        output.fail(output.EXIT_USAGE, "usage: plainkeep plugin trust <name> --yes", verb="plugin")
    name = names[0]
    lock = _load_lock()
    entry = lock.get("plugins", {}).get(name)
    if entry is None:
        output.fail(output.EXIT_NOT_FOUND, f"no installed pack '{name}' (see `plainkeep plugin list`)", verb="plugin")
    ceiling = _max_risk(entry.get("verbs", []))
    entry["trusted"] = True
    entry["accepted_ceiling"] = ceiling
    _save_lock(lock)
    return output.emit({"name": name, "trusted": True, "accepted_ceiling": ceiling}, "plugin",
                       human=lambda _: f"{GREEN}trusted{RESET} '{name}' — its verbs now run at their "
                       f"declared risk (accepted ceiling: {ceiling}). Transmit-block + path-wall still apply.")


def cmd_update(argv):
    _needs_yes(argv, "update")
    names = [a for a in argv if not a.startswith("-")]
    if not names:
        output.fail(output.EXIT_USAGE, "usage: plainkeep plugin update <name> --yes", verb="plugin")
    name = names[0]
    lock = _load_lock()
    entry = lock.get("plugins", {}).get(name)
    if entry is None:
        output.fail(output.EXIT_NOT_FOUND, f"no installed pack '{name}' (see `plainkeep plugin list`)", verb="plugin")
    source, tag = entry.get("source"), entry.get("ref")
    data, staging, commit, is_local = _resolve_and_validate(source, tag)  # re-resolves the pin explicitly
    if data["name"] != name:
        output.fail(output.EXIT_USAGE,
                    f"source now declares name '{data['name']}', not '{name}' — refuse to update", verb="plugin")
    dest = paths.PLAINKEEP_HOME / "plugins" / name
    shutil.rmtree(dest, ignore_errors=True)
    vaultio.copytree(staging, dest, ignore=shutil.ignore_patterns(".git"))
    if not is_local:
        shutil.rmtree(staging, ignore_errors=True)

    new_max = _max_risk([{"risk": v.get("risk")} for v in data["verbs"]])
    accepted = entry.get("accepted_ceiling")
    # if the risk surface grew beyond what was accepted, trust is revoked — re-consent required.
    retrust = bool(entry.get("trusted")) and (accepted is None or
                                              RISK_ORDER.get(new_max, 0) > RISK_ORDER.get(accepted, 0))
    entry.update({
        "commit": commit, "version": data["version"], "min_ops_version": data["min_ops_version"],
        "api": data.get("api"),
        "verbs": [{"verb": v["verb"], "risk": v.get("risk"),
                   "reads": v.get("reads", []), "writes": v.get("writes", [])} for v in data["verbs"]],
    })
    if retrust:
        entry["trusted"] = False
        entry["accepted_ceiling"] = None
    _save_lock(lock)
    return output.emit({"name": name, "version": data["version"], "commit": commit,
                        "trusted": bool(entry["trusted"]), "retrust_required": retrust}, "plugin",
                       human=lambda _: f"{GREEN}updated{RESET} '{name}' to v{data['version']} (commit {commit})"
                       + (f"\n{YEL}risk surface grew — re-run `plainkeep plugin trust {name} --yes`{RESET}" if retrust else ""))


def cmd_remove(argv):
    _needs_yes(argv, "remove")
    names = [a for a in argv if not a.startswith("-")]
    if not names:
        output.fail(output.EXIT_USAGE, "usage: plainkeep plugin remove <name> --yes", verb="plugin")
    name = names[0]
    lock = _load_lock()
    entry = lock.get("plugins", {}).get(name)
    dest = paths.PLAINKEEP_HOME / "plugins" / name
    if entry is None and not dest.exists():
        output.fail(output.EXIT_NOT_FOUND, f"no installed pack '{name}'", verb="plugin")
    shutil.rmtree(dest, ignore_errors=True)
    if entry is not None:
        del lock["plugins"][name]
        _save_lock(lock)
    return output.emit({"name": name, "removed": True}, "plugin",
                       human=lambda _: f"{GREEN}removed{RESET} pack '{name}' (dir + lock entry)")


def main(argv):
    _, argv = output.parse_argv(argv)
    action = argv[0] if argv else "list"
    rest = argv[1:]
    if action == "add":
        return cmd_add(rest)
    if action == "list":
        return cmd_list(rest)
    if action == "trust":
        return cmd_trust(rest)
    if action == "update":
        return cmd_update(rest)
    if action == "remove":
        return cmd_remove(rest)
    output.fail(output.EXIT_USAGE,
                "usage: plainkeep plugin add <owner/repo|path>[@tag] --yes | list | trust <name> --yes | "
                "update <name> --yes | remove <name> --yes", verb="plugin")


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
