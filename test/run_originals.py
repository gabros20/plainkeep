#!/usr/bin/env python3
"""
run_originals.py — APPEND-ONLY originals (Phase 2 Task 1c), proved by filesystem side effect.

`~/files/**/in/` holds evidence. The rule used to be READ-ONLY, and it was enforced nowhere that
mattered: `files ingest --client` exists to put an original there, so the one verb that writes an
original was the one verb outside the wall, and its `while dest.exists(): ...` loop was the only
thing between an arrival and an overwrite. The rule is now: an original may ARRIVE by ATOMIC
CREATION; overwrite, replace, mutate and delete of an existing leaf never happen.

The two claims this suite has to make good on are different in kind, so they are tested differently:

  * the WALL admits only the create shape under in/ — an exit code plus a filesystem walk, from a
    real subprocess, because `output.fail` exits the process it refuses;
  * the CREATE-ONLY guarantee is the SYSCALL's, not a prior `exists()` test — which can only be shown
    under contention, so this suite RACES, and keeps the legacy shape alive beside the new one to
    prove the harness is still racing. `case_race_ab` asserts that the BASE loop LOSES FILES. If that
    check ever goes green the gate below has stopped exercising the failing region and proves
    nothing; a suite that can no longer reproduce the bug cannot claim to have fixed it.

Assertions about writes are made by walking the tree and hashing every file, before and after.
Offline, stdlib only.
"""
from __future__ import annotations
import atexit
import errno
import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path
from lib.hermetic import scratch_root, seal
seal()   # hermetic: an empty throwaway registry, never the developer's real vault

REPO = Path(__file__).resolve().parents[1]
GREEN, RED, DIM, BOLD, RESET = "\033[32m", "\033[31m", "\033[2m", "\033[1m", "\033[0m"
results: list[tuple[str, bool, str]] = []

# --- the fixture roots -----------------------------------------------------------------------
# `~/files` for this suite. Carried in a PRIVATE variable and only then copied into
# PLAINKEEP_ROOTS_HOME, so a child process inherits the PARENT's tree instead of minting its own:
# the wall's ~/files anchor is derived from this value, and a child that re-rolled it would be
# walled off from the very files the parent is asserting about. A private name also means an
# inherited PLAINKEEP_ROOTS_HOME (a developer's own lever) can never be mistaken for ours.
_INHERIT = "PLAINKEEP_ORIGINALS_FIXTURE"
os.environ.pop("PLAINKEEP_TEST_HOME", None)   # it outranks ROOTS_HOME in both wall.py and paths.py
_roots = os.environ.get(_INHERIT)
if not _roots:
    _roots = tempfile.mkdtemp(prefix="pk-originals-roots-")
    atexit.register(shutil.rmtree, _roots, True)
os.environ[_INHERIT] = _roots
os.environ["PLAINKEEP_ROOTS_HOME"] = _roots
ROOTS = Path(_roots)

VAULT = Path(scratch_root())   # PLAINKEEP_HOME: a marked throwaway vault, before the engine import
STAGE = Path(tempfile.mkdtemp(prefix="pk-originals-stage-"))
atexit.register(shutil.rmtree, STAGE, True)


def _load(path: Path, name: str):
    """Load an engine module by FILE. `bin/lib` cannot be imported as `lib` here — `test/lib` already
    owns that name in this process — and `vaultio` supports exactly this (its relative import fails,
    and it falls back to putting `bin/lib` on the path itself)."""
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    sys.modules[name] = m
    spec.loader.exec_module(m)
    return m


vaultio = _load(REPO / "bin" / "lib" / "vaultio.py", "pk_vaultio")


def check(name: str, cond: bool, detail: str = "") -> None:
    results.append((name, bool(cond), detail))


def hashes(root: Path) -> dict[str, str]:
    """Every path under `root` -> a fingerprint. A SYMLINK is recorded by its target STRING and never
    followed: "nothing changed" must not be answerable by reading somewhere else."""
    out: dict[str, str] = {}
    for p in sorted(root.rglob("*")):
        rel = str(p.relative_to(root))
        if p.is_symlink():
            out[rel] = "symlink -> " + os.readlink(p)
        elif p.is_file():
            out[rel] = hashlib.sha256(p.read_bytes()).hexdigest()
        else:
            out[rel] = "dir"
    return out


def hub(name: str) -> Path:
    """A fresh `~/files/clients/<name>/in`, created the only way the wall now allows."""
    d = ROOTS / "files" / "clients" / name / "in"
    vaultio.mkdir(d, parents=True, exist_ok=False)
    return d


def staged(name: str, data: bytes) -> Path:
    d = Path(tempfile.mkdtemp(dir=STAGE))
    p = d / name
    p.write_bytes(data)
    return p


@contextmanager
def no_hardlinks():
    """Force the CROSS-DEVICE branch by making `link(2)` report EXDEV for the INCOMING SOURCE only.

    This exercises the branch, not a second volume: the suite mounts nothing. What it does prove is
    that the fallback's create-only guarantee is `O_EXCL`'s and not a leftover of `os.link`.

    A link FROM A STAGING LEAF is left working on purpose. EXDEV means "these two paths are on
    different volumes", and the leaf `_create_only_copy` mints is placed on the destination's own
    volume by construction (`vaultio._staging_dir` walks up only while the device is unchanged), so a
    real EXDEV there is impossible — faking one would simulate a filesystem that cannot exist and
    would test the wrong branch. The fixture keys on the staging PREFIX rather than on directory
    identity, which was the same claim while staging happened to sit beside the destination and
    stopped being true when it moved out of the append-only tree (NEW-3)."""
    real = os.link

    def fake(src, dst, **kw):
        if not Path(src).name.startswith(vaultio.ARRIVING_PREFIX):
            raise OSError(errno.EXDEV, "forced cross-device link")
        return real(src, dst, **kw)
    os.link = fake
    try:
        yield
    finally:
        os.link = real


