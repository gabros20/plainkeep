#!/usr/bin/env python3
"""
plainkeep plugin add <owner/repo|path>[@tag] | list | trust <name> | update <name> | remove <name>
                 | sync [<name>]
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

add/update/remove/trust/sync mutate external state, so each refuses without an explicit --yes
(confirm); list is read-only. `update` re-resolves the pin explicitly and refuses to cross
min_ops_version — it never auto-updates. The git-clone path exists but is not exercised by tests
(local paths only).

THE DEPENDENCY CONTRACT (Phase 2 Task 3, ADR-018). A pack may DECLARE third-party requirements in
`plugin.json`; `sync` installs them into `plugins/.deps/`, which both dispatchers prepend to a plugin
spawn's PYTHONPATH. Declared, never inferred from imports. Recorded in the lockfile, so the overlay
is rebuilt from what the user CONSENTED to rather than from whatever the pack ships today — and new
declarations revoke trust for the same reason a grown risk surface does. The overlay is vault-local,
so an engine update (which replaces the engine tree wholesale) re-applies it by construction; the one
thing that invalidates it is the INTERPRETER changing, which `doctor` watches.
"""
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lib import manifest, output, paths, pluginenv, resolver, vaultio  # noqa: E402

GREEN, RED, YEL, DIM, CYAN, RESET = "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[36m", "\033[0m"
RISK_CLASSES = ("read", "safe_write", "confirm", "deny")
RISK_ORDER = {"read": 0, "safe_write": 1, "confirm": 2, "deny": 3}
LOCK = paths.PLAINKEEP_HOME / "plugins" / "plugins.lock.json"
NAME_RE = re.compile(r"^[a-z][a-z0-9_-]*$")
REPO_RE = re.compile(r"^[\w.-]+/[\w.-]+$")

# THE DEPENDENCY CONTRACT (Phase 2 Task 3, ADR-018). A pack DECLARES what it imports from outside the
# stdlib and the SDK; nothing is ever inferred from its source. `sync` installs the declarations into
# the vault's own overlay (`plugins/.deps/`), which both dispatchers put on a plugin spawn's
# PYTHONPATH — so the deps live with the VAULT and an engine update, which replaces the engine tree
# wholesale, re-applies them by construction rather than by remembering to.
#
# The grammar is a deliberately SMALL subset of PEP 508: a name, optional extras, optional version
# specifiers. Whitespace is stripped before matching, so `httpx >= 0.27` is accepted as `httpx>=0.27`.
#
# WHAT IT REFUSES IS THE POINT. These strings become pip's argv. Anything that could be read as a
# FLAG (`--index-url=…`, `-r reqs.txt`), a URL, a local path (`.`, `./evil`) or an environment marker
# is rejected at `add`, before the pack is installed — a pack that can steer pip can install from
# anywhere, and `plugin add` is the consent gate that has to hold. `--` is additionally passed to pip
# ahead of the requirements, so a leading dash could not smuggle through even if this loosened.
DEP_RE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._-]*"                       # distribution name
    r"(\[[A-Za-z0-9][A-Za-z0-9._,-]*\])?"                # extras
    r"((==|>=|<=|~=|!=|<|>)[A-Za-z0-9][A-Za-z0-9.*+!_-]*"
    r"(,(==|>=|<=|~=|!=|<|>)[A-Za-z0-9][A-Za-z0-9.*+!_-]*)*)?$"
)


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


# --- the dependency contract ---------------------------------------------------------------------
def normalize_dep(raw) -> str:
    """A declaration with every whitespace character removed. Matching happens on this form, and this
    form is what is recorded and handed to pip — so what the lockfile shows is what was installed."""
    return "".join(str(raw).split())


def dependency_errors(deps) -> list[str]:
    """Schema errors for the OPTIONAL `dependencies` array ([] == valid, absent == valid)."""
    if deps is None:
        return []
    if not isinstance(deps, list):
        return ["dependencies: must be a list of requirement strings"]
    errs: list[str] = []
    for i, d in enumerate(deps):
        if not isinstance(d, str) or not d.strip():
            errs.append(f"dependencies[{i}]: must be a non-empty string")
            continue
        norm = normalize_dep(d)
        if not DEP_RE.match(norm):
            errs.append(
                f"dependencies[{i}]: {d!r} is not a plain name[extras][version specifiers] "
                f"requirement — flags, URLs, local paths and environment markers are refused")
    return errs


