"""
enginetree.py — WHERE THE CODE IS (ADR-014 D2/D6, Phase 2 Task 2).

`vaultroot.py` answers "which vault does this invocation act on". This module answers the other half
of the same question, and the two are deliberately separate modules because they must never be
allowed to answer each other: **the engine is code, the vault is data, and neither may be derived
from the other.** Phase 1 derived both from one directory; that is what this task ends.

**The engine root is CODE-RELATIVE, always, and that is a security property rather than a
convenience.** `ENGINE_ROOT` below is `Path(__file__).resolve().parents[2]` — the tree this very file
belongs to. It is not read from the environment, and nothing in this module will read
`PLAINKEEP_ENGINE` to decide where to load code from. ADR-014 D2 states the rule as "caller input
must not control it"; the way to make that true is not to validate the variable but to never consult
it. So `PLAINKEEP_ENGINE` is an **OUTPUT**: both dispatchers export the root they resolved from their
own location, REPLACING whatever the caller had set, and the consumers of the variable are the
processes that genuinely cannot self-locate — a PLUGIN verb under `<vault>/plugins/<pack>/<verb>/`,
a frontend script, a scheduled job. `templates/verb/run.py` (the scaffold every plugin starts from)
is the load-bearing one: it used to bootstrap `lib` through `$PLAINKEEP_HOME/bin`, which IS the
"engine lives in the vault" assumption, and it now bootstraps through `$PLAINKEEP_ENGINE/bin`.

**The installed layout** (the controller's decision for this task):

    ${XDG_DATA_HOME:-$HOME/.local/share}/plainkeep/engine/<version>/     one immutable tree per version
    ${XDG_DATA_HOME:-$HOME/.local/share}/plainkeep/engine/current -> <version>

`PLAINKEEP_ENGINE_HOME` relocates the INSTALL ROOT — where versions are written and where `current`
is looked for. It is the test/dev override the hermetic harness uses, and it is emphatically NOT a
second way to point a running dispatcher at code: a dispatch never consults it, because a dispatch
never asks where engines are installed. It only matters to `--install` / `--activate` / `--print`,
i.e. to the installer. That split is what lets the suite be hermetic without weakening the rule
above.

**Immutability is enforced, not asserted.** `install()` writes the tree and then makes it read-only
(dirs 0555, files 0444, executables 0555). A tree that cannot be written cannot be hot-patched, which
is the whole reason the engine leaves the vault: `skills/operate-plainkeep/SKILL.md` used to tell an
operator to edit `bin/<verb>/run.py` in place. The cost is stated rather than hidden — CPython cannot
write `__pycache__` into a read-only tree, so every invocation re-compiles the lib modules it
imports. Measured, and carried in the Task 2 report.

**What the engine OWNS** is enumerated in `OWNED_TREES`/`OWNED_FILES` below and verified, because a
manifest nothing executes is paperwork. `install()` refuses to leave behind a tree that fails
`verify()`, and `require_intact()` — which both dispatchers run on every invocation through
`vaultroot._select_cli` — refuses a tree whose dispatch-critical files have gone missing.

Stdlib only and import-light, for `vaultroot.py`'s reason: it is imported on the first act of every
invocation.
"""
from __future__ import annotations
import os
import shutil
import stat
import sys
from pathlib import Path

try:
    from . import output, vaultreg  # type: ignore  # (namespace siblings)
except ImportError:      # loaded top-level / exec'd standalone with bin/lib on sys.path
    _LIB = os.path.dirname(os.path.abspath(__file__))
    if _LIB not in sys.path:
        sys.path.insert(0, _LIB)
    import output      # type: ignore
    import vaultreg    # type: ignore

VaultError = vaultreg.VaultError

# The variable the dispatchers EXPORT (never read — see the module header).
ENV_ENGINE = "PLAINKEEP_ENGINE"
# The variable that relocates the INSTALL ROOT, for the hermetic suite and for a dev who wants a
# second install. Consulted only by the installer surface below, never by a dispatch.
ENV_INSTALL_ROOT = "PLAINKEEP_ENGINE_HOME"

# THE ENGINE ROOT. `bin/lib/enginetree.py` -> parents[0]=lib, [1]=bin, [2]=the tree.
# `.resolve()` matters: the launcher is normally reached through `<install>/engine/current/plainkeep`
# (a symlink), and the floor, the core and this module must all name the same canonical tree or the
# disjointness check below compares two spellings of one directory and silently answers "no".
ENGINE_ROOT = Path(__file__).resolve().parents[2]