@contextmanager
def no_hardlinks_at_all():
    """A filesystem with NO hard links (exFAT, some FUSE mounts): every `link(2)` reports EPERM.

    This is the only configuration in which `_create_only_copy` cannot stage, so it is the only one
    that reaches `_direct_create_only_copy` — the shape whose partial-write window is documented and
    deliberately kept as the fallback rather than the norm."""
    real = os.link

    def fake(src, dst, **kw):
        raise OSError(errno.EPERM, "filesystem has no hard links")
    os.link = fake
    try:
        yield
    finally:
        os.link = real


def arriving_debris(d: Path) -> list[str]:
    """Staging leaves left behind, under `in/` AND in the directory staging actually uses.

    Both are walked on purpose. A leaf under `in/` is PERMANENT — append-only forbids removing it —
    and one in the staging directory is merely litter, but a check that looked only where debris can
    no longer appear would be green by construction. Names are returned prefixed with where they were
    found so a failure says which of the two it is."""
    seen = []
    for where in {d, vaultio._staging_dir(d / "x")}:
        seen += [f"{'in/' if where == d else 'staging/'}{p.name}" for p in where.iterdir()
                 if p.name.startswith(vaultio.ARRIVING_PREFIX)]
    return sorted(seen)


@contextmanager
def copy_fails_midway():
    """A copy that dies after writing some bytes — the only way to observe the partial-file path."""
    real = shutil.copyfileobj

    def fake(inp, out, *a, **kw):
        out.write(b"HALF")
        raise OSError(errno.EIO, "forced I/O error mid-copy")
    shutil.copyfileobj = fake
    try:
        yield
    finally:
        shutil.copyfileobj = real


# ==============================================================================================
# A. The primitive: creation succeeds, and every other arrangement of the destination refuses.
# ==============================================================================================
def case_create() -> None:
    d = hub("create")
    src = staged("brief.pdf", b"THE ORIGINAL")
    dst = vaultio.move_create_only(src, d / "brief.pdf")
    check("create: a previously-absent leaf is created",
          dst.is_file() and dst.read_bytes() == b"THE ORIGINAL", str(hashes(d)))
    check("create: the source is gone (it was a move, not a copy)", not src.exists())


def _refuses(label: str, d: Path, dst: Path, src_bytes: bytes = b"AN ARRIVAL") -> None:
    """Attempt a create onto an occupied `dst` and assert nothing under `d` changed."""
    src = staged("arrival.pdf", src_bytes)
    before = hashes(d)
    err: BaseException | None = None
    try:
        vaultio.move_create_only(src, dst)
    except BaseException as e:   # noqa: BLE001 — the type IS the assertion, on the next line
        err = e
    after = hashes(d)
    check(f"{label}: refused with FileExistsError", isinstance(err, FileExistsError), repr(err))
    check(f"{label}: not one byte under in/ changed (walked + hashed)", before == after,
          f"before={before} after={after}")
    check(f"{label}: the arriving file is still where it was", src.is_file()
          and src.read_bytes() == src_bytes)


def case_existing_file() -> None:
    d = hub("existing")
    (d / "brief.pdf").write_bytes(b"THE SIGNED ORIGINAL")
    _refuses("existing file", d, d / "brief.pdf")


def case_symlink_live() -> None:
    """A symlink AT the destination pointing at a real original. `link(2)` does not follow it, so the
    original behind it cannot be reached by aiming at the link."""
    d = hub("symlink-live")
    (d / "brief.pdf").write_bytes(b"THE SIGNED ORIGINAL")
    (d / "latest.pdf").symlink_to(d / "brief.pdf")
    _refuses("symlink to an original", d, d / "latest.pdf")
    check("symlink to an original: the target still holds its own bytes",
          (d / "brief.pdf").read_bytes() == b"THE SIGNED ORIGINAL")


def case_symlink_dangling() -> None:
    """A DANGLING symlink is the sharper case: `exists()` says False, so an exists()-guarded write
    would go straight through it and create the file it points at."""
    d = hub("symlink-dangling")
    (d / "ghost.pdf").symlink_to(d / "nowhere.pdf")
    check("dangling symlink: exists() answers False (which is why exists() is not a guard)",
          not (d / "ghost.pdf").exists() and (d / "ghost.pdf").is_symlink())
    _refuses("dangling symlink", d, d / "ghost.pdf")
    check("dangling symlink: its target was NOT created", not (d / "nowhere.pdf").exists())


def case_directory() -> None:
    d = hub("directory")
    (d / "brief.pdf").mkdir()
    _refuses("directory at the destination", d, d / "brief.pdf")


# ==============================================================================================
# A2. The ARRIVED LEAF is an inode the vault OWNS — nothing outside `in/` can still write to it.
#
# `link(2)` hands out a second NAME for one inode, and on macOS it FOLLOWS a symlink. So both shapes
# below used to file something the vault did not own: the other name stayed outside `in/`, stayed
# writable by anything, and a later write through it edited the "original" with no verb, no wall, no
# exit code and no trace — leaving the shadow note's sha256 a lie. Reproduced end-to-end before the
# fix. These assert the byte-level consequence, not the mechanism that now prevents it.
# ==============================================================================================
def _stays_put(label: str, d: Path, leaf: Path, outside: Path, was: bytes) -> None:
    """Write through `outside` — the handle the world beyond the vault kept — and assert the arrived
    leaf did not move a byte. This is the assertion the whole finding reduces to."""
    before = hashes(d)
    outside.write_bytes(b"TAMPERED CONTRACT AMOUNT")
    check(f"{label}: the leaf is not a second name for the outside file (own inode, st_nlink=1)",
          leaf.stat().st_ino != outside.stat().st_ino and leaf.stat().st_nlink == 1,
          f"leaf nlink={leaf.stat().st_nlink}")
    check(f"{label}: writing through the outside name changed NOTHING under in/",
          before == hashes(d), f"before={before} after={hashes(d)}")
    check(f"{label}: the original still holds the bytes it arrived with", leaf.read_bytes() == was)


