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
import os
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
    """Force the CROSS-DEVICE branch by making `link(2)` report EXDEV.

    This exercises the branch, not a second volume: the suite mounts nothing. What it does prove is
    that the fallback's create-only guarantee is `O_EXCL`'s and not a leftover of `os.link`."""
    real = os.link

    def fake(src, dst, **kw):
        raise OSError(errno.EXDEV, "forced cross-device link")
    os.link = fake
    try:
        yield
    finally:
        os.link = real


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
# C. The race. Two shapes, one harness — the legacy one has to LOSE or the harness is not racing.
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
    shadows = len(list((VAULT / "wiki" / "files").glob("*.md"))) if (VAULT / "wiki" / "files").exists() else 0
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
                 case_directory, case_cross_device, case_partial_copy_cleanup,
                 case_container_mkdir, case_wall_refuses_non_atomic, case_race_ab,
                 case_race_real_verb):
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
