"""
provision.py — HOW THE PYTHON SIDE OF THE ENGINE GETS ITS PACKAGES (Phase 2 Task 4).

`enginetree.py` answers *where the code is*. This module answers the other half of an installed
engine: **which Python distributions are present, and by whose resolution.** The answer is `uv sync`
against a `pyproject.toml` + `uv.lock` that ship INSIDE the engine tree — not a `pip install` of a
requirements file, and not a wheel, because neither imposes the project's exact transitive
resolution on the machine that installs it. A lock that does not travel with the code it locks is a
lock in name only.

Five decisions, made rather than left open, each with the reason it is not the other thing:

**1. uv is DOWNLOADED, never vendored.** A uv binary is ~30 MB per platform; vendoring six targets
would roughly triple the release artifact to carry something that moves on its own cadence and that
most machines already have a copy of somewhere.

**2. It is PINNED by exact version + sha256, and both are recorded in the engine tree**
(`bin/lib/uvpin.json`, right beside this file). The download is verified BEFORE it is made
executable; a mismatch deletes the download and refuses. "Downloaded" without a pin would mean the
resolver changes underneath an engine version that is otherwise immutable — which is the property
ADR-017 exists to establish, given away at the last step.

    **AND THE PIN ITSELF IS HELD TO THE ENGINE'S CHECKSUMS** (`require_delivered_intact`, below).
    A pin that names both the URL and the digest is only worth as much as the pin's own integrity:
    edit both together and the verification step verifies the attacker's bytes against the attacker's
    number. So every entry point that can download or execute — `ensure_uv`, `sync`, and the read of
    the delivered dependency matrix — first asks whether the WHOLE tree still matches what `install()`
    recorded. That gate used to name two files by hand and the pin was not one of them.

**3. It is installed to `<engine-root>/tools/uv/<version>/uv` — INSIDE the versioned engine
directory.** So it is replaced atomically with the engine (a new engine version gets its own tools
directory, provisioned on first use) and it rolls back with the engine (`--activate <older>` points
`current` at a tree carrying the uv that tree was tested against). A shared `~/.local/share/plainkeep/
tools/uv` would have been one directory to keep warm across upgrades, and would also mean a rollback
that rolls the code back and not the toolchain.

    **WHERE THIS LANDS RELATIVE TO THE SEAL, stated explicitly because it is the one place this task
    touches ADR-017 D4.** An installed engine is sealed read-only, so a tree that could not be
    written at all could never be provisioned. The resolution is a single named exception rather than
    an unsealing: `install()` creates `<engine>/tools/` in its staging tree, seals the whole engine as
    before, and then `chmod 0755` on `tools/` ALONE — chmod needs ownership, not a writable parent, so
    nothing else in the tree is unsealed even momentarily. What that buys, precisely:

      * `tools/` and `tools/uv/` are writable — they are where provisioning lands;
      * every provisioned ARTIFACT is sealed after its checksum is verified: `tools/uv/<version>/`
        and the `uv` binary in it go to 0555, so the verified binary is not casually replaceable;
      * `tools/venv/` (the environment `uv sync` manages) is necessarily writable, because uv owns it;
      * nothing under `tools/` is in the ownership manifest, so `verify()`'s seal check — which walks
        the manifest — never sees these modes and never has to special-case them. The exception is in
        the model (`enginetree.PROVISION_DIR`, checked for presence and for being writable) rather
        than being an accident of what the walk happens to miss.

    It is NOT a hole in the immutability claim as ADR-017 states it: that claim is that the CODE
    cannot be hot-patched. `tools/` holds no engine code — it holds a verified third-party binary and
    a package environment, both reconstructible from the pin and the lock by deleting the directory.

**4. A uv already on the machine is IGNORED.** Not preferred, not fallen back to, not consulted at
all — this module never reads `PATH`, and it sets `UV_NO_CONFIG=1` so the user's `uv.toml` cannot
steer a resolution either. Borrowing the operator's uv would silently un-pin the thing the pin exists
for: their uv is some version, resolving with some config, and the engine would then be running a
lock through a resolver it was never tested against. The one concession is a single line saying so,
which `--print system-uv` emits for `doctor`.

**5. Offline REFUSES, with the exact manual command.** Not "falls back to pip", not "warns and
continues": both of those produce a machine whose environment does not match the lock while
reporting success. `offline_refusal()` prints the download URL, the expected sha256 and the
destination path, so an air-gapped install is a documented two-step rather than a dead end. No
partial provisioning is left behind — the staging directory is removed on every failure path.

Stdlib only, and deliberately so: this is the module that runs when the machine has nothing.
"""
from __future__ import annotations
import hashlib
import json
import os
import platform
import re
import shutil
import ssl
import subprocess
import sys
import tarfile
import urllib.error
import urllib.request
from pathlib import Path

