#!/usr/bin/env python3
"""
run_engineupdate.py — `vault init` and engine `update`, driven THROUGH FAILURE (Phase 2 Task 5).

This suite's subject is not that the happy path works. It is the sentence the update contract
actually makes:

    after an interruption at ANY boundary, either the old pair or the new pair is FULLY RUNNABLE —
    never neither — and re-running the same command converges.

That is a claim about what survives a `SIGKILL`, and there is exactly one way to check it: send the
`SIGKILL`. So `case_kill_matrix` below kills a real `--update` at each of the eight boundaries
`enginetree.KILL_STAGES` names — provision (three of them, including the one measured residue),
checksum, self-test, activation, the pointer switch, cleanup — and after each one drives a REAL VERB
through the REAL dispatcher at `engine/current/plainkeep`. Not a file listing: a listing shows that
paths exist, and every one of the six unwired rules ADR-019 catalogues would have passed a check of
that shape. A dispatch is the only thing that shows the engine RUNS.

The residue this module's own docstring admits — a kill between `remove_version()` and `os.rename`
leaves no tree under that version name — is covered rather than avoided, in BOTH of its shapes:
`case_kill_matrix` proves it is harmless on the update path (the target is never the running
version, so the running pair is untouched), and `case_the_open_residue` proves it is still open on
`--install --force` over the ACTIVE version, with the recovery command that fixes it. A gate that
skipped the second would be describing a window as closed that is not.

HERMETIC, twice over, and this is the constraint that matters most on a machine with real notes on
it. Every install goes to a temp `PLAINKEEP_ENGINE_HOME`; every dispatch runs against a throwaway
marked vault with a throwaway `PLAINKEEP_CONFIG_HOME`. `~/.local/share/plainkeep`,
`~/.config/plainkeep` and the developer's vault are neither read nor written.

Offline, stdlib only.
"""
from __future__ import annotations
import ast
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from lib.hermetic import seal
seal()   # hermetic: an empty throwaway registry, never the developer's real vault

REPO = Path(__file__).resolve().parents[1]
ENGINETREE = REPO / "bin" / "lib" / "enginetree.py"
PY = sys.executable
GREEN, RED, DIM, BOLD, RESET = "\033[32m", "\033[31m", "\033[2m", "\033[1m", "\033[0m"
results: list[tuple[str, bool, str]] = []
notes: list[str] = []
# Which dispatcher modes a runnability proof ACTUALLY ran in. `modes_for()` derives them from the
# pair under test, and a derivation is exactly the kind of thing that can quietly collapse to one
# value — so the set is recorded and asserted at the end rather than assumed.
_modes_used: set[str] = set()

EXIT_OK, EXIT_UNEXPECTED, EXIT_USAGE, EXIT_CONFIRM, EXIT_NOT_FOUND, EXIT_DENY = 0, 1, 2, 3, 4, 5
SIGKILL_RC = -signal.SIGKILL

# The dispatcher modes every runnability proof is made in. `PLAINKEEP_CORE=require` is the one that
# exercises the compiled core; `off` is the bash floor. Neither is `PLAINKEEP_REQUIRE_CORE` and
# neither is `PLAINKEEP_PARITY_FAULT_SIGNALS` — those run signal-delivery cells and are never set
# here.
MODES = ("off", "require")


def check(name: str, cond: bool, detail: str = "") -> None:
    results.append((name, bool(cond), detail))


# --------------------------------------------------------------------------------------------
# Harness
# --------------------------------------------------------------------------------------------
def _clean_env(**extra) -> dict:
    env = {k: v for k, v in os.environ.items()
           if k not in ("PLAINKEEP_ENGINE", "PLAINKEEP_HOME", "PLAINKEEP_VAULT_ID",
                        "PLAINKEEP_VAULT_MECHANISM", "PLAINKEEP_ENGINE_KILL_AT",
                        "PLAINKEEP_PLUGIN_PACK", "PYTHONPATH")}
    env.update({k: str(v) for k, v in extra.items()})
    return env


def et(root: Path, *args: str, **extra) -> subprocess.CompletedProcess:
    """The installer/updater CLI, with the install root pointed at `root`."""
    return subprocess.run([PY, str(ENGINETREE), *args], capture_output=True, text=True,
                          env=_clean_env(PLAINKEEP_ENGINE_HOME=root, **extra))


def scratch_vault(tmp: Path, label: str) -> tuple[Path, Path]:
    """A marked throwaway vault and an empty config home — what every dispatch below runs against.

    Built with `vaultfx.mark_vault` rather than by shelling out to `vault register`, for the reason
    that module states: registering writes a REGISTRY, and a registry lives outside every vault.
    `PLAINKEEP_HOME` needs only the marker."""
    from lib.vaultfx import mark_vault
    v, c = tmp / f"vault-{label}", tmp / f"cfg-{label}"
    v.mkdir(parents=True, exist_ok=True)
    c.mkdir(parents=True, exist_ok=True)
    mark_vault(v)
    return v, c


def dispatch(root: Path, vault: Path, cfg: Path, *argv: str,
             mode: str = "off") -> subprocess.CompletedProcess:
    """Run a verb through `<root>/engine/current/plainkeep` — the launcher an operator has on PATH."""
    launcher = root / "engine" / "current" / "plainkeep"
    try:
        return subprocess.run([str(launcher), *argv], capture_output=True, text=True,
                              env=_clean_env(PLAINKEEP_ENGINE_HOME=root, PLAINKEEP_HOME=vault,
                                             PLAINKEEP_CONFIG_HOME=cfg, PLAINKEEP_CORE=mode))
    except OSError as e:
        # `current` dangling, or gone. That is one of the states this suite EXISTS to reach, so it
        # is an answer ("nothing runs") rather than an error that takes the batch down with it.
        return subprocess.CompletedProcess([str(launcher), *argv], 127, "", f"{e}")


def modes_for(root: Path) -> tuple[str, ...]:
    """Which dispatcher modes the ACTIVE pair can honestly be asked about.

    `require` refuses to degrade when there is no core binary, so demanding it of an engine-only
    pair would fail a pair that is working exactly as designed — the bash floor is the zero-install
    path. The modes are derived from what the pair CARRIES rather than pinned, and
    `case_modes_really_are_both` below proves this never silently collapses to floor-only."""
    active = root / "engine" / "current"
    return MODES if (active / ".local" / "bin" / "plainkeep-core").is_file() else ("off",)


def runnable(root: Path, vault: Path, cfg: Path, *, modes=None) -> tuple[bool, str]:
    """Is the ACTIVE pair fully runnable? Answered by using it, in every dispatcher mode it has.

    Two verbs, deliberately: `vault status --json` walks discovery → gate → resolver → verb → the
    `--json` envelope and REPORTS the engine it ran out of, and `capture` actually writes a note.
    "Runnable" that only means "a read-only diagnostic exits 0" is a weaker claim than this task
    makes."""
    for mode in (modes if modes is not None else modes_for(root)):
        _modes_used.add(mode)
        r = dispatch(root, vault, cfg, "vault", "status", "--json", mode=mode)
        if r.returncode != EXIT_OK:
            return False, f"[{mode}] vault status rc={r.returncode} {r.stderr.strip()[:160]}"
        try:
            data = json.loads(r.stdout)["data"]
        except Exception as e:                                  # noqa: BLE001 - report, don't raise
            return False, f"[{mode}] no --json envelope ({e}) {r.stdout[:120]}"
        if not data.get("engine_intact"):
            return False, f"[{mode}] engine_intact is false at {data.get('engine_root')}"
        w = dispatch(root, vault, cfg, "capture", f"runnability probe {mode} {time.time()}",
                     mode=mode)
        if w.returncode != EXIT_OK:
            return False, f"[{mode}] capture rc={w.returncode} {w.stderr.strip()[:160]}"
    return True, ""


