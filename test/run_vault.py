#!/usr/bin/env python3
"""
run_vault.py — the vault MARKER + REGISTRY contract (ADR-014, Phase 2 Task 1a).

Task 1a ships identity, not discovery: a vault gets an immutable id in `<vault>/.plainkeep/vault.json`
and a name in a registry outside every vault, and `plainkeep vault` is the only thing that writes
either. This suite gates that contract and, just as importantly, gates the claim that **discovery is
unchanged** — if Task 1a moved how a root gets selected, it would not be shippable alone.

Everything here fails closed, so most of these cases are refusals: unknown schema, malformed JSON, a
duplicate id/name/path, a `default` pointing at nothing, a rebind onto a different vault's marker.

Offline, stdlib only.
"""
from __future__ import annotations
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from lib.hermetic import seal
seal()   # hermetic: an empty throwaway registry, never the developer's real vault

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib import vaultfx  # noqa: E402
GREEN, RED, DIM, BOLD, RESET = "\033[32m", "\033[31m", "\033[2m", "\033[1m", "\033[0m"
results: list[tuple[str, bool, str]] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    results.append((name, bool(cond), detail))


def vault(home: Path, cfg: Path, *args, cwd: Path | None = None, **env_extra):
    env = {**os.environ, "PLAINKEEP_HOME": str(home), "PLAINKEEP_CONFIG_HOME": str(cfg), **env_extra}
    return subprocess.run([sys.executable, str(REPO / "bin" / "vault" / "run.py"), *args],
                          capture_output=True, text=True, env=env,
                          cwd=None if cwd is None else str(cwd))


def load_vaultreg():
    spec = importlib.util.spec_from_file_location("vaultreg_t", REPO / "bin" / "lib" / "vaultreg.py")
    m = importlib.util.module_from_spec(spec)
    sys.modules["vaultreg_t"] = m
    spec.loader.exec_module(m)
    return m


# --------------------------------------------------------------------------------------------
# A. The round trip: register -> list -> default -> rebind -> deregister
# --------------------------------------------------------------------------------------------
def case_roundtrip() -> None:
    with tempfile.TemporaryDirectory() as td:
        t = Path(td)
        cfg, a, b = t / "cfg", t / "a", t / "b"
        a.mkdir(); b.mkdir()

        r = vault(a, cfg, "register", str(a), "--name", "alpha")
        check("register without --yes refuses with EXIT_CONFIRM (3)", r.returncode == 3,
              f"rc={r.returncode} {r.stderr.strip()}")
        check("register without --yes wrote NOTHING",
              not (a / ".plainkeep").exists() and not cfg.exists())

        r = vault(a, cfg, "register", str(a), "--name", "alpha", "--yes")
        check("register --yes succeeds", r.returncode == 0, r.stdout + r.stderr)
        marker = a / ".plainkeep" / "vault.json"
        check("register writes the marker", marker.is_file())
        doc = json.loads(marker.read_text()) if marker.is_file() else {}
        check("marker carries schema + uuid id",
              doc.get("schema") == "plainkeep.vault/1" and len(doc.get("id", "")) == 36, str(doc))
        check("marker carries NO name (names live in the registry)", "name" not in doc, str(doc))
        reg = json.loads((cfg / "registry.json").read_text())
        check("registry stores the CANONICAL path",
              reg["vaults"][0]["path"] == os.path.realpath(a), reg["vaults"][0]["path"])
        check("first registered vault becomes the default",
              reg["default"] == doc["id"], str(reg["default"]))

        r = vault(a, cfg, "register", str(b), "--name", "beta", "--yes")
        check("a second vault registers", r.returncode == 0, r.stdout + r.stderr)
        reg = json.loads((cfg / "registry.json").read_text())
        check("registering a second vault does NOT steal the default", reg["default"] == doc["id"])

        r = vault(a, cfg, "list", "--json")
        rows = [json.loads(l) for l in r.stdout.strip().splitlines()]
        check("list emits a header plus one row per vault", len(rows) == 3, r.stdout)
        check("list marks exactly one default", sum(1 for x in rows[1:] if x.get("default")) == 1)

        r = vault(a, cfg, "default", "beta", "--yes")
        reg = json.loads((cfg / "registry.json").read_text())
        check("default <name> switches the default",
              r.returncode == 0 and reg["default"] != doc["id"], r.stdout + r.stderr)
        check("default is stored as an ID, never a name or path",
              reg["default"] == next(v["id"] for v in reg["vaults"] if v["name"] == "beta"))

        # a MOVED vault keeps its id — that is what makes this a rebind, not a re-registration
        moved = t / "a-moved"
        shutil.move(str(a), str(moved))
        r = vault(moved, cfg, "rebind", "alpha", str(moved), "--yes")
        reg = json.loads((cfg / "registry.json").read_text())
        entry = next(v for v in reg["vaults"] if v["name"] == "alpha")
        check("rebind re-points a moved vault",
              r.returncode == 0 and entry["path"] == os.path.realpath(moved), r.stdout + r.stderr)
        check("rebind keeps the id", entry["id"] == doc["id"])

        r = vault(moved, cfg, "deregister", "beta", "--yes")
        reg = json.loads((cfg / "registry.json").read_text())
        check("deregister removes the entry",
              r.returncode == 0 and len(reg["vaults"]) == 1, r.stdout + r.stderr)
        check("deregister leaves the vault's marker alone", (b / ".plainkeep" / "vault.json").is_file())
        check("deregistering the default clears it and does NOT auto-promote another",
              reg["default"] is None, str(reg["default"]))


