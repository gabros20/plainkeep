"""
hermetic.py — the one call that keeps a suite off the developer's REAL vault.

Read this before deciding a suite does not need it.

Since ADR-014 Phase 2 Task 1b, an invocation with no `PLAINKEEP_HOME` does not fail — it falls
through to the marker walk-up from `$PWD` and then to the registry `default`. `script/setup` (step
4b) registers the checkout it sets up, and the Task 1b instructions tell a developer to register
theirs. Both of those are correct. Their consequence for the suite is not: a test that runs
`plainkeep <verb>` with no `PLAINKEEP_HOME` and a cwd inside the repo now resolves to a REAL,
REGISTERED vault and writes into it, with exit 0 and nothing in the output to say so.

That is not hypothetical. It happened twice while Task 1b was being written: two `run_core_parity`
shim checks silently dispatched against the repo instead of their fixture, and ten notes were
captured into the real vault before anyone noticed. Neither failure was loud — the first looked like
an unrelated "unknown verb", the second looked like a green run.

`seal()` closes it at the root by making the REGISTRY hermetic: `PLAINKEEP_CONFIG_HOME` points at an
empty throwaway directory for this process and every child it spawns, so

  * the registry `default` (chain step 4) finds nothing, and
  * a marker found by walking up out of the repo (step 3) is UNREGISTERED, which refuses.

A refusal is the correct outcome for a test that forgot to say which vault it meant. The real
`~/.config/plainkeep/registry.json` is neither read nor written by any sealed process.

**Why the registry lever and not `PLAINKEEP_HOME`:** most suites set `PLAINKEEP_HOME` themselves,
per invocation, at a fixture. Pinning it here would silently override them. Chain step 2 asks a
candidate for a MARKER but not for registration, so an empty registry leaves every such suite
working exactly as it did — and leaves only the suites that named no vault at all, which are the
ones this exists for.

**Direct runs matter as much as `run_all.py` runs.** Putting `PLAINKEEP_CONFIG_HOME` in `run_all.py`
alone would seal the batch and leave `python3 test/run_foo.py` — how a suite is actually run while
being written, which is when the stray writes happened — exactly as exposed as before. So the seal
lives in the suite, `run_all.py` verifies every suite carries it (see `_unsealed()` there), and both
paths are hermetic for the same reason rather than by two different mechanisms.

Idempotent, and it never overrides a `PLAINKEEP_CONFIG_HOME` the caller already chose: a suite that
needs a POPULATED registry (`run_vault.py`, `run_discovery.py`) sets its own per invocation, and a
child process inherits the parent's seal rather than making a second one.
"""
from __future__ import annotations
import atexit
import os
import shutil
import tempfile

try:
    from . import vaultfx  # type: ignore  # (namespace sibling)
except ImportError:        # imported as top-level `hermetic` with test/lib on sys.path
    import vaultfx         # type: ignore

ENV_CONFIG_HOME = "PLAINKEEP_CONFIG_HOME"
ENV_HOME = "PLAINKEEP_HOME"

_sealed: str | None = None


def seal() -> str:
    """Point `PLAINKEEP_CONFIG_HOME` at an empty throwaway registry. Returns the directory.

    The memo caches WHICH directory, never the fact that the variable is set: every return path
    re-asserts `os.environ[ENV_CONFIG_HOME]`. Memoizing the assignment away is how a suite could
    permanently unseal the process — a `finally` that POPS the variable instead of restoring it
    (run_vault.py did) removes the seal, and a later `seal()` returning the memo unchanged could
    not put it back. The gate in run_all.py cannot see that: it proves seal() is CALLED, never that
    the seal is still held."""
    global _sealed
    if _sealed is not None:
        os.environ[ENV_CONFIG_HOME] = _sealed
        return _sealed
    chosen = os.environ.get(ENV_CONFIG_HOME)
    if chosen:                                  # inherited from run_all.py, or set by the suite
        _sealed = chosen
        os.environ[ENV_CONFIG_HOME] = chosen
        return _sealed
    d = tempfile.mkdtemp(prefix="pk-hermetic-")
    os.environ[ENV_CONFIG_HOME] = d
    # Best-effort cleanup: an aborted suite leaving an empty temp dir behind is not worth a failure.
    atexit.register(shutil.rmtree, d, True)
    _sealed = d
    return d


def scratch_root() -> str:
    """`PLAINKEEP_HOME` for a suite that must have a root SELECTED before its first engine import.

    Several suites load `bin/lib` modules in-process to exercise pure functions, and those modules
    resolve the data root at import with no fallback since Task 1b — so the suite has to name one
    before the import, process-wide. Three of them named the CHECKOUT, on the reasoning that nothing
    is written through an in-process pure function. True of the pure functions; not true of what
    inherits the variable. Measured: `guardrail.py` invoked as a SUBPROCESS by those same suites
    (run_notetypes, run_trust) inherited it and appended a line to the real vault's audit log on
    every green run.

    A marked throwaway vault answers the import-time requirement without being anybody's notes, and
    a subprocess that inherits it writes there instead. Never overrides a caller's choice."""
    v = os.environ.get(ENV_HOME)
    if v:
        return v
    d = tempfile.mkdtemp(prefix="pk-scratch-vault-")
    vaultfx.mark_vault(d)
    os.environ[ENV_HOME] = d
    atexit.register(shutil.rmtree, d, True)
    return d