VERSIONS_DIRNAME = "engine"
CURRENT_NAME = "current"

# --- the ownership manifest ----------------------------------------------------------------------
# The engine-owned set, enumerated. The plan section lists it as prose and the advisor made the
# enumeration blocking, for the reason this file exists: an ownership table that nothing executes
# describes an intention rather than a disposition. These two tuples are what `install()` copies and
# what `verify()` checks, so the table IS the installer.
#
# `templates/` is a mixed directory — `templates/obsidian`, `templates/wiki`, `templates/project-repo`
# and `templates/tax-formula.md` are USER data that lives in the vault and that an update must never
# overwrite. Only `templates/verb` is a code scaffold, so only it is named here.
OWNED_TREES = (
    "bin",                              # every verb dir (run.py + cmd.json), bin/lib, bin/share/worker
    "templates/verb",                   # the scaffold `plainkeep new verb` renders
    "frontends/raycast",                # they shell to the installed launcher
    "skills/operate-plainkeep",         # the agent manual — engine-owned, rewritten in this task
)
OWNED_FILES = (
    "VERSION",                          # load-bearing: manifest.py reads it as <engine>/VERSION
    "plainkeep",                        # the launcher itself ships inside the tree it launches
)

# Directories under `bin/` that are NOT verbs, so `verify()` does not demand a run.py of them.
NON_VERB_BIN_DIRS = ("lib",)

# The dispatch-critical files, probed on EVERY invocation by `require_intact()`. Deliberately the
# cheap half of `verify()`: five stats, not the ~150 the full manifest walk costs. The full walk runs
# at INSTALL time (where a broken tree must never be left behind) and in `plainkeep doctor`.
DISPATCH_PROBE = (
    "bin/lib/vaultroot.py",             # the floor and the core both spawn this first
    "bin/lib/guardrail.py",             # ...then this, as the gate
    "bin/lib/resolver.py",              # ...then this, to find the verb
    "VERSION",                          # manifest.py's <engine>/VERSION
)


def launcher(root: Path | None = None) -> Path:
    """The `plainkeep` launcher inside an engine tree. What a scheduled job or a frontend must
    invoke, now that `$PLAINKEEP_HOME/plainkeep` is not a thing that exists."""
    return (root or ENGINE_ROOT) / "plainkeep"


def engine_bin(root: Path | None = None) -> Path:
    return (root or ENGINE_ROOT) / "bin"


# --- the install root (installer surface only) ----------------------------------------------------
def install_root() -> Path:
    """`${PLAINKEEP_ENGINE_HOME:-${XDG_DATA_HOME:-$HOME/.local/share}/plainkeep}`.

    XDG-correct and next to the vault registry's config dir, which is the controller's decision for
    this task. Read ONLY by the installer surface — see the module header for why a dispatch must
    never ask this question."""
    v = os.environ.get(ENV_INSTALL_ROOT)
    if v and v.strip():
        return Path(os.path.expanduser(v.strip()))
    xdg = os.environ.get("XDG_DATA_HOME")
    base = (Path(os.path.expanduser(xdg)) if xdg and xdg.strip()
            else Path(os.path.expanduser("~")) / ".local" / "share")
    return base / "plainkeep"


def versions_dir() -> Path:
    return install_root() / VERSIONS_DIRNAME


def current_link() -> Path:
    return versions_dir() / CURRENT_NAME


def installed_versions() -> list[str]:
    try:
        return sorted(p.name for p in versions_dir().iterdir()
                      if p.is_dir() and not p.is_symlink())
    except OSError:
        return []


def active_engine() -> Path | None:
    """What `current` points at, resolved — or None when nothing is activated."""
    link = current_link()
    try:
        if not link.is_symlink():
            return None
        return Path(os.path.realpath(link))
    except OSError:
        return None


# --- verification ---------------------------------------------------------------------------------
def _verb_dirs(root: Path) -> list[Path]:
    try:
        return sorted(d for d in (root / "bin").iterdir()
                      if d.is_dir() and d.name not in NON_VERB_BIN_DIRS
                      and not d.name.startswith("__"))
    except OSError:
        return []