try:
    from . import enginetree, output, vaultreg  # type: ignore  # (namespace siblings)
except ImportError:      # loaded top-level / exec'd standalone with bin/lib on sys.path
    _LIB = os.path.dirname(os.path.abspath(__file__))
    if _LIB not in sys.path:
        sys.path.insert(0, _LIB)
    import enginetree    # type: ignore
    import output        # type: ignore
    import vaultreg      # type: ignore

VaultError = vaultreg.VaultError

# The pin, and the delivered project. All three are engine-owned paths, relative to the engine root.
PIN_REL = "bin/lib/uvpin.json"
PYPROJECT_REL = "pyproject.toml"
LOCK_REL = "uv.lock"

# The writable exception inside the sealed tree — see the module header, decision 3. Named here AND
# in enginetree (which creates it) rather than spelled twice: `enginetree.PROVISION_DIR` is the
# authority, this is the import.
TOOLS_DIRNAME = enginetree.PROVISION_DIR
VENV_DIRNAME = "venv"
PYTHON_DIRNAME = "python"

# How long a single network read may block. A provisioning step that hangs forever is worse than one
# that refuses with the manual command, because the manual command is a real way forward.
NET_TIMEOUT_SECONDS = 60


# --- the pin ---------------------------------------------------------------------------------------
def engine_root(root: Path | None = None) -> Path:
    return Path(root) if root is not None else enginetree.ENGINE_ROOT


def load_pin(root: Path | None = None) -> dict:
    """The uv pin as recorded in the engine tree, validated on the way out.

    Validated rather than trusted because this file decides what gets downloaded and what digest it
    is held to: a pin with a malformed digest would otherwise reach `ensure_uv` and be compared
    against a real sha256, never match, and produce a "the download was tampered with" refusal about
    a typo in our own file."""
    p = engine_root(root) / PIN_REL
    try:
        pin = json.loads(p.read_text(encoding="utf-8"))
    except OSError as e:
        raise VaultError(f"cannot read the uv pin at {p} ({e})",
                         hint="it is engine-owned — reinstall the engine "
                              "(python3 bin/lib/enginetree.py --install <checkout>)")
    except ValueError as e:
        raise VaultError(f"the uv pin at {p} is not valid JSON ({e})")
    version = str(pin.get("version") or "")
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version):
        raise VaultError(f"the uv pin at {p} names no usable version ({version!r})")
    arts = pin.get("artifacts")
    if not isinstance(arts, dict) or not arts:
        raise VaultError(f"the uv pin at {p} lists no artifacts")
    for target, digest in arts.items():
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise VaultError(f"the uv pin at {p} has a malformed sha256 for {target}")
    return pin


def platform_target() -> str:
    """This machine's uv release target (`aarch64-apple-darwin`, `x86_64-unknown-linux-gnu`, …).

    An unpinned platform REFUSES by name instead of guessing a nearby target: running an
    x86_64-linux-gnu uv on musl is not a degraded experience, it is a binary that does not start, and
    the refusal is what tells the operator which line to add to the pin."""
    system, machine = platform.system(), platform.machine().lower()
    arch = {"arm64": "aarch64", "aarch64": "aarch64", "x86_64": "x86_64", "amd64": "x86_64"}.get(machine)
    if arch is None:
        raise VaultError(f"no uv build is pinned for this CPU ({platform.machine()})",
                         code=output.EXIT_DENY)
    if system == "Darwin":
        return f"{arch}-apple-darwin"
    if system == "Linux":
        # `libc_ver()` reads the ELF of the running interpreter and answers ('glibc', '2.x') there or
        # ('', '') on musl, which is the cheapest stdlib signal that distinguishes them. It is a
        # HEURISTIC — a glibc interpreter on a musl host would be misread — and the cost of being
        # wrong is a uv that will not start, which is loud. Recorded rather than smoothed.
        libc = platform.libc_ver()[0]
        return f"{arch}-unknown-linux-{'gnu' if libc == 'glibc' else 'musl'}"
    raise VaultError(f"no uv build is pinned for this platform ({system})", code=output.EXIT_DENY)