def case_symlink_source() -> None:
    """The drop-folder shape: `ln -s ~/scans/real-brief.pdf ~/drop/brief.pdf`, then ingest the link."""
    d = hub("symlink-source")
    target = staged("real-brief.pdf", b"THE SIGNED ORIGINAL")
    link = target.parent / "brief.pdf"
    link.symlink_to(target)
    leaf = vaultio.move_create_only(link, d / "brief.pdf")
    check("symlink source: it ARRIVES (refusing would be sound, and would break an ordinary case)",
          leaf.is_file() and not leaf.is_symlink() and leaf.read_bytes() == b"THE SIGNED ORIGINAL")
    check("symlink source: the LINK is gone and its target is untouched — never ours to delete",
          not link.is_symlink() and target.is_file())
    _stays_put("symlink source", d, leaf, target, b"THE SIGNED ORIGINAL")


def case_hardlinked_source() -> None:
    """A source that already carried a second hard link — a backup tool, a de-duplicated download."""
    d = hub("shared-source")
    src = staged("brief.pdf", b"THE SIGNED ORIGINAL")
    alias = src.parent / "alias.pdf"
    os.link(src, alias)
    check("shared source: the fixture really is shared before ingest (st_nlink=2)",
          src.stat().st_nlink == 2)
    leaf = vaultio.move_create_only(src, d / "brief.pdf")
    check("shared source: it arrives", leaf.is_file()
          and leaf.read_bytes() == b"THE SIGNED ORIGINAL")
    check("shared source: the name we were handed is gone; the other one is not ours to remove",
          not src.exists() and alias.is_file())
    _stays_put("shared source", d, leaf, alias, b"THE SIGNED ORIGINAL")


def case_cross_device() -> None:
    d = hub("xdev")
    src = staged("brief.pdf", b"ACROSS THE DEVICE")
    with no_hardlinks():
        dst = vaultio.move_create_only(src, d / "brief.pdf")
    check("cross-device branch: the copy fallback creates the leaf",
          dst.is_file() and dst.read_bytes() == b"ACROSS THE DEVICE")
    check("cross-device branch: the source is gone", not src.exists())

    (d / "taken.pdf").write_bytes(b"ALREADY HERE")
    src2 = staged("brief.pdf", b"A SECOND ARRIVAL")
    before = hashes(d)
    err = None
    try:
        with no_hardlinks():
            vaultio.move_create_only(src2, d / "taken.pdf")
    except BaseException as e:   # noqa: BLE001
        err = e
    check("cross-device branch: O_EXCL refuses an occupied destination too",
          isinstance(err, FileExistsError), repr(err))
    check("cross-device branch: nothing under in/ changed", before == hashes(d))
    check("cross-device branch: no staging leaf survives (append-only makes debris permanent)",
          arriving_debris(d) == [], str(arriving_debris(d)))


def case_no_hardlinks_at_all() -> None:
    """The one filesystem shape that cannot stage — no hard links anywhere — so `O_EXCL` is applied
    straight to the destination. Create-only still holds; the partial-write window is the documented
    price, which is why this is the fallback and not the shape."""
    d = hub("nolinks")
    src = staged("brief.pdf", b"ON A FILESYSTEM WITHOUT LINKS")
    with no_hardlinks_at_all():
        dst = vaultio.move_create_only(src, d / "brief.pdf")
    check("no-hardlinks branch: the direct O_EXCL copy still creates the leaf",
          dst.is_file() and dst.read_bytes() == b"ON A FILESYSTEM WITHOUT LINKS")
    check("no-hardlinks branch: the source is gone and no staging leaf is left",
          not src.exists() and arriving_debris(d) == [])

    src2 = staged("brief.pdf", b"A SECOND ARRIVAL")
    before = hashes(d)
    err = None
    try:
        with no_hardlinks_at_all():
            vaultio.move_create_only(src2, d / "brief.pdf")
    except BaseException as e:   # noqa: BLE001
        err = e
    check("no-hardlinks branch: O_EXCL still refuses an occupied destination",
          isinstance(err, FileExistsError), repr(err))
    check("no-hardlinks branch: nothing under in/ changed", before == hashes(d))


def case_destination_never_partial() -> None:
    """The destination NAME never exists in a half-written state.

    `_create_only_copy` fills a staging leaf and links it onto `dst` only once the bytes are there
    and verified, so a copy that dies mid-way cannot leave a truncated "original" — which under
    append-only would be PERMANENT, because nothing may ever replace that name. Cleanup after the
    fact cannot be told from never-creating-it by walking the tree afterwards, so this OBSERVES the
    destination from inside the copy, at the moment the old shape would have had it short."""
    d = hub("never-partial")
    src = staged("brief.pdf", b"AN ORIGINAL ARRIVING SLOWLY")
    dst = d / "brief.pdf"
    seen: list[bool] = []
    real = shutil.copyfileobj

    def observing(inp, out, *a, **kw):
        out.write(inp.read(4))          # four REAL bytes: whatever holds them is genuinely partial
        out.flush()
        seen.append(dst.exists())       # ...and at this instant, is the destination name occupied?
        return real(inp, out, *a, **kw)

    shutil.copyfileobj = observing
    try:
        with no_hardlinks():
            vaultio.move_create_only(src, dst)
    finally:
        shutil.copyfileobj = real
    check("never partial: the destination name did NOT exist while the copy was still running",
          seen == [False], f"observations={seen} (True = a truncated 'original' was visible)")
    check("never partial: and it holds every byte once the copy is done",
          dst.is_file() and dst.read_bytes() == b"AN ORIGINAL ARRIVING SLOWLY",
          f"bytes={dst.read_bytes()!r}")
    check("never partial: no staging leaf is left behind", arriving_debris(d) == [])


