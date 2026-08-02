"""Shared setup layer registry for `plainkeep setup` and `plainkeep doctor`.

This module is deliberately verb-agnostic. It may invoke sibling verbs by path, but it does not
import their `run.py` modules, so setup status can be reused without cross-verb import side effects.
"""
from __future__ import annotations
from dataclasses import dataclass
import hashlib
import importlib.util
import os
import platform
import re
import shutil
import subprocess
import sys

from lib import embed, enginetree, enrichlib, imagelib, paths, vaultio

# The ENGINE's bin/ (paths.BIN) — this module reads engine-owned files through it, `bin/ui/version.txt`
# above all: the pin `plainkeep setup ui` downloads against and compares the installed binary to.
BIN = paths.BIN
# What a DATA vault must contain. `bin` and `skills` left this list in Phase 2 Task 2: both are
# engine-owned, and demanding them of a vault is the "engine lives in the vault" assumption wearing a
# readiness check. It was not inert — `plainkeep doctor --init` CREATED both, so every vault grew an
# empty `bin/` and `skills/` that nothing would ever put anything into, and a data-only vault (the
# shape Task 5's `init` produces) reported not-ready for lacking a copy of the engine.
REQUIRED_DIRS = ["wiki", "tasks/inbox", "tasks/active", "tasks/waiting", "tasks/done",
                 "journal", "inbox", "templates", "jobs"]
# Layer status enum (documented in docs/machine-contract.md §7 and docs/setup.md). `not_applicable`
# (Task 8) means the layer cannot apply on THIS host — e.g. launchd scheduling off macOS — and is
# advisory-only: it never fails `plainkeep doctor` and never causes a nonzero `--all` exit.
STATUSES = {"ready", "partial", "absent", "blocked", "not_applicable"}

# The SEARCH-ONLY dependency set (ADR-009): a strict subset of requirements.txt, isolated in
# $PLAINKEEP_HOME/.venv so the retrieval planes never drag in the file-processing packages (Pillow /
# trafilatura / mlx-vlm) that belong to the `models` layer. requirements-search.txt is the source of
# truth when present (the committed, human-followable install story); this inline mirror is the
# fallback for a lean vault that dropped the file. Env markers ride through pip on the command line.
SEARCH_DEPS = [
    'lancedb>=0.25,<0.26 ; platform_system == "Darwin" and platform_machine == "x86_64"',
    'lancedb>=0.33 ; platform_system != "Darwin" or platform_machine != "x86_64"',
    "fastembed>=0.4",
]


@dataclass(frozen=True)
class Layer:
    id: str
    title: str
    why: str
    required: bool
    gate: str
    handoff: str = ""


LAYERS: list[Layer] = [
    Layer("skeleton", "Vault structure", "Required folders and Obsidian seed files", True, "safe_write"),
    Layer("search", "Semantic search", "Vector index dependencies and embedding model", False, "confirm"),
    Layer("backups", "Durability", "Encrypted off-machine backup configuration", False, "blocked", "plainkeep backup init"),
    Layer("models", "File-processing / LLM", "Local models and optional file-processing runtimes", False, "confirm"),
    Layer("automation", "Schedules", "Rendered launchd job plists", False, "safe_write"),
    Layer("ui", "Terminal UI", "The guided plainkeep-ui binary for humans (`plainkeep ui`)", False, "confirm"),
]


def _truthy(v: str | None) -> bool:
    return (v or "").strip().lower() in ("1", "true", "yes", "on")


def _fake(fake: bool = False) -> bool:
    return fake or _truthy(os.environ.get("PLAINKEEP_SETUP_FAKE"))


def _has(mod: str) -> bool:
    try:
        return importlib.util.find_spec(mod) is not None
    except Exception:
        return False