def active_version(root: Path) -> str | None:
    link = root / "engine" / "current"
    try:
        return Path(os.path.realpath(link)).name if link.is_symlink() else None
    except OSError:
        return None


def pairs(root: Path) -> dict:
    r = et(root, "--print", "pairs", "--json")
    try:
        return json.loads(r.stdout)
    except Exception:                                           # noqa: BLE001
        return {}


def slim_source(tmp: Path, label: str, *, mutate=None) -> Path:
    """A copy of the repo carrying exactly the engine-owned set (plus the core when asked).

    Used where a case needs to DAMAGE a source: mutating the real checkout is not an option, and a
    copy is also how the version under test gets different bytes from the one already installed."""
    src = tmp / f"src-{label}"
    if src.exists():
        shutil.rmtree(src, ignore_errors=True)
    ign = shutil.ignore_patterns("__pycache__", "*.pyc")
    for rel in ("bin", "templates/verb", "frontends/raycast", "skills/operate-plainkeep"):
        d = src / rel
        d.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(REPO / rel, d, ignore=ign, symlinks=True)
    for rel in ("VERSION", "plainkeep"):
        shutil.copy2(REPO / rel, src / rel)
    if mutate:
        mutate(src)
    return src


def with_core(src: Path) -> Path:
    core = REPO / ".local" / "bin" / "plainkeep-core"
    if core.is_file():
        d = src / ".local" / "bin" / "plainkeep-core"
        d.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(core, d)
    return src


def unlock(p: Path) -> None:
    """Installed trees are sealed 0555; a fixture that has to be edited or removed needs them back."""
    for q in [p, *p.rglob("*")]:
        try:
            if q.is_dir() and not q.is_symlink():
                q.chmod(0o755)
            elif not q.is_symlink():
                q.chmod(0o644)
        except OSError:
            pass


# --------------------------------------------------------------------------------------------
# A. `vault init` — a DATA-ONLY vault that works the moment it exists.
# --------------------------------------------------------------------------------------------
def case_init(tmp: Path) -> None:
    root = tmp / "init-engine"
    r = et(root, "--install", str(with_core(slim_source(tmp, "init"))), "--version", "1.0.0")
    check("fixture: an engine is installed and activated", r.returncode == EXIT_OK,
          r.stdout + r.stderr)
    eng = root / "engine" / "current"
    cfg = tmp / "init-cfg"
    cfg.mkdir()
    target = tmp / "fresh-vault"

    # THE BOOTSTRAP PATH: no vault exists yet, so the verb is invoked directly — the same shape
    # `vaultroot.bootstrap_hint` hands an operator and `script/setup` uses. A top-level
    # `plainkeep init` could not be run here at all, which is why it is not one.
    r = subprocess.run([PY, str(eng / "bin" / "vault" / "run.py"), "init", str(target), "--yes",
                        "--json"],
                       capture_output=True, text=True,
                       env=_clean_env(PLAINKEEP_ENGINE_HOME=root, PLAINKEEP_HOME=target,
                                      PLAINKEEP_CONFIG_HOME=cfg))
    check("init: creates a vault with no vault present at all (the bootstrap path)",
          r.returncode == EXIT_OK, f"rc={r.returncode} {r.stderr[:300]}")
    try:
        data = json.loads(r.stdout)["data"]
    except Exception as e:                                      # noqa: BLE001
        check("init: emits a --json envelope", False, f"{e} {r.stdout[:200]}")
        return
    check("init: reports the vault as data-only", data.get("data_only") is True,
          str(data.get("engine_paths")))
    check("init: generated plainkeep.json by really dispatching into the new vault",
          data.get("manifest") is True and (target / "plainkeep.json").is_file(),
          str(data.get("manifest_detail")))

    # The claim, checked against the filesystem rather than against the verb's own report.
    for rel in ("bin", "plainkeep", "VERSION", "skills", "templates/verb"):
        check(f"init: the vault carries NO {rel}", not (target / rel).exists())
    check("init: the marker is there", (target / ".plainkeep" / "vault.json").is_file())
    reg = json.loads((cfg / "registry.json").read_text(encoding="utf-8"))
    check("init: the registry has exactly this vault, as the default",
          len(reg["vaults"]) == 1 and reg["default"] == reg["vaults"][0]["id"]
          and reg["vaults"][0]["id"] == data["id"], json.dumps(reg)[:200])
    for rel in ("wiki", "tasks/inbox", "tasks/active", "tasks/waiting", "tasks/done", "journal",
                "inbox", "templates", "jobs", "plugins"):
        check(f"init: skeleton {rel}/", (target / rel).is_dir())
    for rel in (".gitignore", "jobs/registry.json", "AGENTS.md", "CLAUDE.md"):
        check(f"init: configuration {rel}", (target / rel).is_file())
    check("init: .gitignore excludes the marker (an id names ONE vault)",
          ".plainkeep/" in (target / ".gitignore").read_text(encoding="utf-8"))
    check("init: CLAUDE.md bridges to AGENTS.md (what doctor gates on)",
          "@AGENTS.md" in (target / "CLAUDE.md").read_text(encoding="utf-8"))

    # USABLE IMMEDIATELY, through the real dispatcher, in both modes — including a health check,
    # because a fresh vault whose first `doctor` is red is not usable in any sense an operator
    # would accept.
    for mode in MODES:
        d = dispatch(root, target, cfg, "doctor", mode=mode)
        check(f"init: `plainkeep doctor` is GREEN on the fresh vault (PLAINKEEP_CORE={mode})",
              d.returncode == EXIT_OK, f"rc={d.returncode} " + (d.stdout + d.stderr)[-300:])
        c = dispatch(root, target, cfg, "capture", "a first note", mode=mode)
        check(f"init: `plainkeep capture` writes into it (PLAINKEEP_CORE={mode})",
              c.returncode == EXIT_OK, c.stderr[:200])
    check("init: the captured notes really landed in the new vault's inbox",
          len(list((target / "inbox").glob("*.md"))) >= 2)

    # ...AND THROUGH THE REAL DISPATCHER. Everything above used the bootstrap invocation, which is
    # the path for a machine with no vault; the ordinary path is `plainkeep vault init`, and it has
    # to survive the gate, the resolver and the verb spawn. ADR-019 D1: a rule is not enforced until
    # a test drives the product's own entry point, and "init works" is exactly such a rule.
    for mode in MODES:
        second = tmp / f"second-vault-{mode}"
        d = dispatch(root, target, cfg, "vault", "init", str(second), "--name", f"second{mode}",
                     "--yes", "--json", mode=mode)
        check(f"init: `plainkeep vault init` works through the real dispatcher "
              f"(PLAINKEEP_CORE={mode})", d.returncode == EXIT_OK,
              f"rc={d.returncode} {(d.stdout + d.stderr)[-250:]}")
        if d.returncode == EXIT_OK:
            payload = json.loads(d.stdout)["data"]
            check(f"init: ...it made a data-only vault (PLAINKEEP_CORE={mode})",
                  payload["data_only"] is True and not (second / "bin").exists(),
                  str(payload.get("engine_paths")))
            check(f"init: ...the vault it acted on is NOT the one it was dispatched from "
                  f"(PLAINKEEP_CORE={mode})",
                  payload["path"] == str(os.path.realpath(second)), payload["path"])
        e = dispatch(root, target, cfg, "vault", "init", str(tmp / f"no-yes-{mode}"), mode=mode)
        check(f"init: ...and still refuses without --yes through the dispatcher "
              f"(PLAINKEEP_CORE={mode})",
              e.returncode == EXIT_CONFIRM and not (tmp / f"no-yes-{mode}").exists(),
              f"rc={e.returncode} {(e.stdout + e.stderr)[:200]}")

    # The action is on the SURFACE: `plainkeep.json` is what an agent reads to learn what exists, and
    # an action absent from it is one no agent will ever call (the frozen machine contract, §4.3).
    surface = json.loads((target / "plainkeep.json").read_text(encoding="utf-8"))
    vault_verb = next((v for v in surface["verbs"] if v.get("verb") == "vault"), None) \
        if isinstance(surface.get("verbs"), list) else surface.get("verbs", {}).get("vault")
    blob = json.dumps(vault_verb or surface)
    check("init: the action is declared in plainkeep.json, so an agent can find it",
          '"init"' in blob and "DATA-ONLY" in blob, blob[:200])

    # A SECOND init of the same path is refused rather than half-repeated.
    r2 = subprocess.run([PY, str(eng / "bin" / "vault" / "run.py"), "init", str(target), "--yes"],
                        capture_output=True, text=True,
                        env=_clean_env(PLAINKEEP_ENGINE_HOME=root, PLAINKEEP_HOME=target,
                                       PLAINKEEP_CONFIG_HOME=cfg))
    check("init: re-initing an existing vault REFUSES (it is `register`'s job, or nobody's)",
          r2.returncode != EXIT_OK and "already a plainkeep vault" in (r2.stdout + r2.stderr),
          f"rc={r2.returncode} {(r2.stdout + r2.stderr)[:200]}")