def case_partial_copy_cleanup() -> None:
    """A cross-device copy that fails halfway must leave NO file at the destination. Under
    append-only a truncated leaf would be permanent: nothing could ever replace it."""
    d = hub("partial")
    (d / "sibling.pdf").write_bytes(b"AN UNRELATED ORIGINAL")
    src = staged("brief.pdf", b"WOULD HAVE BEEN AN ORIGINAL")
    before = hashes(d)
    err = None
    try:
        with no_hardlinks(), copy_fails_midway():
            vaultio.move_create_only(src, d / "brief.pdf")
    except BaseException as e:   # noqa: BLE001
        err = e
    check("partial copy: the failure is raised, not swallowed", isinstance(err, OSError), repr(err))
    check("partial copy: NO half-written file is left at the destination",
          not (d / "brief.pdf").exists(), str(hashes(d)))
    check("partial copy: the rest of in/ is untouched", before == hashes(d))
    check("partial copy: the source still holds all of its bytes",
          src.read_bytes() == b"WOULD HAVE BEEN AN ORIGINAL")
    check("partial copy: no staging leaf survives the failure either",
          arriving_debris(d) == [], str(arriving_debris(d)))


def case_container_mkdir() -> None:
    """`new client` creates the empty `in/` container. That is a create, and the tolerant form is
    not — the wall can tell them apart because `mkdir(2)` fails EEXIST and `exist_ok=True` hides it."""
    d = ROOTS / "files" / "clients" / "container" / "in"
    vaultio.mkdir(d, parents=True, exist_ok=False)
    check("container: mkdir(exist_ok=False) under in/ is allowed", d.is_dir())
    err = None
    try:
        vaultio.mkdir(d, parents=True, exist_ok=False)
    except BaseException as e:   # noqa: BLE001
        err = e
    check("container: a second atomic mkdir refuses rather than passing silently",
          isinstance(err, FileExistsError), repr(err))


# ==============================================================================================
# B. The wall: every NON-atomic write shape under in/ is refused, in a real process, exit 5.
# ==============================================================================================
WORKER = '''
import sys
sys.path.insert(0, {binpath!r})
from lib import vaultio
op, dst = sys.argv[1], sys.argv[2]
src = sys.argv[3] if len(sys.argv) > 3 else None
if op == "write_text":
    vaultio.write_text(dst, "forged")
elif op == "write_text_claiming_create_only":
    vaultio.write_text(dst, "forged", create_only=True)
elif op == "write_bytes":
    vaultio.write_bytes(dst, b"forged")
elif op == "append_text":
    vaultio.append_text(dst, "appended")
elif op == "mkdir_tolerant":
    vaultio.mkdir(dst)
elif op == "move":
    vaultio.move(src, dst)
elif op == "copy2":
    vaultio.copy2(src, dst)
elif op == "replace":
    vaultio.replace(src, dst)
elif op == "create_only":
    vaultio.move_create_only(src, dst)
print("NOT REFUSED", file=sys.stderr)
'''

# A copy killed after some bytes are down, with NO unwinding: `os._exit` skips every `finally`, which
# is what makes it a faithful stand-in for SIGKILL and why it needs its own process.
KILL_MIDCOPY = '''
import os, shutil, sys
sys.path.insert(0, {binpath!r})
from lib import vaultio
dst, src = sys.argv[1], sys.argv[2]
def fake(inp, out, *a, **kw):
    out.write(b"HALF")
    out.flush()
    os._exit(137)
shutil.copyfileobj = fake
vaultio.move_create_only(src, dst)
'''


def case_wall_refuses_non_atomic() -> None:
    d = hub("wall")
    (d / "brief.pdf").write_bytes(b"THE SIGNED ORIGINAL")
    worker = STAGE / "worker.py"
    worker.write_text(WORKER.format(binpath=str(REPO / "bin")), encoding="utf-8")

    # Each row: (op, destination, does it need a source?) — `create_only` is the control. A suite in
    # which everything is denied would prove only that the wall is broken in the other direction.
    rows = [
        ("write_text", d / "brief.pdf", False, 5),           # overwrite an original outright
        ("write_text", d / "new.pdf", False, 5),             # even a NEW leaf: write_text is not atomic
        ("write_text_claiming_create_only", d / "new.pdf", False, 5),   # forged flag: stripped
        ("write_bytes", d / "brief.pdf", False, 5),
        ("append_text", d / "brief.pdf", False, 5),
        ("mkdir_tolerant", d / "sub", False, 5),
        ("move", d / "brief.pdf", True, 5),
        ("copy2", d / "brief.pdf", True, 5),
        ("replace", d / "brief.pdf", True, 5),
        ("create_only", d / "arrived.pdf", True, 0),         # the control: this one must WORK
    ]
    for op, dst, needs_src, want_rc in rows:
        src = staged("arrival.pdf", b"AN ARRIVAL") if needs_src else None
        before = hashes(d)
        argv = [sys.executable, str(worker), op, str(dst)] + ([str(src)] if src else [])
        r = subprocess.run(argv, capture_output=True, text=True, env=os.environ)
        after = hashes(d)
        if want_rc == 5:
            check(f"wall: `{op}` -> {dst.name} refused with EXIT_DENY (5)", r.returncode == 5,
                  f"rc={r.returncode} out={r.stdout.strip()} err={r.stderr.strip()}")
            check(f"wall: `{op}` -> {dst.name} changed NOTHING under in/", before == after,
                  f"before={before} after={after}")
            if src:
                check(f"wall: `{op}` left the arriving file in place", src.is_file())
        else:
            check(f"wall: `{op}` -> {dst.name} is ALLOWED (the wall is not simply denying everything)",
                  r.returncode == 0 and dst.is_file(),
                  f"rc={r.returncode} err={r.stderr.strip()}")
    check("wall: the pre-existing original is byte-identical after all of it",
          (d / "brief.pdf").read_bytes() == b"THE SIGNED ORIGINAL")


# ==============================================================================================
# C. The uniquifier's BOUND. Attempt-and-catch has to stop somewhere, and `_arrive` stops after
# `UNIQUIFY_LIMIT` names with a refusal that did not exist before this task. A new user-visible
# refusal whose failing region never executes is a green test of nothing, so it is DRIVEN here —
# through the real verb, in a real process, because `output.fail` exits the process it refuses.
# ==============================================================================================
def _uniquify_limit() -> int:
    """The bound, read out of the shipped source rather than restated. A test that hardcodes 100
    stops testing the code the day someone edits the constant, and `bin/files/run.py` cannot simply
    be imported here — `test/lib` already owns the name `lib` in this process."""
    src = (REPO / "bin" / "files" / "run.py").read_text(encoding="utf-8")
    m = re.search(r"^UNIQUIFY_LIMIT\s*=\s*(\d+)\s*$", src, re.M)
    return int(m.group(1)) if m else -1