def _ollama_has(model: str) -> bool:
    """Probe `ollama list` for a model tag.

    This intentionally copies the small probe from `bin/models/run.py` instead of importing that verb:
    setup is shared library code and must avoid cross-verb imports.
    """
    if not shutil.which("ollama"):
        return False
    try:
        r = subprocess.run(["ollama", "list"], capture_output=True, text=True, timeout=10)
    except Exception:
        return False
    if r.returncode != 0:
        return False
    base = model.split(":")[0]
    for ln in r.stdout.strip().splitlines()[1:]:
        if not ln.strip():
            continue
        tag = ln.split()[0]
        if tag == model or tag.split(":")[0] == base:
            return True
    return False


def _embed_model() -> str:
    return os.environ.get("PLAINKEEP_EMBED_MODEL", embed.model_name())


def _ollama_present() -> bool:
    """Is the ollama binary on PATH? (The external prerequisite for pulling any local model.)

    `PLAINKEEP_ASSUME_OLLAMA` (truthy) forces this True — an install/test seam mirroring `PLAINKEEP_SETUP_FAKE`:
    it lets a host-independent test exercise the confirm/attemptable path for the ollama-gated layers
    (search/models) on a machine or CI runner that has no ollama, and lets a user whose ollama lives
    on a nonstandard PATH opt out of the `blocked` gate."""
    if _truthy(os.environ.get("PLAINKEEP_ASSUME_OLLAMA")):
        return True
    return shutil.which("ollama") is not None


def _venv_python() -> "os.PathLike[str] | str":
    """The optional venv's interpreter path (may not exist yet)."""
    return paths.PLAINKEEP_HOME / ".venv" / "bin" / "python3"


def _usable_venv_python() -> str | None:
    """The ONE "is the venv interpreter actually usable" probe, shared by the dispatcher's interpreter
    choice, `_search_interpreter`, and the create-if-missing logic (FIX 3). Returns the path ONLY if
    `$PLAINKEEP_HOME/.venv/bin/python3` exists AND actually STARTS — mirroring the dispatcher's `-x`+start
    probe. A half-built or ABI-broken venv (dir/symlink present but python won't run) returns None so
    callers REPAIR it rather than trusting an existing `.venv` dir as complete. Never raises."""
    vp = _venv_python()
    try:
        r = subprocess.run([str(vp), "-c", ""], capture_output=True, timeout=10)
        return str(vp) if r.returncode == 0 else None
    except Exception:
        return None


def _ensure_venv(res: dict, *, fake: bool) -> None:
    """Provision `$PLAINKEEP_HOME/.venv` as the single home for ALL optional deps (search + models, ADR-009 /
    FIX 2). Idempotent: a usable venv is left untouched; a MISSING or half-built/broken one is
    (re)created via the same start-probe as the dispatcher (FIX 3), so a partial `.venv` dir can't wedge
    a later `_venv_pip`. Records the create command in `res['ran']`. In fake/dry mode it only previews
    the create (records the string, runs nothing)."""
    venv = paths.PLAINKEEP_HOME / ".venv"
    if _fake(fake):
        res["ran"].append(_run([sys.executable, "-m", "venv", str(venv)], fake=fake))
        return
    if _usable_venv_python() is not None:
        return
    # Missing, or present-but-unstartable (stale symlink / ABI break): clear any partial tree so the
    # create can't trip on it, then build fresh. .venv is a disposable, rebuildable cache (ADR-009).
    if venv.exists():
        shutil.rmtree(venv, ignore_errors=True)
    res["ran"].append(_run([sys.executable, "-m", "venv", str(venv)], fake=fake))


def _search_interpreter() -> str:
    """The interpreter the dispatcher would pick for verbs: the repo-local .venv python when it exists
    AND starts (ADR-009 / FIX 3), else whichever python3 is running us. What `deps-importable` must
    probe."""
    return _usable_venv_python() or sys.executable


def _deps_importable() -> bool:
    """OPERATIONAL probe (Task 10): can the dispatcher-selected interpreter actually import BOTH
    vector-plane deps? A file being pip-installed isn't enough — `plainkeep index` imports through the
    venv, so we import through the same interpreter. Never raises."""
    interp = _search_interpreter()
    try:
        r = subprocess.run([interp, "-c", "import lancedb, fastembed"],
                           capture_output=True, timeout=30)
        return r.returncode == 0
    except Exception:
        return False


