"""
vaultmig.py — build a PHASE 1 vault: notes and a copy of the engine in one directory.

`test/lib/vaultfx.py` makes the vault Phase 2 produces — data only, marked, dispatched by an engine
that lives somewhere else. This module makes the one Phase 2 has to migrate AWAY from, which is the
shape every existing vault is actually in and the only shape `bin/lib/migrate.py` ever sees.

THE DATA HALF IS NOT HAND-ROLLED. It comes from a real `vault init` through the real installed
launcher, so the skeleton, the `.gitignore`, the jobs registry, the agent adapters and the generated
`plainkeep.json` are the product's own output rather than this file's idea of them. A fixture that
restated them would go stale the first time `init` changed and would then be testing migration
against a vault no version of plainkeep has ever produced — the failure `slim_source` in
`run_engineupdate.py` documents having already paid for once.

THE ENGINE HALF IS A REAL COPY, taken from `enginetree.OWNED_TREES`/`OWNED_FILES` plus the legacy
update machinery (`script/`, `.plainkeep-engine-ref`). Same reason: the allowlist migration checks
against is derived from that manifest, so a fixture that enumerated engine paths itself could agree
with a stale copy of the list and pass while the product disagreed with the real one.

WHAT MAKES IT SHAPED LIKE THE REAL VAULT, and each of these is a hazard the panel named:

  * a `.venv/` with a HAND-INSTALLED package — gitignored, therefore invisible to every git-based
    check, therefore exactly the thing a tree-diff-driven migration could destroy without noticing.
    It is protected content and it is retained; Phase 3 removes it.
  * a plugin pack with a dispatchable verb, so `plugins/` is non-empty user content sitting next to
    engine code in the same tree.
  * a job in `jobs/registry.json` with a schedule, so there is a plist to regenerate and exercise.
  * real notes, captured through the real dispatcher, so the protected manifest hashes bytes the
    product wrote rather than bytes this file wrote.
  * an untracked file and a dirty working tree on request, because "refuses a dirty vault" is an
    acceptance item and a fixture that is always clean cannot exercise it.

Offline, stdlib only.
"""
from __future__ import annotations
import json
import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
PY = sys.executable or "python3"
ENGINETREE = REPO / "bin" / "lib" / "enginetree.py"

# The legacy update machinery. NOT part of `enginetree`'s ownership manifest — it exists only to
# refresh the vault-local copy that migration removes — so it is named here, in the fixture, and
# `migrate.LEGACY_ONLY_TREES`/`LEGACY_ONLY_FILES` name it in the product. `case_allowlist_is_derived`
# in run_migrate.py pins the two spellings against each other rather than letting them drift.
LEGACY_TREES = ("script",)


def _clean_env(**extra) -> dict:
    """A subprocess environment with every vault/engine selector removed, then `extra` applied.

    The same scrub `run_engineupdate.py` uses and for the same reason: a suite that inherits the
    developer's `PLAINKEEP_HOME` builds fixtures that pass because of the machine they ran on. Three
    tasks in this phase wrote into the real environment by accident and one captured ten notes into
    the real vault."""
    env = {k: v for k, v in os.environ.items()
           if k not in ("PLAINKEEP_ENGINE", "PLAINKEEP_HOME", "PLAINKEEP_VAULT_ID",
                        "PLAINKEEP_VAULT_MECHANISM", "PLAINKEEP_ENGINE_KILL_AT",
                        "PLAINKEEP_MIGRATE_KILL_AT", "PLAINKEEP_PLUGIN_PACK", "PYTHONPATH")}
    env.update({k: str(v) for k, v in extra.items()})
    return env


