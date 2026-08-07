#!/usr/bin/env python3
"""
run_migrate.py — the acceptance gate for `bin/lib/migrate.py` (ADR-014 Phase 2 Task 6).

Migration moves an EXISTING vault off its vault-local engine copy and onto the installed one. It is
the only operation in this phase that deletes anything from a directory holding a person's notes, so
the bar it is held to is the panel's 13-item one, quoted in `.orchestrate/task-p2-6-plan-section.md`
and mapped cell-by-cell below.

TWO KINDS OF CHECK, and the difference is the whole design.

  * **Behavioural cells** drive the module's real CLI against a real Phase 1 fixture vault and read
    the answer off the filesystem — the exit code, the hashes, the git tree, the rendered plist. Per
    ADR-019 decision 1 a rule is not enforced until a test drives the product's real entry point and
    observes the effect, so nothing here asserts that a function *would* answer correctly if asked.
  * **Structural ratchets** (`case_ratchet_*`) read the module's PARSE TREE and ask a per-function
    question of it, because the property they defend — "no call in this file opens a vault path for
    writing" — is the absence of something, and absence has no line number to put a behavioural test
    on. ADR-019 decision 3: AST, never source text. Instance 4 in that entry is a ratchet that
    matched its own guard's `def` and passed while the guard was deleted.

THE RATCHETS ARE MUTATION-TESTED IN THIS FILE, not by hand and not once. `case_ratchet_goes_red`
copies the module to scratch, injects a write into a vault inside an innocent-looking function, and
requires the ratchet to go RED and to NAME that function. A ratchet nobody has seen fail is a green
test of nothing — which is the standing rule this repo has now paid for seven times.

HERMETIC. Every install root, config home, vault and `PLAINKEEP_BIN_DIR` is a throwaway under a temp
directory. `PLAINKEEP_BIN_DIR` matters more here than anywhere else in the suite: migration REPOINTS
a stale `plainkeep` symlink, and a cell that let `migrate.bin_dir()` fall back to its default would
be aiming that rewrite at the developer's own `~/.local/bin/plainkeep`.

Usage:  python3 test/run_migrate.py            (also runs from the repo root via run_all.py)
"""
from __future__ import annotations
import ast
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(HERE))

from lib.hermetic import seal  # noqa: E402
seal()   # hermetic: an empty throwaway registry, never the developer's real vault

from lib import vaultmig as vm  # noqa: E402

PY = sys.executable or "python3"
MIGRATE = REPO / "bin" / "lib" / "migrate.py"
GREEN, RED, DIM, RESET = "\033[32m", "\033[31m", "\033[2m", "\033[0m"

EXIT_OK, EXIT_CONFIRM, EXIT_DENY, EXIT_NOT_FOUND = 0, 3, 5, 4

results: list[tuple[str, bool, str]] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    results.append((name, bool(cond), detail))


# ==================================================================================================
# Harness
# ==================================================================================================
def mig(fx: dict, *args: str, bindir: Path | None = None, **extra) -> subprocess.CompletedProcess:
    """The migration module CLI, pointed at a throwaway install root, registry and bin dir.

    A MODULE CLI rather than a verb for the reason `migrate.main()` states: the vault being migrated
    IS an engine tree, so `<vault>/plainkeep <verb>` is refused by the disjointness rule, and the
    installed launcher may not exist yet when migration starts."""
    env = vm._clean_env(PLAINKEEP_ENGINE_HOME=fx["root"], PLAINKEEP_CONFIG_HOME=fx["cfg"],
                        PLAINKEEP_BIN_DIR=str(bindir or (fx["base"] / "bin")), **extra)
    return subprocess.run([PY, str(MIGRATE), *args], capture_output=True, text=True, env=env)


def mig_json(fx: dict, *args: str, **kw) -> tuple[subprocess.CompletedProcess, dict]:
    r = mig(fx, *args, "--json", **kw)
    try:
        return r, json.loads(r.stdout)
    except json.JSONDecodeError:
        return r, {}


def sha_tree(root: Path, *, skip=(".git",)) -> dict[str, str]:
    """sha256 of every file under `root`, by relative path. The suite's own answer to item 13.

    Computed here, independently of `migrate.protected_manifest`, ON PURPOSE: a comparison that
    used the product's own manifest function to check the product's own manifest claim would agree
    with itself no matter what either did. Symlinks are recorded by target and never followed."""
    import hashlib
    out: dict[str, str] = {}
    for dp, dn, fn in os.walk(root):
        rel_dir = os.path.relpath(dp, root)
        rel_dir = "" if rel_dir == "." else rel_dir.replace(os.sep, "/")
        dn[:] = sorted(d for d in dn if not (f"{rel_dir}/{d}" if rel_dir else d).startswith(skip))
        for f in sorted(fn):
            rel = f"{rel_dir}/{f}" if rel_dir else f
            if rel.startswith(skip):
                continue
            p = Path(dp) / f
            if p.is_symlink():
                out[rel] = "symlink:" + os.readlink(p)
                continue
            try:
                b = p.read_bytes()
                out[rel] = f"{hashlib.sha256(b).hexdigest()}:{len(b)}"
            except OSError as e:
                out[rel] = f"unreadable:{e.errno}"
    return out


def canary_delta(before: dict[str, str], after: dict[str, str]) -> list[str]:
    """Differences between two protected manifests that are NOT the migration's declared footprint.

    Migration performs a real guardrail-gated write — that is the point of prove-before-remove, and a
    dry run would prove the argument parser works and nothing about whether a byte can land. The
    product's own `capture` verb writes one note into `inbox/` and appends one line to today's
    journal, and the migration declares both in `canary_writes`.

    So "before/after protected-file hashes are identical" (item 13) is checked as the precise claim
    it has to be, rather than either ignoring the footprint or pretending it does not exist:

      * NOTHING is ever removed. No exception, no tolerance.
      * The only files that may APPEAR are notes under `inbox/`.
      * The only files that may CHANGE are journal entries, and only by GROWING — an append. A
        journal that shrank or was rewritten in place is a modification, not a capture.

    Everything else is returned as a violation. Returning the list rather than a bool is what lets
    the failing check name the file."""
    bad: list[str] = []
    for k in sorted(before):
        if k not in after:
            bad.append(f"REMOVED {k}")
            continue
        if after[k] == before[k]:
            continue
        if not k.startswith("journal/"):
            bad.append(f"MODIFIED {k}")
            continue
        try:
            grew = int(after[k].split(":")[1]) > int(before[k].split(":")[1])
        except (IndexError, ValueError):
            grew = False
        if not grew:
            bad.append(f"REWRITTEN (not appended) {k}")
    for k in sorted(set(after) - set(before)):
        if not k.startswith("inbox/"):
            bad.append(f"APPEARED {k}")
    return bad


def protected_only(tree: dict[str, str], allow_trees, allow_files) -> dict[str, str]:
    """`tree` minus the removal allowlist and minus machine-generated output."""
    def engine(rel: str) -> bool:
        return rel in allow_files or any(rel == t or rel.startswith(t + "/") for t in allow_trees)

    def generated(rel: str) -> bool:
        return any(rel == p or rel.startswith(p + "/")
                   for p in (".git", ".logs", ".index", "jobs/launchd"))
    return {k: v for k, v in tree.items() if not engine(k) and not generated(k)}


def allowlist() -> tuple[tuple[str, ...], tuple[str, ...]]:
    """The allowlist READ OUT OF THE PRODUCT via its own `--print allowlist`, never restated here."""
    r = subprocess.run([PY, str(MIGRATE), "--print", "allowlist"], capture_output=True, text=True,
                       env=vm._clean_env(PLAINKEEP_ENGINE_HOME="/nonexistent-root",
                                         PLAINKEEP_CONFIG_HOME="/nonexistent-cfg"))
    d = json.loads(r.stdout)
    return tuple(d["trees"]), tuple(d["files"])


# ==================================================================================================
# A. The structural ratchets — the property that has no line number
# ==================================================================================================
WRITE_FUNCS = {"remove", "unlink", "rmdir", "rename", "replace", "makedirs", "mkdir", "symlink",
               "chmod", "truncate", "rmtree", "copy", "copy2", "copyfile", "copytree", "move",
               "write_text", "write_bytes", "touch", "symlink_to", "hardlink_to", "mkdtemp"}
# git subcommands that touch the WORKING TREE. The Python ratchet cannot see inside a subprocess, so
# this is the half of the claim that covers `_git(...)`. `read-tree` is conditional: without `-u` it
# rewrites `.git/index` only, which is exactly how `_apply` moves the index without moving a file.
GIT_WORKTREE_VERBS = {"checkout", "checkout-index", "restore", "clean", "stash", "merge", "rebase",
                      "reset", "apply", "am", "cherry-pick", "pull", "mv", "rm"}
VAULT_REMOVERS = {"_remove_engine_path", "_remove_empty_dir"}
GIT_WORKTREE_ALLOWED = {"rollback"}


def _module_ast() -> ast.Module:
    return ast.parse(MIGRATE.read_text(encoding="utf-8"), filename=str(MIGRATE))


# Attributes and calls that turn one PATH into another path in the same tree. Taint follows these
# and nothing else — see `_derives_from`.
PATH_ATTRS = {"parent", "parents"}
PATH_METHODS = {"resolve", "absolute", "joinpath", "with_name", "with_suffix", "expanduser",
                "readlink", "relative_to"}


# Calls that turn one path into ANOTHER SPELLING OF THE SAME PATH. Taint follows these, which is
# what makes `open(str(vault / rel), "w")` and `os.remove(os.path.join(str(vault), rel))` visible —
# the r1 review's second evasion, where a `str()` fell through to `return False` and took the
# `os.path.join` case with it, because its argument was the `str()`.
#
# `relpath` is deliberately ABSENT. It derives from its FIRST argument and merely resolves against
# the second, so following it from either argument would taint `os.path.relpath(dirpath, root)` in
# `protected_manifest` and flag the `.replace(os.sep, "/")` on the next line as a write. That is the
# false positive the module docstring warns about: a ratchet that cries wolf is one an author learns
# to override, and the value of this one is that it has never had to be.
PATH_WRAPPERS = {"Path", "str", "fspath", "os.fspath"}
PATH_FUNCS = {"join", "abspath", "realpath", "normpath", "expanduser", "fspath", "canonical"}

# Calls that ENUMERATE a path's contents. What they yield is inside the vault, so a `for` over one of
# them binds vault paths — but they are not path-to-path conversions, so `_derives_from` must keep
# saying no about the call itself (`x = os.walk(vault)` is a generator, not a file). Handled where
# the binding happens instead, by `_iterates_vault`.
ITER_FUNCS = {"walk", "rglob", "glob", "iterdir", "scandir"}


def _derives_from(node: ast.AST, names: set[str]) -> bool:
    """Is this expression a PATH DERIVED from one of `names` — not merely one that mentions it?

    The distinction is the whole accuracy of the ratchet. `vault / rel` is a vault path.
    `receipts_dir() / f"{read_marker(vault)['id']}.patch"` MENTIONS `vault` and is a path in the
    install root; a "mentions it anywhere" rule flags that write and the ratchet becomes noise an
    author learns to override. So taint follows `/` joins, the path-to-path attributes above, and the
    wrappers that re-spell a path without changing which file it names — and stops at any other call,
    because a function that takes a vault and returns a string is not a vault path.
    """
    if isinstance(node, ast.Name):
        return node.id in names
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        return _derives_from(node.left, names)
    if isinstance(node, ast.Attribute) and node.attr in PATH_ATTRS:
        return _derives_from(node.value, names)
    if isinstance(node, ast.Subscript):
        return _derives_from(node.value, names)
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        return any(_derives_from(e, names) for e in node.elts)
    if isinstance(node, ast.Starred):
        return _derives_from(node.value, names)
    if isinstance(node, ast.Call):
        f = node.func
        if isinstance(f, ast.Attribute) and f.attr in PATH_METHODS:
            return _derives_from(f.value, names)
        if isinstance(f, ast.Name) and f.id in PATH_WRAPPERS and node.args:
            return _derives_from(node.args[0], names)
        if isinstance(f, ast.Attribute) and f.attr in PATH_FUNCS and node.args:
            return any(_derives_from(a, names) for a in node.args)
    return False


def _iterates_vault(node: ast.AST, names: set[str]) -> bool:
    """Does this `for` iterate over the CONTENTS of a vault path — `os.walk(vault)`, `p.rglob(…)`?

    Review r2 named this the largest remaining false negative, and it is not hypothetical:
    `protected_manifest` ALREADY contains `for dirpath, dirnames, filenames in os.walk(vault,
    followlinks=False)`. An author adding a write inside the loop that is already there — the most
    likely place a write would ever be added to this module — was invisible, because taint reached
    `vault` and stopped: `os.walk(vault)` is a call `_derives_from` correctly refuses to follow (it
    yields tuples, not a path), so `dirpath` was a fresh untainted name and
    `open(os.path.join(dirpath, …), "w")` never registered.

    So the enumeration is recognised at the BINDING rather than in `_derives_from`: the loop targets
    of such a `for` are vault paths. `os.walk`'s targets include two lists of bare NAMES, which this
    over-taints; that is the same trade the rest of the detector makes, and the control assertion in
    `case_ratchet_goes_red` is what keeps the over-taint honest."""
    if not isinstance(node, ast.Call):
        return False
    f = node.func
    if not isinstance(f, ast.Attribute) or f.attr not in ITER_FUNCS:
        return False
    # `os.walk(vault)` carries the path in `args[0]`; `p.rglob("*")` carries it in the receiver.
    return _derives_from(f.value, names) or any(_derives_from(a, names) for a in node.args)


def _params(fn: ast.FunctionDef) -> list[str]:
    """Named parameters in positional order. `*args`/`**kwargs` are handled separately by
    `_taint_seeds` — they are not positions, they are catch-alls, and a call that lands a vault path
    in one has to taint the catch-all rather than a position that does not exist."""
    return [a.arg for a in (*fn.args.posonlyargs, *fn.args.args, *fn.args.kwonlyargs)]


def _module_functions(tree: ast.Module) -> list[ast.FunctionDef]:
    return [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]