def _index_built() -> bool:
    return (paths.PLAINKEEP_HOME / ".index" / "plainkeep.sqlite").exists()


def _search_pip_args() -> list[str]:
    """`pip install` args for the search-only set: `-r requirements-search.txt` when the committed
    file is present, else the inline SEARCH_DEPS mirror (lean vault)."""
    req = paths.PLAINKEEP_HOME / "requirements-search.txt"
    if req.exists():
        return ["-r", str(req)]
    return list(SEARCH_DEPS)


def _platform_system() -> str:
    return platform.system()


def _platform_machine() -> str:
    return platform.machine()


def _run(cmd: list[str], *, fake: bool = False) -> str:
    display = " ".join(cmd)
    if _fake(fake):
        return display
    subprocess.run(cmd, check=True)
    return display


def _run_verb(verb: str, *args: str, fake: bool = False) -> str:
    """Invoke a sibling verb through the DISPATCHER (Task 9, "one door") — not `bin/<verb>/run.py`
    directly — so doctor/models/job/index re-enter the guardrail + resolver + logs like any other
    caller. Non-recursive (none of these verbs call setup). PLAINKEEP_SETUP_FAKE keeps a dry-run inert
    (the display string is recorded, nothing runs). The dispatcher itself prefers the .venv python
    (ADR-009), so a re-entered `plainkeep index` sees the vector deps with no PATH surgery here."""
    return _run([str(enginetree.launcher()), verb, *args], fake=fake)


def _venv_pip(*pkgs_or_reqs: str, fake: bool = False) -> str:
    """`pip install` into the optional venv specifically (Task 10 / FIX 2) — the isolated interpreter
    the dispatcher prefers, never the caller's site-packages. ALL optional deps (search + models) land
    here, so the stdlib floor stays clean and the dispatcher sees every optional dep consistently."""
    return _run([str(_venv_python()), "-m", "pip", "install", *pkgs_or_reqs], fake=fake)


def _layer(layer_id: str) -> Layer:
    for layer in LAYERS:
        if layer.id == layer_id:
            return layer
    raise ValueError(f"unknown layer '{layer_id}' (one of: {', '.join(l.id for l in LAYERS)})")


def _items_status(items: list[dict], *, optional: bool = True) -> str:
    oks = [bool(i.get("ok")) for i in items]
    if all(oks):
        return "ready"
    if any(oks):
        return "partial"
    return "absent" if optional else "partial"


def _row(layer: Layer, state: str, detail: str, items: list[dict], next_: str = "") -> dict:
    return {"id": layer.id, "title": layer.title, "status": state, "required": layer.required,
            "detail": detail, "items": items, "next": next_}


def _status_skeleton(layer: Layer) -> dict:
    # Required-readiness depends ONLY on REQUIRED_DIRS. The .obsidian/* config-pack items stay
    # visible in the row (so `plainkeep setup` shows their state) but are advisory: they report ok/not-ok
    # and never flip this required layer to non-ready (a fresh clone that hasn't seeded .obsidian/
    # must still let `plainkeep doctor` pass). Seeding is handled by `plainkeep doctor --init`.
    required = [{"id": str(rel), "title": str(rel), "ok": (paths.PLAINKEEP_HOME / rel).is_dir()} for rel in REQUIRED_DIRS]
    items = list(required)
    pack = paths.PLAINKEEP_HOME / "templates" / "obsidian"
    if pack.is_dir():
        for src in sorted(pack.glob("*.json")):
            rel = f".obsidian/{src.name}"
            items.append({"id": rel, "title": rel, "advisory": True,
                          "ok": (paths.PLAINKEEP_HOME / ".obsidian" / src.name).is_file()})
    state = _items_status(required, optional=False)
    detail = "vault skeleton ready" if state == "ready" else "required vault structure is incomplete"
    return _row(layer, state, detail, items, "plainkeep setup skeleton" if state != "ready" else "")