def artifact(pin: dict, target: str | None = None) -> tuple[str, str, str, str]:
    """`(target, url, sha256, member)` for this machine, from the pin."""
    target = target or platform_target()
    digest = pin["artifacts"].get(target)
    if digest is None:
        known = ", ".join(sorted(pin["artifacts"]))
        raise VaultError(f"uv {pin['version']} is not pinned for {target}",
                         code=output.EXIT_DENY,
                         hint=f"pinned targets: {known} — add one to {PIN_REL} to support this host")
    url = str(pin["url_template"]).format(version=pin["version"], target=target)
    member = str(pin.get("member_template") or "uv-{target}/uv").format(target=target)
    return target, url, digest, member


# --- where things land -----------------------------------------------------------------------------
def tools_dir(root: Path | None = None) -> Path:
    return engine_root(root) / TOOLS_DIRNAME


def uv_path(root: Path | None = None, pin: dict | None = None) -> Path:
    pin = pin or load_pin(root)
    return tools_dir(root) / "uv" / str(pin["version"]) / "uv"


def project_env(root: Path | None = None) -> Path:
    """The environment `uv sync` manages. Inside `tools/` for decision 3's reason, and named through
    `UV_PROJECT_ENVIRONMENT` rather than left at uv's `<project>/.venv` default — the project IS the
    engine root, and `.venv` there would be a writable directory in the sealed part of the tree."""
    return tools_dir(root) / VENV_DIRNAME


def engine_python(root: Path | None = None) -> Path | None:
    """The PINNED ENGINE INTERPRETER, or None when the engine has not been provisioned.

    This is the answer to ADR-013's carried dependency inversion: the compiled core's O_NONBLOCK
    helper spawned a bare `python3` from PATH, inside a binary whose whole point is not needing one.
    `cli/src/core/enginepython.ts` is the port that fixes it and it computes exactly this path —
    `test/run_provision.py` compares the two answers rather than trusting that they agree."""
    p = project_env(root) / "bin" / "python3"
    return p if os.access(str(p), os.X_OK) else None


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# --- the refusals ----------------------------------------------------------------------------------
def offline_refusal(root: Path | None = None, pin: dict | None = None) -> str:
    """The exact two-step an air-gapped operator runs, as one block of copy-pasteable shell.

    It names all three things a manual install needs and that an operator cannot derive: the URL, the
    expected sha256, and the destination path. A refusal that says "no network" and stops is what
    turns an air-gapped machine into a dead end."""
    pin = pin or load_pin(root)
    target, url, digest, member = artifact(pin)
    dest = uv_path(root, pin)
    tmp = f"/tmp/uv-{target}.tar.gz"
    return (
        f"plainkeep needs uv {pin['version']} and cannot reach the network to fetch it.\n"
        f"\n"
        f"Fetch it on a machine that can, verify it, and put it where the engine expects it:\n"
        f"\n"
        f"  curl -fsSL -o {tmp} \\\n"
        f"    {url}\n"
        f"  echo '{digest}  {tmp}' | shasum -a 256 -c -\n"
        f"  mkdir -p {dest.parent}\n"
        f"  tar -xzOf {tmp} {member} > {dest}\n"
        f"  chmod 555 {dest}\n"
        f"\n"
        f"Nothing was left half-installed; re-run the same command when it is in place."
    )


def _refuse_offline(root: Path | None, pin: dict, why: str) -> None:
    raise VaultError(f"cannot download uv {pin['version']} ({why})",
                     code=output.EXIT_UNEXPECTED,
                     hint=offline_refusal(root, pin))