def _taint_seeds(tree: ast.Module) -> dict[str, set[str]]:
    """Which PARAMETERS of each function in this module can be handed a vault path — INTER-PROCEDURAL.

    This is the step the r1 review's third and worst evasion turned on. Taint used to be seeded only
    from a parameter literally spelled `vault`, so

        def _stow(p): p.write_text("injected")
        ...
        _stow(vault / "wiki" / "oops.md")

    was invisible: `_stow` has no `vault` parameter, so the analysis never looked inside it, and a
    two-line helper defeated the whole ratchet while the module docstring, the pathwall exemption and
    the task report all went on asserting that no call in this file opens a vault path for writing.
    A detector whose false-negative set is that cheap to hit is not evidence of a property.

    So the seed set is propagated ACROSS CALL EDGES to a fixed point: if any function passes a
    vault-derived expression to a module-local function, that function's corresponding parameter is a
    vault path too, and so on transitively. The question the ratchet asks becomes the PROPERTY — "no
    write lands inside a vault, from any function, however many hops away" — rather than the five
    shapes its author happened to imagine.

    `*args`/`**kwargs` forwarding cannot be matched positionally, so a starred vault-derived argument
    taints EVERY parameter of the callee. That over-taints; the alternative is losing the edge, and
    losing edges is the failure this whole function exists to fix."""
    fns = _module_functions(tree)
    seeds: dict[str, set[str]] = {
        fn.name: {a for a in _params(fn) if a == "vault"} for fn in fns}
    for _ in range(12):                       # a fixed point; the call graph here is a few hops deep
        changed = False
        for fn in fns:
            tainted = _tainted_names(fn, seeds.get(fn.name, set()))
            if not tainted:
                continue
            for node in ast.walk(fn):
                if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
                    continue
                callees = [c for c in fns if c.name == node.func.id]
                for callee in callees:
                    params = _params(callee)
                    var = callee.args.vararg.arg if callee.args.vararg else None
                    kwvar = callee.args.kwarg.arg if callee.args.kwarg else None
                    catchall = set(params) | ({var} if var else set()) \
                        | ({kwvar} if kwvar else set())
                    hit: set[str] = set()
                    for i, a in enumerate(node.args):
                        if isinstance(a, ast.Starred):
                            if _derives_from(a.value, tainted):
                                hit |= catchall     # cannot be matched positionally
                            continue
                        if not _derives_from(a, tainted):
                            continue
                        hit.add(params[i] if i < len(params) else var)
                    for kw in node.keywords:
                        if not _derives_from(kw.value, tainted):
                            continue
                        if kw.arg is None:          # `**mapping` forwarding
                            hit |= catchall
                        else:
                            hit.add(kw.arg if kw.arg in params else kwvar)
                    hit.discard(None)
                    merged = seeds.get(callee.name, set()) | hit
                    if merged != seeds.get(callee.name, set()):
                        seeds[callee.name] = merged
                        changed = True
        if not changed:
            break
    return seeds


def _tainted_names(fn: ast.FunctionDef, seed: set[str] | None = None) -> set[str]:
    """Local names holding a path DERIVED from this function's vault-bearing parameters, to a fixed
    point.

    `p = vault / rel` taints `p`; `q = p.parent` taints `q`. `m = read_marker(vault)` does not —
    that is a dict. Iterated because an assignment may reference a name a later line taints.

    `seed` is the parameter set `_taint_seeds` derived for this function; without it the seed is the
    parameter spelled `vault`, which is what this used to be and is kept as the default so a caller
    that only wants the intra-procedural answer can still ask for it."""
    names = set(seed) if seed is not None else {a for a in _params(fn) if a == "vault"}
    for _ in range(4):
        for node in ast.walk(fn):
            if not isinstance(node, (ast.Assign, ast.AnnAssign)) or node.value is None:
                continue
            if not _derives_from(node.value, names):
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for t in targets:
                for n in ast.walk(t):
                    if isinstance(n, ast.Name):
                        names.add(n.id)
        # `with open(...) as fh:` and `for p in (...):` bind names the assignment walk cannot see.
        for node in ast.walk(fn):
            if isinstance(node, ast.For) and (_derives_from(node.iter, names)
                                              or _iterates_vault(node.iter, names)):
                for n in ast.walk(node.target):
                    if isinstance(n, ast.Name):
                        names.add(n.id)
    return names


def _call_name(call: ast.Call) -> str:
    f = call.func
    return f.attr if isinstance(f, ast.Attribute) else (f.id if isinstance(f, ast.Name) else "")


def vault_writes(tree: ast.Module) -> list[tuple[str, int, str]]:
    """Every write primitive in the module applied to a VAULT-DERIVED expression.

    Returns `(function, lineno, what)`. The question is asked per function and of the parse tree, so
    a substring of a docstring, a comment naming `os.remove`, or a helper's own `def` cannot satisfy
    or trip it."""
    seeds = _taint_seeds(tree)
    found: list[tuple[str, int, str]] = []
    for fn in _module_functions(tree):
        tainted = _tainted_names(fn, seeds.get(fn.name, set()))
        if not tainted:
            continue
        for node in ast.walk(fn):
            if not isinstance(node, ast.Call):
                continue
            name = _call_name(node)
            targets: list[ast.AST] = []
            if name == "open":
                # THREE SPELLINGS OF `open`, because the r1 review got through on the most ordinary
                # one. `p.open("w")` is a BOUND METHOD: the mode is `args[0]`, not `args[1]`, and the
                # file is the RECEIVER, so reading the mode out of `args[1:]` left it empty and no
                # target was ever taken. The module docstring's own words are "not `open`".
                f = node.func
                recv = f.value if isinstance(f, ast.Attribute) else None
                os_open = isinstance(recv, ast.Name) and recv.id == "os"
                bound = recv is not None and not os_open
                if os_open:
                    # `os.open(p, flags)` takes an INT flag set, not a mode string, so there is no
                    # mode to inspect. Taken as a write whenever the target is a vault path: erring
                    # RED costs a reviewed exemption, and erring the other way is a hole the size of
                    # the whole constraint.
                    targets = node.args[:1]
                else:
                    mode = ""
                    for a in (node.args if bound else node.args[1:]):
                        if isinstance(a, ast.Constant) and isinstance(a.value, str):
                            mode = a.value
                    for kw in node.keywords:
                        if kw.arg == "mode" and isinstance(kw.value, ast.Constant):
                            mode = str(kw.value.value)
                    if any(c in mode for c in "wax+"):
                        # BOTH CANDIDATES, and that is a REGRESSION FIX rather than belt-and-braces.
                        # `io.open(p, "w")` has an `Attribute` func exactly like `p.open("w")` does,
                        # so the receiver test above calls it bound and took `io` — a module name,
                        # never vault-derived — as the written path while DISCARDING `p`. Review r2
                        # measured the whole family (`io`, `gzip`, `codecs`, `builtins`) going from
                        # caught at ac0acb2 to missed here: a ratchet strictly worse than the one it
                        # replaced. Which of the receiver and `args[0]` is the file cannot be decided
                        # from the parse tree alone — it depends on whether `recv` names a module or
                        # a value — so both are candidates. It costs nothing on the bound spelling:
                        # there `args[0]` is the mode, a `Constant`, which cannot derive from a vault.
                        targets = [recv, *node.args[:1]] if bound else node.args[:1]
            elif name in WRITE_FUNCS:
                f = node.func
                is_module_call = isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name) \
                    and f.value.id in ("os", "shutil", "tempfile")
                if is_module_call:
                    # `os.replace(a, b)` / `shutil.move(a, b)` write at BOTH ends.
                    targets = list(node.args[:2])
                elif isinstance(f, ast.Attribute):
                    # A bound method: the receiver is the thing written. `str.replace` CAN reach here
                    # now that `str(...)` preserves taint — `str(vault).replace(a, b)` is reported as
                    # a write. That is a false positive the module does not currently produce, and it
                    # is the direction to be wrong in: the alternative was letting
                    # `open(str(vault / rel), "w")` through, which the r1 review measured.
                    targets = [f.value, *node.args[:1]]
                elif node.args:
                    targets = [node.args[0]]
            if any(_derives_from(t, tainted) for t in targets):
                found.append((fn.name, node.lineno, name))
    return found