def _status_search(layer: Layer) -> dict:
    """OPERATIONAL readiness (Task 10): "ready" means search actually works end to end, not merely
    that a wheel is on disk. Core = deps import through the dispatcher's interpreter + the embed model
    is pulled + the index is built; PLAINKEEP_VECTORS/PLAINKEEP_RERANK are advisory (surfaced as a handoff if
    unset, never blocking). ollama is the hard external prerequisite — absent, the layer is `blocked`
    with the exact install command (Task 8), because `advance` would otherwise crash pulling the
    model."""
    model = _embed_model()
    deps = _deps_importable()
    model_pulled = _ollama_has(model)
    index_built = _index_built()
    vectors_env = _truthy(os.environ.get("PLAINKEEP_VECTORS"))
    rerank_env = _truthy(os.environ.get("PLAINKEEP_RERANK"))
    items = [
        {"id": "deps-importable", "title": "lancedb + fastembed importable", "ok": deps},
        {"id": "model-pulled", "title": model, "ok": model_pulled},
        {"id": "index-built", "title": ".index/plainkeep.sqlite", "ok": index_built},
        {"id": "PLAINKEEP_VECTORS", "title": "PLAINKEEP_VECTORS=1", "ok": vectors_env, "advisory": True},
        {"id": "PLAINKEEP_RERANK", "title": "PLAINKEEP_RERANK=1", "ok": rerank_env, "advisory": True},
    ]
    if not _ollama_present():
        return _row(layer, "blocked", "ollama is required to pull the embedding model", items,
                    "install ollama: https://ollama.com")
    core = [deps, model_pulled, index_built]
    if all(core):
        # Operational; nudge the advisory env flags so retrieval actually uses the vector/rerank arms.
        nxt = "" if (vectors_env and rerank_env) else "export PLAINKEEP_VECTORS=1 PLAINKEEP_RERANK=1"
        detail = "semantic search ready" if (vectors_env and rerank_env) else \
            "semantic search operational (set PLAINKEEP_VECTORS=1 / PLAINKEEP_RERANK=1 to enable the arms)"
        return _row(layer, "ready", detail, items, nxt)
    state = "partial" if any(core) else "absent"
    return _row(layer, state, "semantic search prerequisites are incomplete", items, "plainkeep setup search --yes")


def _status_backups(layer: Layer) -> dict:
    config_ok = (paths.PLAINKEEP_HOME / ".backup" / "config.json").exists()
    restic_ok = shutil.which("restic") is not None
    items = [
        {"id": "config", "title": ".backup/config.json", "ok": config_ok},
        {"id": "restic", "title": "restic", "ok": restic_ok},
    ]
    if config_ok and restic_ok:
        return _row(layer, "ready", "backup configuration ready", items)
    # The external binary is the hard prerequisite: name the exact install command (Task 8) rather
    # than deferring to the generic init handoff, which can't proceed without restic anyway.
    if not restic_ok:
        return _row(layer, "blocked", "restic is required for encrypted off-machine backups", items,
                    "install restic: brew install restic")
    return _row(layer, "blocked", "backup setup needs human initialization", items, layer.handoff)


def _status_models(layer: Layer) -> dict:
    vlm_model = (os.environ.get("PLAINKEEP_VLM", "qwen3-vl:4b") or "qwen3-vl:4b").strip()
    vlm_ok = False if vlm_model.lower() == "none" else (imagelib._has_mlx() or (_ollama_has(vlm_model) if imagelib._has_ollama() else False))
    items = [
        {"id": "enrich", "title": enrichlib.DEFAULT_MODEL, "ok": _ollama_has(enrichlib.DEFAULT_MODEL)},
        {"id": "ocr", "title": imagelib.ocr_backend_label() or "none", "ok": imagelib.ocr_backend_label() is not None},
        {"id": "vlm", "title": vlm_model, "ok": vlm_ok},
        {"id": "stt", "title": "speech-to-text runtime", "ok": any(_has(m) for m in ("parakeet_mlx", "mlx_whisper", "faster_whisper"))},
    ]
    # ollama backs the enrich/embed model pulls; without it `advance` (→ `plainkeep models pull`) can't run.
    # Report `blocked` with the install command (Task 8) instead of letting it crash mid-pull.
    if not _ollama_present():
        return _row(layer, "blocked", "ollama is required for local model runtimes", items,
                    "install ollama: https://ollama.com")
    state = _items_status(items)
    detail = "file-processing models ready" if state == "ready" else "file-processing model layer is incomplete"
    return _row(layer, state, detail, items, "plainkeep setup models --yes" if state != "ready" else "")