# --- the bootstrap ---------------------------------------------------------------------------------
def _fetch(url: str, dest: Path, *, timeout: int = NET_TIMEOUT_SECONDS) -> str:
    """Download `url` to `dest`. Returns which transport did it, for the report.

    TWO TRANSPORTS, and the second one is not belt-and-braces — it is the difference between working
    and not on a stock macOS. A python.org CPython ships with NO CA bundle until someone runs
    `Install Certificates.command`, so `ssl` there fails every HTTPS request with
    CERTIFICATE_VERIFY_FAILED while `curl` (system trust store) succeeds. Measured on this machine,
    which is in exactly that state: urllib → certificate verify failed, curl → 200.

    Handing that to the operator as "cannot reach the network" would be a LIE — the network is fine
    and the trust store is not — so the two failures are told apart and only the second one is
    reported as offline.

    **Does the fallback weaken the integrity story? No, and this is the reason the pin is worth
    having.** What is trusted is the sha256 recorded in the engine tree, checked after the bytes
    land, whichever transport brought them. TLS here protects the request's privacy and its redirect
    chain; it is not what makes the binary the right binary."""
    req = urllib.request.Request(url, headers={"User-Agent": "plainkeep-provision"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r, open(dest, "wb") as out:
            shutil.copyfileobj(r, out)
        return "urllib"
    except urllib.error.URLError as e:
        if not isinstance(getattr(e, "reason", None), ssl.SSLError):
            raise
        curl = shutil.which("curl")
        if curl is None:
            raise VaultError(
                "this interpreter cannot verify TLS certificates and there is no curl to fall back "
                f"on ({e.reason})",
                code=output.EXIT_UNEXPECTED,
                hint="a python.org CPython on macOS ships without a CA bundle until you run\n"
                     "     /Applications/Python\\ 3.x/Install\\ Certificates.command\n"
                     "     — or install curl, or provide the download by hand (see below)")
        r = subprocess.run([curl, "-fsSL", "--max-time", str(timeout), "-o", str(dest), url],
                           capture_output=True, text=True)
        if r.returncode != 0:
            raise urllib.error.URLError(f"curl exit {r.returncode}: {(r.stderr or '').strip()[:200]}")
        return "curl"


def _extract_member(archive: Path, member: str, dest: Path) -> None:
    """Extract ONE named member, by name, writing its bytes to `dest`.

    Not `TarFile.extractall`, and not `extract()` either: both write paths the ARCHIVE chooses, and
    the archive is a downloaded file. Python 3.12 added `filter="data"` for exactly this and the
    floor is 3.10, so the safe spelling here is to read the one member we asked for and write it
    ourselves — the archive never names a destination."""
    with tarfile.open(archive, "r:gz") as tf:
        try:
            info = tf.getmember(member)
        except KeyError:
            raise VaultError(f"the uv archive does not contain {member}",
                             hint="the pin's member_template does not match this release's layout")
        if not info.isfile():
            raise VaultError(f"{member} in the uv archive is not a regular file")
        src = tf.extractfile(info)
        if src is None:
            raise VaultError(f"cannot read {member} from the uv archive")
        with open(dest, "wb") as out:
            shutil.copyfileobj(src, out)


# --- the gate ---------------------------------------------------------------------------------------
def require_delivered_intact(root: Path | None = None) -> None:
    """Refuse to act on an engine tree whose contents are not what `install()` recorded.

    **THE WHOLE TREE, not a named subset, and the narrowing is what this replaces.** This gate used to
    be spelled `digest_problems(root, only=(PYPROJECT_REL, LOCK_REL))` at the one call site that had
    it — "the two files we are about to hand to uv", two digests instead of ~114. The file that was
    left out is `bin/lib/uvpin.json`, which is the file that decides WHAT GETS DOWNLOADED AND RUN: it
    carries both the URL and the sha256 the download is held to, so tampering with it is
    self-consistent and the "verify before making it executable" step verifies the attacker's bytes
    against the attacker's digest. Measured on a throwaway install: a payload served from a local
    `file://` URL was installed, sealed 0555, and executed twice by `--sync`, exit 0, on both the
    Python and the compiled-core path — while `digest_problems(root)` reported the tampered pin the
    entire time.

    So the allowlist is gone rather than extended. An allowlist has to be re-derived by hand every
    time this module learns to read another delivered file, and getting that wrong is silent and
    remote-code-execution shaped; the full check is derived from the manifest and cannot go stale.
    What it costs is ~114 sha256 of small files — 50 ms, measured — on an operation whose next steps
    are a 35 MB download and a package install. That is not a budget worth optimising against the one
    property this module exists to have.

    Empty on a checkout (`_looks_installed` is false there), so a contributor's tree is unaffected."""
    problems = enginetree.digest_problems(engine_root(root))
    if problems:
        raise VaultError(
            "refusing to provision from a delivered project that does not match its "
            "recorded checksums:\n  " + "\n  ".join(problems),
            code=output.EXIT_DENY,
            hint="the engine tree was modified after it was installed — reinstall it "
                 "(python3 bin/lib/enginetree.py --install <checkout> --force)")


def ensure_uv(root: Path | None = None, *, allow_network: bool = True,
              pin: dict | None = None, check_digests: bool = True) -> Path:
    """The pinned uv, downloading and verifying it if this engine has not got it yet.

    THE GATE RUNS HERE, not only in `sync()`, because this is reachable on its own —
    `provision.py --ensure-uv` and `plainkeep-core --core-provision ensure-uv` both land here without
    passing through `sync()`, and a gate that only `sync()` ran left the bootstrap ungated on the two
    commands whose entire job is to install and seal an executable. `check_digests=False` is for the
    one caller that has already asked (`sync()`), so the tree is hashed once per provisioning run.

    IDEMPOTENT, and idempotent by CONTENT rather than by presence: an existing binary is re-hashed
    against the pin, so a truncated download from a killed run is replaced instead of being trusted
    for the life of the engine. That costs one sha256 of ~35 MB (~25 ms) on every provisioning call
    and nothing at all on a dispatch, which never calls this."""
    if check_digests:
        require_delivered_intact(root)
    pin = pin or load_pin(root)
    dest = uv_path(root, pin)
    _, url, digest, member = artifact(pin)
    if dest.is_file():
        if sha256_file(dest) == digest:
            return dest
        # A binary that does not match the pin is not a uv we are willing to run, and leaving it in
        # place would make every later call re-fail identically. `tools/uv/<version>/` is sealed, so
        # the removal has to unseal it first.
        _unseal(dest.parent)
        dest.unlink()
    if not allow_network:
        _refuse_offline(root, pin, "offline")
    staging = tools_dir(root) / f".incoming-uv-{pin['version']}.{os.getpid()}"
    try:
        staging.mkdir(parents=True)
    except OSError as e:
        raise VaultError(f"cannot write into {tools_dir(root)} ({e})",
                         hint="the engine's tools/ directory is the one writable part of an "
                              "installed tree — reinstall the engine if it is missing")
    archive = staging / f"uv-{pin['version']}.tar.gz"
    try:
        try:
            _fetch(url, archive)
        except urllib.error.HTTPError as e:
            raise VaultError(f"cannot download uv {pin['version']} (HTTP {e.code} from {url})",
                             code=output.EXIT_UNEXPECTED,
                             hint=offline_refusal(root, pin))
        except (urllib.error.URLError, OSError, TimeoutError) as e:
            _refuse_offline(root, pin, str(getattr(e, "reason", None) or e))
        got = sha256_file(archive)
        if got != digest:
            # THE POINT OF THE PIN. Refuse, and delete — a mismatched archive left on disk is
            # something a later step might reach for.
            raise VaultError(
                f"the uv {pin['version']} download does not match its pinned sha256",
                code=output.EXIT_DENY,
                hint=f"expected {digest}\n     got      {got}\n"
                     f"     from     {url}\n"
                     "the download was deleted and nothing was installed")
        binary = staging / "uv"
        _extract_member(archive, member, binary)
        archive.unlink()
        # Sealed BEFORE it is reachable under its final name: what lands in `tools/uv/<version>/` is
        # already verified and already read-only, so there is no window in which a writable,
        # executable, unverified binary sits at the path everything else will run.
        binary.chmod(0o555)
        dest.parent.parent.mkdir(parents=True, exist_ok=True)
        if dest.parent.exists():                       # a killed run between rename and seal
            _unseal(dest.parent)
            shutil.rmtree(dest.parent, ignore_errors=True)
        os.rename(staging, dest.parent)
        staging = None  # type: ignore[assignment]
        dest.parent.chmod(0o555)
        return dest
    finally:
        # NO PARTIAL PROVISIONING IS LEFT BEHIND — every failure path above lands here, including the
        # checksum refusal, and the staging tree (with the archive in it) goes.
        if staging is not None and staging.exists():
            shutil.rmtree(staging, ignore_errors=True)


def _unseal(d: Path) -> None:
    """Make a provisioned artifact directory writable again, so it can be replaced. Best effort for
    `_chmod_tree`'s reason: this is only ever a prelude to a removal that fails loudly on its own."""
    for p in [d, *d.rglob("*")]:
        try:
            p.chmod(0o755 if p.is_dir() else 0o644)
        except OSError:
            pass


def system_uv() -> str | None:
    """Whichever uv is on the operator's PATH, or None. FOR REPORTING ONLY — `doctor` says one line
    about it so the operator is not confused about which uv ran. Nothing in this module dispatches to
    it, and nothing in this module calls this function."""
    return shutil.which("uv")


# --- the delivered project -------------------------------------------------------------------------
_ARRAY_RE = re.compile(r"^\s*([A-Za-z0-9_.\-]+)\s*=\s*\[(.*?)\]\s*$", re.S | re.M)
# BOTH TOML string forms, because the dependency table needs both: an environment marker contains
# double quotes (`platform_system == "Darwin"`), so those entries are written as TOML LITERAL strings
# in single quotes. A double-quote-only reader silently returned the marker's inner fragments —
# `["Darwin", "x86_64", "fastembed>=0.4"]` — which is a dependency list that parses, resolves to
# nothing recognisable, and would have been noticed only by whoever ran the install. Caught by the
# tomllib cross-check in test/run_provision.py; the regex is fixed here and the cross-check stays.
_STRING_RE = re.compile(r'"((?:[^"\\]|\\.)*)"' + r"|'([^']*)'")


def _toml_string_arrays(text: str, table: str) -> dict[str, list[str]]:
    """Every `key = [ "…", "…" ]` in one named TOML table.

    A DELIBERATELY TINY reader, not a TOML parser, and it is here because `tomllib` is 3.11+ while
    ADR-009's floor is 3.10 — an engine that could only read its own dependency manifest on a newer
    interpreter than it claims to support would be a contract that documents itself as broken. The
    subset it handles is the one `pyproject.toml`'s dependency tables actually use: a table header,
    keys whose values are arrays of basic strings, possibly spanning lines. Anything else in the file
    is ignored rather than mis-parsed.

    It is not trusted on its own: `test/run_provision.py` re-reads the same file with `tomllib` on
    3.11+ and asserts the two agree, so the shortcut has a real oracle rather than a self-test."""
    body, depth = [], False
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("[") and not s.startswith("[["):
            depth = s.rstrip() == f"[{table}]"
            continue
        if depth:
            body.append(line)
    out: dict[str, list[str]] = {}
    for m in _ARRAY_RE.finditer("\n".join(body)):
        out[m.group(1)] = [(basic.replace('\\"', '"') if basic else literal)
                           for basic, literal in _STRING_RE.findall(m.group(2))]
    return out


def _pyproject_text(root: Path | None = None) -> str:
    """The delivered `pyproject.toml`, GATED — one place, so `base_deps`/`extras`/`extra_deps` and
    everything downstream of them are covered by reading the file through here.

    The gate belongs on this read and not only on `sync()`'s, because THIS is the read the product
    actually performs: `setuplib.search_deps()`/`models_deps()` turn these strings into a real
    `pip install` command line (`bin/setup/run.py`), while `sync()` has no `plainkeep <verb>` caller
    at all. Hot-patching one string into the delivered `[search]` extra put an attacker-chosen package
    into that command line while `digest_problems` reported the file as tampered — the same
    ADR-019 shape as the pin: the evidence existed and nothing consulted it."""
    p = engine_root(root) / PYPROJECT_REL
    require_delivered_intact(root)
    try:
        return p.read_text(encoding="utf-8")
    except OSError as e:
        raise VaultError(f"cannot read the delivered project at {p} ({e})",
                         hint="pyproject.toml and uv.lock are engine-owned and ship with the tree — "
                              "reinstall the engine")


def base_deps(root: Path | None = None) -> list[str]:
    """`[project].dependencies` — EMPTY, and the emptiness is the contract (ADR-009's stdlib floor).
    Read rather than asserted so that a dependency added there reddens `test/run_provision.py`
    instead of quietly becoming a required install."""
    return _toml_string_arrays(_pyproject_text(root), "project").get("dependencies", [])


def extras(root: Path | None = None) -> dict[str, list[str]]:
    """`[project.optional-dependencies]` — the frozen dependency matrix, as delivered.

    THIS IS THE SOURCE OF TRUTH the product reads. `setuplib.SEARCH_DEPS` and the `models` layer's
    package list used to be hand-written Python lists that happened to agree with
    `requirements-search.txt`; they now come from here, so the extra a `uv sync` installs and the set
    `plainkeep setup` installs cannot drift apart by editing one of them."""
    return _toml_string_arrays(_pyproject_text(root), "project.optional-dependencies")


def extra_deps(name: str, root: Path | None = None) -> list[str]:
    e = extras(root)
    if name not in e:
        raise VaultError(f"the delivered project declares no [{name}] extra",
                         hint=f"declared extras: {', '.join(sorted(e)) or '(none)'}")
    return e[name]


# --- uv sync ---------------------------------------------------------------------------------------
def sync_env(root: Path | None = None, *, offline: bool = False) -> dict[str, str]:
    """The environment every `uv` invocation runs under. One place, because each entry is a decision:

    `UV_NO_CONFIG=1`   — the operator's `uv.toml` / `[tool.uv]` overrides must not steer a resolution
                         the engine pinned. Same reason PATH's uv is ignored.
    `UV_PROJECT_ENVIRONMENT`, `UV_PYTHON_INSTALL_DIR` — keep the environment and any managed
                         interpreter inside `tools/`, so both roll back with the engine version.
    `UV_PYTHON_DOWNLOADS` — left at uv's default (automatic) ON PURPOSE: it is what makes the
                         no-system-python3 gate pass. A managed CPython is a Python distribution,
                         which is what uv provisions; it is not a system package and not a model
                         weight, and neither of those is ever fetched here.
    `UV_OFFLINE`       — the honest spelling of offline: uv refuses rather than reaching out.
    """
    env = dict(os.environ)
    env.update({
        "UV_NO_CONFIG": "1",
        "UV_PROJECT_ENVIRONMENT": str(project_env(root)),
        "UV_PYTHON_INSTALL_DIR": str(tools_dir(root) / PYTHON_DIRNAME),
    })
    if offline:
        env["UV_OFFLINE"] = "1"
    return env


def sync_argv(root: Path | None = None, *, extras_wanted: tuple[str, ...] = (),
              uv: Path | None = None) -> list[str]:
    """The exact `uv sync` command, as a list, so a test can assert what it is without running it.

    `--frozen` is the load-bearing flag: it uses `uv.lock` AS DELIVERED, never re-resolving and never
    writing a new lock — which a plain `uv sync` would do, into a sealed tree, so the first sign of it
    would be a permission error rather than a changed resolution.

    **WHAT `--frozen` DOES NOT DO, measured rather than assumed** — and the reason `check_argv` below
    exists. `--frozen` means "do not update the lock and do not check it": with `pyproject.toml`
    edited to require `pk-sample==2.0.0` against a lock pinning `1.0.0`, `uv sync --frozen` installed
    1.0.0 and exited 0. The flag that refuses is `--locked` / `uv lock --check`, and the two are
    mutually exclusive on one command line (`uv sync --frozen --locked` is a usage error). So the
    agreement between the delivered project and the delivered lock is checked FIRST, as its own
    invocation, and the sync itself stays `--frozen`.

    `--no-config` doubles `UV_NO_CONFIG` on the command line because a flag survives an environment
    a caller rebuilt."""
    root_p = engine_root(root)
    cmd = [str(uv or uv_path(root)), "sync", "--frozen", "--no-config",
           "--project", str(root_p)]
    for name in extras_wanted:
        cmd += ["--extra", name]
    return cmd


def check_argv(root: Path | None = None, uv: Path | None = None) -> list[str]:
    """`uv lock --check` — "the delivered lock still describes the delivered project", asked before
    anything is installed. Runs offline against a complete lock (measured: it caught an edited
    `pyproject.toml` with `--offline` set, in 13 ms)."""
    return [str(uv or uv_path(root)), "lock", "--check", "--no-config",
            "--project", str(engine_root(root))]


def sync(root: Path | None = None, *, extras_wanted: tuple[str, ...] = (),
         offline: bool = False, allow_network: bool | None = None,
         check_digests: bool = True) -> list[str]:
    """Provision the engine's Python environment from the DELIVERED project and lock.

    Order matters and is the gate 4b actually asks for: the delivered tree is checked against the
    digests recorded at install time BEFORE uv is downloaded, made executable or allowed to read
    anything, so a tampered lock — or a tampered PIN, which is what chooses the binary — fails its
    checksum rather than installing a resolution nobody chose. `ensure_uv` gates too; the flag is
    passed down so the tree is hashed once rather than twice."""
    root_p = engine_root(root)
    if check_digests:
        require_delivered_intact(root_p)
    if allow_network is None:
        allow_network = not offline
    uv = ensure_uv(root_p, allow_network=allow_network, check_digests=False)
    env = sync_env(root_p, offline=offline)
    chk = subprocess.run(check_argv(root_p, uv=uv), env=env, capture_output=True, text=True)
    if chk.returncode != 0:
        raise VaultError(
            "the delivered uv.lock no longer describes the delivered pyproject.toml",
            code=output.EXIT_DENY,
            hint="nothing was installed. `uv sync --frozen` would have used the stale lock and "
                 "exited 0 (measured), so this is checked separately — reinstall the engine, or "
                 "re-run `uv lock` in the checkout and ship the pair together")
    cmd = sync_argv(root_p, extras_wanted=extras_wanted, uv=uv)
    r = subprocess.run(cmd, env=env, capture_output=True, text=True)
    if r.returncode != 0:
        tail = (r.stderr or r.stdout or "").strip().splitlines()[-8:]
        # Parenthesised deliberately: `a + b if tail else c` parses the way this wants, and relying
        # on that is the kind of line a later edit gets wrong silently.
        message = ("uv sync failed:\n  " + "\n  ".join(tail)) if tail else "uv sync failed"
        raise VaultError(message, code=output.EXIT_UNEXPECTED,
                         # NOT "the environment is unchanged" — that is uv's business and this module
                         # has not measured it. What IS known is where to look and what to re-run.
                         hint=f"the environment uv manages is {project_env(root_p)} — it is "
                              "reconstructible: remove it and provision again")
    return cmd


def installed_dists(root: Path | None = None) -> dict[str, str]:
    """`{name: version}` for everything present in the provisioned environment, read from the
    `.dist-info` directories rather than by running pip — there is no pip in a uv-managed
    environment, and the point of the check is what is ON DISK."""
    out: dict[str, str] = {}
    site = project_env(root) / "lib"
    if not site.is_dir():
        return out
    for info in site.glob("python*/site-packages/*.dist-info"):
        name, _, version = info.name[: -len(".dist-info")].rpartition("-")
        if name:
            out[re.sub(r"[-_.]+", "-", name).lower()] = version
    return out


# --- CLI ---------------------------------------------------------------------------------------------
_USAGE = ("usage: provision.py --print [pin|target|uv|python|env|extras|offline|system-uv]\n"
          "       provision.py --ensure-uv [--offline]\n"
          "       provision.py --sync [--extra NAME]... [--offline]")


def main(argv: list[str]) -> int:
    """The provisioning surface, a MODULE CLI for `enginetree.py --install`'s reason: a
    `plainkeep provision` verb would change the verb surface, plainkeep.json and the completion
    catalogs, none of which this task is scoped to move. `plainkeep setup` and the harness call it."""
    if not argv:
        print(_USAGE, file=sys.stderr)
        return output.EXIT_USAGE
    offline = "--offline" in argv
    argv = [a for a in argv if a != "--offline"]
    try:
        if argv[0] == "--print":
            what = argv[1] if len(argv) > 1 else "pin"
            if what == "pin":
                pin = load_pin()
                target, url, digest, member = artifact(pin)
                print(json.dumps({"version": pin["version"], "target": target, "url": url,
                                  "sha256": digest, "member": member,
                                  "dest": str(uv_path(None, pin))}, indent=2))
            elif what == "target":
                print(platform_target())
            elif what == "uv":
                print(uv_path())
            elif what == "python":
                p = engine_python()
                print(p if p else "", end="" if p is None else "\n")
                return output.EXIT_OK if p else output.EXIT_NOT_FOUND
            elif what == "env":
                print(project_env())
            elif what == "extras":
                print(json.dumps({"dependencies": base_deps(), **extras()}, indent=2))
            elif what == "offline":
                print(offline_refusal())
            elif what == "system-uv":
                print(system_uv() or "")
            else:
                print(_USAGE, file=sys.stderr)
                return output.EXIT_USAGE
            return output.EXIT_OK
        if argv[0] == "--ensure-uv":
            print(ensure_uv(allow_network=not offline))
            return output.EXIT_OK
        if argv[0] == "--sync":
            wanted = tuple(argv[i + 1] for i, a in enumerate(argv) if a == "--extra"
                           and i + 1 < len(argv))
            sync(extras_wanted=wanted, offline=offline)
            print(project_env())
            return output.EXIT_OK
    except VaultError as e:
        output.fail(e.code, e.message, e.hint)
        return e.code
    print(_USAGE, file=sys.stderr)
    return output.EXIT_USAGE


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