def declared_deps(data: dict) -> list[str]:
    return [normalize_dep(d) for d in data.get("dependencies") or []]


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
    errs.extend(dependency_errors(data.get("dependencies")))
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

    # MIGRATION PREFLIGHT (ADR-018 D2), said at the one moment the pack can still be looked at. A
    # verb directory that ships its own top-level `lib` shadows the SDK, because `sys.path[0]` is the
    # verb's own directory and beats the PYTHONPATH the dispatcher injects — the reverse of what the
    # pre-Task-2 `sys.path.insert(0, …)` scaffold did. stderr, so it survives `--json` too, and a
    # WARNING rather than a refusal: shipping a `lib` is legal, being silent about it is not.
    for pack, verb in pluginenv.sdk_shadows(paths.PLAINKEEP_HOME):
        if pack == name:
            sys.stderr.write(
                f"plainkeep plugin: warning — {name}/{verb}/ ships a top-level `lib`, which SHADOWS "
                f"the plainkeep SDK for that verb (sys.path[0] is the verb's own directory and beats "
                f"PYTHONPATH). Rename it, or that verb's `from lib import api` is not the engine's.\n")

    lock = _load_lock()
    lock.setdefault("plugins", {})[name] = {
        "source": source, "ref": tag, "commit": commit,
        "version": data["version"], "min_ops_version": data["min_ops_version"],
        "api": data.get("api"),
        "verbs": [{"verb": v["verb"], "risk": v.get("risk"),
                   "reads": v.get("reads", []), "writes": v.get("writes", [])} for v in data["verbs"]],
        # DECLARED, never inferred. Recorded here so the overlay can be rebuilt from the LOCK rather
        # than from whatever the pack's manifest says today, and so `plugin list` can show it.
        "dependencies": declared_deps(data),
        "trusted": False, "accepted_ceiling": None,
    }
    _save_lock(lock)

    deps = declared_deps(data)

    def render(_):
        print(f"{GREEN}installed{RESET} pack '{name}' -> plugins/{name}/  (commit {commit})")
        print("\n".join(_declared_surface(data)))
        if deps:
            print(f"    declares {len(deps)} dependenc(ies): {', '.join(deps)}\n"
                  f"    install them into this vault's overlay: plainkeep plugin sync {name} --yes")
        print(f"\n{YEL}untrusted{RESET} — every verb is capped at confirm until you run:\n"
              f"  plainkeep plugin trust {name} --yes")

    return output.emit({"name": name, "version": data["version"], "commit": commit,
                        "verbs": [v["verb"] for v in data["verbs"]], "trusted": False,
                        "dependencies": deps, "source": source}, "plugin", human=render)


def cmd_list(argv):
    lock = _load_lock().get("plugins", {})
    rows = []
    for name in sorted(lock):
        e = lock[name]
        rows.append({"name": name, "version": e.get("version"), "trusted": bool(e.get("trusted")),
                     "verbs": [v["verb"] for v in e.get("verbs", [])],
                     "source": e.get("source"), "commit": e.get("commit"),
                     "dependencies": e.get("dependencies") or []})

    def render(rs):
        if not rs:
            return "no plugins installed (add one: `plainkeep plugin add <path> --yes`)."
        out = [f"{len(rs)} plugin pack(s):"]
        for r in rs:
            state = f"{GREEN}trusted{RESET}" if r["trusted"] else f"{YEL}untrusted{RESET}"
            out.append(f"  {r['name']:<18} v{r['version']:<8} {state}  "
                       f"{DIM}{', '.join('plainkeep '+v for v in r['verbs'])}{RESET}"
                       + (f"\n{' ' * 20}{DIM}deps: {', '.join(r['dependencies'])}{RESET}"
                          if r["dependencies"] else ""))
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
    # NEW DEPENDENCIES ARE A RISK-SURFACE GROWTH, and are treated as one. A declaration is an
    # instruction to install third-party code into the vault's overlay and put it on the path of
    # every verb the pack ships; a trusted pack that could add one on `update` would be installing
    # arbitrary code under a consent the user gave for something smaller. Dropping or keeping
    # declarations is not a growth and does not revoke.
    new_deps = sorted(set(declared_deps(data)) - set(entry.get("dependencies") or []))
    # if the risk surface grew beyond what was accepted, trust is revoked — re-consent required.
    retrust = bool(entry.get("trusted")) and (accepted is None or bool(new_deps) or
                                              RISK_ORDER.get(new_max, 0) > RISK_ORDER.get(accepted, 0))
    entry.update({
        "commit": commit, "version": data["version"], "min_ops_version": data["min_ops_version"],
        "api": data.get("api"),
        "verbs": [{"verb": v["verb"], "risk": v.get("risk"),
                   "reads": v.get("reads", []), "writes": v.get("writes", [])} for v in data["verbs"]],
        "dependencies": declared_deps(data),
    })
    if retrust:
        entry["trusted"] = False
        entry["accepted_ceiling"] = None
    _save_lock(lock)
    return output.emit({"name": name, "version": data["version"], "commit": commit,
                        "trusted": bool(entry["trusted"]), "retrust_required": retrust,
                        "new_dependencies": new_deps}, "plugin",
                       human=lambda _: f"{GREEN}updated{RESET} '{name}' to v{data['version']} (commit {commit})"
                       + (f"\n{YEL}new dependencies declared: {', '.join(new_deps)} — "
                          f"install them with `plainkeep plugin sync {name} --yes`{RESET}" if new_deps else "")
                       + (f"\n{YEL}risk surface grew — re-run `plainkeep plugin trust {name} --yes`{RESET}" if retrust else ""))