def _status_automation(layer: Layer) -> dict:
    # launchd is macOS-only: off Darwin the layer cannot apply at all (Task 8) — report
    # `not_applicable` with a one-line reason rather than a perpetually-"absent" nag the host can
    # never satisfy. Advisory everywhere it surfaces (doctor never fails it; `--all` never attempts it).
    if _platform_system() != "Darwin":
        return _row(layer, "not_applicable", "launchd scheduling is macOS-only (no plists on this host)",
                    [{"id": "launchd", "title": "jobs/launchd/*.plist", "ok": False, "advisory": True}], "")
    launchd = paths.PLAINKEEP_HOME / "jobs" / "launchd"
    plists = sorted(launchd.glob("*.plist")) if launchd.exists() else []
    items = [{"id": "launchd", "title": "jobs/launchd/*.plist", "ok": bool(plists)}]
    state = "ready" if plists else "absent"
    detail = "job plists rendered" if state == "ready" else "job plists have not been rendered"
    return _row(layer, state, detail, items, "plainkeep setup automation" if state != "ready" else "")


# --- the `ui` layer (ADR-011): the plainkeep-ui terminal binary for humans. The TS source lives in the
# template's cli/ (NOT in engine.txt, so it never propagates to vaults); what a vault installs is a
# self-contained compiled binary from the template repo's GitHub release, placed where the
# `bin/ui/run.py` shim looks first ($PLAINKEEP_HOME/.local/bin/plainkeep-ui). The template may be PRIVATE, so
# the download uses the authenticated GitHub CLI, never anonymous curl.

UI_ASSETS = {
    ("Darwin", "arm64"): "plainkeep-ui-darwin-arm64",
    ("Darwin", "x86_64"): "plainkeep-ui-darwin-x64",
    ("Linux", "x86_64"): "plainkeep-ui-linux-x64",
    ("Linux", "aarch64"): "plainkeep-ui-linux-arm64",
    ("Linux", "arm64"): "plainkeep-ui-linux-arm64",
}


def _ui_target():
    return paths.PLAINKEEP_HOME / ".local" / "bin" / "plainkeep-ui"


def _ui_installed() -> str | None:
    """Mirror of the bin/ui shim's resolution: explicit $PLAINKEEP_UI_BIN wins, then the vault-local
    install this layer provisions, then PATH. Same isfile+X_OK bar everywhere."""
    override = os.environ.get("PLAINKEEP_UI_BIN")
    if override:
        p = override if os.path.isabs(override) else shutil.which(override)
        return p if (p and os.path.isfile(p) and os.access(p, os.X_OK)) else None
    target = _ui_target()
    if target.is_file() and os.access(target, os.X_OK):
        return str(target)
    return shutil.which("plainkeep-ui")


def _ui_asset() -> str | None:
    return UI_ASSETS.get((_platform_system(), _platform_machine()))


def _gh_present() -> bool:
    """Is the GitHub CLI on PATH? A seam (like _ollama_present) so tests exercise the
    downloadable/blocked branches host-independently."""
    return shutil.which("gh") is not None