def _enginetree():
    """`bin/lib/enginetree.py` imported under its own name, without shadowing `test/lib`.

    `sys.path` here already has `test/` on it so that `lib.hermetic` resolves; adding `bin/` would
    make `lib` ambiguous. Loading the module by file location keeps both `lib` packages addressable,
    which is the same dance `run_engineupdate.py` does for the same reason."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("_pk_enginetree", ENGINETREE)
    mod = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("_pk_enginetree", mod)
    spec.loader.exec_module(mod)
    return mod


def install_engine(root: Path, *, source: Path | None = None) -> Path:
    """Install and activate an engine at `root`, and return the stable launcher.

    Returns `<root>/engine/current/plainkeep` — the name a plist and a PATH symlink must carry, and
    the one migration repoints a stale shim onto."""
    src = source or REPO
    r = subprocess.run([PY, str(ENGINETREE), "--install", str(src)],
                       capture_output=True, text=True, env=_clean_env(PLAINKEEP_ENGINE_HOME=root))
    if r.returncode != 0:
        raise RuntimeError(f"fixture: engine install failed:\n{r.stderr or r.stdout}")
    return root / "engine" / "current" / "plainkeep"


def dispatch(launcher: Path, vault: Path, root: Path, cfg: Path, *argv: str,
             mode: str = "off", cwd: Path | None = None) -> subprocess.CompletedProcess:
    """Run a verb the way an operator does — through the installed launcher, nothing imported."""
    try:
        return subprocess.run([str(launcher), *argv], capture_output=True, text=True,
                              cwd=str(cwd) if cwd else None,
                              env=_clean_env(PLAINKEEP_ENGINE_HOME=root, PLAINKEEP_HOME=vault,
                                             PLAINKEEP_CONFIG_HOME=cfg, PLAINKEEP_CORE=mode))
    except OSError as e:
        return subprocess.CompletedProcess([str(launcher), *argv], 127, "", f"{e}")


def git(root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    r = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True,
                       env={**os.environ, "GIT_AUTHOR_NAME": "fixture",
                            "GIT_AUTHOR_EMAIL": "fixture@plainkeep.invalid",
                            "GIT_COMMITTER_NAME": "fixture",
                            "GIT_COMMITTER_EMAIL": "fixture@plainkeep.invalid"})
    if check and r.returncode != 0:
        raise RuntimeError(f"fixture: git {args[0]} failed:\n{r.stderr or r.stdout}")
    return r


def copy_engine_into(vault: Path, *, source: Path | None = None) -> list[str]:
    """Put a Phase 1 engine copy inside `vault` and return the relative paths written.

    The set is READ FROM THE PRODUCT (`enginetree.OWNED_TREES`/`OWNED_FILES`) plus `script/`. See the
    module docstring for why it is not written out here."""
    src = source or REPO
    et = _enginetree()
    ign = shutil.ignore_patterns("__pycache__", "*.pyc")
    wrote: list[str] = []
    for rel in (*et.OWNED_TREES, *LEGACY_TREES):
        d = vault / rel
        d.parent.mkdir(parents=True, exist_ok=True)
        if d.exists():
            shutil.rmtree(d)
        shutil.copytree(src / rel, d, ignore=ign, symlinks=True)
        wrote.append(rel + "/")
    for rel in et.OWNED_FILES:
        (vault / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src / rel, vault / rel)
        wrote.append(rel)
    return wrote


def add_venv(vault: Path, *, package: str = "handinstalled") -> Path:
    """A `.venv` carrying a package nobody can reinstall from a lockfile.

    Codex's second-most-expected failure: a plugin depends on something somebody `pip install`ed into
    the vault's virtualenv years ago, the migration removes the venv, and the dependency is gone with
    no record of what it was. `.venv/` is gitignored, so NO git-based check can see this directory at
    all — which is the entire point of hashing the filesystem rather than the tree."""
    site = vault / ".venv" / "lib" / "python3.12" / "site-packages" / package
    site.mkdir(parents=True, exist_ok=True)
    (site / "__init__.py").write_text(
        f'"""A package hand-installed into this vault\'s venv. Not in any lockfile."""\n'
        f'MARKER = "{uuid.uuid4()}"\n', encoding="utf-8")
    (vault / ".venv" / "pyvenv.cfg").write_text("home = /usr/bin\nversion = 3.12.0\n",
                                                encoding="utf-8")
    bindir = vault / ".venv" / "bin"
    bindir.mkdir(parents=True, exist_ok=True)
    (bindir / "python").write_text("#!/bin/sh\nexec /usr/bin/env python3 \"$@\"\n", encoding="utf-8")
    (bindir / "python").chmod(0o755)
    return vault / ".venv"