def case_init_refusals(tmp: Path) -> None:
    """The refusals that stop init producing a vault the dispatcher would then reject."""
    root = tmp / "initref-engine"
    et(root, "--install", str(slim_source(tmp, "initref")), "--version", "1.0.0")
    eng = root / "engine" / "current"
    cfg = tmp / "initref-cfg"
    cfg.mkdir()

    def init(path, *extra, home=None):
        return subprocess.run([PY, str(eng / "bin" / "vault" / "run.py"), "init", str(path),
                               *extra],
                              capture_output=True, text=True,
                              env=_clean_env(PLAINKEEP_ENGINE_HOME=root,
                                             PLAINKEEP_HOME=home or path,
                                             PLAINKEEP_CONFIG_HOME=cfg))

    r = init(tmp / "no-consent")
    check("init: refuses without --yes (exit 3, with the re-run line)",
          r.returncode == EXIT_CONFIRM and "--yes" in (r.stdout + r.stderr),
          f"rc={r.returncode} {(r.stdout + r.stderr)[:200]}")
    check("init: ...and created nothing", not (tmp / "no-consent").exists())

    # DISJOINTNESS: a vault inside the engine tree would be refused with exit 5 by every subsequent
    # dispatch. Creating one is worse than refusing — it succeeds and then nothing works.
    inside = Path(os.path.realpath(eng)) / "a-vault-inside-the-engine"
    r = init(inside, "--yes")
    check("init: refuses a target INSIDE the engine tree (exit 5, the disjointness rule)",
          r.returncode == EXIT_DENY and "engine" in (r.stdout + r.stderr).lower(),
          f"rc={r.returncode} {(r.stdout + r.stderr)[:200]}")
    check("init: ...and did not create it", not inside.exists())

    # ...and the engine tree ITSELF.
    r = init(Path(os.path.realpath(eng)), "--yes")
    check("init: refuses the engine tree itself", r.returncode == EXIT_DENY,
          f"rc={r.returncode} {(r.stdout + r.stderr)[:200]}")

    # A CHECKOUT is not a fresh vault: `register` adopts one, `init` does not.
    checkout = slim_source(tmp, "checkout-target")
    r = init(checkout, "--yes")
    check("init: refuses a directory that already carries engine code",
          r.returncode == EXIT_USAGE and "engine code" in (r.stdout + r.stderr),
          f"rc={r.returncode} {(r.stdout + r.stderr)[:200]}")
    check("init: ...and points at `vault register` instead", "register" in (r.stdout + r.stderr))

    # A name collision refuses rather than producing two vaults one name resolves to.
    a, b = tmp / "name-a", tmp / "name-b"
    check("fixture: first init of a name succeeds", init(a, "--name", "dup", "--yes").returncode == 0)
    r = init(b, "--name", "dup", "--yes")
    check("init: a taken registry name REFUSES", r.returncode == EXIT_USAGE
          and "already taken" in (r.stdout + r.stderr), (r.stdout + r.stderr)[:200])
    check("init: ...and the loser is not in the registry",
          len(json.loads((cfg / "registry.json").read_text(encoding="utf-8"))["vaults"]) == 1)


# --------------------------------------------------------------------------------------------
# B. `update` — the happy path, and what it retains.
# --------------------------------------------------------------------------------------------
def case_update_retains_the_previous_pair(tmp: Path) -> None:
    root = tmp / "upd-engine"
    vault, cfg = scratch_vault(tmp, "upd")
    src = with_core(slim_source(tmp, "upd"))
    et(root, "--install", str(src), "--version", "1.0.0")
    ok, why = runnable(root, vault, cfg)
    check("fixture: version 1.0.0 is installed and fully runnable", ok, why)

    r = et(root, "--update", str(src), "--version", "2.0.0", "--json")
    check("update: activates the new pair", r.returncode == EXIT_OK, r.stdout + r.stderr)
    try:
        res = json.loads(r.stdout)
    except Exception:                                           # noqa: BLE001
        check("update: emits --json", False, r.stdout[:200])
        return
    check("update: reports the pair it replaced", res.get("previous") == "1.0.0", json.dumps(res))
    check("update: it is a core+ENGINE pair, not an engine alone", res.get("core") is True,
          json.dumps(res))
    check("update: `current` points at the new version", active_version(root) == "2.0.0")
    ok, why = runnable(root, vault, cfg)
    check("update: the NEW pair is fully runnable in both dispatcher modes", ok, why)

    # RETAINED, and the proof is not that the directory exists.
    old = root / "engine" / "1.0.0"
    check("update: the previous version's tree is still there", old.is_dir())
    prev_launcher = old / "plainkeep"
    p = subprocess.run([str(prev_launcher), "vault", "status", "--json"], capture_output=True,
                       text=True, env=_clean_env(PLAINKEEP_ENGINE_HOME=root, PLAINKEEP_HOME=vault,
                                                 PLAINKEEP_CONFIG_HOME=cfg, PLAINKEEP_CORE="off"))
    check("update: the PREVIOUS pair still dispatches when run directly", p.returncode == EXIT_OK,
          f"rc={p.returncode} {p.stderr[:200]}")
    check("update: ...and it reports itself, not the new engine",
          json.loads(p.stdout)["data"]["engine_root"] == str(os.path.realpath(old))
          if p.returncode == 0 else False)
    rep = pairs(root)
    check("update: --print pairs names the rollback target", rep.get("rollback_to") == "1.0.0",
          json.dumps(rep)[:300])

    # A SECOND run of the same command is a NO-OP.
    r = et(root, "--update", str(src), "--version", "2.0.0", "--json")
    res2 = json.loads(r.stdout) if r.returncode == 0 else {}
    check("update: re-running the same update is a NO-OP, not an error",
          r.returncode == EXIT_OK and res2.get("result") == "already-active",
          f"rc={r.returncode} {r.stdout[:200]}")
    check("update: ...and the no-op still names what a rollback would do",
          res2.get("rollback_to") == "1.0.0", json.dumps(res2)[:200])