def _ui_repo() -> str | None:
    """The repo (owner/repo) hosting the plainkeep-ui releases: the ENGINE's fetch-only `upstream`
    remote when present, else `origin`. Never hardcoded — the same remote script/update trusts for
    engine files is the one trusted for binaries.

    It asked the DATA root's git remote through Phase 1, when the two were one directory. They are
    not, and the engine is the right one of the two: which UI release this install wants is a
    property of the code, not of somebody's notes. A vault that happens to be a git repo of its own
    (most are) would otherwise have named ITS remote as the source of engine binaries."""
    for remote in ("upstream", "origin"):
        try:
            r = subprocess.run(["git", "-C", str(paths.ENGINE), "remote", "get-url", remote],
                               capture_output=True, text=True, timeout=10)
        except Exception:
            return None
        if r.returncode != 0:
            continue
        m = re.search(r"github\.com[:/]+([^/:]+/[^/\s]+?)(?:\.git)?/?$", r.stdout.strip())
        if m:
            return m.group(1)
    return None


def _ui_source_buildable() -> bool:
    """Contributor fallback: a full SOURCE CHECKOUT carries cli/ source, compilable with bun.

    `cli/` is build input, not a shipped engine path — it is deliberately NOT in the ownership
    manifest (`enginetree.OWNED_TREES`), because what ships is the compiled binary. So this is true
    when the engine root is a checkout (a contributor running `./plainkeep`) and false for an
    installed tree, which falls back to the `gh release download` path. It read the DATA root
    through Phase 1, which since Task 2 would look for a build system inside somebody's notes."""
    return (paths.ENGINE / "cli" / "package.json").is_file() and shutil.which("bun") is not None


def _ui_expected_version() -> str | None:
    """The ui version this engine expects (bin/ui/version.txt — engine-owned, so `script/update`
    bumps it in every vault alongside the code that speaks to it). None on a pre-version engine."""
    try:
        v = (BIN / "ui" / "version.txt").read_text().strip()
        return v or None
    except OSError:
        return None


def _ui_installed_version(exe: str) -> str | None:
    """What the installed binary reports for `--version`. A binary too old to know the flag (exits
    non-zero via its TTY guard) returns None — which reads as 'unknown', i.e. update available."""
    try:
        r = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=10)
    except Exception:
        return None
    if r.returncode != 0:
        return None
    lines = r.stdout.strip().splitlines()
    return lines[0].strip() if lines else None


def _status_ui(layer: Layer) -> dict:
    exe = _ui_installed()
    items = [{"id": "binary", "title": ".local/bin/plainkeep-ui (or $PLAINKEEP_UI_BIN / PATH)", "ok": bool(exe)}]
    if exe:
        # Offline update detection: the engine ships the version it expects (bin/ui/version.txt,
        # bumped by `script/update`); the binary self-reports via `--version`. A mismatch makes the
        # layer `partial` — attemptable — so the ordinary `plainkeep setup ui --yes` performs the update
        # (pinned to the expected release). No network in status; no extra flags to learn.
        expected = _ui_expected_version()
        if expected:
            installed = _ui_installed_version(exe)
            if installed != expected:
                items.append({"id": "version", "title": f"plainkeep-ui {expected}", "ok": False})
                return _row(layer, "partial",
                            f"update available: installed {installed or 'unknown'} → {expected}",
                            items, "plainkeep setup ui --yes")
            items.append({"id": "version", "title": f"plainkeep-ui {expected}", "ok": True})
        return _row(layer, "ready", f"plainkeep ui launches {exe}", items)
    asset = _ui_asset()
    can_download = _gh_present() and asset is not None and _ui_repo() is not None
    if can_download or _ui_source_buildable():
        return _row(layer, "absent", "the terminal UI binary is not installed", items,
                    "plainkeep setup ui --yes")
    if asset is None:
        return _row(layer, "not_applicable",
                    f"no prebuilt plainkeep-ui for this platform ({_platform_system()}/{_platform_machine()}), "
                    "and no cli/ source + bun to build from", items, "")
    return _row(layer, "blocked",
                "the GitHub CLI is required to download the plainkeep-ui release binary", items,
                "install the GitHub CLI: brew install gh (then `plainkeep setup ui --yes`)")


