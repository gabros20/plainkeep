#!/usr/bin/env python3
"""
plainkeep doctor [--init] — self-check (§14.4): tools, folder structure, manifest↔bin consistency, agent
adapters, no tracked secrets/binaries, index freshness. --init creates any missing skeleton folders.
Exit 1 if any check FAILs (WARN does not fail).
"""
import json
import os
import re
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lib import enginetree, guardrail, output, paths, pluginenv, provision, setuplib, vaultio  # noqa: E402
from lib.setuplib import REQUIRED_DIRS  # noqa: E402

GREEN, RED, YEL, DIM, RESET = "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[0m"
BIN = Path(__file__).resolve().parents[1]
checks = []  # (level, msg)


def _plugin_overlay() -> dict | None:
    """The lockfile's record of the dependency overlay, or None when this vault has no lockfile / has
    never synced. Never raises — doctor reports, it does not fail on a file it only reads."""
    try:
        lock = json.loads((paths.PLAINKEEP_HOME / "plugins" / "plugins.lock.json")
                          .read_text(encoding="utf-8"))
    except Exception:
        return None
    o = lock.get("overlay")
    return o if isinstance(o, dict) else None


def ok(m): checks.append(("ok", m))
def warn(m): checks.append(("warn", m))
def fail(m): checks.append(("fail", m))


def _real(p) -> str:
    try:
        return str(Path(p).resolve())
    except Exception:
        return str(p)


def _find_git_dirs(root: Path, cap: int = 500) -> list[str]:
    """`.git` dirs under `root`, without descending into `.git` or dependency trees (Part 0.4)."""
    found = []
    for dirpath, dirnames, _ in os.walk(root):
        if ".git" in dirnames:
            found.append(os.path.join(dirpath, ".git"))
        dirnames[:] = [d for d in dirnames
                       if d not in (".git", "node_modules", ".venv", "venv", "__pycache__")]
        if len(found) >= cap:
            break
    return found