def verify(root: Path) -> list[str]:
    """Every way `root` fails to be a complete engine tree, as a list of one-line problems.

    Empty means the whole ownership manifest is present. Called by `install()` (which refuses to
    activate an incomplete tree) and by `plainkeep doctor`. It walks the tree, so it is NOT what a
    dispatch runs — `require_intact()` is."""
    problems: list[str] = []
    for rel in OWNED_FILES:
        if not (root / rel).is_file():
            problems.append(f"missing engine file: {rel}")
    for rel in OWNED_TREES:
        if not (root / rel).is_dir():
            problems.append(f"missing engine tree: {rel}/")
    if not (root / "bin" / "lib").is_dir():
        problems.append("missing engine tree: bin/lib/")
    else:
        for mod in ("vaultroot.py", "vaultreg.py", "enginetree.py", "guardrail.py", "resolver.py",
                    "manifest.py", "output.py", "paths.py", "wall.py", "vaultio.py"):
            if not (root / "bin" / "lib" / mod).is_file():
                problems.append(f"missing engine module: bin/lib/{mod}")
    verbs = _verb_dirs(root)
    if not verbs:
        problems.append("bin/ carries no verb directory at all")
    for d in verbs:
        for f in ("run.py", "cmd.json"):
            if not (d / f).is_file():
                problems.append(f"verb {d.name!r} is missing bin/{d.name}/{f}")
    # The four paths the ownership table assigns to the engine that are neither bin/ nor VERSION, and
    # that no task moved before this one. Named individually because "the directory exists" is not
    # what makes them owned — their CONTENT is what an installed tree has to carry.
    for rel in ("bin/ui/version.txt", "bin/share/worker/worker.js",
                "templates/verb/run.py", "templates/verb/cmd.json",
                "skills/operate-plainkeep/SKILL.md"):
        if not (root / rel).is_file():
            problems.append(f"missing engine file: {rel}")
    if not any((root / "frontends" / "raycast").glob("*.sh")):
        problems.append("missing engine files: frontends/raycast/*.sh")
    return problems


def require_intact(root: Path | None = None) -> None:
    """Refuse a broken engine tree, cheaply, on every invocation.

    ADR-014 D2 gives `PLAINKEEP_ENGINE` the failure mode "absent/unverified → refuse". This is that
    refusal, and it lives where BOTH dispatchers already run one shared decision (`vaultroot
    --select`) so the floor and the core refuse with byte-identical text.

    It replaces Task 1b's `require_engine(sel)`, which asked whether the selected VAULT carried the
    engine. That probe existed only because Phase 1 ran the engine out of the vault it acted on; this
    task removes the reason for it, and leaving it in place would refuse every data-only vault — which
    is exactly what Task 5's `init` must be able to produce."""
    r = root or ENGINE_ROOT
    for rel in DISPATCH_PROBE:
        if not (r / rel).is_file():
            raise VaultError(
                f"the plainkeep engine at {r} is incomplete (no {rel})",
                hint="reinstall it:\n"
                     f"    python3 {Path(__file__).resolve()} --install <source-checkout>")
    if not _verb_dirs(r):
        raise VaultError(
            f"the plainkeep engine at {r} carries no verb directory under bin/",
            hint="reinstall it:\n"
                 f"    python3 {Path(__file__).resolve()} --install <source-checkout>")


# --- disjointness (ADR-014 D3, ACTIVATED in this task) --------------------------------------------
def _is_within(inner: str, outer: str) -> bool:
    """`inner` IS `outer` or lives under it, on a path boundary. Both must already be canonical."""
    return inner == outer or inner.startswith(outer.rstrip("/") + "/")


def disjointness_verdict(data_root: str, engine_root: Path | None = None) -> str | None:
    """Why this data root and the engine tree overlap, or None when they are disjoint.

    ADR-014 D3 requires "not inside the engine root and the engine root not inside it". Task 1b
    DEFINED the rule and deliberately did not enforce it: while the engine was `<vault>/bin` the rule
    was unsatisfiable and would have refused every existing vault. It becomes true in this task, so it
    turns on in this task — a sequencing split rather than a silent legacy exception, which would have
    defeated the contract outright.

    Both sides are canonicalized before comparing, for `wall._anchored`'s reason: on macOS `/tmp/v`
    and `/private/tmp/v` are one directory under two names, and comparing spellings answers "disjoint"
    for a pair that is not."""
    eng = str(engine_root or ENGINE_ROOT)
    eng = vaultreg.canonical(eng)
    data = vaultreg.canonical(data_root)
    if data == eng:
        return (f"it IS the engine tree ({eng}) — a vault is data and an engine is code, and one "
                f"directory cannot be both")
    if _is_within(data, eng):
        return f"it is inside the engine tree ({eng})"
    if _is_within(eng, data):
        return f"the engine tree ({eng}) is inside it"
    return None