def case_rollback_is_a_tested_command_sequence(tmp: Path) -> None:
    """THE ROLLBACK RUNBOOK, executed. Every line below is a command an operator types; the checks
    are what each one must produce. It is here rather than in a doc because a rollback nobody has
    run is a rollback nobody knows works."""
    root = tmp / "rb-engine"
    vault, cfg = scratch_vault(tmp, "rb")
    src = with_core(slim_source(tmp, "rb"))
    et(root, "--install", str(src), "--version", "1.0.0")
    et(root, "--update", str(src), "--version", "2.0.0")
    check("rollback runbook: precondition — 2.0.0 is active", active_version(root) == "2.0.0")

    # 1. `enginetree.py --print pairs` — what would a rollback do?
    rep = pairs(root)
    check("rollback step 1: `--print pairs` says the target is 1.0.0",
          rep.get("rollback_to") == "1.0.0" and rep.get("active") == "2.0.0", json.dumps(rep)[:200])

    # 2. `enginetree.py --rollback` — do it.
    r = et(root, "--rollback", "--json")
    check("rollback step 2: `--rollback` exits 0", r.returncode == EXIT_OK, r.stdout + r.stderr)
    res = json.loads(r.stdout) if r.returncode == 0 else {}
    check("rollback step 2: ...and reports the switch", res.get("result") == "rolled-back"
          and res.get("version") == "1.0.0", json.dumps(res)[:200])

    # 3. the assertion — the OLD pair is what runs now, and it runs.
    check("rollback step 3: `current` points at 1.0.0", active_version(root) == "1.0.0")
    ok, why = runnable(root, vault, cfg)
    check("rollback step 3: the rolled-back pair is fully runnable in both modes", ok, why)

    # 4. and forward again, because a rollback that strands you is not a rollback.
    r = et(root, "--rollback", "--json")
    check("rollback step 4: rolling back again returns to 2.0.0",
          r.returncode == EXIT_OK and active_version(root) == "2.0.0", r.stdout + r.stderr)
    ok, why = runnable(root, vault, cfg)
    check("rollback step 4: ...and that pair runs too", ok, why)

    # 5. with nothing retained, it refuses instead of guessing.
    root2 = tmp / "rb-engine-2"
    et(root2, "--install", str(src), "--version", "1.0.0")
    r = et(root2, "--rollback")
    check("rollback: with no previous pair it REFUSES (exit 4) rather than picking one",
          r.returncode == EXIT_NOT_FOUND, f"rc={r.returncode} {(r.stdout + r.stderr)[:200]}")


def case_prune_never_takes_what_you_need(tmp: Path) -> None:
    root = tmp / "prune-engine"
    vault, cfg = scratch_vault(tmp, "prune")
    src = slim_source(tmp, "prune")
    et(root, "--install", str(src), "--version", "1.0.0")
    for v in ("2.0.0", "3.0.0", "4.0.0"):
        et(root, "--update", str(src), "--version", v)
    listed = sorted(p.name for p in (root / "engine").iterdir()
                    if p.is_dir() and not p.is_symlink() and not p.name.startswith("."))
    check("prune: an update keeps exactly the active pair and the rollback target",
          listed == ["3.0.0", "4.0.0"], str(listed))
    check("prune: ...the active one being the newest", active_version(root) == "4.0.0")
    ok, why = runnable(root, vault, cfg)
    check("prune: the surviving active pair runs", ok, why)
    r = et(root, "--rollback")
    check("prune: ...and the surviving previous pair is the one a rollback reaches",
          r.returncode == EXIT_OK and active_version(root) == "3.0.0", r.stdout + r.stderr)
    ok, why = runnable(root, vault, cfg)
    check("prune: ...and it runs", ok, why)
    # `--keep 1` must not be able to opt out of retaining the previous pair.
    et(root, "--rollback")
    et(root, "--update", str(src), "--version", "5.0.0", "--keep", "1")
    listed = sorted(p.name for p in (root / "engine").iterdir()
                    if p.is_dir() and not p.is_symlink() and not p.name.startswith("."))
    check("prune: `--keep 1` still retains the previous pair (the contract is not a flag)",
          len(listed) == 2 and "5.0.0" in listed, str(listed))

    # THE ROLLBACK TARGET IS PROTECTED BY NAME, not by luck. Every sequence above happens to leave
    # the rollback target as the NEWEST non-active version, and prune drops oldest-first — so with
    # `rollback_target()` removed from the protected set the cells above stayed green (measured, by
    # call-site mutation). This one makes the target the OLDEST version on disk, which is the only
    # arrangement in which the two policies disagree.
    root2 = tmp / "prune-engine-2"
    vault2, cfg2 = scratch_vault(tmp, "prune2")
    et(root2, "--install", str(src), "--version", "1.0.0")
    for v in ("7.0.0", "8.0.0"):                      # newer trees, deliberately not activated
        et(root2, "--install", str(src), "--version", v, "--no-activate")
    et(root2, "--update", str(src), "--version", "9.0.0")
    surviving = sorted(p.name for p in (root2 / "engine").iterdir()
                       if p.is_dir() and not p.is_symlink() and not p.name.startswith("."))
    check("prune: the rollback target survives even as the OLDEST version on disk",
          surviving == ["1.0.0", "9.0.0"], str(surviving))
    r = et(root2, "--rollback")
    check("prune: ...and rolling back to it really works", r.returncode == EXIT_OK
          and active_version(root2) == "1.0.0", (r.stdout + r.stderr)[:200])
    check("prune: ...and the pair it lands on is fully runnable", runnable(root2, vault2, cfg2)[0])


