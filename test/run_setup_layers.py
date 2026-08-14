#!/usr/bin/env python3
"""Offline behavior tests for bin/lib/setuplib.py."""
from __future__ import annotations
import importlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from lib.hermetic import seal
from lib import launchdfx      # TEST-ONLY fake launchctl — imported HERE, above the sys.path swap
seal()   # hermetic: an empty throwaway registry, never the developer's real vault

REPO = Path(__file__).resolve().parents[1]
sys.path = [str(REPO / "bin"), *[p for p in sys.path if Path(p or ".").resolve() != Path(__file__).resolve().parent]]
# Below this line `lib` means bin/lib, so the test/lib packages imported ABOVE it (the seal) have to
# leave sys.modules with them — an import is cached by NAME, and `from lib import paths` would
# otherwise be answered by the already-loaded test/lib.
for _cached in [m for m in list(sys.modules) if m == "lib" or m.startswith("lib.")]:
    del sys.modules[_cached]

GREEN, RED, DIM, BOLD, RESET = "\033[32m", "\033[31m", "\033[2m", "\033[1m", "\033[0m"
results = []


def check(name, cond, detail=""):
    results.append((name, bool(cond), detail))


def reload_setuplib(home: Path):
    os.environ["PLAINKEEP_HOME"] = str(home)
    from lib import paths  # noqa: E402
    importlib.reload(paths)
    from lib import setuplib  # noqa: E402
    return importlib.reload(setuplib)


def make_home(tmp: Path) -> Path:
    home = tmp / "plainkeep"
    home.mkdir(parents=True)
    return home


def seed_skeleton(mod, home: Path):
    for rel in mod.REQUIRED_DIRS:
        (home / rel).mkdir(parents=True, exist_ok=True)
    (home / ".obsidian").mkdir()


def patch_probe(mod, **values):
    old = {}
    for name, value in values.items():
        old[name] = getattr(mod, name)
        setattr(mod, name, value)
    return old


def restore(mod, old):
    for name, value in old.items():
        setattr(mod, name, value)


def run_setup(args, home: Path, *, fake: bool = True, assume_ollama: bool = True, extra_env=None):
    # assume_ollama defaults True so these CLI subprocess tests are HOST-INDEPENDENT: the ollama-gated
    # layers (search/models) stay attemptable+confirm on a runner with no ollama (Linux CI), instead
    # of reporting `blocked` and skipping — which would make the confirm/failure-envelope assertions
    # pass only on a Mac that happens to have ollama. Pass assume_ollama=False to test the blocked path.
    env = {
        **os.environ,
        "PLAINKEEP_HOME": str(home),
        "PYTHONPATH": str(REPO / "bin"),
        "PLAINKEEP_EMBED_MODEL": "plainkeep-setup-test-embed-model",
    }
    if fake:
        env["PLAINKEEP_SETUP_FAKE"] = "1"
    else:
        env.pop("PLAINKEEP_SETUP_FAKE", None)
    if assume_ollama:
        env["PLAINKEEP_ASSUME_OLLAMA"] = "1"
    if extra_env:
        env.update(extra_env)
    return subprocess.run([sys.executable, str(REPO / "bin" / "setup" / "run.py"), *args],
                          capture_output=True, text=True, env=env)