def add_plugin(vault: Path, *, pack: str = "sitepack", verb: str = "zzfixture") -> Path:
    """A plugin pack with a dispatchable verb — user content living beside engine code."""
    d = vault / "plugins" / pack / verb
    d.mkdir(parents=True, exist_ok=True)
    (d / "run.py").write_text(
        "#!/usr/bin/env python3\n"
        '"""A fixture plugin verb. Exists so `plugins/` is real user content, not an empty dir."""\n'
        "import json, sys\n\n\n"
        "def main(argv):\n"
        "    print(json.dumps({'verb': '%s', 'ok': True, 'argv': argv}))\n"
        "    return 0\n\n\n"
        'if __name__ == "__main__":\n'
        "    raise SystemExit(main(sys.argv[1:]))\n" % verb, encoding="utf-8")
    return d


def add_job(vault: Path, *, name: str = "fixture-consolidate",
            command: str = "plainkeep status", daily: str = "03:30") -> dict:
    """A scheduled job, so `job apply` has a plist to render and migration has one to regenerate.

    A READ verb on purpose. `regenerate_schedules` runs each rendered plist's exact
    `ProgramArguments` with `--dry-run` appended, and a job whose verb writes would put bytes into
    the vault at the moment the migration is about to hash it."""
    reg_path = vault / "jobs" / "registry.json"
    reg = json.loads(reg_path.read_text(encoding="utf-8"))
    reg["jobs"][name] = {"command": command, "schedule": {"daily": daily}, "risk": "read"}
    reg_path.write_text(json.dumps(reg, indent=2) + "\n", encoding="utf-8")
    return reg