# --------------------------------------------------------------------------------------------
# C. The gates: checksum, self-test, serialization.
# --------------------------------------------------------------------------------------------
def case_checksum_gate(tmp: Path) -> None:
    root = tmp / "sum-engine"
    vault, cfg = scratch_vault(tmp, "sum")
    src = with_core(slim_source(tmp, "sum"))
    et(root, "--install", str(src), "--version", "1.0.0")

    r = et(root, "--digests", str(src))
    check("checksum: `--digests` produces a manifest of the pair", r.returncode == EXIT_OK
          and len(json.loads(r.stdout)["files"]) > 100, r.stderr[:200])
    manifest = json.loads(r.stdout)
    check("checksum: ...and it covers the compiled core, which verify() never asks about",
          any(k.endswith("plainkeep-core") for k in manifest["files"]))
    good = tmp / "good-manifest.json"
    good.write_text(json.dumps(manifest), encoding="utf-8")
    bad = tmp / "bad-manifest.json"
    files = dict(manifest["files"])
    files["bin/lib/guardrail.py"] = "0" * 64
    bad.write_text(json.dumps({"files": files}), encoding="utf-8")

    r = et(root, "--update", str(src), "--version", "2.0.0", "--expect", str(bad))
    check("checksum: a source that disagrees with the recorded manifest is REFUSED (exit 5)",
          r.returncode == EXIT_DENY and "checksum mismatch" in (r.stdout + r.stderr),
          f"rc={r.returncode} {(r.stdout + r.stderr)[:250]}")
    check("checksum: ...and the refusal names the file, not the network",
          "guardrail.py" in (r.stdout + r.stderr))
    check("checksum: ...and nothing was activated", active_version(root) == "1.0.0")
    check("checksum: ...and no half-installed 2.0.0 was left behind",
          not (root / "engine" / "2.0.0").exists())
    ok, why = runnable(root, vault, cfg)
    check("checksum: ...and the running pair is untouched and fully runnable", ok, why)

    r = et(root, "--update", str(src), "--version", "2.0.0", "--expect", str(good))
    check("checksum: the SAME update with the true manifest succeeds",
          r.returncode == EXIT_OK and active_version(root) == "2.0.0", (r.stdout + r.stderr)[:250])
    rec = json.loads((root / "engine" / ".pairs" / "2.0.0.json").read_text(encoding="utf-8"))
    check("checksum: the pair manifest is recorded OUTSIDE the sealed tree it covers",
          rec["files"] == manifest["files"] and rec["core"] is True,
          str((root / "engine" / ".pairs" / "2.0.0.json")))

    # THE OTHER COMPARISON, and the one `--expect` does not reach. `--expect` checks the SOURCE
    # against a record; this checks the STAGED TREE against the source it was copied from, which is
    # what catches a copy that did not survive. Call-site mutation found this gap: with
    # `digest_problems(dst, source_digests)` replaced by `[]`, every cell above still passed, because
    # every one of them exercised the source comparison instead. A gate that never reaches the
    # failing region is a green test of nothing (ADR-015, ADR-019 D2).
    #
    # The state is reached the way it is reached in life: an update killed after the tree landed and
    # was sealed, something modified the tree in the meantime, and the re-run picks the tree up
    # again (`reused`) rather than re-copying it.
    r = et(root, "--update", str(src), "--version", "3.0.0", PLAINKEEP_ENGINE_KILL_AT="checksum")
    staged = root / "engine" / "3.0.0"
    check("checksum: fixture — a killed update left a complete, sealed, UNACTIVATED tree",
          r.returncode == SIGKILL_RC and staged.is_dir() and active_version(root) == "2.0.0",
          f"rc={r.returncode} active={active_version(root)}")
    victim = staged / "bin" / "lib" / "guardrail.py"
    mode = victim.stat().st_mode
    victim.chmod(0o644)
    victim.write_text(victim.read_text(encoding="utf-8") + "\n# TAMPERED\n", encoding="utf-8")
    victim.chmod(mode)
    check("checksum: ...and the tampered tree still passes verify() — modes are not contents",
          et(root, "--verify", str(staged)).returncode == EXIT_OK)
    r = et(root, "--update", str(src), "--version", "3.0.0")
    check("checksum: the re-run REFUSES a staged tree that no longer matches its source (exit 5)",
          r.returncode == EXIT_DENY and "does not match its source" in (r.stdout + r.stderr),
          f"rc={r.returncode} {(r.stdout + r.stderr)[:250]}")
    check("checksum: ...and the refusal names the tampered file",
          "guardrail.py" in (r.stdout + r.stderr), (r.stdout + r.stderr)[:250])
    check("checksum: ...and the tampered tree was removed, never activated",
          active_version(root) == "2.0.0" and not staged.exists())
    ok, why = runnable(root, vault, cfg)
    check("checksum: ...and the running pair is still fully runnable", ok, why)


def case_selftest_gate(tmp: Path) -> None:
    """A tree can be COMPLETE and still not run. `verify()` answers presence; only a dispatch
    answers works — which is why the self-test exists and why it runs before the pointer moves."""
    root = tmp / "st-engine"
    vault, cfg = scratch_vault(tmp, "st")
    good = with_core(slim_source(tmp, "st"))
    et(root, "--install", str(good), "--version", "1.0.0")

    def break_launcher(s: Path) -> None:
        (s / "plainkeep").write_text("#!/usr/bin/env bash\nexit 9\n", encoding="utf-8")
        (s / "plainkeep").chmod(0o755)

    broken = with_core(slim_source(tmp, "st-broken", mutate=break_launcher))
    v = et(root, "--verify", str(broken))
    check("self-test: the broken source still passes the COMPLETENESS check "
          "(which is the point)", v.returncode == EXIT_OK, v.stdout + v.stderr)
    r = et(root, "--update", str(broken), "--version", "2.0.0")
    check("self-test: a pair that does not dispatch is REFUSED (exit 5)",
          r.returncode == EXIT_DENY and "self-test" in (r.stdout + r.stderr),
          f"rc={r.returncode} {(r.stdout + r.stderr)[:250]}")
    check("self-test: ...and nothing was activated", active_version(root) == "1.0.0")
    check("self-test: ...and the failed version was removed, not left as debris",
          not (root / "engine" / "2.0.0").exists())
    ok, why = runnable(root, vault, cfg)
    check("self-test: ...and the running pair is fully runnable", ok, why)

    # THE PAIR half: a truncated core dispatches fine on the floor and dies under
    # PLAINKEEP_CORE=require. Nothing that inspects the TREE can see this.
    core = REPO / ".local" / "bin" / "plainkeep-core"
    if core.is_file():
        def truncate_core(s: Path) -> None:
            d = s / ".local" / "bin" / "plainkeep-core"
            d.parent.mkdir(parents=True, exist_ok=True)
            d.write_bytes(core.read_bytes()[:4_000_000])
            d.chmod(0o755)

        halfcore = slim_source(tmp, "st-core", mutate=truncate_core)
        r = et(root, "--update", str(halfcore), "--version", "3.0.0")
        check("self-test: a pair whose CORE is truncated is refused, though the tree is complete",
              r.returncode == EXIT_DENY and "PLAINKEEP_CORE=require" in (r.stdout + r.stderr),
              f"rc={r.returncode} {(r.stdout + r.stderr)[:250]}")
        check("self-test: ...and the running pair still runs", runnable(root, vault, cfg)[0])
    else:
        notes.append("no compiled core in this checkout (cli/: `bun run build`) — the "
                     "truncated-core cell was SKIPPED, so nothing here shows the `require`-mode "
                     "leg of the pair self-test firing.")

    # And the self-test does not touch anything of the operator's: it runs against its own
    # throwaway vault and its own throwaway registry.
    before = sorted(p.name for p in vault.rglob("*"))
    et(root, "--update", str(good), "--version", "4.0.0")
    check("self-test: it dispatched against ITS OWN throwaway vault, not the one in scope",
          sorted(p.name for p in vault.rglob("*")) == before, "the scratch vault changed")