def _ui_verify_and_install(asset_path, checksums_path, target) -> None:
    """sha256-gate the downloaded asset against the release's checksums.txt, then move it into place
    executable. Raises OSError on any mismatch (the caller surfaces it as a layer failure) and
    removes the unverified download so a bad binary never lingers executable."""
    digest = hashlib.sha256(asset_path.read_bytes()).hexdigest()
    want = None
    for ln in checksums_path.read_text().splitlines():
        parts = ln.split()
        if len(parts) >= 2 and parts[-1].lstrip("*") == asset_path.name:
            want = parts[0]
            break
    if want is None:
        asset_path.unlink(missing_ok=True)
        raise OSError(f"checksums.txt has no entry for {asset_path.name}")
    if digest != want:
        asset_path.unlink(missing_ok=True)
        raise OSError(f"sha256 mismatch for {asset_path.name}: got {digest}, want {want}")
    checksums_path.unlink(missing_ok=True)
    if str(asset_path) != str(target):
        vaultio.replace(asset_path, target)
    target.chmod(0o755)


def _install_ui(res: dict, *, fake: bool) -> None:
    """Provision the plainkeep-ui binary into $PLAINKEEP_HOME/.local/bin (where the bin/ui shim looks first).
    Primary: `gh release download` the prebuilt asset + checksums.txt from the template repo (gh is
    already authenticated for anyone who cloned a private template) and verify the sha256. Fallback
    (contributor checkout): compile cli/ from source with bun. Steps are recorded in res['ran'];
    fake/dry mode records the plan and runs nothing."""
    target = _ui_target()
    bindir = target.parent
    asset = _ui_asset()
    repo = _ui_repo()
    if _gh_present() and asset and repo:
        # Pin to the engine's expected release when known (install and update land the exact version
        # this engine's contract was tested with); a pre-version engine falls back to latest.
        expected = _ui_expected_version()
        tag = [f"ui-v{expected}"] if expected else []
        dl = ["gh", "release", "download", *tag, "--repo", repo, "--pattern", asset,
              "--pattern", "checksums.txt", "--dir", str(bindir), "--clobber"]
        if _fake(fake):
            res["ran"].append(_run(dl, fake=fake))
            res["ran"].append(f"verify sha256 (checksums.txt) + install {target}")
            return
        vaultio.mkdir(bindir)
        res["ran"].append(_run(dl))
        _ui_verify_and_install(bindir / asset, bindir / "checksums.txt", target)
        res["ran"].append(f"verified sha256 + installed {target}")
        return
    if _ui_source_buildable():
        src = paths.ENGINE / "cli"
        if not _fake(fake):
            vaultio.mkdir(bindir)
        res["ran"].append(_run(["bun", "install", "--cwd", str(src)], fake=fake))
        res["ran"].append(_run(["bun", "build", "--compile", str(src / "src" / "tui" / "index.ts"),
                                "--outfile", str(target)], fake=fake))
        return
    raise FileNotFoundError(
        "no way to install plainkeep-ui: need the GitHub CLI (gh) for the release download, "
        "or cli/ source + bun to build from")


def status(layer_id=None) -> list[dict]:
    selected = [_layer(layer_id)] if layer_id else list(LAYERS)
    out = []
    for layer in selected:
        if layer.id == "skeleton":
            out.append(_status_skeleton(layer))
        elif layer.id == "search":
            out.append(_status_search(layer))
        elif layer.id == "backups":
            out.append(_status_backups(layer))
        elif layer.id == "models":
            out.append(_status_models(layer))
        elif layer.id == "automation":
            out.append(_status_automation(layer))
        elif layer.id == "ui":
            out.append(_status_ui(layer))
    return out


def _result() -> dict:
    return {"ran": [], "skipped": [], "handoff": [], "confirm_needed": False}


def _confirm(layer: Layer, yes: bool, res: dict) -> bool:
    if layer.gate == "confirm" and not yes:
        res["confirm_needed"] = True
        res["skipped"].append(layer.id)
        return False
    return True