def _hub_with_names_taken(slug: str, taken: int) -> Path:
    """A hub whose `in/` already holds the first `taken` names `_arrive` will try for `brief.pdf`
    (`brief.pdf`, `brief-2.pdf`, …). The sitting tenants are written DIRECTLY: they are fixture,
    not arrivals, and going through the wall would only re-test the wall."""
    (VAULT / "wiki" / "clients").mkdir(parents=True, exist_ok=True)
    (VAULT / "journal").mkdir(exist_ok=True)
    (VAULT / "wiki" / "clients" / f"{slug}.md").write_text(
        f"---\ntype: client\ntitle: {slug}\nstatus: active\n---\n# {slug}\n", encoding="utf-8")
    d = hub(slug)
    for i in range(1, taken + 1):
        (d / ("brief.pdf" if i == 1 else f"brief-{i}.pdf")).write_bytes(f"SITTING-TENANT-{i}".encode())
    return d


def _ingest(src: Path, slug: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(REPO / "bin" / "files" / "run.py"), "ingest", str(src),
         "--client", slug], capture_output=True, text=True, env=os.environ)


def case_uniquify_limit() -> None:
    limit = _uniquify_limit()
    check("uniquifier: the shipped bound is readable from bin/files/run.py",
          limit > 1, f"UNIQUIFY_LIMIT={limit}")
    if limit < 2:
        return

    # The control first. A refusal case on its own would pass just as well against a verb that
    # refused one name early, or refused everything — this pins the bound to where it is claimed.
    d = _hub_with_names_taken("limitedge", limit - 1)
    src = staged("brief.pdf", b"THE LAST REACHABLE NAME")
    r = _ingest(src, "limitedge")
    last = d / f"brief-{limit}.pdf"
    check(f"uniquifier: with {limit - 1} names taken the {limit}th is still USED (the bound is not "
          f"off by one)",
          r.returncode == 0 and last.is_file() and last.read_bytes() == b"THE LAST REACHABLE NAME",
          f"rc={r.returncode} err={r.stderr.strip()[:400]}")

    # And now the failing region: every name the loop is allowed to try is occupied.
    d = _hub_with_names_taken("limitfull", limit)
    src = staged("brief.pdf", b"THE ARRIVAL WITH NOWHERE TO GO")
    before = hashes(d)
    r = _ingest(src, "limitfull")
    after = hashes(d)
    said = r.stdout + r.stderr
    check(f"uniquifier: all {limit} names taken -> EXIT_UNEXPECTED (1)", r.returncode == 1,
          f"rc={r.returncode} out={r.stdout.strip()[:400]} err={r.stderr.strip()[:400]}")
    # `ingest` has other exit-1 refusals (an unknown hub, for one), so the code alone does not say
    # WHICH branch ran. The message does.
    check("uniquifier: the refusal is the BOUND's — it names the limit and says nothing moved",
          str(limit) in said and "was NOT moved" in said, f"said={said.strip()[:400]}")
    check(f"uniquifier: not one byte under in/ changed ({len(before)} entries walked + hashed)",
          before == after, f"symmetric-difference={sorted(set(before.items()) ^ set(after.items()))[:6]}")
    check(f"uniquifier: no `brief-{limit + 1}.pdf` — the loop stopped AT the bound, it did not run on",
          not (d / f"brief-{limit + 1}.pdf").exists(), str(sorted(p.name for p in d.iterdir()))[:400])
    check("uniquifier: the arriving original is still where it was, with all of its bytes",
          src.is_file() and src.read_bytes() == b"THE ARRIVAL WITH NOWHERE TO GO")


# ==============================================================================================
# C2. The SOURCE side of the wall. `~/files/**/in/` is append-only in BOTH directions — an original
# is never DELETED either, which `classify({"kind": "delete", …})` has said since Task 1c and which
# the validated case `originals-in-delete-denied` pins. The wall was DESTINATION-only, so that case
# had no seam that could enforce it: `move_create_only` unlinked its source unconditionally, and
# ingesting a path already under `in/` renamed an existing original to `-2` (exit 0, "filed") or
# moved it out of the hub entirely. Real verb, real processes — `output.fail` exits the one it
# refuses — plus the PRIMITIVE on its own, because a guard that lives only in the verb is the same
# defect one layer up.
# ==============================================================================================
def case_source_under_originals() -> None:
    d = _hub_with_names_taken("srcguard", 0)
    # Written directly and with NO shadow note on purpose: `_find_by_hash` short-circuits a
    # previously-INGESTED original before `_arrive` is ever reached, so it shields this by accident.
    # The exposed shape is the hand-filed original, and a guard that needs a wiki note is not a guard.
    (d / "brief.pdf").write_bytes(b"THE HAND-FILED ORIGINAL")
    (d / "scan.pdf").write_bytes(b"ANOTHER HAND-FILED ORIGINAL")
    other = _hub_with_names_taken("srcguard2", 0)
    before, before_other = hashes(d), hashes(other)

    r = _ingest(d / "brief.pdf", "srcguard")           # 1. renames the original in place
    said = r.stdout + r.stderr
    check("source wall: ingesting a path ALREADY under in/ is refused with EXIT_DENY (5)",
          r.returncode == 5, f"rc={r.returncode} out={r.stdout.strip()[:300]} "
                             f"err={r.stderr.strip()[:300]}")
    check("source wall: the refusal names the wall rather than a stray errno",
          "guardrail" in said.lower(), f"said={said.strip()[:300]}")
    check("source wall: not one byte under in/ changed, and no `brief-2.pdf` appeared",
          before == hashes(d), f"before={before} after={hashes(d)}")

    r2 = _ingest(d / "scan.pdf", "srcguard2")          # 2. moves it out of the hub entirely
    check("source wall: moving an original from one hub's in/ to another is refused too (5)",
          r2.returncode == 5, f"rc={r2.returncode} err={r2.stderr.strip()[:300]}")
    check("source wall: both hubs are byte-identical afterwards",
          before == hashes(d) and before_other == hashes(other),
          f"src={hashes(d)} dst={hashes(other)}")

    # 3. The PRIMITIVE, with no verb in front of it. `move_create_only` is the seam; if the guard
    #    lived in `files ingest` instead, the next caller would reintroduce the finding for free.
    worker = STAGE / "worker.py"
    worker.write_text(WORKER.format(binpath=str(REPO / "bin")), encoding="utf-8")
    dest = hub("srcguard3")
    r3 = subprocess.run([sys.executable, str(worker), "create_only", str(dest / "brief.pdf"),
                         str(d / "brief.pdf")], capture_output=True, text=True, env=os.environ)
    check("source wall: `vaultio.move_create_only` refuses it directly — the seam is in the "
          "primitive, not the verb", r3.returncode == 5,
          f"rc={r3.returncode} err={r3.stderr.strip()[:300]}")
    check("source wall: the primitive created nothing at the destination and removed nothing",
          not (dest / "brief.pdf").exists() and before == hashes(d))