def phase1_vault(tmp: Path, label: str, *, notes: int = 3, source: Path | None = None) -> dict:
    """THE FIXTURE. A committed, clean, marked, registered Phase 1 vault with an engine inside it.

    Returns every handle a case needs: the vault, the install root, the config home, the launcher,
    the vault id and the commit `.plainkeep-engine-ref` records.

    The ordering matters and is the product's own: install an engine, `vault init` a data vault with
    it, capture real notes through it, and only then lay the Phase 1 engine copy on top and commit.
    Doing it the other way — engine first — would hit `vault init`'s own refusal that the target
    already carries engine paths, which is the correct behaviour and would make the fixture
    unbuildable through the product."""
    base = tmp / label
    root, cfg, vault = base / "engine-root", base / "cfg", base / "vault"
    for d in (root, cfg):
        d.mkdir(parents=True, exist_ok=True)
    vault.parent.mkdir(parents=True, exist_ok=True)

    launcher = install_engine(root, source=source)

    # THE BOOTSTRAP PATH, and it is the INSTALLED engine's copy of the verb, not the repo's: no vault
    # exists yet, so there is nothing to dispatch through, and `PLAINKEEP_HOME` names the target the
    # verb is about to create. Same shape `vaultroot.bootstrap_hint` hands an operator, same one
    # `script/setup` uses, and the same one migration's own module CLI exists for.
    r = subprocess.run([PY, str(root / "engine" / "current" / "bin" / "vault" / "run.py"),
                        "init", str(vault), "--name", label.replace("_", "-"), "--yes"],
                       capture_output=True, text=True,
                       env=_clean_env(PLAINKEEP_ENGINE_HOME=root, PLAINKEEP_HOME=vault,
                                      PLAINKEEP_CONFIG_HOME=cfg))
    if r.returncode != 0:
        raise RuntimeError(f"fixture: vault init failed:\n{r.stderr or r.stdout}")

    for i in range(notes):
        c = dispatch(launcher, vault, root, cfg, "capture", f"fixture note {i} for {label}")
        if c.returncode != 0:
            raise RuntimeError(f"fixture: capture {i} failed:\n{c.stderr or c.stdout}")

    add_plugin(vault)
    add_job(vault)
    add_venv(vault)

    # `jobs/launchd/` holds RENDERED plists: machine-specific absolute paths, regenerated by
    # `job apply`. The real vault's `.gitignore` ignores them (see this repo's, line 36) and
    # `vault init`'s does not, so the fixture carries the real vault's shape rather than init's —
    # otherwise `regenerate_schedules` would leave the tree dirty for a reason no real vault has.
    gi = vault / ".gitignore"
    gi.write_text(gi.read_text(encoding="utf-8") + "\n# Rendered launchd plists\njobs/launchd/\n",
                  encoding="utf-8")

    engine_rels = copy_engine_into(vault, source=source)

    # TWO COMMITS, and the split reproduces the real relationship rather than a convenient one.
    # `.plainkeep-engine-ref` names the UPSTREAM commit a vault last synced from, and that commit's
    # tree cannot contain the file — nothing can name the commit it lives in. In a real vault the ref
    # points into a FETCHED upstream history; here the first commit plays upstream (engine paths and
    # data, no ref file) and the second is the vault recording what it synced to.
    #
    # A fixture that pointed the ref at its own HEAD would be a vault no `script/update` has ever
    # produced, and it would have hidden the product bug this arrangement found: `_divergence` used
    # to compare `.plainkeep-engine-ref` against the very commit it records, which reports `A` in
    # every real vault and refuses every real migration.
    git(vault, "init", "-q", "-b", "main")
    git(vault, "add", "-A")
    git(vault, "commit", "-q", "-m", "fixture: a phase 1 vault — notes and an engine copy in one tree")
    upstream = git(vault, "rev-parse", "HEAD").stdout.strip()
    (vault / ".plainkeep-engine-ref").write_text(upstream + "\n", encoding="utf-8")
    git(vault, "add", ".plainkeep-engine-ref")
    git(vault, "commit", "-q", "-m", "fixture: record the engine sync ref")
    ref = upstream

    vid = json.loads((vault / ".plainkeep" / "vault.json").read_text(encoding="utf-8"))["id"]
    return {"vault": vault, "root": root, "cfg": cfg, "launcher": launcher,
            "vault_id": vid, "engine_ref": ref,
            "head": git(vault, "rev-parse", "HEAD").stdout.strip(),
            "engine_rels": engine_rels, "base": base}


def dirty(vault: Path, rel: str = "wiki/scratch.md", body: str = "uncommitted\n") -> Path:
    """An untracked file — the working tree an operator actually has when they try to migrate."""
    p = vault / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")
    return p


def diverge(vault: Path, rel: str = "bin/lib/vaultroot.py",
            marker: str = "# LOCAL EDIT an agent made inside this vault\n") -> Path:
    """A committed local edit to `bin/**` — the specific hazard acceptance item 3 exists for.

    Committed, not left dirty: a dirty tree is refused by an earlier gate, so leaving it uncommitted
    would prove the clean-tree check fires and say nothing about the divergence check. The two
    refusals are different and this fixture is for the second one."""
    p = vault / rel
    p.write_text(marker + p.read_text(encoding="utf-8"), encoding="utf-8")
    git(vault, "add", rel)
    git(vault, "commit", "-q", "-m", "an agent edited the engine inside the vault")
    return p


def unlock(p: Path) -> None:
    """Installed engine trees are sealed 0555; a temp dir holding one cannot be removed until they
    are writable again."""
    for q in [p, *p.rglob("*")]:
        try:
            if q.is_dir() and not q.is_symlink():
                q.chmod(0o755)
            elif not q.is_symlink():
                q.chmod(0o644)
        except OSError:
            pass


def wipe(p: Path) -> None:
    if p.exists():
        unlock(p)
        shutil.rmtree(p, ignore_errors=True)