def advance(layer_id, *, yes: bool, fake: bool) -> dict:
    layer = _layer(layer_id)
    res = _result()
    # Test-only fault injection (`PLAINKEEP_SETUP_FORCE_FAIL=<layer_id>`): raise a controlled action failure
    # for the targeted layer BEFORE any skip/gate logic, so the CLI's error-envelope path
    # (`_action_failed` → exit 1, no traceback) is exercised deterministically on any host — without
    # depending on a real missing binary (which has side effects where the binary IS present). Never
    # set in normal use.
    if os.environ.get("PLAINKEEP_SETUP_FORCE_FAIL", "") == layer_id:
        raise subprocess.CalledProcessError(1, [f"plainkeep setup {layer_id}", "(PLAINKEEP_SETUP_FORCE_FAIL)"])
    before = status(layer.id)[0]
    if before["status"] == "ready":
        res["skipped"].append(layer.id)
        return res
    if layer.gate == "blocked":  # static human handoff (backups) — regardless of which piece is missing
        if layer.handoff:
            res["handoff"].append(layer.handoff)
        res["skipped"].append(layer.id)
        return res
    # Dynamic block (Task 8): a missing external binary (ollama) or a host that can't run the layer
    # (not_applicable). Never attempt it — surface the exact remediation from `next` and skip, so
    # `advance` cannot crash mid-install on a prerequisite the status probe already flagged.
    if before["status"] in ("blocked", "not_applicable"):
        if before.get("next"):
            res["handoff"].append(before["next"])
        res["skipped"].append(layer.id)
        return res
    if not _confirm(layer, yes, res):
        return res

    # FIX 5: if a multi-step install fails midway, `res["ran"]` holds the steps that DID succeed. A
    # bare raise loses them (the caller only sees the exception), so attach the partial progress to the
    # exception before re-raising — `_advance_all` reads `ops_partial_ran` to preserve step visibility.
    try:
        if layer.id == "skeleton":
            res["ran"].append(_run_verb("doctor", "--init", fake=fake))
        elif layer.id == "search":
            # Venv-correct, isolated search layer (Task 10 / ADR-009 / FIX 2):
            # (1) ensure $PLAINKEEP_HOME/.venv (create/repair if missing or broken), (2) install ONLY the
            # search deps into it (never the whole requirements.txt), (3) pull the embed model,
            # (4) re-enter `plainkeep index` — the dispatcher runs it on the .venv python, so lancedb/
            # fastembed import for the build.
            _ensure_venv(res, fake=fake)
            res["ran"].append(_venv_pip(*_search_pip_args(), fake=fake))
            res["ran"].append(_run(["ollama", "pull", _embed_model()], fake=fake))
            res["ran"].append(_run_verb("index", fake=fake))
        elif layer.id == "models":
            # The .venv is the CANONICAL home for ALL optional deps (FIX 2): the file-processing
            # packages install into the SAME venv the dispatcher prefers, so `plainkeep files`/`enrich`/
            # `doctor` see Pillow/trafilatura/mlx-vlm consistently instead of the old silent capability
            # regression (installed into bare python, then invisible once .venv exists).
            res["ran"].append(_run_verb("models", "pull", "--all", "--yes", fake=fake))
            _ensure_venv(res, fake=fake)
            pkgs = ["Pillow", "trafilatura"]
            if _platform_system() == "Darwin" and _platform_machine().lower() in ("arm64", "aarch64"):
                pkgs.append("mlx-vlm")
            res["ran"].append(_venv_pip(*pkgs, fake=fake))
        elif layer.id == "automation":
            res["ran"].append(_run_verb("job", "apply", fake=fake))
        elif layer.id == "ui":
            # ADR-011: download the compiled plainkeep-ui release binary (sha256-verified) into
            # $PLAINKEEP_HOME/.local/bin — or compile from cli/ source in a contributor checkout.
            _install_ui(res, fake=fake)
    except BaseException as exc:
        exc.ops_partial_ran = list(res["ran"])
        raise
    return res
