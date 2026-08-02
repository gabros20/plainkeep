"""
vaultreg.py — the vault MARKER and the vault REGISTRY (ADR-014, Phase 2 Task 1a).

Two files, and the split between them is the design:

  * **The marker**, `<vault>/.plainkeep/vault.json`, says "this directory is a plainkeep vault" and
    carries an IMMUTABLE `id`. It carries no name and no path — a vault does not know what it is
    called or where it lives, so renaming or moving one never writes into it.
  * **The registry**, `$XDG_CONFIG_HOME/plainkeep/registry.json`, lives outside every vault and maps
    ids to names and canonical paths, with one `default`. It is the only thing that knows which
    vaults exist.

Why the marker cannot be the whole answer: a plain clone of the public template must NOT be
adoptable as a vault, or an arbitrary checkout could spoof one (Codex, panel Q2). `.plainkeep/` is
gitignored, so a clone has no marker. Why the registry cannot be the whole answer: a moved vault has
to stay identifiable from the inside, which is what the `id` is for — a move is a `rebind`, not a
re-registration.

EVERYTHING HERE FAILS CLOSED. Unknown schema, malformed JSON, a duplicate id/name/path, a `default`
pointing at nothing: each REFUSES with the offending file named. Nothing is auto-created by a read
path, nothing is silently repaired, and there is no last-wins. The registry is created only by
`vault register`.

Scope: this module and the `vault` verb own the two FILES and their validation, and nothing here
resolves a root. Discovery — the `--vault` selector, `PLAINKEEP_HOME`, marker walk-up, the registry
default — shipped in Task 1b and lives in `vaultroot.py`, which consumes what is defined here:
`read_marker` is how a candidate proves it is a vault, and `read_registry`/`find` are how the two
mechanisms that go THROUGH the registry (a selector and a walked-up marker) are resolved. The layering
is one-way — `vaultroot` imports `vaultreg`, never the reverse.

(Until Task 1b this said "It changes NO discovery behaviour — `PLAINKEEP_HOME` still resolves exactly
as it did". That was true of Task 1a and has been false since Task 1b shipped: `PLAINKEEP_HOME` is now
VALIDATED against this module's marker, and an unmarked root refuses.)
"""
from __future__ import annotations
import json
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

try:
    from . import output  # type: ignore  # (namespace sibling)
except ImportError:      # loaded top-level with bin/lib on sys.path
    import sys as _sys
    _sys.path.insert(0, str(Path(__file__).resolve().parent))
    import output  # type: ignore

MARKER_SCHEMA = "plainkeep.vault/1"
REGISTRY_SCHEMA = "plainkeep.registry/1"
MARKER_DIR = ".plainkeep"
MARKER_NAME = "vault.json"
NAME_RE = re.compile(r"^[a-z][a-z0-9_-]*$")
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


class VaultError(Exception):
    """A refusal. `code` is from the frozen exit-code protocol (lib/output.py)."""

    def __init__(self, message: str, code: int = output.EXIT_USAGE, hint: str | None = None):
        super().__init__(message)
        self.message, self.code, self.hint = message, code, hint
        # Filled in by vaultroot.discover(): the per-mechanism account of the discovery chain, so a
        # refusal can be explained rather than only reported. Empty for every other refusal.
        self.saw: dict = {}


# --- canonical paths ----------------------------------------------------------------------------
def canonical(p) -> str:
    """The one spelling of a path this module ever stores or compares. Registry entries are written
    canonical and matched canonical-to-canonical — never against the caller's spelling, which on
    macOS differs from the resolved form for anything under /tmp or /var.

    It returns the path's REAL spelling, which is what may be stored, exported and printed. It is NOT
    a comparison key: `realpath` normalises symlinks and `..` but never CASE, and the default macOS
    APFS volume is case-insensitive. Compare with `same_path` / `path_within` below, never with `==`
    or `startswith`."""
    return os.path.realpath(os.path.abspath(os.path.expanduser(str(p))))


def _same_entry(a: str, b: str) -> bool:
    """Do these two spellings name ONE directory entry? Asked of the FILESYSTEM (st_dev, st_ino),
    not of the strings.

    This is the axis `canonical()` cannot normalise. `realpath` resolves symlinks and `..`, so two
    spellings that differ only in CASE — or in Unicode normalisation, HFS+'s other fold — survive it
    unchanged and compare unequal for a pair that is one directory. macOS's default APFS volume folds
    case, so this is the ordinary configuration rather than an exotic one.

    Identity is used rather than a `.lower()` fold because a fold is wrong in the other direction: on
    a case-SENSITIVE volume `/x/Foo` and `/x/foo` are two directories, and folding would merge them.
    `stat` answers for the volume the paths actually live on, whichever that is, and needs no platform
    sniffing. A path that does not exist answers False, which leaves the caller with plain string
    equality — the behaviour there has always been."""
    try:
        sa, sb = os.stat(a), os.stat(b)
    except OSError:
        return False
    return (sa.st_dev, sa.st_ino) == (sb.st_dev, sb.st_ino)