def main() -> int:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        home = make_home(tmp)
        mod = reload_setuplib(home)

        ids = [layer.id for layer in mod.LAYERS]
        check("registry has L1-L5 + ui in order",
              ids == ["skeleton", "search", "backups", "models", "automation", "ui"], str(ids))
        check("registry exposes layer shape",
              all(layer.id and layer.title and layer.gate and isinstance(layer.required, bool) for layer in mod.LAYERS),
              str(mod.LAYERS))
        check("REQUIRED_DIRS moved into shared lib", "wiki" in mod.REQUIRED_DIRS and "tasks/active" in mod.REQUIRED_DIRS,
              str(mod.REQUIRED_DIRS))

        rows = mod.status()
        valid = {"ready", "partial", "absent", "blocked", "not_applicable"}
        check("status returns public rows", len(rows) == 6 and all({"id", "title", "status", "required", "detail", "items", "next"}.issubset(r) for r in rows), str(rows))
        check("status values are valid", {r["status"] for r in rows} <= valid, str(rows))
        check("single-layer status filters", [r["id"] for r in mod.status("search")] == ["search"])

        # Skeleton: absent -> ready and idempotent advance.
        s0 = mod.status("skeleton")[0]
        check("skeleton absent in empty vault", s0["status"] in ("partial", "absent") and s0["required"], str(s0))
        os.environ["PLAINKEEP_SETUP_FAKE"] = "1"
        a0 = mod.advance("skeleton", yes=False, fake=True)
        check("fake skeleton advance records doctor init", a0["ran"] and "doctor" in a0["ran"][0], str(a0))
        seed_skeleton(mod, home)
        s1 = mod.status("skeleton")[0]
        check("skeleton ready after required dirs exist", s1["status"] == "ready", str(s1))
        a1 = mod.advance("skeleton", yes=False, fake=True)
        check("skeleton advance is idempotent when ready", not a1["ran"] and "skeleton" in a1["skipped"], str(a1))

        # The Obsidian config pack is ADVISORY: its .obsidian/*.json items stay VISIBLE in the
        # skeleton row (so `plainkeep setup` surfaces their state) but never flip the required layer to
        # non-ready — skeleton readiness depends only on REQUIRED_DIRS. A fresh clone that hasn't
        # seeded .obsidian/ yet must still let `plainkeep doctor` pass.
        (home / "templates" / "obsidian").mkdir(parents=True, exist_ok=True)
        (home / "templates" / "obsidian" / "app.json").write_text("{}", encoding="utf-8")
        (home / "templates" / "obsidian" / "appearance.json").write_text("{}", encoding="utf-8")
        (home / ".obsidian" / "app.json").unlink(missing_ok=True)
        (home / ".obsidian" / "appearance.json").unlink(missing_ok=True)
        s_pack_missing = mod.status("skeleton")[0]
        check("skeleton stays ready when obsidian pack unseeded (advisory), but reports the item",
              s_pack_missing["status"] == "ready"
              and any(i["id"] == ".obsidian/app.json" and not i["ok"] and i.get("advisory")
                      for i in s_pack_missing["items"]),
              str(s_pack_missing))
        (home / ".obsidian" / "app.json").write_text("{}", encoding="utf-8")
        s_pack_partial = mod.status("skeleton")[0]
        check("skeleton ready with a partially-seeded obsidian pack; missing file still reported",
              s_pack_partial["status"] == "ready"
              and any(i["id"] == ".obsidian/appearance.json" and not i["ok"] and i.get("advisory")
                      for i in s_pack_partial["items"]),
              str(s_pack_partial))
        (home / ".obsidian" / "appearance.json").write_text("{}", encoding="utf-8")
        s_pack_seeded = mod.status("skeleton")[0]
        check("skeleton ready with all obsidian json templates seeded (advisory items ok)",
              s_pack_seeded["status"] == "ready"
              and all(i["ok"] for i in s_pack_seeded["items"] if i.get("advisory")),
              str(s_pack_seeded))

        # Search: absent / partial / ready / blocked and the confirm gate. Readiness is OPERATIONAL
        # now (Task 10): deps-importable (probed via the venv/dispatcher interpreter) + model-pulled +
        # index-built; PLAINKEEP_VECTORS/PLAINKEEP_RERANK are advisory. ollama is the hard external prerequisite.
        # Patch the probes hermetically so the machine's real ollama/venv state never leaks in.
        def _search_probes(deps, model, index, ollama=True):
            return patch_probe(mod,
                               _deps_importable=lambda: deps,
                               _ollama_has=lambda m: model,
                               _index_built=lambda: index,
                               _ollama_present=lambda: ollama)

        old = _search_probes(False, False, False)
        try:
            search_absent = mod.status("search")[0]
            check("search absent when no components ready", search_absent["status"] == "absent", str(search_absent))
        finally:
            restore(mod, old)

        old = _search_probes(True, False, False)
        try:
            search_partial = mod.status("search")[0]
            check("search partial reports item state", search_partial["status"] == "partial"
                  and any(i["id"] == "deps-importable" and i["ok"] for i in search_partial["items"]), str(search_partial))
        finally:
            restore(mod, old)

        old = _search_probes(True, True, True)
        try:
            search_ready = mod.status("search")[0]
            check("search ready when deps, model, and index are operational", search_ready["status"] == "ready", str(search_ready))
        finally:
            restore(mod, old)

        # Task 8: ollama absent → blocked with the EXACT install command, never absent/partial.
        old = _search_probes(True, False, False, ollama=False)
        try:
            search_blocked = mod.status("search")[0]
            check("search blocked with install hint when ollama is missing",
                  search_blocked["status"] == "blocked" and "install ollama" in search_blocked["next"],
                  str(search_blocked))
        finally:
            restore(mod, old)

        old = _search_probes(False, False, False)
        try:
            gated = mod.advance("search", yes=False, fake=True)
            check("search advance without yes asks for confirmation", gated["confirm_needed"] and not gated["ran"], str(gated))
            allowed = mod.advance("search", yes=True, fake=True)
            joined = " | ".join(allowed["ran"])
            check("search fake advance records venv, search-only pip, ollama pull, index build",
                  len(allowed["ran"]) == 4 and "venv" in allowed["ran"][0] and "pip install" in allowed["ran"][1]
                  and "ollama pull" in allowed["ran"][2] and "index" in allowed["ran"][3]
                  and "lancedb" in joined and "fastembed" in joined
                  and "Pillow" not in joined and "trafilatura" not in joined,  # search-only, NOT the models deps
                  str(allowed))
        finally:
            restore(mod, old)

        old = _search_probes(False, False, False)
        real_run = mod.subprocess.run
        def boom(*args, **kwargs):
            raise AssertionError(f"subprocess.run should not execute in fake mode: {args}")
        mod.subprocess.run = boom
        try:
            fake_safe = mod.advance("search", yes=True, fake=True)
            check("PLAINKEEP_SETUP_FAKE records without subprocess execution", len(fake_safe["ran"]) == 4, str(fake_safe))
        except AssertionError as e:
            check("PLAINKEEP_SETUP_FAKE records without subprocess execution", False, str(e))
        finally:
            mod.subprocess.run = real_run
            restore(mod, old)

        # Backups: blocked handoff, never auto-run.
        backups = mod.advance("backups", yes=True, fake=True)
        check("backups advance is blocked handoff", backups["handoff"] == ["plainkeep backup init"] and not backups["ran"], str(backups))

        # Models: fake command recording includes conditional package support but no real execution.
        # Patch ollama present (else Task 8 reports the layer `blocked` and advance would skip it).
        old = patch_probe(mod,
                          _platform_system=lambda: "Darwin",
                          _platform_machine=lambda: "arm64",
                          _ollama_present=lambda: True,
                          _ollama_has=lambda model: False)
        try:
            models = mod.advance("models", yes=True, fake=True)
            joined = " | ".join(models["ran"])
            check("models fake advance records model pull and package installs",
                  "models" in joined and "pull" in joined and "Pillow" in joined and "trafilatura" in joined and "mlx-vlm" in joined,
                  str(models))
        finally:
            restore(mod, old)

        # Task 8: automation off macOS is not_applicable (advisory), with a one-line reason.
        old = patch_probe(mod, _platform_system=lambda: "Linux")
        try:
            auto_na = mod.status("automation")[0]
            check("automation not_applicable off macOS with a reason",
                  auto_na["status"] == "not_applicable" and "macOS" in auto_na["detail"], str(auto_na))
            na_adv = mod.advance("automation", yes=True, fake=True)
            check("advance skips a not_applicable layer (never attempts it)",
                  not na_adv["ran"] and "automation" in na_adv["skipped"], str(na_adv))
        finally:
            restore(mod, old)

        # --- ADR-022: "automation ready" means LOADED, not merely rendered. ---
        #
        # The layer used to answer "ready" for `jobs/launchd/*.plist` existing, which is a claim about
        # a file the operator wrote and not about anything launchd knows. On a machine where the
        # activation step was never pasted, setup reported the layer done and the schedule never ran —
        # the exact gap `plainkeep job status` now shows as three separate columns.
        #
        # Every probe below goes through the SAME injected seam the verb uses (a fake launchctl and a
        # redirected LaunchAgents dir): no real `launchctl` runs, and the developer's own
        # ~/Library/LaunchAgents is never read or written.
        auto_home = make_home(tmp / "automation")
        (auto_home / "jobs").mkdir(parents=True)
        (auto_home / "jobs" / "registry.json").write_text(json.dumps({
            "external_allowlist": [],
            "jobs": {
                "start": {"command": "plainkeep start --automated", "schedule": {"daily": "07:30"},
                          "risk": "safe_write"},
                "danger": {"command": "plainkeep capture x", "schedule": {"daily": "09:00"},
                           "risk": "confirm"},
            }}), encoding="utf-8")
        auto_fake = launchdfx.install(tmp / "automation-machine")
        auto_env_saved = {k: os.environ.get(k) for k in auto_fake.env}
        os.environ.update(auto_fake.env)
        mod_a = reload_setuplib(auto_home)
        old = patch_probe(mod_a, _platform_system=lambda: "Darwin")
        try:
            a0 = mod_a.status("automation")[0]
            item_ids = [i["id"] for i in a0["items"]]
            check("automation layer reports rendered AND loaded as separate items",
                  item_ids == ["rendered", "loaded"], str(a0))
            check("automation is absent when nothing is rendered",
                  a0["status"] == "absent", str(a0))

            # Rendered but never activated: the state the old layer called `ready`.
            launchd_dir = auto_home / "jobs" / "launchd"
            launchd_dir.mkdir(parents=True, exist_ok=True)
            from lib import launchdlib  # noqa: E402  (bin/lib — sys.path is bin/ here)
            reg = launchdlib.load_registry()
            (launchd_dir / "com.plainkeep.start.plist").write_text(
                launchdlib.plist("start", reg["jobs"]["start"]), encoding="utf-8")
            a1 = mod_a.status("automation")[0]
            check("rendered-but-not-loaded is PARTIAL, never ready",
                  a1["status"] == "partial"
                  and [i["ok"] for i in a1["items"]] == [True, False], str(a1))
            check("a rendered-but-unloaded layer points at the activation verb",
                  "job enable" in (a1.get("next") or ""), str(a1.get("next")))

            # ONE advance path, and it now covers both halves: render, then activate. Run it from the
            # partial state above — `advance` skips an already-ready layer, so asking it here is the
            # only place the two-step plan is observable.
            os.environ["PLAINKEEP_SETUP_FAKE"] = "1"
            auto_fake.clear()
            adv = mod_a.advance("automation", yes=True, fake=True)
            joined = " | ".join(adv["ran"])
            check("automation advance renders AND enables (through the job verb)",
                  "job apply" in joined and "job enable" in joined and "--yes" in joined, joined)
            check("a fake automation advance mutates no launchd state",
                  not [c for c in auto_fake.calls() if c.split()[:1] in (["bootstrap"], ["bootout"])],
                  str(auto_fake.calls()))
            os.environ.pop("PLAINKEEP_SETUP_FAKE", None)

            # Loaded, through the same seam launchd would answer on.
            auto_fake.mark_loaded("com.plainkeep.start")
            a2 = mod_a.status("automation")[0]
            check("automation is ready only once launchd has the job loaded",
                  a2["status"] == "ready" and all(i["ok"] for i in a2["items"]), str(a2))
        finally:
            restore(mod_a, old)
            for k, v in auto_env_saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
            mod = reload_setuplib(home)

        # Task 8: models blocked (ollama absent) → advance skips with the install hint, never crashes.
        old = patch_probe(mod, _ollama_present=lambda: False, _ollama_has=lambda model: False)
        try:
            m_blocked = mod.status("models")[0]
            blk_adv = mod.advance("models", yes=True, fake=True)
            check("models blocked when ollama missing → advance skips with hint, no run",
                  m_blocked["status"] == "blocked" and not blk_adv["ran"]
                  and any("install ollama" in h for h in blk_adv["handoff"]), str((m_blocked, blk_adv)))
        finally:
            restore(mod, old)

        # Automation and repeated advance support all-style orchestration order.
        ordered = []
        for layer in mod.LAYERS:
            res = mod.advance(layer.id, yes=True, fake=True)
            ordered.append(layer.id)
            check(f"advance {layer.id} returns public result",
                  {"ran", "skipped", "handoff", "confirm_needed"}.issubset(res), str(res))
        check("repeated advance follows registry order", ordered == ids, str(ordered))

        try:
            mod.status("bogus")
            unknown_status_ok = False
        except ValueError as e:
            unknown_status_ok = "unknown layer" in str(e)
        check("unknown layer status is clear error", unknown_status_ok)

        try:
            mod.advance("bogus", yes=True, fake=True)
            unknown_advance_ok = False
        except ValueError as e:
            unknown_advance_ok = "unknown layer" in str(e)
        check("unknown layer advance is clear error", unknown_advance_ok)

        # --- ui layer (ADR-011): host-independent probes via the _gh_present/_ui_* seams. The layer
        # is absent (attemptable) when the release download OR a source build is possible; blocked
        # with a teaching hint when neither is; not_applicable on a platform with no prebuilt asset;
        # ready once an executable binary is where the bin/ui shim looks. Fake advance previews the
        # gh download + sha256 verify without touching the network. ---
        old = patch_probe(mod, _ui_installed=lambda: None, _ui_asset=lambda: "plainkeep-ui-test-arm64",
                          _ui_repo=lambda: "owner/template", _ui_source_buildable=lambda: False,
                          _gh_present=lambda: True, _ui_expected_version=lambda: None)
        try:
            u_abs = mod.status("ui")[0]
            check("ui absent (attemptable) when downloadable but not installed",
                  u_abs["status"] == "absent" and u_abs["next"] == "plainkeep setup ui --yes", str(u_abs))
            u_adv = mod.advance("ui", yes=True, fake=True)
            u_joined = " | ".join(u_adv["ran"])
            check("fake ui advance previews gh release download + sha256 verify (no network)",
                  "gh release download" in u_joined and "owner/template" in u_joined
                  and "checksums.txt" in u_joined and "sha256" in u_joined, str(u_adv["ran"]))
        finally:
            restore(mod, old)

        # Update detection (offline): the engine ships bin/ui/version.txt; the binary self-reports
        # --version. A mismatch (or a pre---version binary reporting None) makes the layer `partial`
        # ("update available") so the ordinary `plainkeep setup ui --yes` re-downloads, PINNED to the
        # engine's expected release tag. Matching versions stay `ready` and skip.
        old = patch_probe(mod, _ui_installed=lambda: "/fake/plainkeep-ui",
                          _ui_asset=lambda: "plainkeep-ui-test-arm64", _ui_repo=lambda: "owner/template",
                          _ui_source_buildable=lambda: False, _gh_present=lambda: True,
                          _ui_expected_version=lambda: "9.9.9",
                          _ui_installed_version=lambda exe: "1.0.0")
        try:
            u_upd = mod.status("ui")[0]
            check("ui partial (update available) when installed version != engine's expected",
                  u_upd["status"] == "partial" and "update available" in u_upd["detail"]
                  and u_upd["next"] == "plainkeep setup ui --yes", str(u_upd))
            u_uadv = mod.advance("ui", yes=True, fake=True)
            check("fake ui update advance pins the download to the expected release tag",
                  any("gh release download ui-v9.9.9" in c for c in u_uadv["ran"]), str(u_uadv["ran"]))
        finally:
            restore(mod, old)

        old = patch_probe(mod, _ui_installed=lambda: "/fake/plainkeep-ui",
                          _ui_expected_version=lambda: "9.9.9",
                          _ui_installed_version=lambda exe: "9.9.9")
        try:
            u_cur = mod.status("ui")[0]
            check("ui ready when installed version matches the engine's expected",
                  u_cur["status"] == "ready"
                  and any(i.get("id") == "version" and i.get("ok") for i in u_cur["items"]),
                  str(u_cur))
            u_cadv = mod.advance("ui", yes=True, fake=True)
            check("up-to-date ui advance skips (no re-download)",
                  not u_cadv["ran"] and "ui" in u_cadv["skipped"], str(u_cadv))
        finally:
            restore(mod, old)

        old = patch_probe(mod, _ui_installed=lambda: None, _ui_asset=lambda: "plainkeep-ui-test-arm64",
                          _ui_repo=lambda: None, _ui_source_buildable=lambda: False,
                          _gh_present=lambda: False)
        try:
            u_blk = mod.status("ui")[0]
            check("ui blocked without gh, hint teaches the install",
                  u_blk["status"] == "blocked" and "gh" in u_blk["next"], str(u_blk))
            u_badv = mod.advance("ui", yes=True, fake=True)
            check("blocked ui advance skips with the handoff (never attempts)",
                  not u_badv["ran"] and "ui" in u_badv["skipped"]
                  and any("gh" in h for h in u_badv["handoff"]), str(u_badv))
        finally:
            restore(mod, old)

        old = patch_probe(mod, _ui_installed=lambda: None, _ui_asset=lambda: None,
                          _ui_repo=lambda: "owner/template", _ui_source_buildable=lambda: False,
                          _gh_present=lambda: True)
        try:
            u_na = mod.status("ui")[0]
            check("ui not_applicable on a platform with no prebuilt asset and no source build",
                  u_na["status"] == "not_applicable", str(u_na))
        finally:
            restore(mod, old)

        # Source-build fallback (contributor checkout): no gh, but cli/ + bun → still attemptable,
        # and the fake advance previews the bun compile into .local/bin.
        old = patch_probe(mod, _ui_installed=lambda: None, _ui_asset=lambda: "plainkeep-ui-test-arm64",
                          _ui_repo=lambda: None, _ui_source_buildable=lambda: True,
                          _gh_present=lambda: False)
        try:
            u_src = mod.status("ui")[0]
            check("ui attemptable via source build when gh is absent", u_src["status"] == "absent", str(u_src))
            u_sadv = mod.advance("ui", yes=True, fake=True)
            u_sjoined = " | ".join(u_sadv["ran"])
            check("fake ui source advance previews bun install + compile",
                  "bun install" in u_sjoined and "bun build --compile" in u_sjoined
                  and ".local/bin/plainkeep-ui" in u_sjoined, str(u_sadv["ran"]))
        finally:
            restore(mod, old)

        # Ready once an executable binary sits where the shim looks first (.local/bin/plainkeep-ui)
        # AND it reports the version the ENGINE pins.
        #
        # The stub used to be `#!/bin/sh` and nothing else, and it passed for a reason that stopped
        # being true in Phase 2 Task 2: `_ui_expected_version()` read the pin as
        # `$PLAINKEEP_HOME/bin/ui/version.txt`, which this fixture vault does not have, so the engine
        # read as "pre-version" and update detection was switched off entirely. The pin is
        # engine-owned (`<engine>/bin/ui/version.txt`) and is now found wherever the vault is, so a
        # binary that answers `--version` with nothing is correctly `partial` — "update available".
        # The stub therefore answers with the real pin, which is what a matching install looks like.
        expected_ui = (REPO / "bin" / "ui" / "version.txt").read_text(encoding="utf-8").strip()
        ui_bin = home / ".local" / "bin" / "plainkeep-ui"
        ui_bin.parent.mkdir(parents=True, exist_ok=True)
        ui_bin.write_text(f"#!/bin/sh\necho {expected_ui}\n")
        ui_bin.chmod(0o755)
        check("the ui pin is ENGINE-owned and found with no copy of it in the vault",
              mod._ui_expected_version() == expected_ui
              and not (home / "bin" / "ui" / "version.txt").exists(),
              f"expected={mod._ui_expected_version()!r} pin={expected_ui!r}")
        u_rdy = mod.status("ui")[0]
        check("ui ready once .local/bin/plainkeep-ui is installed+executable", u_rdy["status"] == "ready", str(u_rdy))
        u_skip = mod.advance("ui", yes=True, fake=True)
        check("ready ui advance is idempotent (skips)", not u_skip["ran"] and "ui" in u_skip["skipped"], str(u_skip))
        ui_bin.unlink()

        # --- Best-effort --all (Task 8): a failure in ONE attempted AUTO layer must not abort the
        # others; only an ATTEMPTED failure yields a nonzero exit; ready/blocked/not_applicable layers
        # are skipped (not attempted). Drive the real bin/setup/run.py:_advance_all with stubbed
        # status/advance so no real installs run. ---
        import contextlib as _ctx
        import io as _io
        import importlib.util as _ilu
        _spec = _ilu.spec_from_file_location("setup_run_besteffort", REPO / "bin" / "setup" / "run.py")
        setup_run = _ilu.module_from_spec(_spec)
        _spec.loader.exec_module(setup_run)

        _ALL_STATUS = {"skeleton": "partial", "search": "absent", "backups": "blocked",
                       "models": "not_applicable", "automation": "absent", "ui": "blocked"}

        def fake_status(layer_id=None):
            def row(i):
                return {"id": i, "title": i, "status": _ALL_STATUS[i], "required": i == "skeleton",
                        "detail": "", "items": [], "next": f"next-{i}"}
            order = ["skeleton", "search", "backups", "models", "automation", "ui"]
            return [row(layer_id)] if layer_id else [row(i) for i in order]

        def fake_advance(layer_id, *, yes, fake):
            if layer_id == "search":
                raise subprocess.CalledProcessError(1, ["plainkeep", "index"])
            r = mod._result()
            r["ran"].append(f"did {layer_id}")
            return r

        _saved = (setup_run.setuplib.status, setup_run.setuplib.advance)
        setup_run.setuplib.status = fake_status
        setup_run.setuplib.advance = fake_advance
        try:
            _buf = _io.StringIO()
            with _ctx.redirect_stdout(_buf):
                rc = setup_run._advance_all(yes=True)
            out = _buf.getvalue()
            check("best-effort --all: one failing layer does not abort the rest (exit 1)", rc == 1, out)
            check("best-effort --all: layers after the failure still ran",
                  "did skeleton" in out and "did automation" in out and "FAILED" in out, out)
            check("best-effort --all: not_applicable/blocked layers are skipped, not attempted",
                  "models: skipped" in out and "did models" not in out, out)
        finally:
            setup_run.setuplib.status, setup_run.setuplib.advance = _saved

        # --- FIX 2: the models layer installs its deps into the .venv (the dispatcher-preferred
        # interpreter), NOT bare sys.executable — closing the silent capability regression. ---
        old = patch_probe(mod, _platform_system=lambda: "Darwin", _platform_machine=lambda: "arm64",
                          _ollama_present=lambda: True, _ollama_has=lambda model: False)
        try:
            models_v = mod.advance("models", yes=True, fake=True)
            joined_v = " | ".join(models_v["ran"])
            venv_py = str(mod._venv_python())
            check("FIX 2: models ensures the .venv (records a `-m venv` create)",
                  any("-m venv" in c for c in models_v["ran"]), str(models_v["ran"]))
            check("FIX 2: models deps pip-install into the .venv interpreter, not bare python",
                  venv_py in joined_v and "Pillow" in joined_v and "trafilatura" in joined_v
                  and "mlx-vlm" in joined_v, str(models_v["ran"]))
            check("FIX 2: no models step targets the bare interpreter's pip",
                  not any(c.startswith(sys.executable + " -m pip") for c in models_v["ran"]),
                  str(models_v["ran"]))
        finally:
            restore(mod, old)

        # --- FIX 4: the confirm gate must IGNORE skipped (blocked/not_applicable/ready) layers. When
        # every confirm-class AUTO layer is blocked/not_applicable, `--all` attempts nothing and must
        # NOT demand --yes (that was a spurious exit 3, e.g. on a host with no ollama). ---
        _F4_STATUS = {"skeleton": "ready", "search": "blocked", "backups": "blocked",
                      "models": "not_applicable", "automation": "ready", "ui": "blocked"}

        def f4_status(layer_id=None):
            def row(i):
                return {"id": i, "title": i, "status": _F4_STATUS[i], "required": i == "skeleton",
                        "detail": "", "items": [], "next": f"next-{i}"}
            order = ["skeleton", "search", "backups", "models", "automation", "ui"]
            return [row(layer_id)] if layer_id else [row(i) for i in order]

        def f4_advance(layer_id, *, yes, fake):
            r = mod._result()
            r["ran"].append(f"did {layer_id}")
            return r

        _saved4 = (setup_run.setuplib.status, setup_run.setuplib.advance)
        setup_run.setuplib.status = f4_status
        setup_run.setuplib.advance = f4_advance
        try:
            _buf = _io.StringIO()
            f4_exit = None
            try:
                with _ctx.redirect_stdout(_buf):
                    f4_exit = setup_run._advance_all(yes=False)  # NO --yes on purpose
            except SystemExit as e:  # a spurious confirm gate would exit 3 here
                f4_exit = e.code
            check("FIX 4 (--all): no --yes demanded when only blocked/not_applicable confirm layers",
                  f4_exit == 0, str(f4_exit) + _buf.getvalue())
            # And the single-layer path: a BLOCKED confirm layer must not exit 3 for a missing --yes.
            _buf2 = _io.StringIO()
            one_exit = None
            try:
                with _ctx.redirect_stdout(_buf2):
                    one_exit = setup_run._advance_one("search", yes=False)  # search is BLOCKED above
            except SystemExit as e:
                one_exit = e.code
            check("FIX 4 (single-layer): a BLOCKED confirm layer does not exit 3 for missing --yes",
                  one_exit != 3, str(one_exit) + _buf2.getvalue())
        finally:
            setup_run.setuplib.status, setup_run.setuplib.advance = _saved4

        # --- FIX 5: best-effort `--all` preserves partial progress (steps that ran before a failure)
        # AND aggregates every blocked/skipped layer's `next` remediation into the top-level handoff. ---
        _F5_STATUS = {"skeleton": "absent", "search": "absent", "backups": "blocked",
                      "models": "blocked", "automation": "absent", "ui": "blocked"}

        def f5_status(layer_id=None):
            def row(i):
                return {"id": i, "title": i, "status": _F5_STATUS[i], "required": i == "skeleton",
                        "detail": "", "items": [], "next": f"next-{i}"}
            order = ["skeleton", "search", "backups", "models", "automation", "ui"]
            return [row(layer_id)] if layer_id else [row(i) for i in order]

        def f5_advance(layer_id, *, yes, fake):
            if layer_id == "search":  # ran 2 steps, then blew up on the 3rd
                exc = subprocess.CalledProcessError(1, ["plainkeep", "index"])
                exc.ops_partial_ran = ["python -m venv .venv",
                                       ".venv/bin/python3 -m pip install lancedb fastembed"]
                raise exc
            r = mod._result()
            r["ran"].append(f"did {layer_id}")
            return r

        _saved5 = (setup_run.setuplib.status, setup_run.setuplib.advance)
        setup_run.setuplib.status = f5_status
        setup_run.setuplib.advance = f5_advance
        try:
            _buf5 = _io.StringIO()
            with _ctx.redirect_stdout(_buf5):
                rc5 = setup_run._advance_all(yes=True)
            out5 = _buf5.getvalue()
            check("FIX 5: an attempted-layer failure still exits 1", rc5 == 1, out5)
            check("FIX 5: partial progress (steps run before the failure) is preserved/surfaced",
                  "python -m venv .venv" in out5
                  and ".venv/bin/python3 -m pip install lancedb fastembed" in out5
                  and "FAILED" in out5, out5)
            check("FIX 5: blocked AUTO layer's `next` remediation is aggregated into the handoff",
                  "next-models" in out5, out5)
        finally:
            setup_run.setuplib.status, setup_run.setuplib.advance = _saved5

        # --- Task 11: the interactive `--wizard`. The prompt/answer loop is factored around an
        # injected input-callable (`ask`) and output-callable (`say`) so it is unit-testable WITHOUT a
        # real tty: we drive `_run_wizard` in-process with scripted answers and capture every printed
        # line. Safe defaults: skeleton ON (Enter accepts), search/models/automation OFF, backups a
        # printed handoff (never prompted). ---
        check("wizard default-Y accepted on Enter", setup_run._ask_yes_no("x", True, lambda p: "") is True)
        check("wizard default-N skipped on Enter", setup_run._ask_yes_no("x", False, lambda p: "") is False)
        check("wizard explicit 'y' overrides default-N", setup_run._ask_yes_no("x", False, lambda p: "y") is True)
        check("wizard explicit 'n' overrides default-Y", setup_run._ask_yes_no("x", True, lambda p: "n") is False)
        check("wizard closed stdin (EOF) takes the safe default",
              setup_run._ask_yes_no("x", True, lambda p: (_ for _ in ()).throw(EOFError())) is True)

        wiz_rows = [
            {"id": "skeleton", "title": "Vault structure", "status": "absent", "required": True,
             "detail": "", "items": [], "next": "plainkeep setup skeleton"},
            {"id": "search", "title": "Semantic search", "status": "absent", "required": False,
             "detail": "", "items": [], "next": "plainkeep setup search --yes"},
            {"id": "backups", "title": "Durability", "status": "blocked", "required": False,
             "detail": "backup setup needs human initialization", "items": [], "next": "plainkeep backup init"},
            {"id": "models", "title": "File-processing", "status": "ready", "required": False,
             "detail": "file-processing models ready", "items": [], "next": ""},
            {"id": "automation", "title": "Schedules", "status": "absent", "required": False,
             "detail": "", "items": [], "next": "plainkeep setup automation"},
            {"id": "ui", "title": "Terminal UI", "status": "absent", "required": False,
             "detail": "", "items": [], "next": "plainkeep setup ui --yes"},
        ]
        wiz_calls = []

        def wiz_advance(layer_id, *, yes, fake):
            wiz_calls.append((layer_id, yes))
            r = mod._result()
            r["ran"].append(f"did {layer_id}")
            return r

        # Prompts fire only for attemptable layers, in LAYERS order: skeleton, search, automation, ui.
        # Answers: Enter (accept default-Y skeleton), Enter (skip default-N search), 'y' (accept
        # automation), Enter (accept default-Y ui — the TUI defaults ON for the wizard's audience).
        wiz_answers = iter(["", "", "y", ""])
        wiz_lines = []
        _saved_w = setup_run.setuplib.advance
        setup_run.setuplib.advance = wiz_advance
        try:
            wiz_summary = setup_run._run_wizard(wiz_rows, lambda p: next(wiz_answers), wiz_lines.append)
        finally:
            setup_run.setuplib.advance = _saved_w
        wiz_out = "\n".join(wiz_lines)
        check("wizard advances accepted layers via the SAME advance(yes=True)",
              wiz_calls == [("skeleton", True), ("automation", True), ("ui", True)], str(wiz_calls))
        check("wizard ui default is ON (Enter accepts the TUI download)",
              ("ui", True) in wiz_calls, str(wiz_calls))
        check("wizard skips a default-N layer left at its default (search)",
              "search" in wiz_summary["skipped"] and "search" not in [c[0] for c in wiz_calls],
              str(wiz_summary))
        check("wizard summary lists what advanced",
              wiz_summary["advanced"] == ["skeleton", "automation", "ui"], str(wiz_summary))
        check("wizard notes an already-ready layer and never advances it",
              "already ready" in wiz_out and ("models", True) not in wiz_calls, wiz_out)
        check("wizard surfaces the backups handoff (never prompts/auto-runs it)",
              "plainkeep backup init" in wiz_out and ("backups", True) not in wiz_calls, wiz_out)
        check("wizard prints standing next-steps (push + backup init)",
              "git push -u origin main" in wiz_out, wiz_out)

        # --- ADR-022: automation is the DEFAULT offering, so the wizard's default is ON. ---
        #
        # It defaulted OFF ("no vectors, no model pulls, no jobs"), which put the product's core —
        # a day that starts and closes without being asked — behind a prompt the operator had to
        # know to say yes to. It is still ONE skippable prompt; what changed is which way Enter goes,
        # and that the prompt SAYS what it is about to schedule instead of the word "Schedules".
        check("wizard automation default is ON", setup_run.WIZARD_DEFAULTS["automation"] is True,
              str(setup_run.WIZARD_DEFAULTS))
        check("wizard still defaults search/models OFF (only automation flipped)",
              setup_run.WIZARD_DEFAULTS["search"] is False and setup_run.WIZARD_DEFAULTS["models"] is False,
              str(setup_run.WIZARD_DEFAULTS))

        wiz2_calls = []

        def wiz2_advance(layer_id, *, yes, fake):
            wiz2_calls.append((layer_id, yes))
            r = mod._result()
            r["ran"].append(f"did {layer_id}")
            return r

        # Answers: Enter, Enter, Enter, Enter — every layer left at its default. Automation must be
        # among the ones that ADVANCED.
        wiz2_answers = iter(["", "", "", ""])
        wiz2_lines = []
        wiz2_prompts = []

        def wiz2_ask(prompt):
            wiz2_prompts.append(prompt)
            return next(wiz2_answers)

        _saved_w2 = setup_run.setuplib.advance
        setup_run.setuplib.advance = wiz2_advance
        try:
            wiz2_summary = setup_run._run_wizard(wiz_rows, wiz2_ask, wiz2_lines.append)
        finally:
            setup_run.setuplib.advance = _saved_w2
        wiz2_out = "\n".join(wiz2_lines)
        check("wizard schedules automation on a bare Enter (it is the default now)",
              ("automation", True) in wiz2_calls and "automation" in wiz2_summary["advanced"],
              str(wiz2_calls))
        check("the wizard intro no longer says automation is off",
              "automation default to OFF" not in wiz2_out and "no jobs" not in wiz2_out, wiz2_out)
        check("the automation prompt spells out what gets scheduled (not just 'Schedules')",
              any("07:30" in p and "18:30" in p for p in wiz2_prompts), str(wiz2_prompts))

        os.environ.pop("PLAINKEEP_SETUP_FAKE", None)

        # --- FIX 3: ONE usable-venv probe (start-probe, not os.path.exists) shared by the dispatcher
        # interpreter choice and the create-if-missing logic; a half-built/broken .venv is REPAIRED,
        # not trusted as complete. (fake env is popped above so _ensure_venv really creates.) ---
        probe_home = make_home(tmp / "venvprobe")
        mod3 = reload_setuplib(probe_home)
        check("FIX 3: usable-venv probe returns None when .venv is absent",
              mod3._usable_venv_python() is None)
        broken_bin = probe_home / ".venv" / "bin"
        broken_bin.mkdir(parents=True)
        (broken_bin / "python3").write_text("#!/nonexistent/interpreter\nnot a real python\n")
        os.chmod(broken_bin / "python3", 0o755)
        check("FIX 3: usable-venv probe returns None for a present-but-unstartable .venv python",
              mod3._usable_venv_python() is None)
        check("FIX 3: _search_interpreter falls back to sys.executable when the venv is broken",
              mod3._search_interpreter() == sys.executable)
        res_repair = mod3._result()
        mod3._ensure_venv(res_repair, fake=False)
        check("FIX 3: _ensure_venv repairs a half-built .venv into a startable interpreter",
              any("venv" in c for c in res_repair["ran"]) and mod3._usable_venv_python() is not None,
              str(res_repair["ran"]))
        res_idem = mod3._result()
        mod3._ensure_venv(res_idem, fake=False)
        check("FIX 3: _ensure_venv is idempotent over an already-usable venv (no re-create)",
              res_idem["ran"] == [], str(res_idem["ran"]))
        mod = reload_setuplib(home)  # restore module state for any later in-process use

        # --- FIX 1: the `plainkeep` dispatcher must PROBE that .venv/bin/python3 actually starts, not just
        # test `-x`. A broken (executable-but-unstartable) venv python must fall back to bare python3
        # so plainkeep keeps working, instead of returning 126/127 on every verb. Build a PLAINKEEP_HOME that
        # mirrors the repo (symlinks) but carries a deliberately-broken .venv, then run `plainkeep help`. ---
        broken_home = tmp / "brokenvenv"
        broken_home.mkdir()
        for entry in REPO.iterdir():
            # `.logs` joins the skip list for the same reason `plainkeep.json` is copied below: the
            # dispatch this fixture performs runs the guardrail, the guardrail APPENDS to
            # `<root>/.logs/plainkeep.log`, and through a symlink that append landed in the real
            # checkout's audit log on every green run (measured: one line per run).
            # `.plainkeep` joins the skip list because the fixture MARKS ITSELF below — see there.
            if entry.name in (".venv", ".git", ".logs", ".orchestrate", "__pycache__",
                              ".plainkeep"):
                continue
            # plainkeep.json is COPIED, not symlinked: `plainkeep help` regenerates a stale manifest,
            # and through a symlink that write lands in the real checkout. The path-wall now refuses
            # it (guardrail takes the stricter of path and realpath), which is the correct verdict —
            # this fixture wanted an isolated home and was writing through to the developer's repo.
            if entry.name == "plainkeep.json":
                shutil.copy2(entry, broken_home / entry.name)
                continue
            os.symlink(entry, broken_home / entry.name)
        # THE FIXTURE MARKS ITS OWN HOME, and this is what made the cell environment-dependent.
        # The mirror above symlinked whatever `.plainkeep` the checkout happened to have. A
        # developer's checkout is registered (`script/setup` step 4b marks it), so the marker came
        # for free and the cell was green. A bare worktree or a `git archive` export has no
        # `.plainkeep`, the dispatcher refused with "not a plainkeep vault (no .plainkeep/vault.json)"
        # — exit 0, so the failure read as a dispatcher bug — and the cell went red for a reason that
        # has nothing to do with the venv probe it exists to check. Measured: red in this worktree,
        # green in the developer's registered checkout, on identical code. A fixture that needs a
        # marked root has to make one.
        from lib import vaultreg  # noqa: E402  (`lib` is bin/lib below the shuffle at the top)
        (broken_home / vaultreg.MARKER_DIR).mkdir(parents=True, exist_ok=True)
        vaultreg.marker_path(broken_home).write_text(
            vaultreg.marker_bytes(vaultreg.new_marker_doc()), encoding="utf-8")
        vbin = broken_home / ".venv" / "bin"
        vbin.mkdir(parents=True)
        (vbin / "python3").write_text("#!/nonexistent/interp\nnot a real python\n")
        os.chmod(vbin / "python3", 0o755)
        f1_env = {**os.environ, "PLAINKEEP_HOME": str(broken_home)}
        f1_env.pop("PLAINKEEP_SETUP_FAKE", None)
        f1 = subprocess.run([str(REPO / "plainkeep"), "help"], capture_output=True, text=True, env=f1_env)
        check("FIX 1: broken/unstartable .venv python → dispatcher falls back to bare python3, plainkeep runs",
              f1.returncode == 0 and "126" not in (f1.stderr[-40:] or ""),
              (f1.stderr + f1.stdout)[:400])

        # Verb surface: subprocess tests exercise the real command boundary.
        cli_home = make_home(tmp / "cli")
        dashboard = run_setup([], cli_home)
        check("setup dashboard exits ok", dashboard.returncode == 0, dashboard.stderr + dashboard.stdout)
        check("setup dashboard lists six layers",
              all(layer in dashboard.stdout for layer in ("skeleton", "search", "backups", "models", "automation", "ui")),
              dashboard.stdout)
        check("setup dashboard shows next commands", "plainkeep setup search --yes" in dashboard.stdout, dashboard.stdout)

        json_dash = run_setup(["--json"], cli_home)
        try:
            lines = [json.loads(ln) for ln in json_dash.stdout.splitlines() if ln.strip()]
            json_ok = json_dash.returncode == 0 and len(lines) == 7 and lines[0]["count"] == 6
        except Exception as e:
            lines = []
            json_ok = False
            json_dash.stderr += str(e)
        check("setup --json emits header plus six parseable layer rows", json_ok, json_dash.stdout + json_dash.stderr)
        check("setup --json rows expose public fields",
              bool(lines) and all({"id", "title", "status", "required", "detail", "items", "next"}.issubset(r) for r in lines[1:]),
              str(lines))

        gated_cli = run_setup(["search"], cli_home)
        check("setup search without yes exits confirm", gated_cli.returncode == 3, gated_cli.stderr + gated_cli.stdout)
        check("setup search confirm includes rerun hint",
              "re-run: plainkeep setup search --yes" in (gated_cli.stderr + gated_cli.stdout),
              gated_cli.stderr + gated_cli.stdout)

        gated_json = run_setup(["search", "--json"], cli_home)
        try:
            gated_env = json.loads(gated_json.stdout)
            gated_json_ok = (gated_json.returncode == 3
                             and gated_env["ok"] is False
                             and gated_env["verb"] == "setup"
                             and gated_env["error"]["code"] == 3
                             and gated_env["error"]["hint"] == "re-run: plainkeep setup search --yes")
        except Exception as e:
            gated_json_ok = False
            gated_json.stderr += str(e)
        check("setup search --json confirm emits stable error envelope", gated_json_ok,
              gated_json.stderr + gated_json.stdout)

        all_preview = run_setup(["--all"], cli_home)
        check("setup --all without yes exits confirm", all_preview.returncode == 3, all_preview.stderr + all_preview.stdout)
        check("setup --all names confirm layers", "search" in all_preview.stderr and "models" in all_preview.stderr,
              all_preview.stderr + all_preview.stdout)

        # Task 11 tty-guard: `--wizard` over a pipe (no tty, as here under capture) must NOT prompt —
        # exit 2 and print the exact non-interactive alternatives. `--wizard --json` likewise exits 2.
        wiz_notty = run_setup(["--wizard"], cli_home)
        check("setup --wizard without a tty exits usage (2)", wiz_notty.returncode == 2,
              wiz_notty.stderr + wiz_notty.stdout)
        check("setup --wizard non-tty names the non-interactive alternatives",
              "plainkeep setup --all --yes" in (wiz_notty.stderr + wiz_notty.stdout)
              and "plainkeep setup --json" in (wiz_notty.stderr + wiz_notty.stdout),
              wiz_notty.stderr + wiz_notty.stdout)
        wiz_json = run_setup(["--wizard", "--json"], cli_home)
        check("setup --wizard --json exits usage (2)", wiz_json.returncode == 2,
              wiz_json.stderr + wiz_json.stdout)
        wiz_dry = run_setup(["--wizard", "--dry-run"], cli_home)
        check("setup --wizard --dry-run exits usage (2)", wiz_dry.returncode == 2,
              wiz_dry.stderr + wiz_dry.stdout)

        backups_cli = run_setup(["backups", "--yes"], cli_home)
        check("setup backups --yes exits ok", backups_cli.returncode == 0, backups_cli.stderr + backups_cli.stdout)
        check("setup backups --yes reports handoff", "plainkeep backup init" in backups_cli.stdout,
              backups_cli.stderr + backups_cli.stdout)

        unknown_cli = run_setup(["bogus"], cli_home)
        check("setup unknown layer exits usage", unknown_cli.returncode == 2, unknown_cli.stderr + unknown_cli.stdout)
        check("setup unknown layer lists valid ids",
              all(layer in unknown_cli.stderr for layer in ("skeleton", "search", "backups", "models", "automation")),
              unknown_cli.stderr + unknown_cli.stdout)

        fake_yes = run_setup(["search", "--yes"], cli_home)
        check("setup fake search --yes records commands", fake_yes.returncode == 0 and "ollama pull" in fake_yes.stdout,
              fake_yes.stderr + fake_yes.stdout)

        # Deterministic action failure via the PLAINKEEP_SETUP_FORCE_FAIL seam (host-independent): the layer
        # raises inside advance() and the CLI must emit a clean exit-1 error envelope, no traceback.
        # (Previously this leaned on automation actually failing, which only happened on macOS — off
        # Darwin automation is not_applicable and would skip, so the failure path never ran on CI.)
        fail_json = run_setup(["automation", "--yes", "--json"], cli_home, fake=False,
                              extra_env={"PLAINKEEP_SETUP_FORCE_FAIL": "automation"})
        try:
            fail_env = json.loads(fail_json.stdout)
            fail_json_ok = (fail_json.returncode == 1
                            and fail_env["ok"] is False
                            and fail_env["verb"] == "setup"
                            and fail_env["error"]["code"] == 1
                            and "automation" in fail_env["error"]["message"]
                            and "Traceback" not in fail_json.stderr
                            and "Traceback" not in fail_json.stdout)
        except Exception as e:
            fail_json_ok = False
            fail_json.stderr += str(e)
        check("setup action failure --json emits error envelope without traceback", fail_json_ok,
              fail_json.stderr + fail_json.stdout)

        # --- Task 7a: --dry-run previews without --yes and writes/installs NOTHING. (fake=False here:
        # --dry-run must force the inert path on its own, not lean on PLAINKEEP_SETUP_FAKE.) ---
        dry_home = make_home(tmp / "dry")
        dry_one = run_setup(["search", "--dry-run", "--json"], dry_home, fake=False)
        try:
            d1 = [json.loads(ln) for ln in dry_one.stdout.splitlines() if ln.strip()]
            dry_one_ok = dry_one.returncode == 0 and d1 and d1[0]["data"].get("dry_run") is True
        except Exception as e:
            dry_one_ok = False
            dry_one.stderr += str(e)
        check("setup search --dry-run: ok envelope w/ dry_run, no --yes needed (confirm-class)",
              dry_one_ok, dry_one.stderr + dry_one.stdout)
        check("setup search --dry-run creates no .venv and no vault folders",
              not (dry_home / ".venv").exists() and not (dry_home / "wiki").exists())

        dry_all = run_setup(["--all", "--dry-run", "--json"], dry_home, fake=False)
        try:
            da = [json.loads(ln) for ln in dry_all.stdout.splitlines() if ln.strip()]
            dry_all_ok = dry_all.returncode == 0 and da and da[0]["data"].get("dry_run") is True
        except Exception as e:
            dry_all_ok = False
            dry_all.stderr += str(e)
        check("setup --all --dry-run: ok envelope w/ dry_run, no --yes needed", dry_all_ok,
              dry_all.stderr + dry_all.stdout)
        check("setup --all --dry-run renders no plists, pulls no model, creates no .venv",
              not (dry_home / ".venv").exists() and not (dry_home / "jobs" / "launchd").exists())

    print(f"\n{BOLD}Setup layer registry — {len(results)} checks{RESET}\n")
    passed = sum(1 for _, ok, _ in results if ok)
    for name, ok, detail in results:
        mark = f"{GREEN}PASS{RESET}" if ok else f"{RED}FAIL{RESET}"
        print(f"  {mark} {name:<66}" + (f" {DIM}{detail.strip()[:100]}{RESET}" if (detail and not ok) else ""))
    failed = len(results) - passed
    print(f"\n{BOLD}Result:{RESET} {GREEN}{passed} passed{RESET}, "
          f"{(RED if failed else DIM)}{failed} failed{RESET}, {len(results)} checks")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