def case_move_and_replace_source() -> None:
    """The OTHER two primitives that take a source away. `move_create_only` got the source guard;
    `move` and `replace` — eleven lines up, same file, same seam — did not, and `shutil.move` /
    `rename(2)` removed the source with exit 0. The destination is an ordinary `~/files/**/out/`
    write, so the DESTINATION wall has nothing to say: the only thing that can refuse this is the
    source being classified, which is the whole point.

    Latent rather than live — `move`'s one production caller sweeps `~/Desktop` into `~/.Trash` — so
    the control row matters as much as the refusals: a guard that denied every move would have broken
    that caller silently."""
    d = hub("moveguard")
    out = ROOTS / "files" / "clients" / "moveguard" / "out"
    vaultio.mkdir(out, parents=True, exist_ok=False)
    (d / "evidence.pdf").write_bytes(b"THE ORIGINAL")
    (d / "second.pdf").write_bytes(b"ANOTHER ORIGINAL")
    worker = STAGE / "worker.py"
    worker.write_text(WORKER.format(binpath=str(REPO / "bin")), encoding="utf-8")
    before = hashes(d)

    for op, leaf in (("move", "evidence.pdf"), ("replace", "second.pdf")):
        r = subprocess.run([sys.executable, str(worker), op, str(out / leaf), str(d / leaf)],
                           capture_output=True, text=True, env=os.environ)
        check(f"move guard: `vaultio.{op}` refuses a source under in/ with EXIT_DENY (5)",
              r.returncode == 5, f"rc={r.returncode} out={r.stdout.strip()[:200]} "
                                 f"err={r.stderr.strip()[:200]}")
        check(f"move guard: `{op}` names the append-only rule, not a stray reason",
              "append-only" in (r.stdout + r.stderr), (r.stdout + r.stderr).strip()[:200])
        check(f"move guard: `{op}` left the original where it was and put nothing in out/",
              before == hashes(d) and not (out / leaf).exists(), f"in={hashes(d)} out={hashes(out)}")

    # The control. `move` OUT of a directory that is not an originals tree must still work, or the
    # guard has broken sweep's ~/Desktop -> ~/.Trash caller rather than protected anything.
    ordinary = staged("receipt.pdf", b"NOT EVIDENCE")
    r = subprocess.run([sys.executable, str(worker), "move", str(out / "receipt.pdf"),
                        str(ordinary)], capture_output=True, text=True, env=os.environ)
    check("move guard: an ordinary move is still ALLOWED (the guard is not denying every move)",
          r.returncode == 0 and (out / "receipt.pdf").is_file() and not ordinary.exists(),
          f"rc={r.returncode} err={r.stderr.strip()[:200]}")


def case_env_in_source_path() -> None:
    """The source guard must enforce the APPEND-ONLY rule and nothing else.

    `classify` tests `(^|/)\\.env($|\\.|/)` BEFORE it reaches its delete branch, so routing the source
    through it wholesale imported an unrelated rule: an ordinary evidence file that merely SAT under
    a `.env/` directory came back DENY "reading .env / secret values is denied" and the ingest became
    a hard refusal. Nothing was being read and nothing was a secret — the file was being moved.

    The second row is the control that keeps the fix from being a hole: the DESTINATION rule is
    untouched, so a leaf that would land as `in/.env.backup` is still refused. That refusal is the
    write wall's and predates this task."""
    d = hub("envsrc")
    envdir = Path(tempfile.mkdtemp(dir=STAGE)) / ".env" / "sub"
    envdir.mkdir(parents=True)
    src = envdir / "quarterly.pdf"
    src.write_bytes(b"AN ORDINARY QUARTERLY REPORT")
    dst = vaultio.move_create_only(src, d / "quarterly.pdf")
    check("env source: an ordinary file under a `.env/` directory is FILED, not refused",
          dst.is_file() and dst.read_bytes() == b"AN ORDINARY QUARTERLY REPORT", str(hashes(d)))
    check("env source: and the source was taken away, so it was a move", not src.exists())

    secret = staged(".env.backup", b"TOKEN=hunter2")
    before = hashes(d)
    err = None
    try:
        vaultio.move_create_only(secret, d / ".env.backup")
    except BaseException as e:   # noqa: BLE001
        err = e
    check("env source: a `.env*` DESTINATION under in/ is still refused (the write wall, untouched)",
          isinstance(err, SystemExit) and err.code == 5, repr(err))
    check("env source: and nothing under in/ changed", before == hashes(d))