def same_path(a, b) -> bool:
    """Do two CANONICAL paths name one directory? String equality first (the overwhelmingly common
    case, and free), identity second."""
    a, b = str(a), str(b)
    return a == b or _same_entry(a, b)


def path_within(inner: str, outer: str) -> bool:
    """`inner` IS `outer` or lives under it, on a path boundary.

    Both sides must already be canonical. The boundary requirement is what keeps `…/4.0.0-dev-notes`
    from reading as inside `…/4.0.0-dev`; the identity fallback is what keeps `/x/VAULTDIR/sub` from
    reading as OUTSIDE `/x/vaultdir` on a case-folding volume — see `_same_entry`. The stat pair is
    paid only when the string comparison has already answered "no"."""
    outer = outer.rstrip("/") or "/"
    sep = "" if outer == "/" else "/"
    if inner == outer or inner.startswith(outer + sep):
        return True
    if len(inner) < len(outer):
        return False
    if len(inner) > len(outer) and inner[len(outer)] != "/":
        return False                      # not on a path boundary — a sibling sharing a prefix
    return _same_entry(inner[:len(outer)], outer)


# --- the marker ---------------------------------------------------------------------------------
def marker_path(vault) -> Path:
    return Path(vault) / MARKER_DIR / MARKER_NAME


def read_marker(vault) -> dict | None:
    """The marker at `vault`, or None if there is none. Raises VaultError if there is one and it is
    not usable — an unreadable marker is never the same as an absent one."""
    f = marker_path(vault)
    # PRESENCE, not usability — anything at that path counts, including a dangling symlink, whose
    # target does not exist but whose LINK does. `is_file()` used to stand in for this and answered
    # False for a directory and for a dangling symlink alike, i.e. "absent", which is the one answer
    # a broken marker must never give: vaultroot's walk-up then skipped the inner vault and selected
    # the OUTER one with exit 0. "An unreadable marker is never the same as an absent one" is this
    # function's stated doctrine; this is the line that makes it true.
    if not (f.exists() or f.is_symlink()):
        return None
    if not f.is_file():
        raise VaultError(f"vault marker is not a regular file: {f}",
                         hint="a marker is a small JSON file; something replaced it with a "
                              "directory, a dangling symlink or a device node — repair or remove it")
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
    except Exception as e:
        raise VaultError(f"vault marker is not valid JSON: {f} ({e})")
    if not isinstance(data, dict):
        raise VaultError(f"vault marker is not an object: {f}")
    schema = data.get("schema")
    if schema != MARKER_SCHEMA:
        raise VaultError(f"vault marker has unknown schema {schema!r} (expected {MARKER_SCHEMA!r}): {f}",
                         hint="a newer plainkeep wrote it; upgrade rather than downgrade")
    vid = data.get("id")
    if not isinstance(vid, str) or not UUID_RE.match(vid):
        raise VaultError(f"vault marker has no valid id: {f}")
    return data


def new_marker_doc() -> dict:
    return {"schema": MARKER_SCHEMA, "id": str(uuid.uuid4()),
            "created": datetime.now(timezone.utc).isoformat(timespec="seconds")}


def marker_bytes(doc: dict) -> str:
    return json.dumps(doc, indent=2, ensure_ascii=False) + "\n"


# --- the registry -------------------------------------------------------------------------------
def config_dir() -> Path:
    """`$PLAINKEEP_CONFIG_HOME` → `$XDG_CONFIG_HOME/plainkeep` → `~/.config/plainkeep`.

    `PLAINKEEP_CONFIG_HOME` exists so the suite can run hermetically: without it every test would
    read and write the developer's real registry."""
    env = os.environ.get("PLAINKEEP_CONFIG_HOME")
    if env:
        return Path(env)
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else Path(os.environ.get("HOME") or Path.home()) / ".config"
    return base / "plainkeep"


def registry_path() -> Path:
    return config_dir() / "registry.json"


def empty_registry() -> dict:
    return {"schema": REGISTRY_SCHEMA, "default": None, "vaults": []}


def read_registry(*, required: bool = False) -> dict:
    """The validated registry. An ABSENT registry returns the empty shape (or refuses when
    `required`); a PRESENT but invalid one always refuses. Never creates the file."""
    f = registry_path()
    if not f.is_file():
        if required:
            raise VaultError(f"no vault registry: {f}",
                             hint="register this vault with: plainkeep vault register --yes")
        return empty_registry()
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
    except Exception as e:
        raise VaultError(f"vault registry is not valid JSON: {f} ({e})")
    return validate_registry(data, f)