# --------------------------------------------------------------------------------------------
# B. Fail-closed: duplicates, bad markers, bad registries, wrong-id rebind
# --------------------------------------------------------------------------------------------
def case_fail_closed() -> None:
    with tempfile.TemporaryDirectory() as td:
        t = Path(td)
        cfg, a, b = t / "cfg", t / "a", t / "b"
        a.mkdir(); b.mkdir()
        vault(a, cfg, "register", str(a), "--name", "alpha", "--yes")
        vault(a, cfg, "register", str(b), "--name", "beta", "--yes")

        r = vault(a, cfg, "register", str(a), "--yes")
        check("duplicate PATH refuses", r.returncode == 2, f"rc={r.returncode} {r.stderr.strip()}")

        c = t / "c"; c.mkdir()
        r = vault(a, cfg, "register", str(c), "--name", "alpha", "--yes")
        check("duplicate NAME refuses", r.returncode == 2, f"rc={r.returncode} {r.stderr.strip()}")
        check("a refused register leaves no marker behind", not (c / ".plainkeep").exists())

        # same vault copied to a new path = same id in the marker
        d = t / "d"
        shutil.copytree(a, d)
        r = vault(a, cfg, "register", str(d), "--name", "dupe", "--yes")
        check("duplicate ID refuses and names rebind as the fix",
              r.returncode == 2 and "rebind" in (r.stdout + r.stderr), r.stdout + r.stderr)

        r = vault(a, cfg, "rebind", "alpha", str(b), "--yes")
        check("rebind onto a DIFFERENT vault's marker refuses",
              r.returncode == 2 and "different vault" in (r.stdout + r.stderr), r.stdout + r.stderr)

        r = vault(a, cfg, "rebind", "alpha", str(t / "nomarker"), "--yes")
        check("rebind onto a nonexistent path refuses", r.returncode == 4, f"rc={r.returncode}")

        # a marker from a newer plainkeep must refuse, never be rewritten or ignored
        e = t / "e"; e.mkdir(); (e / ".plainkeep").mkdir()
        (e / ".plainkeep" / "vault.json").write_text('{"schema":"plainkeep.vault/2","id":"x"}')
        r = vault(e, cfg, "status")
        check("unknown marker schema is reported, not ignored",
              "unknown schema" in (r.stdout + r.stderr), r.stdout + r.stderr)
        (e / ".plainkeep" / "vault.json").write_text("{not json")
        r = vault(e, cfg, "register", str(e), "--yes")
        check("malformed marker refuses rather than being overwritten",
              r.returncode == 2 and (e / ".plainkeep" / "vault.json").read_text() == "{not json",
              r.stdout + r.stderr)

        # a corrupt registry must take every action down, loudly
        good = (cfg / "registry.json").read_text()
        (cfg / "registry.json").write_text("{oops")
        r = vault(a, cfg, "list")
        check("malformed registry refuses with the file named",
              r.returncode == 2 and "registry.json" in (r.stdout + r.stderr), r.stdout + r.stderr)
        bad = json.loads(good)
        bad["vaults"].append(dict(bad["vaults"][0]))
        (cfg / "registry.json").write_text(json.dumps(bad))
        r = vault(a, cfg, "list")
        check("duplicate entries in the registry refuse (never last-wins)",
              r.returncode == 2 and "duplicate" in (r.stdout + r.stderr), r.stdout + r.stderr)
        bad = json.loads(good)
        bad["default"] = "00000000-0000-0000-0000-000000000000"
        (cfg / "registry.json").write_text(json.dumps(bad))
        r = vault(a, cfg, "list")
        check("a default naming no registered vault refuses", r.returncode == 2, r.stdout + r.stderr)
        bad = json.loads(good)
        bad["schema"] = "plainkeep.registry/2"
        (cfg / "registry.json").write_text(json.dumps(bad))
        r = vault(a, cfg, "list")
        check("unknown registry schema refuses", r.returncode == 2, r.stdout + r.stderr)