def case_updates_are_serialized(tmp: Path) -> None:
    root = tmp / "lock-engine"
    vault, cfg = scratch_vault(tmp, "lock")
    src = with_core(slim_source(tmp, "lock"))
    et(root, "--install", str(src), "--version", "1.0.0")
    env = _clean_env(PLAINKEEP_ENGINE_HOME=root)
    procs = [subprocess.Popen([PY, str(ENGINETREE), "--update", str(src), "--version", v],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
             for v in ("2.0.0", "3.0.0")]
    outs = [p.communicate() for p in procs]
    rcs = [p.returncode for p in procs]
    check("serialization: exactly ONE of two concurrent updates wins",
          rcs.count(EXIT_OK) == 1, f"rcs={rcs} {[o[1][-160:] for o in outs]}")
    losers = [o for rc, o in zip(rcs, outs) if rc != EXIT_OK]
    check("serialization: the loser refuses with the lock named, not a traceback",
          all("another plainkeep update is running" in (o[0] + o[1]) and "Traceback" not in (o[0] + o[1])
              for o in losers), str([o[1][-200:] for o in losers]))
    check("serialization: the winner's activation is intact",
          active_version(root) in ("2.0.0", "3.0.0"), str(active_version(root)))
    ok, why = runnable(root, vault, cfg)
    check("serialization: ...and what is active is fully runnable", ok, why)
    lost = {"2.0.0", "3.0.0"} - {active_version(root)}
    check("serialization: the loser left no half-installed version behind",
          all(not (root / "engine" / v).exists() for v in lost), str(lost))

    # THE LOCK IS KILL-SAFE. This is the property an O_EXCL sentinel would not have, and it is what
    # makes every kill in the matrix below re-runnable at all.
    k = subprocess.run([PY, str(ENGINETREE), "--update", str(src), "--version", "9.9.9"],
                       capture_output=True, text=True,
                       env=_clean_env(PLAINKEEP_ENGINE_HOME=root,
                                      PLAINKEEP_ENGINE_KILL_AT="provision-staged"))
    check("serialization: a killed update leaves no lock a later one has to be told to break",
          k.returncode == SIGKILL_RC
          and et(root, "--print", "pairs").returncode == EXIT_OK
          and et(root, "--update", str(src), "--version", "9.9.9").returncode == EXIT_OK,
          f"kill rc={k.returncode}")


# --------------------------------------------------------------------------------------------
# D. THE KILL MATRIX — the point of this task.
# --------------------------------------------------------------------------------------------
def _converges(root: Path, src: Path, version: str, vault: Path, cfg: Path,
               label: str) -> None:
    """Re-run the same update twice: the first must finish the job, the second must be a no-op."""
    r1 = et(root, "--update", str(src), "--version", version, "--json")
    ok1 = r1.returncode == EXIT_OK
    check(f"{label}: re-running the same command converges", ok1,
          f"rc={r1.returncode} {(r1.stdout + r1.stderr)[-250:]}")
    if ok1:
        check(f"{label}: ...to the new pair, active and fully runnable",
              active_version(root) == version and runnable(root, vault, cfg)[0],
              f"active={active_version(root)} {runnable(root, vault, cfg)[1]}")
    r2 = et(root, "--update", str(src), "--version", version, "--json")
    res = json.loads(r2.stdout) if r2.returncode == EXIT_OK else {}
    check(f"{label}: ...and a THIRD run is a no-op",
          r2.returncode == EXIT_OK and res.get("result") == "already-active",
          f"rc={r2.returncode} {(r2.stdout + r2.stderr)[-200:]}")


def case_kill_matrix(tmp: Path) -> None:
    """SIGKILL at every declared boundary. After each: SOMETHING runs, and a re-run converges."""
    src = with_core(slim_source(tmp, "kill"))

    # Every stage except the replace window, which needs a pre-existing target tree and is set up
    # separately below.
    stages = [s for s in _kill_stages() if s != "provision-replace-window"]
    for stage in stages:
        root = tmp / f"kill-{stage}"
        vault, cfg = scratch_vault(tmp, f"kill-{stage}")
        et(root, "--install", str(src), "--version", "1.0.0")
        if stage == "cleanup":
            # The prune only has something to remove once three versions exist.
            et(root, "--update", str(src), "--version", "2.0.0")
            target, before_active = "3.0.0", "2.0.0"
        else:
            target, before_active = "2.0.0", "1.0.0"

        r = et(root, "--update", str(src), "--version", target,
               PLAINKEEP_ENGINE_KILL_AT=stage)
        check(f"kill@{stage}: the update really was killed there",
              r.returncode == SIGKILL_RC and stage in r.stderr,
              f"rc={r.returncode} {r.stderr[-200:]}")

        # THE INVARIANT. Not "the files are there" — a real verb, in both dispatcher modes.
        active = active_version(root)
        ok, why = runnable(root, vault, cfg)
        check(f"kill@{stage}: a pair is ACTIVE and fully runnable (now {active})", ok,
              f"active={active} {why}")
        check(f"kill@{stage}: ...and it is one of the two pairs, never a third thing",
              active in (before_active, target), str(active))
        _converges(root, src, target, vault, cfg, f"kill@{stage}")

    # THE REPLACE WINDOW, on the update path. It is reached only when the target version already
    # has a tree — which happens after an earlier kill left an incomplete one. The window is the
    # module's own documented residue, and the reason it is HARMLESS here is structural: the target
    # is never the running version, so what the window exposes is a tree nothing is running.
    stage = "provision-replace-window"
    root = tmp / f"kill-{stage}"
    vault, cfg = scratch_vault(tmp, f"kill-{stage}")
    et(root, "--install", str(src), "--version", "1.0.0")
    et(root, "--update", str(src), "--version", "2.0.0")
    et(root, "--rollback")                       # 1.0.0 active again, 2.0.0 present but idle
    check(f"kill@{stage}: fixture — the target version exists and is NOT the running one",
          (root / "engine" / "2.0.0").is_dir() and active_version(root) == "1.0.0")
    unlock(root / "engine" / "2.0.0")
    (root / "engine" / "2.0.0" / "VERSION").unlink()          # make it fail verify() → re-staged
    r = et(root, "--update", str(src), "--version", "2.0.0", PLAINKEEP_ENGINE_KILL_AT=stage)
    check(f"kill@{stage}: killed between remove_version() and the rename",
          r.returncode == SIGKILL_RC and stage in r.stderr, f"rc={r.returncode} {r.stderr[-200:]}")
    check(f"kill@{stage}: the target tree is GONE — the window is real, not theoretical",
          not (root / "engine" / "2.0.0").exists())
    ok, why = runnable(root, vault, cfg)
    check(f"kill@{stage}: ...and the RUNNING pair is untouched and fully runnable", ok,
          f"active={active_version(root)} {why}")
    _converges(root, src, "2.0.0", vault, cfg, f"kill@{stage}")


def _kill_stages() -> tuple[str, ...]:
    r = subprocess.run([PY, "-c",
                        "import sys;sys.path.insert(0,sys.argv[1]);import enginetree;"
                        "print('\\n'.join(enginetree.KILL_STAGES))", str(REPO / "bin" / "lib")],
                       capture_output=True, text=True, env=_clean_env())
    return tuple(x for x in r.stdout.split() if x)


def case_the_kill_hook_is_honest(tmp: Path) -> None:
    """The injection hook is itself a gate, and ADR-019's recursion applies to it: a boundary
    nothing reaches is a green cell that measured nothing, and a hook that could let a run SUCCEED
    would be testing something other than the product."""
    root = tmp / "hook-engine"
    src = slim_source(tmp, "hook")
    et(root, "--install", str(src), "--version", "1.0.0")
    reached, survived = [], []
    for stage in _kill_stages():
        if stage in ("provision-replace-window", "cleanup"):
            continue                       # both need a fixture the matrix above builds for them
        r = et(root, "--update", str(src), "--version", "2.0.0",
               PLAINKEEP_ENGINE_KILL_AT=stage)
        if r.returncode == SIGKILL_RC:
            reached.append(stage)
        if r.returncode == EXIT_OK:
            survived.append(stage)
        # Undo, so the next stage starts from the same state.
        if (root / "engine" / "2.0.0").exists():
            unlock(root / "engine" / "2.0.0")
            shutil.rmtree(root / "engine" / "2.0.0", ignore_errors=True)
        et(root, "--activate", "1.0.0")
    expect = [s for s in _kill_stages() if s not in ("provision-replace-window", "cleanup")]
    check("kill hook: every declared boundary is actually REACHED by a real update",
          reached == expect, f"reached={reached} expected={expect}")
    check("kill hook: no value of the variable lets an update exit 0", not survived, str(survived))
    r = et(root, "--update", str(src), "--version", "2.0.0",
           PLAINKEEP_ENGINE_KILL_AT="not-a-real-boundary")
    check("kill hook: a MISSPELLED stage refuses instead of injecting nothing and passing",
          r.returncode == EXIT_USAGE and "names no boundary" in (r.stdout + r.stderr),
          f"rc={r.returncode} {(r.stdout + r.stderr)[:200]}")


def case_the_open_residue(tmp: Path) -> None:
    """THE WINDOW THAT IS STILL OPEN, measured rather than described.

    `install(--force)` over the ACTIVE version does `remove_version()` then `os.rename`, and a kill
    between them leaves no engine under that name and a dangling `current`. `update()` cannot reach
    that state (it refuses the running version as a target), but `script/setup` runs
    `--install --force` unconditionally, so the exposure is real on the install path and this is
    what it costs and how it is recovered."""
    root = tmp / "residue-engine"
    vault, cfg = scratch_vault(tmp, "residue")
    src = slim_source(tmp, "residue")
    et(root, "--install", str(src), "--version", "1.0.0")
    check("residue: fixture — 1.0.0 is active and runnable", runnable(root, vault, cfg)[0])

    r = et(root, "--install", str(src), "--version", "1.0.0", "--force",
           PLAINKEEP_ENGINE_KILL_AT="provision-replace-window")
    check("residue: `--install --force` over the ACTIVE version, killed in the window",
          r.returncode == SIGKILL_RC, f"rc={r.returncode} {r.stderr[-160:]}")
    ok, why = runnable(root, vault, cfg)
    check("residue: THE WINDOW IS OPEN — there is now no runnable engine (this is the honest "
          "measurement, not a failure of the suite)", not ok, why)
    check("residue: ...and `current` is left dangling", (root / "engine" / "current").is_symlink()
          and not (root / "engine" / "current").resolve().is_dir())
    d = et(root, "--print", "current")
    check("residue: ...which the diagnostic REPORTS rather than printing a path that is not there",
          d.returncode != EXIT_OK, f"rc={d.returncode} {d.stdout.strip()}")

    # The recovery, run rather than described.
    r = et(root, "--install", str(src), "--version", "1.0.0")
    check("residue recovery: a plain `--install` (no --force) restores it", r.returncode == EXIT_OK,
          (r.stdout + r.stderr)[:200])
    ok, why = runnable(root, vault, cfg)
    check("residue recovery: ...and the engine is fully runnable again", ok, why)

    # And the reason `update` cannot get here: it refuses the running version as a target.
    root2 = tmp / "residue-engine-2"
    et(root2, "--install", str(src), "--version", "1.0.0")
    unlock(root2 / "engine" / "1.0.0")
    (root2 / "engine" / "1.0.0" / "VERSION").unlink()      # active AND broken — the worst input
    r = et(root2, "--update", str(src), "--version", "1.0.0")
    check("residue: `update` REFUSES to target the running version even when it is broken",
          r.returncode == EXIT_DENY and "running engine" in (r.stdout + r.stderr),
          f"rc={r.returncode} {(r.stdout + r.stderr)[:250]}")
    check("residue: ...and says how to get out (rollback, or a repairing re-install)",
          "--rollback" in (r.stdout + r.stderr) and "--install" in (r.stdout + r.stderr))


# --------------------------------------------------------------------------------------------
# E. WHEN DOCTOR MAY MUTATE — the policy, enforced end to end and at the call site.
# --------------------------------------------------------------------------------------------
def _snapshot(root: Path, *, skip=()) -> dict:
    """Name → (size, mtime_ns, mode) for everything under `root`. A listing alone would miss an
    in-place rewrite that keeps the length."""
    out = {}
    for p in sorted(root.rglob("*")):
        rel = str(p.relative_to(root))
        if any(rel == s or rel.startswith(s + os.sep) for s in skip):
            continue
        try:
            st = p.lstat()
            out[rel] = (st.st_size, st.st_mtime_ns, st.st_mode)
        except OSError:
            out[rel] = ("gone",)
    return out


def case_doctor_never_mutates(tmp: Path) -> None:
    root = tmp / "doc-engine"
    src = with_core(slim_source(tmp, "doc"))
    et(root, "--install", str(src), "--version", "1.0.0")
    et(root, "--update", str(src), "--version", "2.0.0")
    vault, cfg = scratch_vault(tmp, "doc")
    # A vault MISSING part of its skeleton — the state `--init` exists to fix, and therefore the
    # state in which a self-healing doctor would heal.
    for rel in ("wiki", "journal", "inbox", "jobs", "templates"):
        (vault / rel).mkdir(parents=True, exist_ok=True)
    (vault / "AGENTS.md").write_text("# scratch\n", encoding="utf-8")
    (vault / "CLAUDE.md").write_text("@AGENTS.md\n", encoding="utf-8")

    # `.logs/` is excluded and the reason is not a convenience: the GUARDRAIL appends one audit line
    # per dispatch, before the verb starts. That is the dispatcher writing, not doctor, and it
    # happens for `plainkeep help` just the same. Everything else in the vault is in scope.
    for mode in MODES:
        before_v = _snapshot(vault, skip=(".logs",))
        before_e = _snapshot(root)
        r = dispatch(root, vault, cfg, "doctor", mode=mode)
        after_v = _snapshot(vault, skip=(".logs",))
        after_e = _snapshot(root)
        check(f"doctor: a plain run changes NOTHING in the vault (PLAINKEEP_CORE={mode})",
              before_v == after_v,
              str(sorted(set(before_v) ^ set(after_v))[:6] or
                  [k for k in before_v if before_v[k] != after_v.get(k)][:6]))
        check(f"doctor: ...and nothing in the engine install root (PLAINKEEP_CORE={mode})",
              before_e == after_e,
              str(sorted(set(before_e) ^ set(after_e))[:6] or
                  [k for k in before_e if before_e[k] != after_e.get(k)][:6]))
        check(f"doctor: ...and says so, on the record (PLAINKEEP_CORE={mode})",
              "read-only" in r.stdout, r.stdout[-200:])
        check(f"doctor: ...and still REPORTS the missing structure (PLAINKEEP_CORE={mode})",
              "MISSING" in r.stdout and r.returncode != EXIT_OK,
              f"rc={r.returncode} " + r.stdout[-200:])

    # ...and `--init` is consent, so it writes — but only the two things the policy names, and
    # only inside the vault.
    before_e = _snapshot(root)
    r = dispatch(root, vault, cfg, "doctor", "--init")
    after_e = _snapshot(root)
    check("doctor --init: creates the missing skeleton", (vault / "tasks" / "inbox").is_dir()
          and r.returncode == EXIT_OK, f"rc={r.returncode} " + r.stdout[-200:])
    check("doctor --init: ...and STILL does not touch the engine install root",
          before_e == after_e,
          str([k for k in before_e if before_e[k] != after_e.get(k)][:6]))
    check("doctor --init: ...and says which consent it was given",
          "--init given" in r.stdout, r.stdout[-200:])

    # THE CALL-SITE RATCHET (ADR-019 D2/D3). Mutating the rule's body proves the rule's own tests
    # work; what catches an unwired rule is mutating the CALL SITE. Here the equivalent is
    # structural: a new mutating call in doctor's `main()` that is not under an `--init` branch is
    # the defect, and it is asked of the PARSE TREE per statement, never of the file's text — a
    # substring search for "init" is satisfied by this file's own comments (ADR-019 D3, and
    # instance 4 is the whole argument).
    bad = _unguarded_mutations(REPO / "bin" / "doctor" / "run.py")
    check("doctor: no mutating call in main() sits outside an --init-guarded branch (AST)",
          not bad, "; ".join(bad[:6]))
    # ...and the ratchet is not vacuous: neutered, the same reader finds the real call sites.
    found = _unguarded_mutations(REPO / "bin" / "doctor" / "run.py", guard_names=())
    check("doctor: ...and that ratchet FINDS them when the guard is not counted (it is not vacuous)",
          len(found) >= 3, str(found[:4]))


# The calls that CHANGE something. Deliberately a small, named set rather than "anything that looks
# like a write": a list that tried to be exhaustive would be a list nobody could reason about, and
# the question here is narrow — does doctor's main() reach a mutation outside its consent branch.
_MUTATORS = {"mkdir", "touch", "write_text", "write_bytes", "unlink", "rmdir", "rename",
             "copytree", "copy2", "rmtree", "move", "run", "Popen", "check_call", "check_output",
             "append_text", "symlink_to"}
_GUARD_NAMES = ("init",)


def _unguarded_mutations(f: Path, guard_names=_GUARD_NAMES) -> list[str]:
    """Mutating calls inside `main()` that are not inside a branch testing a guard name."""
    tree = ast.parse(f.read_text(encoding="utf-8"))
    main = next((n for n in tree.body
                 if isinstance(n, ast.FunctionDef) and n.name == "main"), None)
    if main is None:
        return ["main() not found"]
    out: list[str] = []

    def names_in(node) -> set[str]:
        return {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}

    def visit(node, guarded: bool) -> None:
        """Dispatches on THIS node, then recurses. The first version iterated a node's CHILDREN and
        `continue`d on an `If`, which meant an `elif` — represented as an `If` inside the outer
        node's `orelse`, and therefore handed to the recursion as a bare node — had its own test
        never read: all four of doctor's real `--init` sites came back as violations. Left as a note
        because it is the same shape of mistake ADR-019 D3 warns about: a ratchet that asks the
        wrong question of the tree fails in whichever direction its author does not check."""
        if isinstance(node, ast.If):
            # Only the BODY is guarded by a positive test; `else:` is the unguarded half.
            inner = guarded or bool(names_in(node.test) & set(guard_names))
            visit(node.test, guarded)
            for stmt in node.body:
                visit(stmt, inner)
            for stmt in node.orelse:
                visit(stmt, guarded)
            return
        if isinstance(node, ast.Call):
            fn = node.func
            nm = fn.attr if isinstance(fn, ast.Attribute) else (
                fn.id if isinstance(fn, ast.Name) else "")
            if nm in _MUTATORS and not guarded:
                out.append(f"{nm}() at line {node.lineno}")
        for child in ast.iter_child_nodes(node):
            visit(child, guarded)

    for stmt in main.body:
        visit(stmt, False)
    return out


# --------------------------------------------------------------------------------------------
def main() -> int:
    started = time.time()
    with tempfile.TemporaryDirectory(prefix="pk-engineupdate-") as td:
        tmp = Path(os.path.realpath(td))
        try:
            case_init(tmp)
            case_init_refusals(tmp)
            case_update_retains_the_previous_pair(tmp)
            case_rollback_is_a_tested_command_sequence(tmp)
            case_prune_never_takes_what_you_need(tmp)
            case_checksum_gate(tmp)
            case_selftest_gate(tmp)
            case_updates_are_serialized(tmp)
            case_the_kill_hook_is_honest(tmp)
            case_kill_matrix(tmp)
            case_the_open_residue(tmp)
            case_doctor_never_mutates(tmp)
        finally:
            # Every installed tree is sealed 0555, which TemporaryDirectory cannot remove.
            for p in tmp.rglob("*"):
                try:
                    if p.is_dir() and not p.is_symlink():
                        p.chmod(0o755)
                except OSError:
                    pass

    check("parity: the runnability proofs really ran in BOTH dispatcher modes",
          _modes_used == set(MODES), f"modes actually exercised: {sorted(_modes_used)}")

    print(f"{BOLD}engine update + vault init: failure injection (Phase 2 Task 5) — "
          f"{len(results)} checks{RESET}\n")
    passed = sum(1 for _, ok, _ in results if ok)
    for name, ok, detail in results:
        mark = f"{GREEN}PASS{RESET}" if ok else f"{RED}FAIL{RESET}"
        print(f"  {mark} {name:<86}" + (f" {DIM}{detail.strip()[:130]}{RESET}"
                                        if (detail and not ok) else ""))
    failed = len(results) - passed
    print(f"\nSUITE-NOTE: every install here goes to a temp PLAINKEEP_ENGINE_HOME and every dispatch "
          f"to a throwaway marked vault with a throwaway PLAINKEEP_CONFIG_HOME. The developer's "
          f"engine, registry and notes are neither read nor written, and this suite therefore says "
          f"nothing about whether THAT install is intact — `plainkeep doctor` asks that.")
    print(f"SUITE-NOTE: `case_the_open_residue` asserts a window is OPEN. Its green cells are a "
          f"measurement of a known exposure on the `--install --force` path, not a proof that the "
          f"exposure is gone; the update path cannot reach it, and that is what the matrix shows.")
    print(f"SUITE-NOTE: PLAINKEEP_REQUIRE_CORE and PLAINKEEP_PARITY_FAULT_SIGNALS are never set "
          f"here. Both dispatcher modes are exercised with PLAINKEEP_CORE=off/require.")
    for n in notes:
        print(f"SUITE-NOTE: {n}")
    print(f"\n{BOLD}Result:{RESET} {GREEN}{passed} passed{RESET}, "
          f"{(RED if failed else DIM)}{failed} failed{RESET}, {len(results)} checks "
          f"{DIM}({time.time() - started:.0f}s){RESET}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