def case_crash_orphan_is_removable() -> None:
    """A hard kill mid-copy must not leave debris that NOTHING can ever remove.

    `_create_only_copy` staged inside `in/`. An exception unwinds into the `finally` that drops the
    staging leaf; a `SIGKILL` does not — and the `.pk-arriving-*` leaf left under `in/` was then
    permanent by construction, because `classify({"kind": "delete"})` denies it and `_guard_delete`
    enforces that. The feature created litter it forbade itself to clean up.

    Staging now happens OUTSIDE the append-only tree, so the residue is removable without the rule
    being widened by one path. Killed with `os._exit`, which runs no `finally` — the only faithful
    stand-in for SIGKILL, and the reason this needs its own process."""
    d = hub("crashorphan")
    src = staged("brief.pdf", b"X" * 4096)
    link = src.parent / "link.pdf"
    os.symlink(src, link)                       # a symlink source takes the COPY branch
    killer = STAGE / "killer.py"
    killer.write_text(KILL_MIDCOPY.format(binpath=str(REPO / "bin")), encoding="utf-8")
    r = subprocess.run([sys.executable, str(killer), str(d / "brief.pdf"), str(link)],
                       capture_output=True, text=True, env=os.environ)

    check("crash orphan: the process really died mid-copy without unwinding", r.returncode == 137,
          f"rc={r.returncode} err={r.stderr.strip()[:200]}")
    check("crash orphan: the destination name never appeared", not (d / "brief.pdf").exists())
    under_in = [p for p in d.iterdir() if p.name.startswith(vaultio.ARRIVING_PREFIX)]
    check("crash orphan: NO staging leaf is left under in/, where nothing could remove it",
          under_in == [], str([p.name for p in under_in]))

    stage_dir = vaultio._staging_dir(d / "x")
    orphans = [p for p in stage_dir.iterdir() if p.name.startswith(vaultio.ARRIVING_PREFIX)]
    check("crash orphan: the residue is outside the append-only tree", not orphans or
          all(not vaultio._under_originals(p) for p in orphans), str(stage_dir))
    # ...and the mechanism that made it can remove it, which is the property that was missing.
    removed = False
    if orphans:
        vaultio._guard_delete(orphans[0])       # exits 5 if the wall refuses — that WAS the bug
        os.unlink(orphans[0])
        removed = not orphans[0].exists()
    check("crash orphan: the tooling's own delete verdict ALLOWS clearing it", removed or not orphans,
          f"orphans={[p.name for p in orphans]}")


def case_unmovable_source() -> None:
    """`_arrive` used to catch only `FileExistsError`, so every other `OSError` escaped as a raw
    TRACEBACK: exit 1 because that is Python's default, not because the protocol produced it, and
    under `--json` a stack trace on stderr with NO error envelope. `move_create_only` raises
    deliberately when the source cannot be unlinked after the destination exists, so this is a
    reachable branch."""
    d = _hub_with_names_taken("unmovable", 0)
    ro = Path(tempfile.mkdtemp(dir=STAGE))
    src = ro / "brief.pdf"
    src.write_bytes(b"IN A NON-WRITABLE DIRECTORY")
    os.chmod(ro, 0o555)          # the source cannot be unlinked; the destination still can arrive
    try:
        r = _ingest(src, "unmovable")
        said = r.stdout + r.stderr
        check("unmovable source: exit 1 comes from the protocol, and no traceback reaches the caller",
              r.returncode == 1 and "Traceback" not in said, f"rc={r.returncode} said={said[-300:]}")
        check("unmovable source: the message says what failed and that nothing was overwritten",
              "could not file" in said and "overwritten" in said, f"said={said.strip()[:300]}")
        rj = subprocess.run(
            [sys.executable, str(REPO / "bin" / "files" / "run.py"), "ingest", str(src),
             "--client", "unmovable", "--json"], capture_output=True, text=True, env=os.environ)
        env = {}
        for line in rj.stdout.splitlines():
            if line.startswith("{"):
                env = json.loads(line)
        check("unmovable source: --json gets an error ENVELOPE, not a stack trace",
              env.get("ok") is False and env.get("error", {}).get("code") == 1
              and "Traceback" not in rj.stderr,
              f"rc={rj.returncode} stdout={rj.stdout.strip()[:300]} err={rj.stderr.strip()[:300]}")
    finally:
        os.chmod(ro, 0o755)


# ==============================================================================================
# D. The race. Two shapes, one harness — the legacy one has to LOSE or the harness is not racing.
# ==============================================================================================
RACE_N, RACE_ROUNDS = 16, 5


def _legacy_arrive(src: Path, dest_dir: Path) -> None:
    """`bin/files/run.py`'s uniquifying loop as it stood at BASE 5436ec6. Kept HERE, in the test,
    precisely so the bug stays reproducible after the code that had it is gone."""
    dest = dest_dir / src.name
    i = 2
    while dest.exists():
        dest = dest_dir / f"{src.stem}-{i}{src.suffix}"
        i += 1
    shutil.move(str(src), str(dest))


def _atomic_arrive(src: Path, dest_dir: Path) -> None:
    """The shipped shape: attempt each name, let EEXIST answer."""
    for i in range(1, 200):
        cand = dest_dir / (src.name if i == 1 else f"{src.stem}-{i}{src.suffix}")
        try:
            vaultio.move_create_only(src, cand)
            return
        except FileExistsError:
            continue
    raise RuntimeError("uniquifier exhausted")


def _race(arrive, rounds: int) -> tuple[int, int, int]:
    """`RACE_N` threads onto ONE directory, all arriving as `brief.pdf`, released by a barrier.
    Returns (handed_in, still_on_disk, lossy_rounds)."""
    handed = landed = lossy = 0
    for rnd in range(rounds):
        d = hub(f"race-{arrive.__name__}-{rnd}")
        srcs = [staged("brief.pdf", f"ORIGINAL-{i}".encode()) for i in range(RACE_N)]
        barrier = threading.Barrier(RACE_N)

        def worker(p):
            barrier.wait()
            try:
                arrive(p, d)
            except Exception:
                pass          # a loser is the caller's problem; this measures BYTES, not exceptions
        threads = [threading.Thread(target=worker, args=(s,)) for s in srcs]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        survived = len({f.read_bytes() for f in d.iterdir() if f.is_file()})
        handed += RACE_N
        landed += survived
        lossy += 1 if survived < RACE_N else 0
    return handed, landed, lossy