# --------------------------------------------------------------------------------------------
# C. Atomicity — proven by breaking os.replace, not by hoping
# --------------------------------------------------------------------------------------------
def case_atomic() -> None:
    vr = load_vaultreg()
    with tempfile.TemporaryDirectory() as td:
        t = Path(td)
        # RESTORE, never pop (see the finally below). Popping removed the process-wide seal
        # `hermetic.seal()` established at import, and every later invocation in this file survived
        # only because each happens to set the variable explicitly — one that forgot would have
        # dispatched against the developer's real registry with nothing in the output to say so.
        sealed = os.environ.get("PLAINKEEP_CONFIG_HOME")
        os.environ["PLAINKEEP_CONFIG_HOME"] = str(t / "cfg")
        try:
            reg = vr.empty_registry()
            reg["vaults"].append({"id": "11111111-1111-1111-1111-111111111111",
                                  "name": "one", "path": "/tmp/one"})
            reg["default"] = reg["vaults"][0]["id"]
            vr.write_registry(reg)
            before = vr.registry_path().read_text()

            reg2 = json.loads(before)
            reg2["vaults"].append({"id": "22222222-2222-2222-2222-222222222222",
                                   "name": "two", "path": "/tmp/two"})
            real_replace = os.replace
            os.replace = lambda *a, **k: (_ for _ in ()).throw(OSError("simulated crash"))
            try:
                vr.write_registry(reg2)
                crashed = False
            except OSError:
                crashed = True
            finally:
                os.replace = real_replace
            check("an interrupted write raises rather than half-succeeding", crashed)
            check("the OLD registry survives an interrupted write, intact and valid",
                  vr.registry_path().read_text() == before)
            check("the interrupted write leaves no truncated registry",
                  len(vr.read_registry()["vaults"]) == 1)

            lock = vr.registry_path().with_suffix(".json.lock")
            lock.write_text("99999\n")
            try:
                vr.write_registry(reg2)
                locked = False
            except vr.VaultError as e:
                locked = "locked" in e.message
            check("a held lock refuses the write and is never force-broken",
                  locked and lock.is_file())
            lock.unlink()
        finally:
            if sealed is None:
                os.environ.pop("PLAINKEEP_CONFIG_HOME", None)
            else:
                os.environ["PLAINKEEP_CONFIG_HOME"] = sealed

    # The seal SURVIVES this case. It is asserted rather than assumed because the static gate in
    # run_all.py structurally cannot see it: that gate proves seal() is CALLED, never that the seal
    # is still HELD, and this case is the only place in the suite that ever moves the variable. It
    # used to pop it, permanently unsealing the process — every later invocation here survived only
    # because each happens to set PLAINKEEP_CONFIG_HOME explicitly.
    check("the hermetic seal survives a case that repoints PLAINKEEP_CONFIG_HOME",
          os.environ.get("PLAINKEEP_CONFIG_HOME") == seal(),
          f"{os.environ.get('PLAINKEEP_CONFIG_HOME')!r} != {seal()!r}")

    # ...and seal() can RE-establish it, which is the other half and needs its own assertion: the
    # check above passes as long as EITHER the finally restores or seal() re-asserts, so on its own
    # it would gate neither. seal() was memoized on the directory AND on the assignment, so once the
    # variable was gone nothing could put it back.
    held = seal()
    os.environ.pop("PLAINKEEP_CONFIG_HOME", None)
    check("seal() RE-ASSERTS the seal — a memo of the directory, never of the assignment",
          seal() == held and os.environ.get("PLAINKEEP_CONFIG_HOME") == held,
          f"after re-seal: {os.environ.get('PLAINKEEP_CONFIG_HOME')!r}, want {held!r}")
    os.environ["PLAINKEEP_CONFIG_HOME"] = held