def _fm_churn(path: Path) -> str:
    """Reason string if the note's YAML frontmatter is a shape Obsidian's Properties normalizer would
    rewrite (flow-vs-block lists, tab indentation, duplicate keys); '' if stable (Part 0.4)."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except Exception:
        return ""
    if not lines or lines[0].strip() != "---":
        return ""
    end = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
    if end is None:
        return "unterminated frontmatter"
    seen = set()
    for ln in lines[1:end]:
        if ln[:1] == "\t":
            return "tab indentation in frontmatter"
        m = re.match(r"^([A-Za-z0-9_\-]+):\s*(.*)$", ln)
        if not m:
            continue
        key, val = m.group(1), m.group(2).strip()
        if key in seen:
            return f"duplicate key '{key}'"
        seen.add(key)
        if val not in ("[]", "") and re.match(r"^\[.+\]$", val):
            return f"inline list '{key}' normalizes to a block list"
    return ""


def main(argv):
    _, argv = output.parse_argv(argv)
    init = "--init" in argv

    # 1. tools
    for t in ("git", "python3", "sqlite3"):
        (ok if shutil.which(t) else (warn if t == "sqlite3" else fail))(f"tool: {t} {'present' if shutil.which(t) else 'MISSING'}")
    vectors_requested = os.environ.get("PLAINKEEP_VECTORS", "").lower() in ("1", "true", "yes", "on")
    for mod, why in (("lancedb", "stage-2 vectors"), ("fastembed", "stage-3 rerank")):
        try:
            __import__(mod); ok(f"optional: {mod} present ({why})")
        except Exception:
            # A missing optional dep is normally fine — but if PLAINKEEP_VECTORS=1 and lancedb can't
            # import, `plainkeep index` will silently fall back to keyword-only. That mismatch is a
            # misconfiguration worth calling out loudly, with the exact remedy.
            if mod == "lancedb" and vectors_requested:
                warn("PLAINKEEP_VECTORS=1 but lancedb is NOT importable by this python3 (`which python3`) — "
                     "`plainkeep index` falls back to keyword-only. Fix: install requirements.txt into THIS "
                     "interpreter and put $PLAINKEEP_HOME/.venv/bin first on PATH (in an agent terminal, in "
                     "its shell init) — see docs/agent-terminal-search.md; or unset PLAINKEEP_VECTORS.")
            else:
                warn(f"optional: {mod} not installed ({why} disabled)")

    # image reading (docs/design/proposals/2026-07-06-image-reading.md): Layer 1 metadata (Pillow),
    # Layer 2/3 OCR+VLM runtimes (mlx-vlm on Apple Silicon, Ollama anywhere), deterministic OCR
    # fallbacks (ocrmac, tesseract). Every probe here is soft — the whole feature degrades gracefully.
    for mod, why in (("PIL", "image metadata (Pillow)"), ("mlx_vlm", "Apple-Silicon OCR/VLM runtime"),
                      ("ocrmac", "Apple Vision OCR fallback")):
        try:
            __import__(mod); ok(f"optional: {mod} present ({why})")
        except Exception:
            warn(f"optional: {mod} not installed ({why} disabled)")
    (ok if shutil.which("ollama") else warn)(
        "optional: ollama present (OCR/VLM runtime)" if shutil.which("ollama")
        else "optional: ollama not on PATH (OCR/VLM runtime disabled)")
    (ok if shutil.which("tesseract") else warn)(
        "optional: tesseract present (OCR fallback)" if shutil.which("tesseract")
        else "optional: tesseract not on PATH (OCR fallback disabled)")
    vlm_requested = os.environ.get("PLAINKEEP_VLM", "").strip().lower()
    if vlm_requested and vlm_requested != "none":
        try:
            __import__("mlx_vlm"); has_mlx_vlm = True
        except Exception:
            has_mlx_vlm = False
        if not has_mlx_vlm and not shutil.which("ollama"):
            warn(f"PLAINKEEP_VLM={os.environ['PLAINKEEP_VLM']} but neither mlx-vlm nor ollama is available — "
                 "`plainkeep files extract --describe` will skip Layer 3 entirely. Fix: install mlx-vlm "
                 "(Apple Silicon) or run ollama (any host); or unset PLAINKEEP_VLM.")

    # search enrichment (docs/design/proposals/2026-07-07-search-enrichment-pipeline.md): auto-wired
    # into `files extract`/`bookmark` unless PLAINKEEP_ENRICH=off, so a soft probe here mirrors PLAINKEEP_VLM's.
    enrich_requested = os.environ.get("PLAINKEEP_ENRICH", "").strip().lower()
    if enrich_requested != "off":
        try:
            from lib import enrichlib
            enrich_available = enrichlib.available()
        except Exception:
            enrich_available = False
        if enrich_available:
            ok(f"enrich model reachable ({enrichlib.DEFAULT_MODEL} via Ollama)")
        else:
            warn("enrich model/Ollama not reachable — `files extract`/`bookmark` auto-enrich will "
                 "fall back to the stdlib keyword floor (no description, no LLM keywords). Fix: run "
                 "ollama and pull the configured PLAINKEEP_ENRICH_MODEL; or set PLAINKEEP_ENRICH=off.")

    # 1b. the ENGINE tree (Phase 2 Task 2). The full ownership manifest, not the four-file probe the
    # dispatcher runs on every invocation: doctor is where an operator asks "is my install intact",
    # and a manifest that only the installer ever consults is a manifest that goes stale between
    # installs. It reports the tree it is RUNNING FROM (`enginetree.ENGINE_ROOT`, code-relative), not
    # whatever `current` points at — the question worth answering is about the engine that just ran.
    engine_problems = enginetree.verify(paths.ENGINE)
    if engine_problems:
        for p in engine_problems:
            fail(f"engine: {p}")
    else:
        # "complete and sealed", never "intact": `verify()` reads the manifest and the modes, not the
        # contents, so this row says every owned path is present and none of them is writable. It does
        # NOT say the code is the code that was installed — an edit that restores the mode leaves no
        # trace a mode check can see (`enginetree.seal_problems` carries the full statement). The row
        # is worded to promise exactly what was measured, because an operator reads it as assurance.
        ok(f"engine: complete and sealed tree at {paths.ENGINE}")
    # This row reports the rule that is ENFORCED, by calling the same function the enforcement calls.
    # It used to compare equality only, which is a weaker spelling: `disjointness_verdict` (ADR-017
    # D5) covers THREE shapes — identical, data inside engine, engine inside data — and the two
    # containment shapes would have printed "disjoint" here. A containing pair is refused at exit 5
    # in `vaultroot.validate()` before doctor can run, so the row is advisory today; an advisory row
    # that disagrees with the enforced rule is exactly how the two drift apart.
    overlap = enginetree.disjointness_verdict(str(paths.PLAINKEEP_HOME), paths.ENGINE)
    if overlap:
        fail(f"engine: the vault and the engine tree are not disjoint — {overlap}")
    else:
        ok("engine: disjoint from the vault (data is data, code is code)")

    # 1c. PROVISIONING (Phase 2 Task 4 / ADR-020). Three rows, and each of them exists because an
    # operator has a different next move:
    #
    #   * the pinned uv — absent means `plainkeep setup` will try to download it, which needs network;
    #   * the engine interpreter — absent means the engine has never been synced;
    #   * a SYSTEM uv — present means the operator has one and might reasonably expect it to be used.
    #     It is NOT. This row is the whole of the "one line saying so" the pin's design allows: a
    #     silently-ignored tool that the operator installed on purpose is a support question waiting
    #     to happen, and answering it here is cheaper than answering it later.
    #
    # None of them can FAIL doctor, and none is even a WARN: an unprovisioned engine is a NORMAL
    # state (the stdlib floor is the contract — ADR-009), not a broken one. They ride in the `ok`
    # bucket and are worded so that reading one as assurance says only what was measured.
    try:
        uv = provision.uv_path(paths.ENGINE)
        if uv.is_file():
            ok(f"engine: pinned uv {provision.load_pin(paths.ENGINE)['version']} present")
        else:
            ok(f"engine: pinned uv not provisioned yet ({uv}) — `plainkeep setup` fetches it")
        epy = provision.engine_python(paths.ENGINE)
        if epy:
            # The COUNT, not the list: it is the cheap signal that distinguishes "synced with no
            # extras" (the base project declares nothing, so zero is correct and expected) from
            # "synced with [search]" — and it is read from the `.dist-info` directories on disk
            # rather than by running pip, which a uv-managed environment does not have.
            n = len(provision.installed_dists(paths.ENGINE))
            ok(f"engine: interpreter {epy} ({n} distribution(s) from the delivered lock)")
        else:
            ok("engine: no provisioned interpreter yet (the stdlib floor is the contract)")
        sysuv = provision.system_uv()
        if sysuv:
            ok(f"engine: a system uv at {sysuv} is IGNORED — the engine runs its own pinned uv "
               "(ADR-020 D3), so your uv's version and config never steer this resolution")
    except Exception as exc:                 # a broken pin must not take doctor down with it
        warn(f"engine: cannot read the uv pin ({exc})")

    # THE PRECEDENCE INVERSION (Phase 2 Task 3, ADR-018 D2). The SDK reaches a plugin on PYTHONPATH
    # now, and PYTHONPATH loses to `sys.path[0]` — the verb's own directory. A pack shipping a
    # top-level `lib` beside its run.py therefore imports ITS OWN, where the pre-Task-2 scaffold's
    # `sys.path.insert(0, …)` made the engine win. WARN, not FAIL: shipping a `lib` is legal, it is
    # only the silence that is not. This is the one shape the migration cannot fix for the user.
    shadows = pluginenv.sdk_shadows(paths.PLAINKEEP_HOME)
    if shadows:
        warn("plugins: " + ", ".join(f"{p}/{v}" for p, v in shadows)
             + " ship a top-level `lib` beside run.py — it SHADOWS the plainkeep SDK for that verb "
               "(sys.path[0] is the verb's own directory and beats PYTHONPATH)")
    else:
        ok("plugins: no pack shadows the SDK with a top-level `lib`")

    # The dependency overlay was built for ONE interpreter (`pip install --target` can carry compiled
    # extensions). It survives an ENGINE update by construction — it lives in the vault, and both
    # dispatchers prepend it per spawn — so the only thing that can invalidate it is the interpreter
    # moving underneath it, which is what this row watches.
    overlay = _plugin_overlay()
    if overlay is not None:
        want = f"{sys.version_info.major}.{sys.version_info.minor}"
        if overlay.get("python") and overlay["python"] != want:
            warn(f"plugins: the dependency overlay was installed for python {overlay['python']}, "
                 f"this interpreter is {want} — re-run `plainkeep plugin sync --yes`")
        else:
            ok(f"plugins: dependency overlay matches this interpreter (python {want})")

    # 2. folders
    for d in REQUIRED_DIRS:
        p = paths.PLAINKEEP_HOME / d
        if p.is_dir():
            ok(f"folder: {d}/")
        elif init:
            vaultio.mkdir(p)
            (p / ".gitkeep").touch()
            ok(f"folder: {d}/ (created)")
        else:
            fail(f"folder: {d}/ MISSING (run: plainkeep doctor --init)")

    # 2a. Obsidian config pack (Part 3.1): with --init, seed .obsidian/ from templates/obsidian/.
    # .obsidian/ is USER-OWNED (never in engine.txt); refuse-don't-overwrite keeps a customized
    # config safe. URIs open, writes go through verbs — the pack only sets link/attachment policy.
    # This runs BEFORE the setup-layer status below so --init seeds .obsidian/ (and the skeleton dirs
    # above) before that status is computed and reported (checking first made every first
    # `doctor --init` on a fresh clone fail itself).
    pack = paths.PLAINKEEP_HOME / "templates" / "obsidian"
    dest = paths.PLAINKEEP_HOME / ".obsidian"
    if pack.is_dir():
        srcs = sorted(pack.glob("*.json"))
        if init:
            copied = kept = 0
            for s in srcs:
                d = dest / s.name
                if d.exists():
                    kept += 1
                    continue
                vaultio.mkdir(d.parent)
                vaultio.write_text(d, s.read_text(encoding="utf-8"), encoding="utf-8")
                copied += 1
            ok(f"obsidian: config pack (.obsidian/: {copied} copied, {kept} kept)")
        elif not (dest / "app.json").exists():
            warn("obsidian: config pack not installed (run: plainkeep doctor --init to seed .obsidian/)")
        else:
            ok("obsidian: .obsidian/ config present")

    # 2b. setup layers: required layers gate doctor; optional layers are advisory. The skeleton
    # layer's readiness depends only on REQUIRED_DIRS (the .obsidian/* items are advisory), so a
    # not-yet-seeded config pack never fails doctor.
    for r in setuplib.status():
        msg = f"setup layer: {r['id']} {r['status']} - {r['detail']}"
        if r.get("next"):
            msg += f" (next: {r['next']})"
        if r["status"] == "ready":
            ok(msg)
        elif r["status"] == "not_applicable":
            # This host can't run the layer (e.g. launchd off macOS) — informational, never a problem
            # and never a FAIL, even were the layer required (Task 8).
            ok(msg)
        elif r.get("required"):
            fail(msg)
        else:
            warn(msg)

    # 3. manifest <-> bin  (hidden verbs, e.g. __complete, are intentionally absent from plainkeep.json)
    def _hidden(v):
        f = BIN / v / "cmd.json"
        try:
            return bool(json.loads(f.read_text(encoding="utf-8")).get("hidden"))
        except Exception:
            return False
    verbs_on_disk = {p.parent.name for p in BIN.glob("*/run.py") if not _hidden(p.parent.name)}
    for v in sorted(verbs_on_disk):
        if not (BIN / v / "cmd.json").exists():
            warn(f"verb '{v}' has run.py but no cmd.json (won't show in plainkeep help)")
    manifest_json = paths.PLAINKEEP_HOME / "plainkeep.json"
    if manifest_json.exists():
        try:
            man = {c["verb"] for c in json.loads(manifest_json.read_text())["verbs"]}
            missing = verbs_on_disk - man
            ok("plainkeep.json parses and matches bin/" if not missing else f"plainkeep.json stale: missing {missing} (run: plainkeep help)")
            if missing:
                warn(f"plainkeep.json missing verbs: {missing}")
        except Exception as e:
            fail(f"plainkeep.json does not parse: {e}")
    else:
        warn("plainkeep.json not generated yet (run: plainkeep help)")

    # 4. agent adapters
    # The SKILL is engine-owned (Phase 2 Task 2) — it documents the tool, not the notes — so it is
    # read from the engine tree. AGENTS.md/CLAUDE.md stay vault-owned: they are that vault's own
    # instructions to its agents.
    agents, claude, skill = (paths.PLAINKEEP_HOME / "AGENTS.md", paths.PLAINKEEP_HOME / "CLAUDE.md",
                             paths.SKILLS / "operate-plainkeep" / "SKILL.md")
    (ok if agents.exists() else fail)(f"adapter: AGENTS.md {'present' if agents.exists() else 'MISSING'}")
    (ok if (claude.exists() and "@AGENTS.md" in claude.read_text()) else fail)(
        "adapter: CLAUDE.md bridges @AGENTS.md" if claude.exists() else "adapter: CLAUDE.md MISSING")
    (ok if skill.exists() else fail)(f"adapter: skills/operate-plainkeep/SKILL.md {'present' if skill.exists() else 'MISSING'}")

    # per-agent adapters: warn if absent (a vault may not use every agent), FAIL if present-but-broken
    cdx_cfg, cdx_sk = paths.PLAINKEEP_HOME / ".codex" / "config.toml", paths.PLAINKEEP_HOME / ".codex" / "skills"
    if cdx_cfg.exists() or cdx_sk.is_symlink() or cdx_sk.exists():
        (ok if cdx_cfg.exists() else warn)(".codex/config.toml" + ("" if cdx_cfg.exists() else " MISSING"))
        (ok if (cdx_sk / "operate-plainkeep").exists() else fail)(".codex/skills → skills/ resolves"
            if (cdx_sk / "operate-plainkeep").exists() else ".codex/skills symlink is BROKEN")
    else:
        warn(".codex/ adapter not set up (Codex & Grok read AGENTS.md natively regardless)")
    cl_set, cl_sk = paths.PLAINKEEP_HOME / ".claude" / "settings.json", paths.PLAINKEEP_HOME / ".claude" / "skills"
    if cl_set.exists() or cl_sk.is_symlink() or cl_sk.exists():
        try:
            json.loads(cl_set.read_text()); ok(".claude/settings.json parses")
        except FileNotFoundError:
            warn(".claude/settings.json MISSING")
        except Exception as e:
            fail(f".claude/settings.json is INVALID json: {e}")
        (ok if (cl_sk / "operate-plainkeep").exists() else fail)(".claude/skills → skills/ resolves"
            if (cl_sk / "operate-plainkeep").exists() else ".claude/skills symlink is BROKEN")
    else:
        warn(".claude/ adapter not set up (add it to lock Claude Code to the plainkeep surface)")

    # 5. no tracked secrets / binaries in wiki
    tracked = paths.git("ls-files").splitlines()
    if tracked:
        env = [t for t in tracked if Path(t).name.startswith(".env") or t.endswith(".env")]
        (fail if env else ok)(f"no tracked .env files" if not env else f"SECRET RISK: tracked {env}")
        # .canvas (JSON Canvas) and .base (Obsidian Bases) are plaintext view layers plainkeep emits (Part 3)
        wiki_bin = [t for t in tracked if t.startswith("wiki/")
                    and not t.endswith((".md", ".gitkeep", ".canvas", ".base"))]
        (fail if wiki_bin else ok)("wiki is plaintext-only" if not wiki_bin else f"binaries tracked in wiki: {wiki_bin}")
    else:
        warn("not a git repo (or nothing tracked) — skipped secret/binary scan")

    # 6. index
    db = paths.PLAINKEEP_HOME / ".index" / "plainkeep.sqlite"
    (ok if db.exists() else warn)("index built" if db.exists() else "index not built (run: plainkeep index)")

    # 7. sync-wall (Part 0.4): a .git tree must NEVER live under iCloud/Dropbox/Syncthing
    home_real = _real(paths.PLAINKEEP_HOME)
    if guardrail.under_sync_dir(home_real):
        fail(f"~/plainkeep resolves under a cloud-sync tree ({home_real}) — never sync a .git (data loss)")
    else:
        ok("~/plainkeep is not under a cloud-sync tree")
    if paths.WORK_ROOT.is_dir():
        synced = [g for g in _find_git_dirs(paths.WORK_ROOT) if guardrail.under_sync_dir(_real(g))]
        (fail if synced else ok)(
            f"{len(synced)} work repo .git under a cloud-sync tree: {synced[:3]}" if synced
            else "no ~/work repo .git is under a cloud-sync tree")

    # 7b. durability: prefer >=2 push remotes on ~/plainkeep (a second off-machine mirror)
    remotes = paths.git("remote", "-v")
    if remotes.strip():
        push = {}
        for ln in remotes.splitlines():
            parts = ln.split()
            if len(parts) >= 3 and parts[2] == "(push)" and "DISABLED" not in parts[1]:
                push[parts[0]] = parts[1]
        (ok if len(push) >= 2 else warn)(
            f"{len(push)} push remote(s) configured" if len(push) >= 2
            else f"only {len(push)} push remote(s) — add a second off-machine mirror for durability")

    # 8. frontmatter round-trip: notes Obsidian's Properties normalizer would churn (Part 0.4)
    wiki = paths.PLAINKEEP_HOME / "wiki"
    if wiki.is_dir():
        churn = []
        for md in wiki.rglob("*.md"):
            why = _fm_churn(md)
            if why:
                churn.append(f"{md.relative_to(paths.PLAINKEEP_HOME)}: {why}")
            if len(churn) >= 50:
                break
        if churn:
            warn(f"{len(churn)} note(s) whose frontmatter Obsidian Properties would rewrite:")
            for c in churn[:5]:
                warn(f"  churn: {c}")
        else:
            ok("frontmatter is Obsidian-Properties stable")

    # 9. Obsidian lints (Part 3.1): tags lowercase-hyphenated + wikilinks in the body, not frontmatter
    if wiki.is_dir():
        tag_re = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
        bad_tags, fm_links = [], []
        for md in wiki.rglob("*.md"):
            try:
                text = md.read_text(encoding="utf-8")
            except Exception:
                continue
            rel = md.relative_to(paths.PLAINKEEP_HOME)
            for tg in paths.fm_list(text, "tags"):
                if tg and not tag_re.match(tg):
                    bad_tags.append(f"{rel}: '{tg}'")
            # derived_from/source are sanctioned machine pointers (Part 4.3) — a wikilink there is by
            # design (it wires derived->source in the graph), so it is exempt from the body-only lint.
            if any("[[" in ln and not re.match(r"^\s*(derived_from|source):", ln)
                   for ln in paths._fm_block(text)):
                fm_links.append(str(rel))
        if bad_tags:
            warn(f"{len(bad_tags)} tag(s) not lowercase-hyphenated (Obsidian treats #Tag != #tag):")
            for b in bad_tags[:5]:
                warn(f"  tag: {b}")
        else:
            ok("frontmatter tags are lowercase-hyphenated")
        if fm_links:
            warn(f"{len(fm_links)} note(s) with [[wikilinks]] in frontmatter (Obsidian renders them only in the body):")
            for b in fm_links[:5]:
                warn(f"  fm-link: {b}")
        else:
            ok("wikilinks are in note bodies only")

    # 10. provenance planes (Part 4.3): the three note planes must stay well-formed — a derived note
    # points to its source (derived_from + source_sha256/tool) and lives beside its shadow in
    # wiki/files/; an agent concept note carries a status gate. Violations are WARN (surfaced, never
    # a hard fail) — this is the poisoned-memory antidote made checkable.
    files_dir = paths.PLAINKEEP_HOME / "wiki" / "files"
    if wiki.is_dir():
        prov = []
        for md in wiki.rglob("*.md"):
            try:
                fm = paths.frontmatter(md)
            except Exception:
                continue
            rel = md.relative_to(paths.PLAINKEEP_HOME)
            derived = fm.get("derived_from")
            if (fm.get("tool") or fm.get("source_sha256")) and not derived:
                prov.append(f"{rel}: has tool:/source_sha256 but no derived_from (a derived note must point to its source)")
            if fm.get("author") == "agent" and not fm.get("status"):
                prov.append(f"{rel}: author: agent without a status: (agent notes need a draft gate)")
            if derived and md.parent != files_dir:
                prov.append(f"{rel}: derived note outside wiki/files/ (derived material lives beside its shadow)")
        if prov:
            warn(f"{len(prov)} provenance-plane violation(s) (Part 4.3):")
            for p in prov[:5]:
                warn(f"  provenance: {p}")
        else:
            ok("provenance planes consistent (derived/agent notes well-formed)")

    # 11. jobs registry (Part 4.4): only read/safe_write verbs may be scheduled — a confirm/deny-class
    # verb in the registry is a data-loss/transmit risk (e.g. `plainkeep organize apply` must NEVER be
    # scheduled; the weekly `plainkeep organize scan` is safe_write and fine). WARN-only.
    reg = paths.PLAINKEEP_HOME / "jobs" / "registry.json"
    if reg.exists():
        try:
            jobs = json.loads(reg.read_text(encoding="utf-8")).get("jobs", {})
        except Exception:
            jobs = {}
        offenders = []
        for name, job in jobs.items():
            toks = str(job.get("command", "")).split()
            verb = toks[1] if len(toks) > 1 and toks[0] == "plainkeep" else None
            vr = guardrail.risk_of(verb) if verb else None
            if job.get("risk") not in ("read", "safe_write") or vr in ("confirm", "deny"):
                offenders.append(f"{name} ({job.get('command')})")
        (warn if offenders else ok)(
            f"{len(offenders)} confirm/deny-class job(s) scheduled (must never be): {offenders}" if offenders
            else "jobs registry schedules only read/safe_write verbs")

    nfail = sum(1 for lv, _ in checks if lv == "fail")
    nwarn = sum(1 for lv, _ in checks if lv == "warn")
    rows = [{"level": lv, "message": m} for lv, m in checks]

    def render(_):
        for lv, m in checks:
            c = {"ok": GREEN + "ok  ", "warn": YEL + "warn", "fail": RED + "FAIL"}[lv]
            print(f"  {c}{RESET} {m}")
        print(f"\ndoctor: {len(checks)-nfail-nwarn} ok, {nwarn} warn, {nfail} fail")

    output.emit_rows(rows, "doctor", human=render,
                     header={"passed": len(checks) - nfail - nwarn, "warn": nwarn, "fail": nfail})
    return 1 if nfail else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