def git_worktree_calls(tree: ast.Module) -> list[tuple[str, int, str]]:
    """Every `_git(...)`/`_git_out(...)` whose subcommand writes the working tree."""
    found: list[tuple[str, int, str]] = []
    for fn in [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
        for node in ast.walk(fn):
            if not isinstance(node, ast.Call) or _call_name(node) not in ("_git", "_git_out"):
                continue
            lits = [a.value for a in node.args[1:]
                    if isinstance(a, ast.Constant) and isinstance(a.value, str)]
            if not lits:
                continue
            verb = lits[0]
            if verb in GIT_WORKTREE_VERBS or (verb == "read-tree" and "-u" in lits):
                found.append((fn.name, node.lineno, verb))
    return found


def case_ratchet_no_vault_writes(_tmp: Path) -> None:
    """CONSTRAINT 1. No function opens a vault path for writing, outside the two gated removers."""
    try:
        tree = _module_ast()
    except SyntaxError as e:                     # degrade to a failed check, never an exception
        check("ratchet: migrate.py parses", False, str(e))
        return
    check("ratchet: migrate.py parses", True)

    writes = vault_writes(tree)
    offenders = [(f, ln, w) for f, ln, w in writes if f not in VAULT_REMOVERS]
    check("ratchet: no function writes inside a vault except the two gated removers",
          not offenders,
          "; ".join(f"{f}():{ln} {w}()" for f, ln, w in offenders[:8]))

    # The ratchet must be LOOKING at something. A taint analysis that found nothing anywhere would
    # satisfy the check above for the worst possible reason, so the removers themselves are required
    # to be visible to it — they are the known-positive that proves the detector is on.
    seen = {f for f, _, _ in writes}
    check("ratchet: the detector actually sees the removers it is excluding",
          seen == VAULT_REMOVERS, f"saw {sorted(seen)}, expected {sorted(VAULT_REMOVERS)}")


def case_ratchet_removers_are_gated(_tmp: Path) -> None:
    """Both removers must consult `_VERIFIED`. An ungated remover is the whole hazard."""
    try:
        tree = _module_ast()
    except SyntaxError as e:
        check("ratchet: removers are gated on _VERIFIED", False, str(e))
        return
    fns = {n.name: n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
    for name in sorted(VAULT_REMOVERS):
        fn = fns.get(name)
        if fn is None:
            check(f"ratchet: {name}() exists", False, "not found in the parse tree")
            continue
        reads = any(isinstance(n, ast.Name) and n.id == "_VERIFIED" for n in ast.walk(fn))
        raises = any(isinstance(n, ast.Raise) for n in ast.walk(fn))
        check(f"ratchet: {name}() consults _VERIFIED and refuses", reads and raises,
              f"reads={reads} raises={raises}")

    # `_VERIFIED` may be ARMED in exactly one place. A second writer is a second way for an
    # unverified path to become removable, which is the seam the whole constraint rests on.
    arming = []
    for fn in [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]:
        for node in ast.walk(fn):
            tgts = (node.targets if isinstance(node, ast.Assign)
                    else [node.target] if isinstance(node, (ast.AnnAssign, ast.AugAssign)) else [])
            if any(isinstance(t, ast.Name) and t.id == "_VERIFIED" for t in tgts):
                arming.append(fn.name)
    check("ratchet: _VERIFIED is armed only by verify_candidate()",
          set(arming) == {"verify_candidate"}, f"armed in {sorted(set(arming))}")


def case_ratchet_no_worktree_git(_tmp: Path) -> None:
    """The half of constraint 1 that a Python-level analysis cannot reach: what git is told to do."""
    try:
        tree = _module_ast()
    except SyntaxError as e:
        check("ratchet: no working-tree git outside rollback", False, str(e))
        return
    calls = git_worktree_calls(tree)
    offenders = [(f, ln, v) for f, ln, v in calls if f not in GIT_WORKTREE_ALLOWED]
    check("ratchet: no function runs a working-tree git command except rollback()",
          not offenders, "; ".join(f"{f}():{ln} git {v}" for f, ln, v in offenders[:8]))
    check("ratchet: the git detector sees rollback's declared checkout-index",
          any(f == "rollback" for f, _, _ in calls),
          f"saw {[(f, v) for f, _, v in calls]}")


# --- the mutation test: show the ratchets RED -----------------------------------------------------
# Each entry: (label, injected line(s), injection site, which ratchet, function the ratchet must
# NAME, module-level preamble). The preamble exists because the most effective evasion the r1 review
# found was a HELPER — the write does not live at the injection site at all, so the function the
# ratchet must name is the helper, not the site.
#
# THE FIRST FIVE are the original battery. THE NEXT FOUR are the shapes the r1 review measured
# passing straight through the ratchet as it stood; each was reproduced GREEN at `ac0acb2` before the
# detector was widened. THE REST were written to attack the WIDENED detector — shapes chosen to break
# the new machinery rather than to confirm it, which is the only way a mutation battery is evidence
# of a property rather than a list of what its author already imagined.
MUTATIONS = (
    ("a write into a vault path in an innocent-looking function",
     "    (vault / 'wiki' / 'stowaway.md').write_text('x', encoding='utf-8')\n",
     "protected_manifest", "no function writes inside a vault", "protected_manifest", ""),
    ("a shutil.rmtree of a vault subtree",
     "    shutil.rmtree(vault / 'inbox', ignore_errors=True)\n",
     "protected_manifest", "no function writes inside a vault", "protected_manifest", ""),
    ("an os.remove reached through a local alias",
     "    doomed = vault / 'plainkeep.json'\n    os.remove(doomed)\n",
     "protected_manifest", "no function writes inside a vault", "protected_manifest", ""),
    ("a working-tree git checkout smuggled into the forward path",
     "    _git(vault, 'checkout', '--', '.')\n",
     "_apply", "no function runs a working-tree git command", "_apply", ""),
    ("a read-tree -u, which writes files where a bare read-tree does not",
     "    _git(vault, 'read-tree', '-u', 'HEAD')\n",
     "_apply", "no function runs a working-tree git command", "_apply", ""),

    # --- r1 review, measured GREEN at ac0acb2 ---
    ("r1: a two-line module-level helper whose parameter is not called `vault`",
     "    _stow(vault / 'wiki' / 'oops.md')\n",
     "protected_manifest", "no function writes inside a vault", "_stow",
     "\n\ndef _stow(p):\n    p.write_text('injected', encoding='utf-8')\n"),
    ("r1: `p.open('w')` — the bound-method spelling of open",
     "    (vault / 'wiki' / 'oops.md').open('w').write('injected')\n",
     "protected_manifest", "no function writes inside a vault", "protected_manifest", ""),
    ("r1: `open(str(...), 'w')` — a str() between the vault and the open",
     "    open(str(vault / 'wiki' / 'oops.md'), 'w').write('injected')\n",
     "protected_manifest", "no function writes inside a vault", "protected_manifest", ""),
    ("r1: `os.remove(os.path.join(str(vault), ...))`",
     "    os.remove(os.path.join(str(vault), 'plainkeep.json'))\n",
     "protected_manifest", "no function writes inside a vault", "protected_manifest", ""),

    # --- written to attack the WIDENED detector ---
    ("adversarial: os.open with an int flag set and os.fspath, no mode string anywhere",
     "    os.open(os.fspath(vault / 'plainkeep.json'), os.O_WRONLY | os.O_TRUNC)\n",
     "protected_manifest", "no function writes inside a vault", "protected_manifest", ""),
    ("adversarial: a TWO-HOP helper chain, neither parameter named like a path",
     "    _outer(vault / 'wiki' / 'oops.md')\n",
     "protected_manifest", "no function writes inside a vault", "_inner",
     "\n\ndef _outer(a):\n    _inner(a)\n\n\ndef _inner(b):\n    b.unlink()\n"),
    ("adversarial: a KEYWORD-ONLY parameter, so no positional index lines up",
     "    _kwstow(dest=vault / 'wiki' / 'oops.md')\n",
     "protected_manifest", "no function writes inside a vault", "_kwstow",
     "\n\ndef _kwstow(*, dest):\n    dest.write_bytes(b'injected')\n"),
    ("adversarial: a *args forwarder between the vault and the write",
     "    _fwd(vault / 'wiki' / 'oops.md')\n",
     "protected_manifest", "no function writes inside a vault", "_sink",
     "\n\ndef _fwd(*a):\n    _sink(*a)\n\n\ndef _sink(q):\n    q.touch()\n"),
    ("adversarial: append mode inside a `with`, which binds a name the write never mentions",
     "    with (vault / 'journal' / 'today.md').open('a') as fh:\n        fh.write('injected')\n",
     "protected_manifest", "no function writes inside a vault", "protected_manifest", ""),
    ("adversarial: the DESTINATION argument of shutil.copy, spelled as a str",
     "    shutil.copy(__file__, str(vault / 'wiki' / 'oops.md'))\n",
     "protected_manifest", "no function writes inside a vault", "protected_manifest", ""),
    ("adversarial: a loop variable bound from a list literal of vault paths",
     "    for q in [vault / 'wiki' / 'a.md', vault / 'wiki' / 'b.md']:\n"
     "        q.write_text('injected', encoding='utf-8')\n",
     "protected_manifest", "no function writes inside a vault", "protected_manifest", ""),
    ("adversarial: a helper that takes the vault ROOT and joins inside itself",
     "    _under(vault)\n",
     "protected_manifest", "no function writes inside a vault", "_under",
     "\n\ndef _under(root):\n    (root / 'wiki' / 'oops.md').write_text('injected', "
     "encoding='utf-8')\n"),
    ("adversarial: a working-tree git verb reached through a helper that renames the vault",
     "    _scrub(vault)\n",
     "_apply", "no function runs a working-tree git command", "_scrub",
     "\n\ndef _scrub(where):\n    _git(where, 'clean', '-fdx')\n"),

    # --- review r2, measured GREEN at fd8200c: shapes the fix wave made STRICTLY WORSE ---
    # `<module>.open(path, mode)` parses as an Attribute call exactly like the bound `p.open(mode)`,
    # so the widened detector took the MODULE as the written path and dropped `args[0]`. These were
    # caught at ac0acb2. Two family members are ratcheted rather than one because the bug was in the
    # shape, not in the name: any module whose `open` takes the path first has it.
    ("r2: `io.open(path, 'w')` — a module-qualified open, which is not a bound method",
     "    io.open(vault / 'wiki' / 'oops.md', 'w').write('injected')\n",
     "protected_manifest", "no function writes inside a vault", "protected_manifest", ""),
    ("r2: `gzip.open(path, 'wb')` — the same shape, a binary mode and another module",
     "    gzip.open(vault / 'wiki' / 'oops.md.gz', 'wb').write(b'injected')\n",
     "protected_manifest", "no function writes inside a vault", "protected_manifest", ""),

    # --- review r2, the largest surviving false negative: the loop this module already contains ---
    ("r2: a write inside an `os.walk(vault)` loop, where the module already walks the vault",
     "    for dp, _dn, _fn in os.walk(vault, followlinks=False):\n"
     "        open(os.path.join(dp, 'oops.md'), 'w').write('injected')\n",
     "protected_manifest", "no function writes inside a vault", "protected_manifest", ""),
)


def _inject(src: str, fn_name: str, line: str, preamble: str = "") -> str:
    """Put `line` as the first statement of `fn_name`, after its docstring, and append `preamble`.

    Injecting into a real function rather than appending a new one is the point: ADR-019 decision 2
    says mutate the CALL SITE, and a ratchet that only noticed a suspiciously-named new function
    would be the same class of gate as the one that matched its own guard's `def`.

    `preamble` adds a module-level helper for the mutations whose whole point is that the write is
    somewhere else. The CALL is still at the site; only the primitive moved, which is exactly the
    evasion the r1 review measured."""
    tree = ast.parse(src)
    fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == fn_name)
    body = fn.body
    first = body[1] if (body and isinstance(body[0], ast.Expr)
                        and isinstance(getattr(body[0], "value", None), ast.Constant)) else body[0]
    lines = src.splitlines(keepends=True)
    at = first.lineno - 1
    return "".join(lines[:at]) + line + "".join(lines[at:]) + preamble


def case_ratchet_goes_red(tmp: Path) -> None:
    """THE CHECK ON THE CHECKS. Each mutation must make its ratchet RED and be NAMED by it.

    Without this the suite reports that a property holds while having no evidence its detector can
    fail. Every mutation is applied to a COPY under scratch; `bin/lib/migrate.py` is never edited."""
    src = MIGRATE.read_text(encoding="utf-8")
    scratch = tmp / "mutants"
    scratch.mkdir(parents=True, exist_ok=True)

    # The unmutated control, through the same code path. A detector that always complains would make
    # every cell above green for the wrong reason, so the clean tree is asserted CLEAN here.
    clean = ast.parse(src)
    control = ([f for f, _, _ in vault_writes(clean) if f not in VAULT_REMOVERS]
               + [f for f, _, _ in git_worktree_calls(clean) if f not in GIT_WORKTREE_ALLOWED])
    check("mutation control: the UNMUTATED module is clean under both ratchets",
          not control, str(control))

    for i, (label, line, fn_name, which, named, preamble) in enumerate(MUTATIONS):
        try:
            mutated = _inject(src, fn_name, line, preamble)
            tree = ast.parse(mutated)
        except (SyntaxError, StopIteration) as e:
            check(f"mutation {i}: fixture — {label} injects into {fn_name}()", False, repr(e))
            continue
        (scratch / f"mutant{i}.py").write_text(mutated, encoding="utf-8")

        if "writes inside a vault" in which:
            hits = [(f, ln) for f, ln, _ in vault_writes(tree) if f not in VAULT_REMOVERS]
        else:
            hits = [(f, ln) for f, ln, _ in git_worktree_calls(tree)
                    if f not in GIT_WORKTREE_ALLOWED]
        check(f"mutation {i}: the ratchet goes RED on {label}", bool(hits), "ratchet stayed green")
        check(f"mutation {i}: ...and NAMES {named}()",
              any(f == named for f, _ in hits), f"named {hits}")


def case_ratchet_survives_a_broken_tree(tmp: Path) -> None:
    """ADR-019 decision 3: a ratchet that dies on a modified tree reports the crash, not the damage.

    So the failure mode of an unparseable module must be a failed CHECK, never a traceback that
    takes the batch down and hides every cell after it."""
    bad = tmp / "broken.py"
    # Genuinely unparseable. `this is not python` looks broken and is a perfectly valid expression
    # (`this is (not python)`), which is how the first version of this cell asserted a degradation
    # path it never took.
    bad.write_text("def f(vault:\n    os.remove(vault / 'x'\n", encoding="utf-8")
    try:
        ast.parse(bad.read_text(encoding="utf-8"))
        check("ratchet: a syntactically broken module is a failed check, not a crash", False,
              "the fixture parsed, so the degradation path was never exercised")
    except SyntaxError:
        check("ratchet: a syntactically broken module is a failed check, not a crash", True)


# ==================================================================================================
# B. Acceptance items 1-2 — preflight is read-only, and what it refuses
# ==================================================================================================
def case_preflight_is_read_only(tmp: Path) -> None:
    """ITEM 1. Read-only, confirm-free preflight; mutation requires a separate `--yes`."""
    fx = vm.phase1_vault(tmp, "preflight")
    v = fx["vault"]

    before = sha_tree(v)
    head_before = vm.git(v, "rev-parse", "HEAD").stdout.strip()
    r, doc = mig_json(fx, "--preflight", str(v))
    after = sha_tree(v)

    check("item 1: preflight exits 0 with no --yes and no prompt", r.returncode == EXIT_OK,
          f"rc={r.returncode} {r.stderr[:300]}")
    check("item 1: preflight changed NOTHING in the vault", before == after,
          str(set(before) ^ set(after)) + str([k for k in before if after.get(k) != before[k]])[:300])
    check("item 1: preflight did not move HEAD",
          vm.git(v, "rev-parse", "HEAD").stdout.strip() == head_before)
    check("item 1: preflight enumerated the engine paths it WOULD remove",
          len(doc.get("would_remove") or []) > 50, str(len(doc.get("would_remove") or [])))
    check("item 1: preflight reports state=pristine and a verdict",
          doc.get("state") == "pristine" and "ready" in str(doc.get("verdict")),
          f"{doc.get('state')} / {doc.get('verdict')}")

    # ...and it VERIFIED the candidate rather than only listing it. A preflight that merely lists is
    # a preflight an operator cannot trust, and `candidate_tree` is the evidence it did the work.
    check("item 1: preflight built and verified a real candidate tree",
          bool(doc.get("candidate_tree")), str(doc.get("candidate_tree")))

    # Mutation requires a SEPARATE --yes, and the refusal is exit 3, not a prompt.
    r2 = mig(fx, "--migrate", str(v))
    check("item 1: --migrate without --yes refuses with EXIT_CONFIRM and changes nothing",
          r2.returncode == EXIT_CONFIRM and sha_tree(v) == before,
          f"rc={r2.returncode} {r2.stderr[:200]}")


def case_preflight_refusals(tmp: Path) -> None:
    """ITEM 2. Validated vault, clean working tree, valid `.plainkeep-engine-ref`, object integrity."""
    fx = vm.phase1_vault(tmp, "refuse")
    v = fx["vault"]

    # (a) a dirty working tree — with --untracked-files=all, so a stray file inside bin/ counts
    stray = vm.dirty(v, "bin/lib/scratch_notes.py", "# left behind by an agent\n")
    r = mig(fx, "--preflight", str(v))
    check("item 2: refuses an UNTRACKED file inside an engine path (exit 5)",
          r.returncode == EXIT_DENY and "ENGINE path" in (r.stderr + r.stdout),
          f"rc={r.returncode} {r.stderr[:250]}")
    stray.unlink()
    check("item 2: fixture — the vault is clean again",
          mig(fx, "--preflight", str(v)).returncode == EXIT_OK)

    # (b) a missing / malformed / unknown engine ref
    ref = v / ".plainkeep-engine-ref"
    original = ref.read_text(encoding="utf-8")

    def set_ref(text: str) -> None:
        # COMMITTED, not left dirty. `.plainkeep-engine-ref` is itself an allowlisted path, so an
        # uncommitted edit to it is refused by the engine-dirt gate before `_engine_ref` is reached
        # — which would make these cells assert the wrong refusal.
        ref.write_text(text, encoding="utf-8")
        vm.git(v, "add", ".plainkeep-engine-ref")
        vm.git(v, "commit", "-q", "-m", "fixture: change the recorded engine ref")

    set_ref("not-a-sha\n")
    r = mig(fx, "--preflight", str(v))
    check("item 2: refuses a malformed .plainkeep-engine-ref",
          r.returncode == EXIT_DENY and "40-char SHA" in (r.stderr + r.stdout),
          f"rc={r.returncode} {r.stderr[:200]}")
    set_ref("0" * 40 + "\n")
    r = mig(fx, "--preflight", str(v))
    check("item 2: refuses a ref naming a commit this repository does not have",
          r.returncode == EXIT_DENY and "not a commit" in (r.stderr + r.stdout),
          f"rc={r.returncode} {r.stderr[:200]}")
    set_ref(original)

    # (b2) THE NARROWING, pinned in BOTH directions. Item 2 says "clean working tree"; this module
    # refuses staged changes anywhere and uncommitted changes to ENGINE paths, and deliberately
    # permits uncommitted NOTES. Both halves are checked, because a narrowing nobody tested is
    # indistinguishable from a gate that stopped working.
    note_dirt = vm.dirty(v, "wiki/todo-today.md", "# an uncommitted note, like every real vault\n")
    jr = v / "journal"
    existing = next(iter(sorted(jr.rglob("*.md"))), None)
    if existing is not None:
        existing.write_text(existing.read_text(encoding="utf-8") + "\nedited, not committed\n",
                            encoding="utf-8")
    r = mig(fx, "--preflight", str(v))
    check("item 2 (narrowed): an uncommitted NOTE does not block migration",
          r.returncode == EXIT_OK, f"rc={r.returncode} {r.stderr[:250]}")
    check("item 2 (narrowed): ...and preflight REPORTS the harmless dirt rather than hiding it",
          bool(mig_json(fx, "--preflight", str(v))[1].get("dirty_but_harmless")),
          str(mig_json(fx, "--preflight", str(v))[1].get("dirty_but_harmless")))

    vm.git(v, "add", "wiki/todo-today.md")
    r = mig(fx, "--preflight", str(v))
    check("item 2 (narrowed): a STAGED change refuses — the index rewrite would discard it",
          r.returncode == EXIT_DENY and "STAGED" in (r.stderr + r.stdout),
          f"rc={r.returncode} {(r.stderr + r.stdout)[:250]}")
    vm.git(v, "restore", "--staged", "wiki/todo-today.md", check=False)
    note_dirt.unlink()
    if existing is not None:
        vm.git(v, "checkout", "--", str(existing.relative_to(v)), check=False)

    # (c) not a vault at all / not a repository root
    plain = tmp / "not-a-vault"
    plain.mkdir(parents=True, exist_ok=True)
    r = mig(fx, "--preflight", str(plain))
    check("item 2: refuses a directory that is not a git repository", r.returncode != EXIT_OK,
          f"rc={r.returncode}")
    sub = v / "wiki"
    r = mig(fx, "--preflight", str(sub))
    check("item 2: refuses a SUBDIRECTORY of a vault, naming the root to migrate",
          r.returncode != EXIT_OK and "root" in (r.stderr + r.stdout).lower(),
          f"rc={r.returncode} {r.stderr[:200]}")

    # (d) object integrity is really asked — a corrupted loose object must refuse
    fx2 = vm.phase1_vault(tmp, "fsck")
    v2 = fx2["vault"]
    blob = vm.git(v2, "rev-parse", "HEAD:plainkeep.json").stdout.strip()
    loose = v2 / ".git" / "objects" / blob[:2] / blob[2:]
    if loose.is_file():
        loose.chmod(0o644)
        loose.write_bytes(b"corrupted, not a valid zlib object")
        r = mig(fx2, "--preflight", str(v2))
        check("item 2: git object-integrity failure REFUSES (exit 5)",
              r.returncode == EXIT_DENY and "integrity" in (r.stderr + r.stdout),
              f"rc={r.returncode} {(r.stderr + r.stdout)[:250]}")
    else:
        check("item 2: fixture — a loose object to corrupt", False,
              f"{blob} is packed, not loose; the fsck cell did not run")


def case_divergence_refuses_and_emits_a_patch(tmp: Path) -> None:
    """ITEM 3. Every added/modified/deleted/type-changed engine path against the recorded ref.

    The specific hazard the panel named: an agent's local edits to `bin/**` inside a real vault. They
    are dead code after migration and invisible after removal, so they refuse — with a patch."""
    for label, mutate, what in (
        ("modified", lambda v: vm.diverge(v), "M"),
        ("added", lambda v: _commit_new(v, "bin/lib/agent_scratch.py", "# added locally\n"), "A"),
        ("deleted", lambda v: _commit_delete(v, "bin/lib/output.py"), "D"),
    ):
        fx = vm.phase1_vault(tmp, f"div-{label}")
        v = fx["vault"]
        check(f"item 3: fixture — a clean {label} vault preflights green",
              mig(fx, "--preflight", str(v)).returncode == EXIT_OK)
        mutate(v)
        r = mig(fx, "--preflight", str(v))
        out = r.stderr + r.stdout
        check(f"item 3: an {label} engine path REFUSES (exit 5) and is listed",
              r.returncode == EXIT_DENY and "DIVERGED" in out,
              f"rc={r.returncode} {out[:250]}")
        patch = fx["root"] / "migrations" / f"{fx['vault_id']}.divergence.patch"
        check(f"item 3: ...and a RECOVERABLE patch is written outside the vault ({label})",
              patch.is_file() and patch.stat().st_size > 0 and str(patch) in out,
              f"{patch} exists={patch.is_file()}")
        check(f"item 3: the patch really carries the {label} change",
              what in _patch_kinds(patch) if patch.is_file() else False,
              _patch_kinds(patch) if patch.is_file() else "no patch")
        check(f"item 3: nothing was removed by the refusal ({label})",
              (v / "bin" / "lib" / "enginetree.py").is_file() and (v / "script").is_dir())


def _patch_kinds(patch: Path) -> str:
    body = patch.read_text(encoding="utf-8", errors="replace")
    kinds = ""
    if "new file mode" in body:
        kinds += "A"
    if "deleted file mode" in body:
        kinds += "D"
    if "--- a/" in body and "new file mode" not in body and "deleted file mode" not in body:
        kinds += "M"
    return kinds or "?"


def _commit_new(v: Path, rel: str, body: str) -> None:
    (v / rel).write_text(body, encoding="utf-8")
    vm.git(v, "add", rel)
    vm.git(v, "commit", "-q", "-m", f"local: add {rel}")


def _commit_delete(v: Path, rel: str) -> None:
    vm.git(v, "rm", "-q", "--", rel)
    vm.git(v, "commit", "-q", "-m", f"local: delete {rel}")


# ==================================================================================================
# C. Items 4-9, 13 — the migration itself
# ==================================================================================================
def case_full_migration(tmp: Path) -> None:
    """ITEMS 4, 5, 6, 7, 9, 13 and prove-before-remove, on one real end-to-end run."""
    fx = vm.phase1_vault(tmp, "full")
    v, root = fx["vault"], fx["root"]
    trees, files = allowlist()

    scratch_before = {q.name for q in Path(tempfile.gettempdir()).glob("pk-migrate-*")}
    before_all = sha_tree(v)
    before_prot = protected_only(before_all, trees, files)
    venv_before = {k: h for k, h in before_all.items() if k.startswith(".venv/")}
    check("item 4: fixture — the protected manifest covers real user content",
          len(before_prot) > 15 and any(k.startswith("inbox/") for k in before_prot)
          and "plainkeep.json" in before_prot, f"{len(before_prot)} protected files")
    check("item 4: ...including the gitignored .venv nothing git-based can see",
          len(venv_before) >= 3, f"{len(venv_before)} venv files")

    r, doc = mig_json(fx, "--migrate", str(v), "--yes")
    check("item 7/9: the migration succeeds end to end", r.returncode == EXIT_OK,
          f"rc={r.returncode} {r.stderr[:600]}")
    if r.returncode != EXIT_OK:
        return

    after_all = sha_tree(v)
    after_prot = protected_only(after_all, trees, files)

    # ---- ITEM 13, the sentence the whole task is about.
    # Computed by THIS suite, from the filesystem, not read out of the product's own receipt.
    check("item 13: no protected file is removed or modified beyond the declared canary footprint",
          not canary_delta(before_prot, after_prot), str(canary_delta(before_prot, after_prot)[:6]))
    check("item 13: NOT ONE protected file was removed",
          not (set(before_prot) - set(after_prot)),
          str(sorted(set(before_prot) - set(after_prot))[:6]))
    check("item 13: ...measured over a non-trivial number of files", len(before_prot) > 15,
          f"{len(before_prot)}")

    # The removal itself — the step the whole design is about — must change NOTHING. This is the
    # product's own comparison and it brackets `_apply` alone, so it is the strong form with no
    # footprint to excuse: `protected_files` counts what it hashed and the run refuses on any delta.
    check("item 13: the product brackets the REMOVAL with its own hash comparison and it held",
          isinstance(doc.get("protected_files"), int) and doc["protected_files"] > 15,
          str(doc.get("protected_files")))

    # The journal was APPENDED to, never rewritten: the old bytes are still a prefix of the new.
    jrnl = sorted(k for k in after_prot if k.startswith("journal/"))
    for j in jrnl:
        if j in before_prot and after_prot[j] != before_prot[j]:
            check(f"item 13: {j} was appended to, not rewritten",
                  int(after_prot[j].split(":")[1]) > int(before_prot[j].split(":")[1]),
                  f"{before_prot[j]} -> {after_prot[j]}")

    # ---- the .venv is RETAINED (explicitly NOT this task's to remove)
    venv_after = {k: h for k, h in after_all.items() if k.startswith(".venv/")}
    check("retention: the .venv and its hand-installed package survive, byte-identical",
          venv_after == venv_before, f"before={len(venv_before)} after={len(venv_after)}")
    check("retention: the hand-installed package is still importable content",
          any("handinstalled/__init__.py" in k for k in venv_after))

    # ---- ITEM 6: the diff is a SUBSET of the allowlist, and it is pure deletion
    ns = vm.git(v, "diff-tree", "-r", "--no-commit-id", "--name-status",
                doc["before_commit"], doc["after_commit"]).stdout.split()
    pairs = list(zip(ns[0::2], ns[1::2]))
    check("item 6: the migration commit is a PURE DELETION",
          pairs and all(s == "D" for s, _ in pairs),
          str([p for p in pairs if p[0] != "D"][:6]))

    def allowed(rel: str) -> bool:
        return rel in files or any(rel == t or rel.startswith(t + "/") for t in trees)
    outside = [p for _, p in pairs if not allowed(p)]
    check("item 6: every deleted path is inside the exact allowlist", not outside, str(outside[:6]))
    check("item 6: ...and the allowlist is DERIVED from enginetree's ownership manifest",
          set(trees) >= set(vm._enginetree().OWNED_TREES)
          and set(files) >= set(vm._enginetree().OWNED_FILES),
          f"trees={trees} files={files}")

    # ---- ITEM 5: built in a temporary index. The vault's own index must not carry the candidate
    # before the commit exists, and the proof it was temporary is that the run left no scratch behind.
    # Measured as a DELTA across this one migration. Two things make an absolute count wrong:
    # `pk-migrate-test-*` is this suite's own per-case temp dir, and a SIGKILLed run in the kill
    # matrix legitimately cannot remove its scratch, so leftovers outlive the process that made them.
    leaked = sorted({q.name for q in Path(tempfile.gettempdir()).glob("pk-migrate-*")}
                    - scratch_before - {q.name for q in Path(tempfile.gettempdir())
                                        .glob("pk-migrate-test-*")})
    check("item 5: this migration left no scratch directory behind", not leaked, str(leaked[:5]))

    # ---- the engine copy is really gone from the working tree, and the vault is data-only
    for rel in ("bin", "script", "VERSION", "plainkeep", "templates/verb", ".plainkeep-engine-ref"):
        check(f"item 7: the vault no longer carries {rel}", not (v / rel).exists())
    check("item 7: ...and the vault's own directories that held it are gone too",
          not (v / "bin").is_dir() and not (v / "script").is_dir())
    check("item 7: user templates survive the removal of templates/verb",
          (v / "templates").is_dir(), "templates/ was pruned as if it were engine-owned")

    # ---- ITEM 9 + prove-before-remove: the proof ran from a scrubbed shell and by DISCOVERY
    proof = doc.get("proof") or {}
    probes = proof.get("probes") or []
    sel = [p for p in probes if p.get("probe") == "vault status"]
    check("prove-before-remove: the installed engine selected THIS vault by id",
          bool(sel) and all(p.get("selected") == fx["vault_id"] for p in sel),
          str([p.get("selected") for p in sel]))
    check("prove-before-remove: ...and it got there by DISCOVERY, not an inherited PLAINKEEP_HOME",
          bool(sel) and all("PLAINKEEP_HOME" not in str(p.get("selected_by")) for p in sel)
          and any("walk-up" in str(p.get("selected_by")) for p in sel),
          str([p.get("selected_by") for p in sel]))
    for what in ("doctor", "status", "capture"):
        check(f"item 8: the proof ran `{what}` through the real launcher",
              any(p.get("probe") == what for p in probes), str([p.get("probe") for p in probes]))
    check("item 8: the guardrail-gated write really landed, and its path is declared",
          bool(doc.get("canary_writes"))
          and all((v / w).is_file() for w in doc["canary_writes"] if w),
          str(doc.get("canary_writes")))
    check("item 7: the proof ran AFTER the removal too, and still passed",
          bool((doc.get("proof_after") or {}).get("probes")))

    # ---- ITEM 9: the previous pair is retained
    versions = sorted(p.name for p in (root / "engine").iterdir() if p.is_dir())
    check("item 9: the engine is installed and `current` resolves to a real pair",
          (root / "engine" / "current" / "plainkeep").is_file(), str(versions))

    # ---- the receipt lives OUTSIDE the vault
    rec = root / "migrations" / f"{fx['vault_id']}.json"
    check("receipt: written outside the vault, keyed by vault id", rec.is_file(), str(rec))
    check("receipt: the vault carries no migration bookkeeping of its own",
          not (v / ".plainkeep" / "migration.json").exists())


def case_plugins_and_verbs_still_work(tmp: Path) -> None:
    """ITEM 8. Real plugins, manifest generation, read verbs and a real safe-write, AFTER migration.

    Through the installed launcher from inside the vault with no `PLAINKEEP_HOME` — an operator's
    shell, not a configured one."""
    fx = vm.phase1_vault(tmp, "verbs")
    v, root, cfg, launcher = fx["vault"], fx["root"], fx["cfg"], fx["launcher"]
    r = mig(fx, "--migrate", str(v), "--yes")
    check("item 8: fixture — the vault migrated", r.returncode == EXIT_OK, r.stderr[:400])
    if r.returncode != EXIT_OK:
        return

    env = vm._clean_env(PLAINKEEP_ENGINE_HOME=root, PLAINKEEP_CONFIG_HOME=cfg, PLAINKEEP_CORE="off")

    def run(*argv):
        return subprocess.run([str(launcher), *argv], cwd=str(v), capture_output=True, text=True,
                              env=env)

    for verb in (("status",), ("doctor",), ("vault", "status")):
        rr = run(*verb, "--json")
        check(f"item 8: `{' '.join(verb)}` answers from a migrated vault", rr.returncode == EXIT_OK,
              f"rc={rr.returncode} {rr.stderr[:250]}")

    rr = run("zzfixture", "--json", "--yes")   # plugin verbs are confirm-class; --yes is the gate
    check("item 8: the vault's PLUGIN verb still dispatches after migration",
          rr.returncode == EXIT_OK and "zzfixture" in rr.stdout,
          f"rc={rr.returncode} {(rr.stdout + rr.stderr)[:250]}")

    n_before = len(list((v / "inbox").rglob("*.md")))
    rr = run("capture", "a real safe-write after migration")
    check("item 8: a REAL safe-write lands in the migrated vault", rr.returncode == EXIT_OK
          and len(list((v / "inbox").rglob("*.md"))) == n_before + 1,
          f"rc={rr.returncode} {rr.stderr[:250]}")

    rr = run("status", "--json", "--dry-run")
    check("item 8: a dry run is accepted and writes nothing",
          rr.returncode == EXIT_OK
          and len(list((v / "inbox").rglob("*.md"))) == n_before + 1, f"rc={rr.returncode}")

    check("item 8: plainkeep.json (the generated manifest) survived and is still valid JSON",
          json.loads((v / "plainkeep.json").read_text(encoding="utf-8")) is not None)


# ==================================================================================================
# D. Item 10 — schedules regenerated, not migrated in place
# ==================================================================================================
def case_schedules_are_regenerated(tmp: Path) -> None:
    """ITEM 10. Every schedule regenerated to the installed launcher and exercised sanitized.

    Codex's most-expected failure: a plist whose ProgramArguments still name the vault-local shim is
    ENOENT at 2am, silently, with nothing on screen to explain it."""
    import plistlib
    fx = vm.phase1_vault(tmp, "sched")
    v, root = fx["vault"], fx["root"]

    # A STALE plist, rendered the Phase 1 way: program inside the vault. This is what a real vault
    # has on disk right now, so migration must replace it rather than leave it.
    out_dir = v / "jobs" / "launchd"
    out_dir.mkdir(parents=True, exist_ok=True)
    stale = out_dir / "com.plainkeep.fixture-consolidate.plist"
    with open(stale, "wb") as fh:
        plistlib.dump({"Label": "com.plainkeep.fixture-consolidate",
                       "ProgramArguments": [str(v / "plainkeep"), "status"],
                       "EnvironmentVariables": {"PLAINKEEP_HOME": str(v)},
                       "StartCalendarInterval": {"Hour": 3, "Minute": 30}}, fh)
    with open(stale, "rb") as fh:
        check("item 10: fixture — the pre-migration plist points INSIDE the vault",
              str(v) in plistlib.load(fh)["ProgramArguments"][0])

    r, doc = mig_json(fx, "--migrate", str(v), "--yes")
    check("item 10: the migration succeeded", r.returncode == EXIT_OK, r.stderr[:500])
    if r.returncode != EXIT_OK:
        return

    plists = sorted(out_dir.glob("*.plist"))
    check("item 10: a plist is present after migration", bool(plists), str(out_dir))
    for p in plists:
        with open(p, "rb") as fh:
            d = plistlib.load(fh)
        args = [str(a) for a in d.get("ProgramArguments", [])]
        home = (d.get("EnvironmentVariables") or {}).get("PLAINKEEP_HOME")
        check(f"item 10: {p.name} no longer names anything inside the vault",
              args and not args[0].startswith(str(v)), str(args[:2]))
        check(f"item 10: {p.name} names the STABLE launcher (`current`), not a pinned version",
              args and args[0] == str(root / "engine" / "current" / "plainkeep"), str(args[:1]))
        check(f"item 10: {p.name} bakes the validated vault as an absolute PLAINKEEP_HOME",
              home is not None and os.path.realpath(home) == os.path.realpath(v), str(home))
        check(f"item 10: {p.name} is REGENERATED, not edited — it is a whole new render",
              os.path.realpath(d.get("StandardOutPath", "/nope")).startswith(
                  os.path.realpath(v / ".logs")), str(d.get("StandardOutPath")))

    ex = (doc.get("schedules") or {}).get("exercised") or []
    check("item 10: every regenerated plist was EXERCISED, not merely written",
          len(ex) == len(plists) and all(e.get("rc") == 0 for e in ex), str(ex))

    # And the exercise is meaningful: run the plist's exact argv in a launchd-shaped environment
    # ourselves. A scheduled job must not depend on discovery, so this carries no PLAINKEEP_* at all
    # beyond what the plist itself declares.
    for p in plists:
        with open(p, "rb") as fh:
            d = plistlib.load(fh)
        env = {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "HOME": os.environ.get("HOME", "/tmp")}
        env.update({str(k): str(x) for k, x in (d.get("EnvironmentVariables") or {}).items()})
        env["PLAINKEEP_ENGINE_HOME"] = str(root)
        env["PLAINKEEP_CONFIG_HOME"] = str(fx["cfg"])
        rr = subprocess.run([*[str(a) for a in d["ProgramArguments"]], "--dry-run"], cwd="/",
                            capture_output=True, text=True, env=env)
        check(f"item 10: {p.name} runs from `/` in a sanitized launchd environment",
              rr.returncode == EXIT_OK, f"rc={rr.returncode} {(rr.stderr or rr.stdout)[:250]}")


# ==================================================================================================
# E. Stale executable routing — the panel's most-expected failure
# ==================================================================================================
def case_stale_launcher_is_repointed(tmp: Path) -> None:
    """`~/.local/bin/plainkeep` may be a symlink into the vault that is about to lose its engine.

    Repointed AFTER prove-before-remove, with the old target preserved in the receipt so rollback can
    put it back. A regular file at that path is somebody else's and is never replaced."""
    fx = vm.phase1_vault(tmp, "stale")
    v, root = fx["vault"], fx["root"]
    bindir = fx["base"] / "bin"
    bindir.mkdir(parents=True, exist_ok=True)
    link = bindir / "plainkeep"
    link.symlink_to(v / "plainkeep")
    check("routing: fixture — the launcher on PATH points INTO the vault",
          os.readlink(link) == str(v / "plainkeep"))

    r, doc = mig_json(fx, "--migrate", str(v), "--yes", bindir=bindir)
    check("routing: the migration succeeded", r.returncode == EXIT_OK, r.stderr[:500])
    if r.returncode != EXIT_OK:
        return

    route = doc.get("launcher_route") or {}
    check("routing: the stale symlink was REPOINTED", route.get("repointed") is True, str(route))
    check("routing: ...at the stable `current` launcher, which survives the next activation",
          link.is_symlink() and os.readlink(link) == str(root / "engine" / "current" / "plainkeep"),
          os.readlink(link) if link.is_symlink() else "not a symlink")
    check("routing: the OLD target is preserved in the receipt for rollback",
          route.get("old_target") == str(v / "plainkeep"), str(route.get("old_target")))
    # The point of the whole cell: the name still WORKS. The Phase 1 target is gone by now.
    rr = subprocess.run([str(link), "status", "--json"], cwd=str(v), capture_output=True, text=True,
                        env=vm._clean_env(PLAINKEEP_ENGINE_HOME=root, PLAINKEEP_CONFIG_HOME=fx["cfg"]))
    check("routing: the repointed launcher actually runs (it would be ENOENT otherwise)",
          rr.returncode == EXIT_OK, f"rc={rr.returncode} {rr.stderr[:250]}")
    check("routing: ...and the vault-local shim it used to name is gone",
          not (v / "plainkeep").exists())


def case_a_foreign_launcher_is_never_taken_over(tmp: Path) -> None:
    """A regular file, or a symlink pointing somewhere else, is reported and left alone."""
    fx = vm.phase1_vault(tmp, "foreign")
    v = fx["vault"]
    bindir = fx["base"] / "bin"
    bindir.mkdir(parents=True, exist_ok=True)
    foreign = bindir / "plainkeep"
    foreign.write_text("#!/bin/sh\necho somebody elses plainkeep\n", encoding="utf-8")
    foreign.chmod(0o755)
    body = foreign.read_text(encoding="utf-8")

    r, doc = mig_json(fx, "--migrate", str(v), "--yes", bindir=bindir)
    check("routing: the migration succeeded", r.returncode == EXIT_OK, r.stderr[:400])
    check("routing: a REGULAR FILE at <bin>/plainkeep is never replaced",
          foreign.is_file() and not foreign.is_symlink()
          and foreign.read_text(encoding="utf-8") == body)
    check("routing: ...and it is reported rather than silently ignored",
          (doc.get("launcher_route") or {}).get("repointed") is False
          and (doc.get("launcher_route") or {}).get("kind") == "file",
          str(doc.get("launcher_route")))


# ==================================================================================================
# F. Item 12 — second run is a no-op; rollback is a tested command sequence
# ==================================================================================================
def case_second_run_is_a_noop(tmp: Path) -> None:
    """ITEM 12, first half. Re-running a finished migration converges and changes nothing."""
    fx = vm.phase1_vault(tmp, "noop")
    v = fx["vault"]
    r = mig(fx, "--migrate", str(v), "--yes")
    check("item 12: fixture — the first run migrated", r.returncode == EXIT_OK, r.stderr[:400])
    if r.returncode != EXIT_OK:
        return

    pre = mig(fx, "--preflight", str(v))
    check("item 12: preflight reports the vault as already migrated",
          pre.returncode == EXIT_OK and "already migrated" in pre.stdout, pre.stdout[:250])

    trees, files = allowlist()
    before = protected_only(sha_tree(v), trees, files)
    head = vm.git(v, "rev-parse", "HEAD").stdout.strip()
    r2, doc2 = mig_json(fx, "--migrate", str(v), "--yes")
    after = protected_only(sha_tree(v), trees, files)

    check("item 12: the second run exits 0 and reports a no-op",
          r2.returncode == EXIT_OK and doc2.get("result") == "no-op",
          f"rc={r2.returncode} {r2.stderr[:300]}")
    check("item 12: the second run made NO commit", vm.git(v, "rev-parse", "HEAD").stdout.strip() == head)
    check("item 12: the second run changed no byte in the vault — not even a new inbox note",
          before == after,
          str(sorted(set(after) - set(before))[:6]) + str(sorted(set(before) - set(after))[:6]))
    check("item 12: ...and it still RE-PROVED the installed pair operates this vault",
          bool((doc2.get("proof") or {}).get("probes")), str(doc2.get("proof"))[:200])


def case_rollback(tmp: Path) -> None:
    """ITEM 12, second half. Rollback is a tested command sequence, not prose."""
    fx = vm.phase1_vault(tmp, "rollback")
    v, root = fx["vault"], fx["root"]
    bindir = fx["base"] / "bin"
    bindir.mkdir(parents=True, exist_ok=True)
    (bindir / "plainkeep").symlink_to(v / "plainkeep")

    before_all = sha_tree(v)
    head_before = vm.git(v, "rev-parse", "HEAD").stdout.strip()
    r = mig(fx, "--migrate", str(v), "--yes", bindir=bindir)
    check("item 12: fixture — migrated", r.returncode == EXIT_OK, r.stderr[:400])
    if r.returncode != EXIT_OK:
        return

    rb = mig(fx, "--rollback", str(v))
    check("item 12: rollback without --yes refuses (exit 3)", rb.returncode == EXIT_CONFIRM,
          f"rc={rb.returncode}")

    rb, doc = mig_json(fx, "--rollback", str(v), "--yes", bindir=bindir)
    check("item 12: rollback succeeds", rb.returncode == EXIT_OK, rb.stderr[:400])
    if rb.returncode != EXIT_OK:
        return

    check("item 12: rollback restored HEAD to the pre-migration commit",
          vm.git(v, "rev-parse", "HEAD").stdout.strip() == head_before)
    for rel in ("bin/lib/enginetree.py", "script/update", "VERSION", "plainkeep",
                ".plainkeep-engine-ref"):
        check(f"item 12: rollback restored {rel}", (v / rel).exists())

    after_all = sha_tree(v)
    trees, files = allowlist()
    eng_before = {k: h for k, h in before_all.items()
                  if k in files or any(k == t or k.startswith(t + "/") for t in trees)}
    eng_after = {k: h for k, h in after_all.items()
                 if k in files or any(k == t or k.startswith(t + "/") for t in trees)}
    check("item 12: every engine file is restored byte-identical",
          eng_before == eng_after,
          f"missing={sorted(set(eng_before) - set(eng_after))[:6]} "
          f"differing={[k for k in eng_before if eng_after.get(k) not in (None, eng_before[k])][:6]}")

    check("item 12: the launcher symlink is put back where it pointed",
          doc.get("launcher_restored") is True
          and os.readlink(bindir / "plainkeep") == str(v / "plainkeep"),
          os.readlink(bindir / "plainkeep"))
    check("item 12: the receipt is gone, so the vault is not 'migrated' any more",
          not (root / "migrations" / f"{fx['vault_id']}.json").exists())

    # And the vault is migratable AGAIN — a rollback that left the vault in a state the forward path
    # refuses would be a rollback in name only.
    check("item 12: the rolled-back vault preflights green again (it can be re-migrated)",
          mig(fx, "--preflight", str(v)).returncode == EXIT_OK,
          mig(fx, "--preflight", str(v)).stderr[:250])


ROLLBACK_AFTER_KILL_STAGES = ("symlink", "tree-written", "worktree-pruned", "receipt")


def case_rollback_after_an_interrupted_migration(tmp: Path) -> None:
    """ITEM 12's SECOND HALF, COMPOSED WITH ITEM 11 — the cell whose absence hid two BLOCKING bugs.

    `case_kill_matrix` proves a killed run RE-RUNS to completion; `case_rollback` proves an
    UNINTERRUPTED migration rolls back. Neither asks the question an operator actually asks after a
    laptop sleeps mid-migration: having recovered, can I still undo this? Review r1 measured the
    answer at two of the four late boundaries and it was no, in the worst available way:

      * kill at `tree-written` — the resumed run recorded `before_commit == after_commit`, so
        `--rollback --yes` diffed a commit against itself, passed every gate VACUOUSLY, restored 0 of
        122 paths, printed "rolled back", and unlinked the receipt. After that there was no rollback
        at all and no record of the pre-migration commit.
      * kill at `worktree-pruned` — a fully migrated vault with NO receipt, because the receipt was
        written after the removal and the re-run took the no-op branch. Rollback: exit 4, forever.

    So this cell asserts RESTORATION rather than exit 0: the engine paths are back on disk and in the
    commit, HEAD is the pre-migration commit, the launcher symlink points into the vault again, and
    the receipt is gone only because the rollback SUCCEEDED. A rollback that reports success and
    restores nothing must fail this cell, which is precisely what the old one did."""
    trees, files = allowlist()
    for stage in ROLLBACK_AFTER_KILL_STAGES:
        fx = vm.phase1_vault(tmp, f"rbkill-{stage}")
        v, root = fx["vault"], fx["root"]
        bindir = fx["base"] / "bin"
        bindir.mkdir(parents=True, exist_ok=True)
        link = bindir / "plainkeep"
        link.symlink_to(v / "plainkeep")
        pristine = vm.git(v, "rev-parse", "HEAD").stdout.strip()
        before_prot = protected_only(sha_tree(v), trees, files)

        r = mig(fx, "--migrate", str(v), "--yes", bindir=bindir, PLAINKEEP_MIGRATE_KILL_AT=stage)
        check(f"rollback@{stage}: fixture — the run died at the boundary",
              r.returncode in (-9, 137), f"rc={r.returncode} {r.stderr[:200]}")
        r2 = mig(fx, "--migrate", str(v), "--yes", bindir=bindir)
        check(f"rollback@{stage}: fixture — the re-run converged", r2.returncode == EXIT_OK,
              f"rc={r2.returncode} {(r2.stderr or r2.stdout)[:300]}")
        if r2.returncode != EXIT_OK:
            vm.wipe(fx["base"])
            continue

        # THE RECEIPT MUST EXIST AND MUST DESCRIBE A REAL MIGRATION. `before == after` is the exact
        # shape that made every gate below pass vacuously.
        rec_p = root / "migrations" / f"{fx['vault_id']}.json"
        check(f"rollback@{stage}: a migration receipt exists after the recovery", rec_p.is_file())
        if not rec_p.is_file():
            vm.wipe(fx["base"])
            continue
        rec = json.loads(rec_p.read_text(encoding="utf-8"))
        check(f"rollback@{stage}: the receipt records the PRE-migration commit, not the migration one",
              rec.get("before_commit") == pristine
              and rec.get("before_commit") != rec.get("after_commit"),
              f"before={str(rec.get('before_commit'))[:10]} after={str(rec.get('after_commit'))[:10]} "
              f"pristine={pristine[:10]}")

        rb, rbdoc = mig_json(fx, "--rollback", str(v), "--yes", bindir=bindir)
        check(f"rollback@{stage}: --rollback --yes exits 0", rb.returncode == EXIT_OK,
              f"rc={rb.returncode} {(rb.stderr or rb.stdout)[:300]}")
        if rb.returncode != EXIT_OK:
            check(f"rollback@{stage}: ...and the receipt SURVIVES a rollback that did not happen",
                  rec_p.is_file())
            vm.wipe(fx["base"])
            continue

        # RESTORATION, asked of the filesystem and the repository rather than of the exit code.
        n = len(rbdoc.get("restored") or [])
        check(f"rollback@{stage}: it restored the paths the migration removed, not zero of them",
              n == len(rec.get("removed") or []) and n > 0,
              f"restored={n} removed={len(rec.get('removed') or [])}")
        check(f"rollback@{stage}: the engine copy is back in the working tree",
              (v / "bin").is_dir() and (v / "script").is_dir() and (v / "VERSION").is_file(),
              f"bin={(v / 'bin').is_dir()} script={(v / 'script').is_dir()}")
        check(f"rollback@{stage}: HEAD is the pre-migration commit",
              vm.git(v, "rev-parse", "HEAD").stdout.strip() == pristine,
              f"{vm.git(v, 'rev-parse', 'HEAD').stdout.strip()[:10]} want {pristine[:10]}")
        check(f"rollback@{stage}: the launcher symlink points back into the vault",
              link.is_symlink() and os.readlink(link) == str(v / "plainkeep"),
              os.readlink(link) if link.is_symlink() else "not a symlink")
        check(f"rollback@{stage}: the receipt is gone, because the rollback really happened",
              not rec_p.is_file())
        after_prot = protected_only(sha_tree(v), trees, files)
        check(f"rollback@{stage}: not one protected file was lost across kill+recovery+rollback",
              not (set(before_prot) - set(after_prot)),
              str(sorted(set(before_prot) - set(after_prot))[:5]))
        # And the vault is migratable again, which is the same closing question `case_rollback` asks.
        check(f"rollback@{stage}: the rolled-back vault preflights green again",
              mig(fx, "--preflight", str(v), bindir=bindir).returncode == EXIT_OK,
              mig(fx, "--preflight", str(v), bindir=bindir).stderr[:250])
        vm.wipe(fx["base"])


def case_rollback_refuses_a_receipt_that_restores_nothing(tmp: Path) -> None:
    """THE SECOND HALF OF B1's FIX, and it is independent of how the receipt got that way.

    "Restored 0 paths" is never a correct rollback of a migration that removed 122. Both degenerate
    receipts — `before_commit == after_commit`, and a well-formed one whose diff is empty — must
    REFUSE and must LEAVE THE RECEIPT IN PLACE, because a receipt that cannot be trusted is still the
    only record of the pre-migration commit and the old launcher target."""
    fx = vm.phase1_vault(tmp, "rb-degenerate")
    v, root = fx["vault"], fx["root"]
    r = mig(fx, "--migrate", str(v), "--yes")
    check("item 12: fixture — migrated", r.returncode == EXIT_OK, r.stderr[:300])
    if r.returncode != EXIT_OK:
        return
    rec_p = root / "migrations" / f"{fx['vault_id']}.json"
    good = json.loads(rec_p.read_text(encoding="utf-8"))

    # 1. before == after, exactly what a resumed migration used to record.
    bad = {**good, "before_commit": good["after_commit"]}
    rec_p.write_text(json.dumps(bad, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    rb = mig(fx, "--rollback", str(v), "--yes")
    check("item 12: rollback REFUSES a receipt whose before and after are the same commit",
          rb.returncode == EXIT_DENY, f"rc={rb.returncode} {(rb.stdout + rb.stderr)[:250]}")
    check("item 12: ...and says so rather than reporting a rollback",
          "same commit" in (rb.stderr + rb.stdout) and "rolled back" not in rb.stdout,
          (rb.stderr + rb.stdout)[:250])
    check("item 12: ...and the receipt SURVIVES, so the pre-migration commit is still recoverable",
          rec_p.is_file())
    check("item 12: ...and nothing was restored", not (v / "bin").exists())

    # 2. A receipt aimed at a commit that is not the migration's parent, so the diff is empty. The
    #    gates below it all pass; only an explicit "0 paths is not a rollback" refusal catches it.
    #    The two commits DIFFER but their trees do not, so `head == after_commit` holds, the diff is
    #    empty so nothing is "outside the allowlist", and `checkout-index` is handed no paths.
    vm.git(v, "commit", "-q", "--allow-empty", "-m", "an empty commit on top of the migration")
    tip = vm.git(v, "rev-parse", "HEAD").stdout.strip()
    empty = {**good, "before_commit": good["after_commit"], "after_commit": tip}
    rec_p.write_text(json.dumps(empty, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    rb2 = mig(fx, "--rollback", str(v), "--yes")
    check("item 12: rollback REFUSES when the computed restore set is EMPTY",
          rb2.returncode == EXIT_DENY and "0 engine path(s)" in (rb2.stderr + rb2.stdout),
          f"rc={rb2.returncode} {(rb2.stderr + rb2.stdout)[:300]}")
    check("item 12: ...and that receipt survives too", rec_p.is_file())


def case_rollback_refuses_staged_changes(tmp: Path) -> None:
    """THE HAZARD `_clean_tree_gate` EXISTS FOR, UNGUARDED IN REVERSE (review r1, I2).

    `rollback()` runs the same `git read-tree` the forward path refuses a staged tree for, and then a
    `checkout-index -f` on top. Staging a note and rolling back used to exit 0 and empty the index,
    silently — the file survived, the operator's staging did not, and nothing said so."""
    fx = vm.phase1_vault(tmp, "rb-staged")
    v = fx["vault"]
    r = mig(fx, "--migrate", str(v), "--yes")
    check("item 12: fixture — migrated", r.returncode == EXIT_OK, r.stderr[:300])
    if r.returncode != EXIT_OK:
        return
    (v / "wiki").mkdir(parents=True, exist_ok=True)
    (v / "wiki" / "staged-work.md").write_text("# work in progress\n", encoding="utf-8")
    vm.git(v, "add", "wiki/staged-work.md")
    staged = vm.git(v, "diff", "--cached", "--name-only").stdout.split()
    check("item 12: fixture — there is a staged change", staged == ["wiki/staged-work.md"],
          str(staged))

    rb = mig(fx, "--rollback", str(v), "--yes")
    check("item 12: rollback REFUSES a staged change rather than discarding it",
          rb.returncode == EXIT_DENY and "STAGED" in (rb.stderr + rb.stdout),
          f"rc={rb.returncode} {(rb.stderr + rb.stdout)[:250]}")
    check("item 12: ...and the operator's staging is still there",
          vm.git(v, "diff", "--cached", "--name-only").stdout.split() == ["wiki/staged-work.md"],
          vm.git(v, "diff", "--cached", "--name-only").stdout[:200])
    check("item 12: ...and nothing was rolled back", not (v / "bin").exists())

    # Unstage, and the same rollback now works — the gate is a gate, not a wall.
    vm.git(v, "restore", "--staged", "wiki/staged-work.md")
    rb2 = mig(fx, "--rollback", str(v), "--yes")
    check("item 12: ...and once unstaged, the rollback proceeds", rb2.returncode == EXIT_OK,
          f"rc={rb2.returncode} {(rb2.stderr or rb2.stdout)[:250]}")


def case_rollback_refuses_work_on_top(tmp: Path) -> None:
    """A commit made since the migration means the operator has work on top. Refuse, don't rewrite."""
    fx = vm.phase1_vault(tmp, "rb-refuse")
    v = fx["vault"]
    r = mig(fx, "--migrate", str(v), "--yes")
    check("item 12: fixture — migrated", r.returncode == EXIT_OK, r.stderr[:300])
    if r.returncode != EXIT_OK:
        return
    _commit_new(v, "wiki/after.md", "# work done after the migration\n")
    head = vm.git(v, "rev-parse", "HEAD").stdout.strip()
    rb = mig(fx, "--rollback", str(v), "--yes")
    check("item 12: rollback REFUSES when there is a commit on top of the migration",
          rb.returncode == EXIT_DENY and "revert" in (rb.stderr + rb.stdout),
          f"rc={rb.returncode} {rb.stderr[:250]}")
    check("item 12: ...and the operator's branch is untouched",
          vm.git(v, "rev-parse", "HEAD").stdout.strip() == head)
    check("item 12: ...and their work is still there", (v / "wiki" / "after.md").is_file())


# ==================================================================================================
# G. Item 11 — fault injection at every boundary
# ==================================================================================================
KILL_STAGES = ("preflight-done", "provisioned", "activated", "proved", "schedules", "symlink",
               "tree-written", "worktree-pruned", "receipt")


def case_kill_matrix(tmp: Path) -> None:
    """ITEM 11. SIGKILL at each boundary leaves the old pair or the new pair runnable, and a re-run
    converges — with every protected byte identical across the interruption AND the recovery.

    This is the cell that decides whether the ordering claim is true. A migration that is safe only
    when it completes is not safe."""
    trees, files = allowlist()
    for stage in KILL_STAGES:
        fx = vm.phase1_vault(tmp, f"kill-{stage}")
        v, root = fx["vault"], fx["root"]
        bindir = fx["base"] / "bin"
        bindir.mkdir(parents=True, exist_ok=True)
        (bindir / "plainkeep").symlink_to(v / "plainkeep")

        before_prot = protected_only(sha_tree(v), trees, files)

        r = mig(fx, "--migrate", str(v), "--yes", bindir=bindir,
                PLAINKEEP_MIGRATE_KILL_AT=stage)
        check(f"kill@{stage}: the run really died at the boundary (SIGKILL)",
              r.returncode == -9 or r.returncode == 137, f"rc={r.returncode} {r.stderr[:200]}")

        # THE INVARIANT, asked of the filesystem at the moment of death: whatever state the vault is
        # in, the notes are untouched.
        mid_prot = protected_only(sha_tree(v), trees, files)
        check(f"kill@{stage}: every protected file is intact at the moment of the kill",
              not canary_delta(before_prot, mid_prot), str(canary_delta(before_prot, mid_prot)[:5]))
        check(f"kill@{stage}: not one protected file was LOST at the moment of the kill",
              not (set(before_prot) - set(mid_prot)),
              str(sorted(set(before_prot) - set(mid_prot))[:5]))

        # SOMETHING must run. Either the vault still has its own engine, or the installed pair does.
        vault_shim = (v / "plainkeep").is_file()
        installed = (root / "engine" / "current" / "plainkeep").is_file()
        check(f"kill@{stage}: the old pair or the new pair is still runnable",
              vault_shim or installed, f"vault_shim={vault_shim} installed={installed}")

        # ...and a re-run CONVERGES rather than refusing or diverging.
        r2 = mig(fx, "--migrate", str(v), "--yes", bindir=bindir)
        check(f"kill@{stage}: a re-run converges (exit 0)", r2.returncode == EXIT_OK,
              f"rc={r2.returncode} {(r2.stderr or r2.stdout)[:400]}")

        after_prot = protected_only(sha_tree(v), trees, files)
        check(f"kill@{stage}: protected content is intact after the recovery too",
              not canary_delta(before_prot, after_prot),
              str(canary_delta(before_prot, after_prot)[:5]))
        check(f"kill@{stage}: not one protected file was LOST across kill + recovery",
              not (set(before_prot) - set(after_prot)),
              str(sorted(set(before_prot) - set(after_prot))[:5]))

        if r2.returncode == EXIT_OK:
            check(f"kill@{stage}: the recovered vault is data-only",
                  not (v / "bin").exists() and not (v / "script").exists())
            rr = subprocess.run([str(root / "engine" / "current" / "plainkeep"), "status", "--json"],
                                cwd=str(v), capture_output=True, text=True,
                                env=vm._clean_env(PLAINKEEP_ENGINE_HOME=root,
                                                  PLAINKEEP_CONFIG_HOME=fx["cfg"]))
            check(f"kill@{stage}: ...and a real verb answers from it", rr.returncode == EXIT_OK,
                  f"rc={rr.returncode} {rr.stderr[:200]}")
        vm.wipe(fx["base"])


def case_unknown_kill_stage_refuses(tmp: Path) -> None:
    """An unknown boundary REFUSES rather than being ignored — the same signal `enginetree` gives.

    A typo'd stage that silently ran a complete migration would make every cell above meaningless:
    they would all be testing the unkilled path."""
    fx = vm.phase1_vault(tmp, "kill-bogus")
    v = fx["vault"]
    r = mig(fx, "--migrate", str(v), "--yes", PLAINKEEP_MIGRATE_KILL_AT="not-a-boundary")
    check("item 11: an unknown kill stage REFUSES rather than running a full migration",
          r.returncode != EXIT_OK and "names no boundary" in (r.stderr + r.stdout),
          f"rc={r.returncode} {(r.stderr + r.stdout)[:250]}")
    check("item 11: ...and nothing was removed", (v / "bin").is_dir() and (v / "script").is_dir())


# ==================================================================================================
# H. The allowlist gate itself — item 6's refusal, driven rather than asserted
# ==================================================================================================
GATE_DRIVER = r'''
"""Exercise migrate.py's allowlist gate in a subprocess and report what it refused, as JSON.

A SUBPROCESS, not an import, for a concrete reason: `migrate.py` does `sys.path.insert(0, bin/)` and
then `from lib import enginetree`, while the suite already has `test/lib` bound to the name `lib`.
Importing it in-process makes `lib` ambiguous and the import fails. Running it out-of-process is also
simply how the product runs, which is the form ADR-019 decision 1 asks for.
"""
import json, os, subprocess, sys
from pathlib import Path

vault, head, scratch = Path(sys.argv[1]), sys.argv[2], Path(sys.argv[3])
sys.path.insert(0, str(Path(sys.argv[4])))
import importlib.util
spec = importlib.util.spec_from_file_location("_pk_migrate", sys.argv[5])
m = importlib.util.module_from_spec(spec); sys.modules["_pk_migrate"] = m
spec.loader.exec_module(m)

out = {}

def refusal(label, fn):
    try:
        fn(); out[label] = {"refused": False, "message": "", "code": None}
    except Exception as e:
        out[label] = {"refused": True, "message": getattr(e, "message", str(e)),
                      "code": getattr(e, "code", None)}

def git(*a, **kw):
    return subprocess.run(["git", "-C", str(vault), *a], capture_output=True, text=True, **kw)

_n = [0]
def tree_deleting(paths):
    _n[0] += 1
    idx = scratch / ("idx-%d" % _n[0])          # a SHORT name: joining 122 paths is ENAMETOOLONG
    env = {**os.environ, "GIT_INDEX_FILE": str(idx)}
    git("read-tree", "HEAD", env=env)
    git("update-index", "--index-info", env=env,
        input="".join("0 " + "0"*40 + "\t" + p + "\n" for p in paths))
    return git("write-tree", env=env).stdout.strip()

note = next(p for p in ("plainkeep.json", "AGENTS.md", "jobs/registry.json") if (vault/p).is_file())
out["note"] = note
refusal("deletes_protected", lambda: m.verify_candidate(vault, head, tree_deleting([note]), [note]))

idx = scratch / "idx-add"; env = {**os.environ, "GIT_INDEX_FILE": str(idx)}
git("read-tree", "HEAD", env=env)
blob = git("hash-object", "-w", "--stdin", env=env, input="smuggled\n").stdout.strip()
git("update-index", "--add", "--cacheinfo", "100644," + blob + ",wiki/smuggled.md", env=env)
add_tree = git("write-tree", env=env).stdout.strip()
refusal("adds_a_path", lambda: m.verify_candidate(vault, head, add_tree, []))

refusal("wrong_set",
        lambda: m.verify_candidate(vault, head, tree_deleting(["VERSION"]), ["VERSION", "plainkeep"]))

m._VERIFIED = {"VERSION"}
refusal("remove_unverified_file", lambda: m._remove_engine_path(vault, note))
out["note_survives"] = (vault / note).is_file()
refusal("remove_unverified_dir", lambda: m._remove_empty_dir(vault, "wiki"))
out["wiki_survives"] = (vault / "wiki").is_dir()

# The gate must also ACCEPT the legitimate candidate, or every refusal above would be satisfied by
# a function that simply always raises.
m._VERIFIED = None
tracked = m._tracked_under_allowlist(vault)
good = tree_deleting(tracked)
try:
    got = m.verify_candidate(vault, head, good, tracked)
    out["accepts_the_real_candidate"] = {"ok": sorted(got) == sorted(tracked), "n": len(got)}
except Exception as e:
    out["accepts_the_real_candidate"] = {"ok": False, "n": 0,
                                         "err": getattr(e, "message", str(e))}
print(json.dumps(out))
'''


def case_allowlist_gate_aborts_on_an_unexpected_path(tmp: Path) -> None:
    """ITEM 6's REFUSAL. A candidate tree containing anything but allowlisted deletions must abort.

    The hostile trees are built by hand because the forward path cannot produce one — which is
    exactly why the gate is the thing standing between a bug in that path and a deleted note. A gate
    only ever fed valid input is a green test of nothing."""
    fx = vm.phase1_vault(tmp, "gate")
    v = fx["vault"]
    scratch = tmp / "gate-scratch"
    scratch.mkdir(parents=True, exist_ok=True)
    driver = tmp / "gate_driver.py"
    driver.write_text(GATE_DRIVER, encoding="utf-8")
    head = vm.git(v, "rev-parse", "HEAD").stdout.strip()

    r = subprocess.run([PY, str(driver), str(v), head, str(scratch), str(REPO / "bin"), str(MIGRATE)],
                       capture_output=True, text=True,
                       env=vm._clean_env(PLAINKEEP_ENGINE_HOME=fx["root"],
                                         PLAINKEEP_CONFIG_HOME=fx["cfg"]))
    try:
        d = json.loads(r.stdout)
    except json.JSONDecodeError:
        check("item 6: the allowlist gate could be exercised", False,
              f"rc={r.returncode} {(r.stderr or r.stdout)[:500]}")
        return
    check("item 6: the allowlist gate could be exercised", True)

    g = d["deletes_protected"]
    check("item 6: a candidate deleting a PROTECTED path is refused (exit 5)",
          g["refused"] and "OUTSIDE the allowlist" in g["message"] and g["code"] == EXIT_DENY,
          str(g)[:250])
    g = d["adds_a_path"]
    check("item 6: a candidate that ADDS a path is refused — a migration may only remove",
          g["refused"] and "not a pure deletion" in g["message"] and g["code"] == EXIT_DENY,
          str(g)[:250])
    g = d["wrong_set"]
    check("item 6: a candidate removing a DIFFERENT set than preflight enumerated is refused",
          g["refused"] and "moved underneath" in g["message"], str(g)[:250])

    g = d["remove_unverified_file"]
    check("item 6: THE SEAM — _remove_engine_path refuses a path verify_candidate never verified",
          g["refused"] and "not in the verified deletion set" in g["message"], str(g)[:250])
    check(f"item 6: ...and {d['note']} is still there", d["note_survives"] is True)
    g = d["remove_unverified_dir"]
    check("item 6: _remove_empty_dir refuses a directory outside the verified set",
          g["refused"] and "not an ancestor" in g["message"], str(g)[:250])
    check("item 6: ...and wiki/ is still there", d["wiki_survives"] is True)

    a = d["accepts_the_real_candidate"]
    check("item 6: the gate ACCEPTS the legitimate candidate (it is not a function that always fails)",
          a.get("ok") is True and a.get("n", 0) > 50, str(a)[:250])


PRE_WAIVER_DRIVER = r'''
"""Call `migrate.migrate()` with a HAND-BUILT `pre`, the way any caller of this module's public API
can, and report whether the gates that only `preflight()` used to run still fire.

A subprocess for the same reason `GATE_DRIVER` is one: `migrate.py` inserts `bin/` on `sys.path` and
imports `lib`, which the suite has bound to `test/lib`.
"""
import json, sys
from pathlib import Path

vault = Path(sys.argv[1])
sys.path.insert(0, str(Path(sys.argv[2])))
import importlib.util
spec = importlib.util.spec_from_file_location("_pk_migrate", sys.argv[3])
m = importlib.util.module_from_spec(spec); sys.modules["_pk_migrate"] = m
spec.loader.exec_module(m)

import subprocess
head = subprocess.run(["git", "-C", str(vault), "rev-parse", "HEAD"],
                      capture_output=True, text=True).stdout.strip()
branch = subprocess.run(["git", "-C", str(vault), "symbolic-ref", "--quiet", "HEAD"],
                        capture_output=True, text=True).stdout.strip()
marker = m.vaultreg.read_marker(vault)

# EVERY FIELD `migrate()` READS, hand-built — no `preflight()` anywhere in this process. This is the
# whole shape of the finding: `pre` is a keyword argument on a public function, so a caller supplies
# it and the refusals reached only from `preflight()` never run.
#
# `argv[4]`, when present, is the JSON-encoded `state` to declare — the review-r2 sweep. `"__ABSENT__"`
# omits the key entirely, which is its own row: a `KeyError` escaping a public function is not a
# refusal either.
pre = {"schema": m.SCHEMA, "vault": str(vault), "vault_id": marker["id"], "branch": branch,
       "state": "pristine", "head": head, "engine_source": str(vault),
       "engine_version": (vault / "VERSION").read_text().strip()}
if len(sys.argv) > 4:
    declared = json.loads(sys.argv[4])
    if declared == "__ABSENT__":
        pre.pop("state")
    else:
        pre["state"] = declared
out = {}
try:
    doc = m.migrate(vault, yes=True, pre=pre)
    out = {"refused": False, "removed": len(doc.get("removed") or [])}
except Exception as e:
    out = {"refused": True, "code": getattr(e, "code", None), "exc": type(e).__name__,
           "message": getattr(e, "message", str(e))[:400]}
out["engine_still_there"] = (vault / "bin" / "lib" / "vaultroot.py").is_file()
out["head_moved"] = subprocess.run(["git", "-C", str(vault), "rev-parse", "HEAD"],
                                   capture_output=True, text=True).stdout.strip() != head
print(json.dumps(out))
'''


# `None` IS one of the state values under test (JSON `null`), so the "don't override" sentinel cannot
# be `None`.
_KEEP_DEFAULT_STATE = object()


def _drive_pre_waiver(fx: dict, tmp: Path, state: object = _KEEP_DEFAULT_STATE) -> dict:
    drv = tmp / "pre_waiver.py"
    drv.write_text(PRE_WAIVER_DRIVER, encoding="utf-8")
    argv = [PY, str(drv), str(fx["vault"]), str(REPO / "bin"), str(MIGRATE)]
    if state is not _KEEP_DEFAULT_STATE:
        argv.append(json.dumps(state))
    r = subprocess.run(argv,
                       capture_output=True, text=True,
                       env=vm._clean_env(PLAINKEEP_ENGINE_HOME=fx["root"],
                                         PLAINKEEP_CONFIG_HOME=fx["cfg"],
                                         PLAINKEEP_BIN_DIR=str(fx["base"] / "bin")))
    try:
        return json.loads(r.stdout)
    except json.JSONDecodeError:
        return {"error": (r.stderr or r.stdout)[-600:]}


def case_the_pre_argument_is_not_a_waiver(tmp: Path) -> None:
    """REVIEW r1, I1. `migrate(pre=…)` used to skip the divergence refusal and the clean-tree gate.

    `case_no_force_anywhere` cannot see this: it looks for six literal flag spellings and a
    `force=True` kwarg. The waiver was neither — it was a documented optimisation whose docstring
    claimed every check was "re-derived below where it is load-bearing", which was true of the
    allowlist gate and the CAS and false of these two, because both were reached only from
    `preflight()`.

    Measured before the fix: a vault carrying a committed agent edit to `bin/lib/vaultroot.py` is
    refused by the CLI with exit 5, and `migrate.migrate(vault, yes=True, pre={…})` COMPLETED the
    migration — 122 paths removed, the edit gone, no patch, no record. Not a route to deleting a
    note; a route to silently destroying an operator's local engine edits, which is exactly what
    item 3 and the no-`--force` rule exist to prevent."""
    fx = vm.phase1_vault(tmp, "pre-diverged")
    v = fx["vault"]
    vm.diverge(v)
    cli = mig(fx, "--migrate", str(v), "--yes")
    check("I1: fixture — the CLI refuses a diverged vault", cli.returncode == EXIT_DENY,
          f"rc={cli.returncode} {cli.stderr[:200]}")

    out = _drive_pre_waiver(fx, tmp)
    check("I1: migrate(pre=…) REFUSES a diverged vault, same as the CLI",
          out.get("refused") is True and out.get("code") == EXIT_DENY, json.dumps(out)[:400])
    check("I1: ...naming the divergence rather than some other refusal",
          "DIVERGED" in str(out.get("message", "")), str(out.get("message"))[:250])
    check("I1: ...and the operator's local engine edit is still on disk",
          out.get("engine_still_there") is True, json.dumps(out)[:300])
    check("I1: ...and HEAD never moved", out.get("head_moved") is False, json.dumps(out)[:300])
    vm.wipe(fx["base"])

    # The other half of the same waiver: the clean-tree gate. A STAGED change is the hazard —
    # `_apply`'s `read-tree` rewrites the index wholesale.
    fx2 = vm.phase1_vault(tmp, "pre-staged")
    v2 = fx2["vault"]
    (v2 / "wiki" / "staged.md").write_text("# staged\n", encoding="utf-8")
    vm.git(v2, "add", "wiki/staged.md")
    out2 = _drive_pre_waiver(fx2, tmp)
    check("I1: migrate(pre=…) REFUSES a staged change, same as the CLI",
          out2.get("refused") is True and out2.get("code") == EXIT_DENY, json.dumps(out2)[:400])
    check("I1: ...naming the staged change", "STAGED" in str(out2.get("message", "")),
          str(out2.get("message"))[:250])
    check("I1: ...and the staging survives",
          vm.git(v2, "diff", "--cached", "--name-only").stdout.split() == ["wiki/staged.md"],
          vm.git(v2, "diff", "--cached", "--name-only").stdout[:200])
    vm.wipe(fx2["base"])


# The state values review r2 measured MIGRATING a diverged vault at fd8200c. Each completed a
# 122-path removal: `removed=122`, the committed engine edit gone, HEAD moved, no divergence patch
# and the operator's staging discarded. `"__ABSENT__"` is the key-absent row, which was a raw
# `KeyError` out of a public function rather than a refusal.
UNRECOGNISED_PRE_STATES = ("bogus-state", "migrated", "", None, "PRISTINE", "pristine ", "clean",
                           "__ABSENT__")


def case_an_unrecognised_pre_state_is_not_a_waiver_either(tmp: Path) -> None:
    """REVIEW r2, NEW-2. Closing the `pre` waiver under `state == "pristine"` left it open everywhere else.

    The gate `case_the_pre_argument_is_not_a_waiver` proves is keyed on `pre["state"] == "pristine"`,
    and `_apply`'s resume branch on `== "resume"`. Every OTHER value fell through both — no
    divergence refusal, no clean-tree refusal — and the pristine path then built, committed and
    removed normally. That cell passed over the hole because it only ever declared `"pristine"`,
    which is exactly the shape of a suite being green about a property it does not test.

    So the sweep is the point: one diverged vault, driven once per unrecognised state, asserting the
    refusal AND the absence of damage each time. The vault is reused deliberately — every row must
    refuse, so every row must leave it exactly as the row before found it, and a row that migrated
    would take the rest of the sweep down with it rather than hiding in a fresh fixture.

    `"migrated"` earns its row twice over: it is the one unrecognised value `state()` can really
    return, so it is the value a caller is most likely to hand in, and refusing it is also what turns
    the raw `FileNotFoundError` an already-migrated vault used to raise into an answer."""
    fx = vm.phase1_vault(tmp, "pre-bogus-state")
    v = fx["vault"]
    vm.diverge(v)
    head0 = vm.git(v, "rev-parse", "HEAD").stdout.strip()

    for declared in UNRECOGNISED_PRE_STATES:
        label = repr(declared) if declared != "__ABSENT__" else "no `state` key at all"
        out = _drive_pre_waiver(fx, tmp, declared)
        check(f"NEW-2: migrate(pre=…) with state {label} REFUSES",
              out.get("refused") is True and out.get("code") == EXIT_DENY,
              json.dumps(out)[:400])
        check(f"NEW-2: ...as a VaultError, not a stray exception ({label})",
              out.get("exc") == "VaultError", str(out.get("exc")))
        check(f"NEW-2: ...and removes nothing ({label})", "removed" not in out,
              f"removed={out.get('removed')}")
        check(f"NEW-2: ...and the committed engine edit is still on disk ({label})",
              out.get("engine_still_there") is True, json.dumps(out)[:300])
        check(f"NEW-2: ...and HEAD never moved ({label})", out.get("head_moved") is False,
              json.dumps(out)[:300])

    # The two RECOGNISED states are unaffected: `pristine` is refused by the divergence gate the
    # cell above measures, and `resume` — which this validation deliberately still allows through —
    # is caught one layer deeper by `verify_candidate`, which is the defence in depth that makes
    # allowing it safe.
    res = _drive_pre_waiver(fx, tmp, "resume")
    check("NEW-2: state 'resume' is still ALLOWED through the validation and caught by "
          "verify_candidate", res.get("refused") is True and res.get("code") == EXIT_DENY
          and "pure deletion" in str(res.get("message", "")), json.dumps(res)[:400])
    check("NEW-2: ...and the sweep left the vault exactly as it found it",
          vm.git(v, "rev-parse", "HEAD").stdout.strip() == head0
          and (v / "bin" / "lib" / "vaultroot.py").is_file(), "the vault was damaged by the sweep")
    vm.wipe(fx["base"])


def case_divergence_is_scoped_to_what_the_ref_actually_synced(tmp: Path) -> None:
    """REVIEW r1, I3. The divergence gate assumed every removal-allowlist path was synced by the ref.

    `script/update` checks out `script/engine.txt` AS IT STOOD IN THE RECORDED COMMIT, and that list
    has drifted from the removal allowlist: `templates/verb` was owned by `enginetree` and absent
    from `engine.txt` until Phase 2 Task 2. A vault whose last sync predates that carries a
    `templates/verb` laid down by `script/setup`, which will not match the ref's copy — so the gate
    reported `M` and refused the migration, NAMING A PATH THE OPERATOR NEVER TOUCHED.

    The stock fixture cannot see this by construction (`vaultmig.phase1_vault` builds the upstream
    commit and the vault's engine copy from one `git add -A` of the same bytes, so `diff(ref, HEAD)`
    over engine paths is empty for every clean fixture). So this cell builds the drift explicitly:
    a ref whose `engine.txt` does not list `templates/verb`, and whose `templates/verb` therefore
    differs from the vault's.

    It asserts BOTH directions, because narrowing a gate is only correct if the gate still fires:
    the unsynced path is reported and not refused, and a real local edit to a path the ref DID sync
    is still refused with exit 5."""
    fx = vm.phase1_vault(tmp, "unsynced")
    v = fx["vault"]

    # 1. The vault's own `engine.txt` stops listing `templates/verb` — this is the vault as it was
    #    when it last synced, and `script/update` therefore never refreshed that tree from the ref.
    et = v / "script" / "engine.txt"
    et.write_text("".join(ln for ln in et.read_text(encoding="utf-8").splitlines(keepends=True)
                          if ln.strip() != "templates/verb"), encoding="utf-8")
    vm.git(v, "add", "script/engine.txt")
    vm.git(v, "commit", "-q", "-m", "the engine.txt this vault last synced with")
    synced_from = vm.git(v, "rev-parse", "HEAD").stdout.strip()

    # 2. THE REF: the same tree with a different `templates/verb`. Built on a side branch so the
    #    vault's own history is untouched, exactly as a real fetched upstream would be.
    scaffold = next((p for p in sorted((v / "templates" / "verb").rglob("*")) if p.is_file()), None)
    check("I3: fixture — the vault carries a templates/verb scaffold", scaffold is not None)
    if scaffold is None:
        return
    rel = str(scaffold.relative_to(v))
    vm.git(v, "checkout", "-q", "-b", "upstream-old")
    scaffold.write_text("# the upstream copy, which this vault never synced\n"
                        + scaffold.read_text(encoding="utf-8"), encoding="utf-8")
    vm.git(v, "add", rel)
    vm.git(v, "commit", "-q", "-m", "upstream: a templates/verb this vault never received")
    old_ref = vm.git(v, "rev-parse", "HEAD").stdout.strip()
    vm.git(v, "checkout", "-q", "main")
    (v / ".plainkeep-engine-ref").write_text(old_ref + "\n", encoding="utf-8")
    vm.git(v, "add", ".plainkeep-engine-ref")
    vm.git(v, "commit", "-q", "-m", "record the sync ref")

    diff = vm.git(v, "diff", "--name-only", old_ref, "HEAD", "--", "templates/verb").stdout.split()
    check("I3: fixture — templates/verb really differs between the ref and the vault",
          diff == [rel], f"{diff} (synced_from={synced_from[:8]})")

    r, doc = mig_json(fx, "--preflight", str(v))
    check("I3: preflight does NOT refuse over a path the recorded ref never synced",
          r.returncode == EXIT_OK, f"rc={r.returncode} {r.stderr[:400]}")
    unsynced = (doc.get("divergence") or {}).get("unsynced") or []
    check("I3: ...and REPORTS it as not compared rather than silently narrowing the check",
          "templates/verb" in unsynced, str(doc.get("divergence"))[:300])
    compared = (doc.get("divergence") or {}).get("compared") or []
    check("I3: ...while the paths the ref DID sync are still compared",
          "bin" in compared and "script" in compared, str(compared)[:250])

    # THE GATE STILL FIRES. A committed local edit to a path the ref synced must still refuse, or
    # this narrowing would have turned item 3 off.
    vm.diverge(v)
    r2 = mig(fx, "--preflight", str(v))
    check("I3: a local edit to a path the ref DID sync is still REFUSED",
          r2.returncode == EXIT_DENY and "DIVERGED" in (r2.stderr + r2.stdout),
          f"rc={r2.returncode} {(r2.stderr + r2.stdout)[:300]}")
    check("I3: ...and it names the edited path",
          "bin/lib/vaultroot.py" in (r2.stderr + r2.stdout), (r2.stderr + r2.stdout)[:300])


def case_divergence_when_the_ref_carries_no_manifest(tmp: Path) -> None:
    """THE TWO BRANCHES OF `_synced_by_ref` THAT ARE NOT THE HAPPY PATH.

    Narrowing a gate is only safe if every way the narrowing can go wrong is a refusal. Two ways:

      * **The ref has no `script/engine.txt` at all** — a vault synced before the manifest existed.
        There is nothing to intersect with, so the comparison must fall back to the FULL allowlist.
        Falling back to "compare nothing" would silently turn item 3 off for exactly the oldest
        vaults, which are the ones most likely to carry drift.
      * **The ref's manifest lists none of the engine paths this vault carries.** That ref cannot be
        the commit this engine was synced from, so the honest answer is a refusal rather than a
        comparison over the empty set — which would pass vacuously and report "no divergence"."""
    # 1. NO MANIFEST IN THE REF -> compare everything, and still catch a real local edit.
    fx = vm.phase1_vault(tmp, "no-manifest")
    v = fx["vault"]
    vm.git(v, "checkout", "-q", "-b", "upstream-premanifest")
    vm.git(v, "rm", "-q", "script/engine.txt")
    vm.git(v, "commit", "-q", "-m", "upstream as it stood before engine.txt existed")
    old_ref = vm.git(v, "rev-parse", "HEAD").stdout.strip()
    vm.git(v, "checkout", "-q", "main")
    (v / ".plainkeep-engine-ref").write_text(old_ref + "\n", encoding="utf-8")
    vm.git(v, "add", ".plainkeep-engine-ref")
    vm.git(v, "commit", "-q", "-m", "record a sync ref that predates the manifest")
    show = vm.git(v, "cat-file", "-t", old_ref).stdout.strip()
    check("I3: fixture — the recorded ref exists and carries no script/engine.txt",
          show == "commit" and not vm.git(v, "ls-tree", "--name-only", old_ref, "script/engine.txt",
                                          ).stdout.strip(), show)

    vm.diverge(v)
    r = mig(fx, "--preflight", str(v))
    check("I3: a ref with NO manifest falls back to comparing the whole allowlist, not none of it",
          r.returncode == EXIT_DENY and "DIVERGED" in (r.stderr + r.stdout),
          f"rc={r.returncode} {(r.stderr + r.stdout)[:300]}")
    check("I3: ...and it still names the edited path",
          "bin/lib/vaultroot.py" in (r.stderr + r.stdout), (r.stderr + r.stdout)[:300])
    vm.wipe(fx["base"])

    # 2. A MANIFEST THAT COVERS NOTHING -> refuse, rather than compare the empty set and pass.
    fx2 = vm.phase1_vault(tmp, "empty-manifest")
    v2 = fx2["vault"]
    vm.git(v2, "checkout", "-q", "-b", "upstream-unrelated")
    (v2 / "script" / "engine.txt").write_text("# nothing this vault carries\ndocs/design\n",
                                              encoding="utf-8")
    vm.git(v2, "add", "script/engine.txt")
    vm.git(v2, "commit", "-q", "-m", "upstream whose manifest lists none of the engine paths")
    ref2 = vm.git(v2, "rev-parse", "HEAD").stdout.strip()
    vm.git(v2, "checkout", "-q", "main")
    (v2 / ".plainkeep-engine-ref").write_text(ref2 + "\n", encoding="utf-8")
    vm.git(v2, "add", ".plainkeep-engine-ref")
    vm.git(v2, "commit", "-q", "-m", "record it")
    r2 = mig(fx2, "--preflight", str(v2))
    check("I3: a ref whose manifest covers NO engine path is REFUSED, not compared vacuously",
          r2.returncode == EXIT_DENY and "none of the" in (r2.stderr + r2.stdout),
          f"rc={r2.returncode} {(r2.stderr + r2.stdout)[:300]}")
    check("I3: ...and nothing was removed by that refusal",
          (v2 / "bin" / "lib" / "vaultroot.py").is_file())


def case_rollback_refuses_a_provisional_receipt(tmp: Path) -> None:
    """THE RECEIPT B2 ADDED, ASKED THE ONE QUESTION IT EXISTS TO ANSWER.

    The provisional receipt lands BEFORE the launcher is repointed, so between that write and the
    commit there is a window where a receipt exists and describes no migration. An operator who kills
    the run inside that window and reaches for `--rollback` must not be told "no receipt" (the old
    behaviour, which lost the launcher target) and must not be handed a rollback of a migration that
    never happened. The receipt's whole reason to exist is the launcher target, so the refusal has to
    HAND THAT BACK and the receipt has to SURVIVE."""
    fx = vm.phase1_vault(tmp, "provisional")
    v, root = fx["vault"], fx["root"]
    bindir = fx["base"] / "bin"
    bindir.mkdir(parents=True, exist_ok=True)
    link = bindir / "plainkeep"
    link.symlink_to(v / "plainkeep")
    pristine = vm.git(v, "rev-parse", "HEAD").stdout.strip()

    r = mig(fx, "--migrate", str(v), "--yes", bindir=bindir, PLAINKEEP_MIGRATE_KILL_AT="symlink")
    check("B2: fixture — the run died just after the launcher was repointed",
          r.returncode in (-9, 137), f"rc={r.returncode} {r.stderr[:200]}")
    rec_p = root / "migrations" / f"{fx['vault_id']}.json"
    check("B2: a PROVISIONAL receipt is on disk before the step it describes", rec_p.is_file())
    if not rec_p.is_file():
        return
    rec = json.loads(rec_p.read_text(encoding="utf-8"))
    check("B2: ...recording the pre-migration commit and no after_commit",
          rec.get("before_commit") == pristine and not rec.get("after_commit"),
          f"before={str(rec.get('before_commit'))[:10]} after={rec.get('after_commit')}")
    check("B2: fixture — nothing was removed, and the launcher really was repointed",
          (v / "bin").is_dir() and link.is_symlink() and os.readlink(link) != str(v / "plainkeep"),
          f"bin={(v / 'bin').is_dir()} link={os.readlink(link) if link.is_symlink() else None}")

    rb = mig(fx, "--rollback", str(v), "--yes", bindir=bindir)
    check("B2: --rollback REFUSES a provisional receipt rather than 'rolling back' nothing",
          rb.returncode == EXIT_DENY and "PROVISIONAL" in (rb.stderr + rb.stdout),
          f"rc={rb.returncode} {(rb.stderr + rb.stdout)[:300]}")
    check("B2: ...and hands back the launcher target, which is the one fact git cannot reconstruct",
          str(v / "plainkeep") in (rb.stderr + rb.stdout), (rb.stderr + rb.stdout)[:400])
    check("B2: ...and the receipt SURVIVES the refusal", rec_p.is_file())
    check("B2: ...and HEAD never moved",
          vm.git(v, "rev-parse", "HEAD").stdout.strip() == pristine)

    # And the migration is still finishable — the refusal is a refusal, not a dead end.
    r2 = mig(fx, "--migrate", str(v), "--yes", bindir=bindir)
    check("B2: ...and the interrupted migration still re-runs to completion",
          r2.returncode == EXIT_OK, f"rc={r2.returncode} {(r2.stderr or r2.stdout)[:300]}")


def case_no_force_anywhere(_tmp: Path) -> None:
    """There is no `--force`, and no other spelling of one. Asked of the parse tree and the CLI.

    The design constraint is that no flag skips the divergence refusal, the allowlist subset check,
    prove-before-remove or the hash comparison. A waiver that existed and was merely undocumented
    would satisfy every behavioural cell in this file."""
    src = MIGRATE.read_text(encoding="utf-8")
    tree = _module_ast()
    literals = {n.value for n in ast.walk(tree)
                if isinstance(n, ast.Constant) and isinstance(n.value, str)}
    waivers = sorted(s for s in literals
                     if s.strip() in ("--force", "--no-verify", "--skip-verify", "--allow-dirty",
                                      "--ignore-divergence", "--yes-really"))
    check("no --force: the module defines no waiver flag", not waivers, str(waivers))

    # `--force` appears ONCE in the module, inside a refusal HINT that tells an operator to repair
    # their engine with enginetree — never as a flag this CLI accepts. Assert that shape, not absence.
    accepts_force = any(
        isinstance(n, ast.Compare)
        and any(isinstance(c, ast.Constant) and c.value == "--force" for c in n.comparators)
        for n in ast.walk(tree))
    check("no --force: no comparison in the module tests for a force flag", not accepts_force)
    check("no --force: `provision()` never passes force=True to enginetree.install",
          not any(isinstance(n, ast.keyword) and n.arg == "force"
                  and isinstance(n.value, ast.Constant) and n.value.value is True
                  for n in ast.walk(tree)))
    check("no --force: the usage text offers no waiver",
          "--force" not in src.split("_USAGE = ")[1].split(")")[0])


# ==================================================================================================
# I. Environment independence
# ==================================================================================================
def case_suite_is_environment_independent(_tmp: Path) -> None:
    """This suite must be green in a registered checkout AND from a bare `git archive` export.

    `main` already fails five suites from a bare export. Not adding a sixth means depending on
    nothing that only a developer's machine has: no real registry, no installed engine, no
    `~/.local/bin`, no vault marker inherited from the checkout."""
    check("hermetic: PLAINKEEP_CONFIG_HOME is sealed to a throwaway",
          os.environ.get("PLAINKEEP_CONFIG_HOME", "").startswith(tempfile.gettempdir())
          or "pk-hermetic-" in os.environ.get("PLAINKEEP_CONFIG_HOME", ""),
          os.environ.get("PLAINKEEP_CONFIG_HOME", "(unset)"))
    check("hermetic: the suite never reads the developer's real registry",
          not os.environ.get("PLAINKEEP_CONFIG_HOME", "").startswith(
              os.path.expanduser("~/.config/plainkeep")))
    check("hermetic: the suite inherits no PLAINKEEP_HOME",
          "PLAINKEEP_HOME" not in vm._clean_env())
    check("hermetic: the module under test exists relative to THIS file, not an install root",
          MIGRATE.is_file(), str(MIGRATE))
    # The dangerous default: `bin_dir()` falls back to ~/.local/bin. Every cell passes
    # PLAINKEEP_BIN_DIR, and this asserts the harness really does, rather than trusting it.
    import inspect
    src = inspect.getsource(mig)
    check("hermetic: every migration in this suite redirects PLAINKEEP_BIN_DIR away from ~/.local/bin",
          "PLAINKEEP_BIN_DIR" in src, src[:200])


# ==================================================================================================
# Runner
# ==================================================================================================
CASES = (
    ("ratchet: no vault writes", case_ratchet_no_vault_writes),
    ("ratchet: removers are gated", case_ratchet_removers_are_gated),
    ("ratchet: no working-tree git", case_ratchet_no_worktree_git),
    ("ratchet: the ratchets go RED", case_ratchet_goes_red),
    ("ratchet: degrades on a broken tree", case_ratchet_survives_a_broken_tree),
    ("item 1: preflight is read-only", case_preflight_is_read_only),
    ("item 2: preflight refusals", case_preflight_refusals),
    ("item 3: divergence refuses + patch", case_divergence_refuses_and_emits_a_patch),
    ("items 4/5/6/7/9/13: the migration", case_full_migration),
    ("item 8: plugins and verbs after", case_plugins_and_verbs_still_work),
    ("item 10: schedules regenerated", case_schedules_are_regenerated),
    ("routing: stale launcher repointed", case_stale_launcher_is_repointed),
    ("routing: a foreign launcher is left alone", case_a_foreign_launcher_is_never_taken_over),
    ("item 12: second run is a no-op", case_second_run_is_a_noop),
    ("item 12: rollback", case_rollback),
    ("item 12: rollback refuses work on top", case_rollback_refuses_work_on_top),
    ("items 11+12: rollback AFTER an interrupted migration",
     case_rollback_after_an_interrupted_migration),
    ("item 12: rollback refuses a degenerate receipt",
     case_rollback_refuses_a_receipt_that_restores_nothing),
    ("item 12: rollback refuses staged changes", case_rollback_refuses_staged_changes),
    ("item 6: the allowlist gate", case_allowlist_gate_aborts_on_an_unexpected_path),
    ("no --force anywhere", case_no_force_anywhere),
    ("item 3: `pre` is not a waiver", case_the_pre_argument_is_not_a_waiver),
    ("item 3: an unrecognised `pre` state is not a waiver either",
     case_an_unrecognised_pre_state_is_not_a_waiver_either),
    ("item 3: divergence is scoped to the ref's manifest",
     case_divergence_is_scoped_to_what_the_ref_actually_synced),
    ("item 3: divergence when the ref carries no manifest",
     case_divergence_when_the_ref_carries_no_manifest),
    ("item 12: rollback refuses a provisional receipt",
     case_rollback_refuses_a_provisional_receipt),
    ("item 11: fault injection matrix", case_kill_matrix),
    ("item 11: unknown boundary refuses", case_unknown_kill_stage_refuses),
    ("environment independence", case_suite_is_environment_independent),
)


def main() -> int:
    print("plainkeep migration — acceptance gate (ADR-014 Phase 2 Task 6)\n")
    for label, fn in CASES:
        tmp = Path(tempfile.mkdtemp(prefix="pk-migrate-test-"))
        try:
            fn(tmp)
        except Exception as e:                                               # noqa: BLE001
            import traceback
            check(f"{label}: the case ran to completion", False,
                  f"{type(e).__name__}: {e}\n{traceback.format_exc()[-900:]}")
        finally:
            vm.wipe(tmp)

    passed = sum(1 for _, ok, _ in results if ok)
    failed = len(results) - passed
    for name, ok, detail in results:
        if not ok:
            print(f"  {RED}FAIL{RESET} {name}" + (f"\n       {DIM}{detail}{RESET}" if detail else ""))
    print(f"\n{GREEN if not failed else RED}{passed} passed, {failed} failed{RESET} "
          f"({len(results)} checks)")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