def _pip_argv(target: Path, reqs: list[str], extra: list[str]) -> list[str]:
    """The pip invocation, as one function so a test can pin the argv without running an install.

    `sys.executable` is the interpreter the DISPATCHER picked for this verb — the vault `.venv` when
    there is one, bare `python3` otherwise — which is the same interpreter a plugin verb will be
    spawned with. Installing for any other one would produce an overlay the plugin cannot import.

    `--target` rather than a venv: the overlay is a plain directory the dispatcher prepends to
    PYTHONPATH, so it OVERLAYS whatever interpreter environment the engine happens to bring instead
    of replacing it. `--` ends pip's option parsing, so a requirement can never be read as a flag.
    """
    return [sys.executable, "-m", "pip", "install", "--upgrade", "--target", str(target),
            "--no-input", "--disable-pip-version-check", *extra, "--", *reqs]


def cmd_sync(argv):
    """plainkeep plugin sync [<name>] --yes [--pip-arg=<flag>]… — build the vault's dependency overlay.

    Confirm-class for the same reason `add` is: it installs third-party code. It is idempotent, it
    reads the LOCK rather than the packs' manifests (the lock is what the user consented to), and it
    is the ONE thing that has to be re-run when the interpreter changes — never when the ENGINE
    changes, because the overlay lives in the vault and the engine tree is not where it was installed.
    """
    _needs_yes(argv, "sync")
    names = [a for a in argv if not a.startswith("-")]
    extra = [a.split("=", 1)[1] for a in argv if a.startswith("--pip-arg=")]
    lock = _load_lock()
    plugins = lock.get("plugins", {})
    if names:
        if names[0] not in plugins:
            output.fail(output.EXIT_NOT_FOUND, f"no installed pack '{names[0]}' (see `plainkeep plugin list`)",
                        verb="plugin")
        selected = {names[0]: plugins[names[0]]}
    else:
        selected = plugins

    reqs: list[str] = []
    per_pack: dict[str, list[str]] = {}
    for name in sorted(selected):
        deps = [normalize_dep(d) for d in selected[name].get("dependencies") or []]
        bad = dependency_errors(deps)
        if bad:
            # A lockfile edited by hand can hold anything. It is read on the way IN to pip's argv, so
            # it is validated on the way in too, with the same grammar `add` applied.
            output.fail(output.EXIT_USAGE, f"pack '{name}' has an invalid recorded dependency:\n  - "
                        + "\n  - ".join(bad), verb="plugin")
        per_pack[name] = deps
        for d in deps:
            if d not in reqs:
                reqs.append(d)

    target = pluginenv.deps_dir(paths.PLAINKEEP_HOME)
    pyver = f"{sys.version_info.major}.{sys.version_info.minor}"
    installed = False
    if reqs:
        vaultio.mkdir(target)
        cmd = _pip_argv(target, reqs, extra)
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            output.fail(output.EXIT_UNEXPECTED,
                        f"pip failed installing the overlay ({' '.join(reqs)}):\n"
                        + (r.stderr.strip() or r.stdout.strip())[-1200:], verb="plugin")
        installed = True
        # THE ONE NAME THE OVERLAY MAY NOT SHIP. It sits AHEAD of the engine on PYTHONPATH (so a
        # declared dependency beats the engine tree's incidental top-level names), which means a
        # distribution that lands a top-level `lib` would shadow the SDK itself and every plugin in
        # this vault would import the wrong thing — silently, since the wrong `lib` may well have an
        # `api`. Refused loudly rather than papered over by reordering, because reordering would hand
        # the same silent shadowing to `models`, `files` and `index` instead.
        if (target / "lib").exists() or (target / "lib.py").exists():
            output.fail(output.EXIT_DENY,
                        f"the overlay now contains a top-level `lib`, which SHADOWS the plainkeep SDK "
                        f"for every plugin in this vault",
                        hint=f"remove {target}/lib and drop the dependency that brought it in — `lib` "
                             f"is a reserved top-level name for plugin dependencies", verb="plugin")

    lock["overlay"] = {"python": pyver, "packs": per_pack}
    _save_lock(lock)

    def render(_):
        if not reqs:
            return (f"no declared dependencies in {len(selected)} pack(s) — overlay not needed "
                    f"(packs declare them in plugin.json's `dependencies`)")
        return (f"{GREEN}synced{RESET} {len(reqs)} dependenc(ies) into plugins/{pluginenv.DEPS_DIRNAME}/ "
                f"for python {pyver}:\n  " + "\n  ".join(reqs))

    return output.emit({"packs": sorted(selected), "requirements": reqs, "python": pyver,
                        "target": str(target), "installed": installed}, "plugin", human=render)


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
    if action == "sync":
        return cmd_sync(rest)
    output.fail(output.EXIT_USAGE,
                "usage: plainkeep plugin add <owner/repo|path>[@tag] --yes | list | trust <name> --yes | "
                "update <name> --yes | remove <name> --yes | sync [<name>] --yes", verb="plugin")


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