# --- installing ------------------------------------------------------------------------------------
def read_version(src: Path) -> str:
    try:
        v = (src / "VERSION").read_text(encoding="utf-8").strip()
    except OSError as e:
        raise VaultError(f"cannot read {src / 'VERSION'} ({e})")
    if not v:
        raise VaultError(f"{src / 'VERSION'} is empty — an engine tree is installed BY VERSION")
    if "/" in v or v in (".", ".."):
        raise VaultError(f"{src / 'VERSION'} is not usable as a directory name: {v!r}")
    return v


def _chmod_tree(root: Path, *, writable: bool) -> None:
    """Make an installed tree read-only (or writable again, to replace it).

    Bottom-up when locking, so a directory is not sealed before its children are; top-down when
    unlocking, so a sealed directory can be descended into at all."""
    entries = list(root.rglob("*"))
    dirs = [p for p in entries if p.is_dir() and not p.is_symlink()]
    files = [p for p in entries if p.is_file() and not p.is_symlink()]
    if writable:
        for d in [root, *dirs]:
            try:
                d.chmod(0o755)
            except OSError:
                pass
        for f in files:
            try:
                f.chmod(0o644 if not (f.stat().st_mode & stat.S_IXUSR) else 0o755)
            except OSError:
                pass
        return
    for f in files:
        try:
            f.chmod(0o555 if (f.stat().st_mode & stat.S_IXUSR) else 0o444)
        except OSError:
            pass
    for d in sorted([root, *dirs], key=lambda p: len(p.parts), reverse=True):
        try:
            d.chmod(0o555)
        except OSError:
            pass


def _copy_owned(src: Path, dst: Path) -> None:
    ignore = shutil.ignore_patterns("__pycache__", "*.pyc")
    for rel in OWNED_TREES:
        s = src / rel
        if not s.is_dir():
            raise VaultError(f"source tree is missing {rel}/ — cannot install an engine from {src}")
        d = dst / rel
        d.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(s, d, ignore=ignore, symlinks=True)
    for rel in OWNED_FILES:
        s = src / rel
        if not s.is_file():
            raise VaultError(f"source tree is missing {rel} — cannot install an engine from {src}")
        d = dst / rel
        d.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(s, d)
    # The compiled core, when the source tree has one. OPTIONAL by design: the bash floor is the
    # zero-install path, and an engine tree without a core binary is a complete engine that dispatches
    # through the floor. It is not in the ownership manifest for that reason, and `verify()` does not
    # ask for it.
    core = src / ".local" / "bin" / "plainkeep-core"
    if core.is_file():
        d = dst / ".local" / "bin" / "plainkeep-core"
        d.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(core, d)


def remove_version(version: str) -> None:
    """Delete one installed version. Only ever called on a tree this module installed, and only to
    replace it: an installed engine is immutable, so `--install --force` removes and rewrites rather
    than patching in place."""
    d = versions_dir() / version
    if not d.is_dir():
        return
    _chmod_tree(d, writable=True)
    shutil.rmtree(d, ignore_errors=True)


def install(src: Path, *, version: str | None = None, force: bool = False,
            activate_it: bool = True, writable: bool = False) -> Path:
    """Install the engine-owned set from a source checkout into `<install root>/engine/<version>/`.

    Staged then renamed: the tree is built under a `.incoming-<version>` sibling, VERIFIED there, and
    only moved into its final name once complete. A half-copied engine is never reachable under a
    version name, which matters because `current` may be pointed at it a line later."""
    src = Path(os.path.abspath(os.path.expanduser(str(src))))
    version = version or read_version(src)
    root = versions_dir()
    dst = root / version
    if dst.exists():
        if not force:
            raise VaultError(f"engine {version} is already installed at {dst}",
                             hint="an installed engine is immutable — pass --force to replace it, "
                                  "or bump VERSION")
        remove_version(version)
    staging = root / f".incoming-{version}"
    if staging.exists():
        _chmod_tree(staging, writable=True)
        shutil.rmtree(staging, ignore_errors=True)
    root.mkdir(parents=True, exist_ok=True)
    staging.mkdir()
    staged = True
    try:
        _copy_owned(src, staging)
        problems = verify(staging)
        if problems:
            raise VaultError(
                f"refusing to install an incomplete engine from {src}:\n  "
                + "\n  ".join(problems),
                hint="the ownership manifest is enumerated in bin/lib/enginetree.py "
                     "(OWNED_TREES / OWNED_FILES)")
        # RENAME FIRST, SEAL SECOND, and the order is forced rather than chosen: renaming a
        # DIRECTORY needs write permission on the directory itself (the kernel updates its `..`
        # entry), so a tree sealed at 0555 cannot be moved into place at all — measured, EACCES on
        # macOS. The property the staging dance exists for is unaffected: what lands under the
        # version name is already complete and already verified, and only its permissions change
        # afterwards.
        os.rename(staging, dst)
        staged = False
        if not writable:
            _chmod_tree(dst, writable=False)
    except BaseException:
        if staged:
            _chmod_tree(staging, writable=True)
            shutil.rmtree(staging, ignore_errors=True)
        else:
            remove_version(version)
        raise
    if activate_it:
        activate(version)
    return dst