# --------------------------------------------------------------------------------------------
# D. The template is not adoptable — and discovery HAS changed (Task 1b)
#
# This section used to assert the opposite. Its case read "discovery is UNCHANGED: PLAINKEEP_HOME
# still wins over a registered default", and it was the evidence that Task 1a was shippable ALONE:
# identity landed without moving how a root gets selected. Task 1b is the task that makes it false,
# so it is REWRITTEN here rather than deleted — the same invocation, asserting the new contract.
#
# What changed, precisely: PLAINKEEP_HOME still wins over the registry default (it is step 2 of four,
# the default is step 4), but it is no longer accepted UNVALIDATED. An unmarked root now refuses with
# exit 2 and writes nothing, where it used to capture happily into whatever directory it named. The
# full chain — every mechanism, every refusal, and the two-vault identity test — is gated in
# test/run_discovery.py; what belongs HERE is the one assertion this file has always owned: that
# Task 1a's marker/registry contract still behaves the way it says it does.
# --------------------------------------------------------------------------------------------
def case_template_and_discovery() -> None:
    check("the repo template carries no committed marker "
          "(a plain clone must not be adoptable as a vault)",
          subprocess.run(["git", "-C", str(REPO), "ls-files", "--error-unmatch",
                          ".plainkeep/vault.json"], capture_output=True).returncode != 0)
    check(".plainkeep/ is gitignored",
          subprocess.run(["git", "-C", str(REPO), "check-ignore", "-q", ".plainkeep/vault.json"],
                         capture_output=True).returncode == 0)

    with tempfile.TemporaryDirectory() as td:
        t = Path(td)
        cfg, a, b = t / "cfg", t / "a", t / "b"
        a.mkdir(); b.mkdir()
        vault(a, cfg, "register", str(a), "--name", "alpha", "--default", "--yes")

        # REWRITTEN BY TASK 1b (see the section header). `b` is an UNMARKED directory: before Task 1b
        # `PLAINKEEP_HOME` naming it was enough and `capture` wrote an inbox note there; now the same
        # invocation refuses. Asserted by FILESYSTEM WALK as well as exit code — this suite's own
        # subject is what gets written where, and an exit code has already been observed lying about
        # that once in this project (a refusal exited 5 having already written the file).
        #
        # Through the DISPATCHER, not `python3 bin/capture/run.py`: validation is the dispatcher's
        # job by design, and a directly-invoked verb still trusts whatever PLAINKEEP_HOME it is
        # handed (lib/vaultroot.active_root reads, it does not re-validate). That boundary is stated
        # in the Task 1b report as a known limit, and it is why this case had to change its
        # invocation shape as well as its expectation.
        r = subprocess.run([str(REPO / "plainkeep"), "capture", "unchanged"],
                           capture_output=True, text=True, cwd=str(b),
                           env={**os.environ, "PLAINKEEP_HOME": str(b), "PLAINKEEP_CORE": "off",
                                "PLAINKEEP_CONFIG_HOME": str(cfg)})
        check("discovery CHANGED (Task 1b): an unmarked PLAINKEEP_HOME is refused, not obeyed",
              r.returncode == 2, f"rc={r.returncode} {r.stdout}{r.stderr}")
        check("...and the refusal wrote NOTHING into the root it refused",
              not (b / "inbox").exists() and not (b / "journal").exists(),
              str(sorted(p.name for p in b.iterdir())))
        check("...and nothing was written into the registered default vault either",
              not (a / "inbox").exists())

        # ...and PLAINKEEP_HOME still OUTRANKS the registry default when it is a valid vault: it is
        # step 2 of four and the default is step 4. What Task 1b added is validation, not a new
        # precedence — asserting only the refusal above would leave that half unproven.
        vaultfx.mark_vault(b)
        # The bash floor spawns `$PLAINKEEP_HOME/bin/lib/*.py` — the engine still lives inside the
        # vault in Phase 1 — so a temp vault needs the engine tree to dispatch at all. (The refusal
        # above needs none of it: discovery runs from the ENGINE's own copy and refuses before the
        # floor ever reaches for the vault's.)
        os.symlink(REPO / "bin", b / "bin")
        r = subprocess.run([str(REPO / "plainkeep"), "capture", "unchanged"],
                           capture_output=True, text=True, cwd=str(b),
                           env={**os.environ, "PLAINKEEP_HOME": str(b), "PLAINKEEP_CORE": "off",
                                "PLAINKEEP_CONFIG_HOME": str(cfg)})
        check("a MARKED PLAINKEEP_HOME still wins over a registered default",
              r.returncode == 0 and any((b / "inbox").glob("cap-*.md")), r.stdout + r.stderr)
        check("...and still nothing landed in the registered default vault",
              not (a / "inbox").exists())

        # `cwd` is pinned to `b` so the chain re-run below is deterministic rather than a function of
        # wherever the suite was launched from.
        r = vault(b, cfg, "status", "--json", cwd=b)
        data = json.loads(r.stdout)["data"]
        check("status reports the active root and that it is unregistered",
              data["active_root"] == os.path.realpath(b) and data["registered_as"] is None, r.stdout)
        check("status names the mechanism that selected the root",
              data["selected_by"] == "PLAINKEEP_HOME", r.stdout)
        # This verb was invoked DIRECTLY, with no dispatcher to record a mechanism — PLAINKEEP_HOME
        # is then genuinely the only thing that pointed it anywhere, and `selected_by_source` is what
        # keeps that distinguishable from a dispatcher's answer instead of collapsing the two.
        check("status says WHERE the mechanism came from, so a direct invocation is not mistaken "
              "for a dispatched one",
              data["selected_by_source"].startswith("PLAINKEEP_HOME (no dispatcher"), r.stdout)
        # REWRITTEN in the r2 fix wave. This used to assert `would_select == realpath(b)`, which was
        # true for a reason that made the field useless: the chain was re-run in a process where the
        # dispatcher had ALREADY exported PLAINKEEP_HOME, so step 2 won every time and `would_select`
        # could not differ from `active_root` in any invocation that can exist. It is now the chain
        # with PLAINKEEP_HOME taken out of the way — so here it REFUSES, naming `b` as an
        # unregistered marker, which is a fact the old reading hid.
        check("status reports what EVERY mechanism saw, not just the winner",
              data["saw"].get("--vault") == "not supplied"
              and str(b) in data["saw"].get("marker walk-up from $PWD", ""), r.stdout)
        check("would_select answers the chain WITHOUT PLAINKEEP_HOME — here an unregistered marker",
              data["would_select"] is None
              and "not in the registry" in (data["selection_error"] or ""), r.stdout)


def main() -> int:
    case_roundtrip()
    case_fail_closed()
    case_atomic()
    case_template_and_discovery()

    print(f"{BOLD}Vault marker + registry (ADR-014 Task 1a) — {len(results)} checks{RESET}\n")
    passed = sum(1 for _, ok, _ in results if ok)
    for name, ok, detail in results:
        mark = f"{GREEN}PASS{RESET}" if ok else f"{RED}FAIL{RESET}"
        print(f"  {mark} {name:<70}" + (f" {DIM}{detail}{RESET}" if (detail and not ok) else ""))
    failed = len(results) - passed
    print("\nSUITE-NOTE: this suite is IDENTITY ONLY — the marker and the registry. Nothing here "
          "proves a root is DISCOVERED correctly; the --vault selector, marker walk-up, the registry "
          "default and the two-vault identity test are gated in test/run_discovery.py.")
    print(f"\n{BOLD}Result:{RESET} {GREEN}{passed} passed{RESET}, "
          f"{(RED if failed else DIM)}{failed} failed{RESET}, {len(results)} checks")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