def validate_registry(data, f) -> dict:
    if not isinstance(data, dict):
        raise VaultError(f"vault registry is not an object: {f}")
    if data.get("schema") != REGISTRY_SCHEMA:
        raise VaultError(f"vault registry has unknown schema {data.get('schema')!r} "
                         f"(expected {REGISTRY_SCHEMA!r}): {f}")
    vaults = data.get("vaults")
    if not isinstance(vaults, list):
        raise VaultError(f"vault registry has no 'vaults' list: {f}")
    seen: dict[str, set] = {"id": set(), "name": set(), "path": set()}
    out = []
    for i, v in enumerate(vaults):
        if not isinstance(v, dict):
            raise VaultError(f"vault registry entry {i} is not an object: {f}")
        for key, ok in (("id", lambda s: UUID_RE.match(s)),
                        ("name", lambda s: NAME_RE.match(s)),
                        ("path", lambda s: s.startswith("/"))):
            val = v.get(key)
            if not isinstance(val, str) or not ok(val):
                raise VaultError(f"vault registry entry {i} has an invalid {key!r}: {f}")
            # "Registry paths are canonical" is enforced HERE, at the one boundary every read passes
            # through, rather than at each of the four places that compare one. `vault register`
            # already writes them canonical; a HAND-EDITED entry, or one whose parent later became a
            # symlink, did not have to be — and vaultroot compares `entry["path"]` against the
            # canonical root, so the walk-up refused a vault that --vault and the registry default
            # both resolved happily, with a `rebind` remediation for a vault that never moved.
            # It also makes the duplicate check below compare canonical-to-canonical, so two
            # spellings of one vault refuse as the ambiguity they are instead of reading as two.
            if key == "path":
                val = canonical(val)
            # Duplicates are a REFUSAL, not a last-wins: two entries claiming one name/path/id means
            # the file is ambiguous, and guessing which the user meant is how a vault gets written to
            # by accident. Paths are compared with `same_path` rather than `==` for the reason stated
            # there: canonical is not a comparison key, and `/x/Vault` and `/x/vault` are one
            # directory on the default macOS volume. `==` read them as two entries and let BOTH into
            # the registry, which is the ambiguity this check exists to refuse.
            dup = (any(same_path(val, s) for s in seen[key]) if key == "path"
                   else val in seen[key])
            if dup:
                raise VaultError(f"vault registry has a duplicate {key} {val!r}: {f}")
            seen[key].add(val)
        out.append({**v, "path": canonical(v["path"])})
    default = data.get("default")
    if default is not None:
        if not isinstance(default, str) or default not in seen["id"]:
            raise VaultError(f"vault registry 'default' does not name a registered vault: {f}")
    return {"schema": REGISTRY_SCHEMA, "default": default, "vaults": out}


def registry_bytes(reg: dict) -> str:
    return json.dumps({"schema": REGISTRY_SCHEMA, "default": reg.get("default"),
                       "vaults": reg.get("vaults", [])}, indent=2, ensure_ascii=False) + "\n"


class _Lock:
    """An `O_EXCL` lock beside the registry. A stale lock is REPORTED, never force-broken — breaking
    it is how two concurrent writers both think they won."""

    def __init__(self, path: Path):
        self.path, self.fd = path, None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            raise VaultError(f"vault registry is locked by another process: {self.path}",
                             hint="if no plainkeep is running, remove that file and retry")
        os.write(self.fd, f"{os.getpid()}\n".encode())
        return self

    def __exit__(self, *exc):
        if self.fd is not None:
            os.close(self.fd)
        self.path.unlink(missing_ok=True)
        return False


def write_registry(reg: dict) -> Path:
    """Atomically replace the registry: validate, write a sibling temp file, fsync, `os.replace`.
    An interrupted write leaves the OLD valid file, never a truncated one."""
    f = registry_path()
    validate_registry({"schema": REGISTRY_SCHEMA, "default": reg.get("default"),
                       "vaults": reg.get("vaults", [])}, f)
    with _Lock(f.with_suffix(".json.lock")):
        tmp = f.with_suffix(".json.tmp")
        fd = os.open(str(tmp), os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
        try:
            os.write(fd, registry_bytes(reg).encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, f)
    return f


# --- lookup -------------------------------------------------------------------------------------
def find(reg: dict, selector: str) -> dict | None:
    """Resolve a selector to an entry: an id, a registered name, or any spelling of a registered
    path. Ids and names are exact; paths are matched canonically."""
    sel = (selector or "").strip()
    if not sel:
        return None
    for v in reg["vaults"]:
        if v["id"] == sel or v["name"] == sel:
            return v
    if sel.startswith("/") or sel.startswith("~") or sel.startswith("."):
        c = canonical(sel)
        for v in reg["vaults"]:
            if same_path(v["path"], c):
                return v
    return None


def entry_for_path(reg: dict, path) -> dict | None:
    c = canonical(path)
    return next((v for v in reg["vaults"] if same_path(v["path"], c)), None)


def suggest_name(path) -> str:
    """A default registry name from the directory name; `vault` if nothing usable is left."""
    base = re.sub(r"[^a-z0-9_-]+", "-", Path(canonical(path)).name.lower()).strip("-")
    return base if base and NAME_RE.match(base) else "vault"