def activate(version: str) -> Path:
    """Point `current` at `<version>`, atomically. A symlink cannot be rewritten in place, so a
    uniquely named one is created beside it and `os.replace`d over the old — an invocation that
    starts mid-switch sees one engine or the other, never nothing."""
    dst = versions_dir() / version
    if not dst.is_dir():
        raise VaultError(f"engine {version} is not installed ({dst} does not exist)",
                         hint="install it first: --install <source-checkout>")
    problems = verify(dst)
    if problems:
        raise VaultError(f"refusing to activate an incomplete engine at {dst}:\n  "
                         + "\n  ".join(problems))
    link = current_link()
    tmp = link.with_name(f".{CURRENT_NAME}.incoming.{os.getpid()}")
    if tmp.exists() or tmp.is_symlink():
        tmp.unlink()
    tmp.symlink_to(dst)
    os.replace(tmp, link)
    return link


# --- CLI -------------------------------------------------------------------------------------------
_USAGE = ("usage: enginetree.py --print [root|install-root|current|versions]\n"
          "       enginetree.py --install <source-checkout> [--version V] [--force] "
          "[--no-activate] [--writable]\n"
          "       enginetree.py --activate <version>\n"
          "       enginetree.py --verify [<engine-root>]")


def main(argv: list[str]) -> int:
    """The installer surface. Deliberately a MODULE CLI and not a `plainkeep engine` verb: a new verb
    would change the verb surface, plainkeep.json, the completion catalogs and the help output, none
    of which this task is scoped to move. `script/setup` and the test harness call this."""
    if not argv:
        print(_USAGE, file=sys.stderr)
        return output.EXIT_USAGE
    cmd, rest = argv[0], argv[1:]
    try:
        if cmd == "--print":
            what = rest[0] if rest else "root"
            if what == "root":
                print(ENGINE_ROOT)
            elif what == "install-root":
                print(install_root())
            elif what == "current":
                a = active_engine()
                if a is None:
                    print(f"plainkeep: no engine activated ({current_link()} is not a symlink)",
                          file=sys.stderr)
                    return output.EXIT_NOT_FOUND
                print(a)
            elif what == "versions":
                for v in installed_versions():
                    print(v)
            else:
                print(_USAGE, file=sys.stderr)
                return output.EXIT_USAGE
            return output.EXIT_OK
        if cmd == "--install":
            if not rest:
                print("plainkeep: --install needs a source checkout", file=sys.stderr)
                return output.EXIT_USAGE
            src = Path(rest[0])
            opts = rest[1:]
            ver = None
            if "--version" in opts:
                i = opts.index("--version")
                if i + 1 >= len(opts):
                    print("plainkeep: --version needs a value", file=sys.stderr)
                    return output.EXIT_USAGE
                ver = opts[i + 1]
            d = install(src, version=ver, force="--force" in opts,
                        activate_it="--no-activate" not in opts, writable="--writable" in opts)
            print(d)
            return output.EXIT_OK
        if cmd == "--activate":
            if not rest:
                print("plainkeep: --activate needs a version", file=sys.stderr)
                return output.EXIT_USAGE
            print(activate(rest[0]))
            return output.EXIT_OK
        if cmd == "--verify":
            root = Path(rest[0]) if rest else ENGINE_ROOT
            problems = verify(root)
            for p in problems:
                print(p, file=sys.stderr)
            print(f"{root}: {'OK' if not problems else f'{len(problems)} problem(s)'}")
            return output.EXIT_OK if not problems else output.EXIT_DENY
    except VaultError as e:
        sys.stderr.write("plainkeep: " + e.message + (f"\n  {e.hint}" if e.hint else "") + "\n")
        return e.code
    print(_USAGE, file=sys.stderr)
    return output.EXIT_USAGE


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