def case_race_ab() -> None:
    lh, ll, llossy = _race(_legacy_arrive, RACE_ROUNDS)
    ah, al, alossy = _race(_atomic_arrive, RACE_ROUNDS)
    check("race: the BASE exists()-then-move loop LOSES originals — so the harness really races",
          ll < lh, f"legacy kept {ll}/{lh} originals in {RACE_ROUNDS} rounds; if this check fails "
                   f"the concurrency gate below is a green test of nothing")
    check("race: atomic creation loses NOTHING under the same contention", al == ah,
          f"kept {al}/{ah}, lossy rounds {alossy}/{RACE_ROUNDS}")
    print(f"MEASURED: {RACE_N} threads x {RACE_ROUNDS} rounds onto one destination — "
          f"BASE loop kept {ll}/{lh} originals ({llossy}/{RACE_ROUNDS} lossy rounds); "
          f"atomic create kept {al}/{ah} ({alossy}/{RACE_ROUNDS} lossy rounds).")


def case_race_real_verb() -> None:
    """The same contention through the REAL verb, in REAL processes — `plainkeep files ingest`."""
    (VAULT / "wiki" / "clients").mkdir(parents=True, exist_ok=True)
    (VAULT / "journal").mkdir(exist_ok=True)
    (VAULT / "wiki" / "clients" / "racehub.md").write_text(
        "---\ntype: client\ntitle: Racehub\nstatus: active\n---\n# Racehub\n", encoding="utf-8")
    # Counted as a DELTA, not a total: `case_uniquify_limit` ingests into the same throwaway vault
    # and leaves shadow notes of its own, and a total would silently report more notes than this
    # race handed out.
    def _shadow_count() -> int:
        d = VAULT / "wiki" / "files"
        return len(list(d.glob("*.md"))) if d.exists() else 0
    shadows_before = _shadow_count()
    srcs = [staged("brief.pdf", f"VERB-ORIGINAL-{i}".encode()) for i in range(RACE_N)]
    procs = [subprocess.Popen(
        [sys.executable, str(REPO / "bin" / "files" / "run.py"), "ingest", str(s),
         "--client", "racehub"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=os.environ)
        for s in srcs]
    for p in procs:
        p.wait()
    d = ROOTS / "files" / "clients" / "racehub" / "in"
    landed = {f.read_bytes() for f in d.iterdir() if f.is_file()}
    want = {f"VERB-ORIGINAL-{i}".encode() for i in range(RACE_N)}
    check(f"race (real verb): all {RACE_N} concurrently-ingested originals are on disk",
          landed == want, f"missing={sorted(want - landed)} extra={sorted(landed - want)}")
    check("race (real verb): every process reported success",
          all(p.returncode == 0 for p in procs), str([p.returncode for p in procs]))
    shadows = _shadow_count() - shadows_before
    print(f"MEASURED: {RACE_N} concurrent `files ingest` processes into one hub — "
          f"{len(landed)}/{RACE_N} originals on disk, {shadows}/{RACE_N} shadow notes written.")
    # Stated every run, not only on the runs where it bites: `files._shadow()` picks its slug with an
    # exists()-scan of the whole wiki and then writes, which is the TOCTOU shape this task removed
    # from in/, one tree over. Whether it loses a note on any given run is a matter of scheduling, so
    # a conditional note would be a warning that comes and goes while the shape never changes.
    print(f"SUITE-NOTE: concurrent ingest is proved lossless for ORIGINALS only. The shadow note "
          f"`files._shadow()` writes beside each one is chosen by an exists()-scan and is NOT "
          f"race-free ({shadows}/{RACE_N} survived this run, a scheduling-dependent number). It "
          f"lives inside the vault — a revertible git diff, not evidence — so Task 1c deliberately "
          f"leaves it; it is a measured gap, not an unknown.")


def _run(case) -> None:
    """One case, and its own exception is a FAILED CHECK rather than the end of the run.

    Measured while mutation-testing this suite: reverting `move_create_only` to `shutil.move` made
    case 7 raise on a file that was no longer there, the run aborted, and the four cases that would
    have caught the mutation most directly never executed. A gate that stops at the first surprise
    reports the surprise instead of the damage."""
    try:
        case()
    except BaseException as e:   # noqa: BLE001
        check(f"{case.__name__}: raised instead of completing", False, f"{type(e).__name__}: {e}")


def main() -> int:
    for case in (case_create, case_existing_file, case_symlink_live, case_symlink_dangling,
                 case_directory, case_symlink_source, case_hardlinked_source, case_cross_device,
                 case_no_hardlinks_at_all, case_destination_never_partial,
                 case_partial_copy_cleanup, case_container_mkdir, case_wall_refuses_non_atomic,
                 case_uniquify_limit, case_source_under_originals, case_move_and_replace_source,
                 case_env_in_source_path, case_crash_orphan_is_removable, case_unmovable_source,
                 case_race_ab, case_race_real_verb):
        _run(case)

    print(f"\n{BOLD}Append-only originals (~/files/**/in/) — {len(results)} checks{RESET}\n")
    passed = sum(1 for _, ok, _ in results if ok)
    for name, ok, detail in results:
        mark = f"{GREEN}PASS{RESET}" if ok else f"{RED}FAIL{RESET}"
        print(f"  {mark} {name:<70}" + (f"\n       {DIM}{detail}{RESET}" if (detail and not ok) else ""))
    failed = len(results) - passed
    print("SUITE-NOTE: the cross-device branch is exercised by forcing `link(2)` to report EXDEV; "
          "the suite mounts no second volume, so what is proved is that the fallback's guarantee is "
          "O_EXCL's, not that it was measured across a real device boundary.")
    print(f"\n{BOLD}Result:{RESET} {GREEN}{passed} passed{RESET}, "
          f"{(RED if failed else DIM)}{failed} failed{RESET}, {len(results)} checks")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
