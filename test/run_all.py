#!/usr/bin/env python3
"""
run_all.py — run every OFFLINE suite (no LLM, no cost) and summarize.

Covers: guardrail (§5), jobs registry (§15), sweep decay machine (§9.4), wiki integrity (§10),
and searchability/vector analysis (§10.2). The LLM-operator simulation (run_simulation.py) is
separate because it needs a model and network.

Usage:  python3 test/run_all.py
"""
from __future__ import annotations
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
GREEN, RED, BOLD, DIM, RESET = "\033[32m", "\033[31m", "\033[1m", "\033[2m", "\033[1m\033[0m"

SUITES = [
    ("guardrail model (§5)", "run_deterministic.py"),
    ("guardrail enforcement (§5)", "run_guardrail.py"),
    ("path-wall on the write path (§5 Iron Law)", "run_pathwall.py"),
    ("append-only originals: ~/files/**/in/ (Phase 2 Task 1c)", "run_originals.py"),
    ("vault marker + registry (ADR-014 Task 1a)", "run_vault.py"),
    ("vault discovery: selection + refusals (ADR-014 Task 1b)", "run_discovery.py"),
    ("jobs registry (§15)", "run_jobs.py"),
    ("jobs scheduler verb (§15)", "run_jobverb.py"),
    ("sweep decay model (§9.4)", "run_sweep.py"),
    ("sweep verb (§9.4)", "run_sweepverb.py"),
    ("wiki integrity (§10)", "run_wiki.py"),
    ("wiki link edges (§10)", "run_wiki_edges.py"),
    ("state & consistency (§7/§8/§10/§14)", "run_state.py"),
    ("searchability model (§10.2)", "run_search.py"),
    ("stage-1 search impl (§10.2)", "run_search_impl.py"),
    ("stage-2 hybrid LanceDB (§10.2)", "run_search_vec.py"),
    ("daily-driver verbs (§4/§7)", "run_verbs.py"),
    ("triage flow (§4/§10)", "run_triage.py"),
    ("agent indirection (§6)", "run_agentlib.py"),
    ("daily/weekly rhythm (§16)", "run_loops.py"),
    ("health verbs: doctor, wiki (§14/§10)", "run_health.py"),
    ("terminal ergonomics: completion + renderer", "run_completion.py"),
    ("note types + bookmarks (issue #1 D+F)", "run_notetypes.py"),
    ("maintenance: backup, consolidate (§14/§15)", "run_maintenance.py"),
    ("backup family + share (5.1/5.2)", "run_backup_share.py"),
    ("setup/update flow (§2/§17)", "run_setup.py"),
    ("engine installer: enginetree (ADR-014 Phase 2 Task 2)", "run_enginetree.py"),
    ("provisioning: uv bootstrap + delivered lock + frozen matrix (ADR-020 Phase 2 Task 4)",
     "run_provision.py"),
    ("trust wave: exit codes + update merge + doctor (0.1/0.3/0.4)", "run_trust.py"),
    ("machine contract: --json + plainkeep.json/3 + dry-run (1.1/1.2/0.5)", "run_json.py"),
    ("multi-root verb resolution: plugins + PLAINKEEP_PATH (2.1/0.2)", "run_resolver.py"),
    ("core-parity: TS<->Python resolver differential oracle (hybrid-core)", "run_core_parity.py"),
    ("core-fuzz: TS<->Python difflib + bool()/str() differential fuzz (hybrid-core)", "run_fuzz.py"),
    ("core-tui: in-core terminal UI on a real pty (hybrid-core)", "run_tui_pty.py"),
    ("frozen SDK + plainkeep plugin: api.py + trust ceiling (2.2/2.3)", "run_plugin.py"),
    ("plugin SDK compatibility + dependency contract (ADR-018 Phase 2 Task 3)", "run_pluginsdk.py"),
    ("agent transport: plainkeep mcp stdio server (2.4)", "run_mcp.py"),
    ("core-mcp: in-core MCP server ↔ bin/mcp/run.py protocol differential (hybrid-core)",
     "run_mcp_protocol.py"),
    ("obsidian frontend zero + canvas/bases (3.1/3.2)", "run_obsidian.py"),
    ("terminal ergonomics + raycast: open/orient/search (3.3/3.4)", "run_terminal.py"),
    ("install funnel: script/get (5.4)", "run_get.py"),
    ("tiered extraction + provenance planes (4.1/4.3)", "run_extract.py"),
    ("distill + self-organization loop (4.2/4.4)", "run_organize.py"),
    ("new: scaffold project/client (§4/§12)", "run_new.py"),
    ("repo: ~/work fleet (§4/§12)", "run_repo.py"),
    ("files: binary-assets plane (§9)", "run_files.py"),
    ("archive + invoice (§4)", "run_archive_invoice.py"),
    ("image-reading backend (imagelib)", "run_image_backend.py"),
    ("files: imagelib wiring (metadata + OCR + describe)", "run_files_image.py"),
    ("search enrichment engine", "run_enrich.py"),
    ("plainkeep models verb", "run_models.py"),
    ("setup layers verb", "run_setup_layers.py"),
    ("enrichment pipeline wiring", "run_enrich_pipeline.py"),
    ("ui release gates: version three-way + bun floor (Phase 2 Task 7)", "run_uirelease.py"),
]


# A suite may print `SUITE-NOTE: <text>` lines to say what it did NOT cover — a filtered run, a
# deliberately gated case. Without this, such a run reaches the summary below as an unqualified PASS,
# because a suite is reduced to its EXIT STATUS here and nothing else: the qualification scrolls past
# hundreds of lines earlier and the line a reader actually reads says PASS (core-parity quality review
# r2, N2). A note NEVER changes a verdict — it travels with it.
SUITE_NOTE_PREFIX = "SUITE-NOTE:"


def _suite_notes(out: str) -> list[str]:
    return [line.split(SUITE_NOTE_PREFIX, 1)[1].strip()
            for line in out.splitlines() if line.startswith(SUITE_NOTE_PREFIX)]


# --- the hermeticity gate -------------------------------------------------------------------------
# Every suite must call `lib.hermetic.seal()` (read that module for why). This gate is here rather
# than in the seal itself because the failure it prevents is a suite that never calls it: since Task
# 1b, a suite invoking plainkeep with no PLAINKEEP_HOME resolves through the marker walk-up and the
# registry default to the developer's REAL vault, and writes into it with exit 0. Nothing about that
# run looks wrong.
#
# The gate is STATIC, and deliberately so: it catches the next suite someone adds, on the run that
# adds it, instead of on the run where it silently files a note into real notes. It runs BEFORE any
# suite, and it is fatal — a batch that skipped it would be a batch whose greenness means less.
#
# It does NOT make a direct `python3 test/run_foo.py` hermetic; the seal in the suite does that, and
# that is the point of putting it there. This only proves the seal is present everywhere.
#
# Matched by pattern rather than by an exact line, because a suite may legitimately import more from
# the module than `seal` (`scratch_root` is the other lever) — a gate that fails on the import LIST
# would be teaching people to write the import a particular way instead of to be hermetic.
SEAL_CALL = "seal()"
SEAL_IMPORT = "from lib.hermetic import seal"
SEAL_IMPORT_RE = re.compile(r"^from lib\.hermetic import .*\bseal\b", re.M)
SEAL_CALL_RE = re.compile(r"^seal\(\)", re.M)


def _unsealed() -> list[str]:
    """The suites in SUITES that do not carry the seal.

    Iterates the list that actually RUNS, not `HERE.glob("run_*.py")`. The glob answered a
    neighbouring question and got both directions wrong: a suite registered in SUITES under any
    other name was never checked (so the gate could pass while the batch ran an unsealed suite), and
    a `run_*.py` helper that is not a suite would have failed the whole batch for not being one.
    Neither case exists today; the point of a gate is the case that does not exist yet."""
    out = []
    for _, script in SUITES:
        f = HERE / script
        if not f.is_file():
            out.append(f"{script} (listed in SUITES but not on disk)")
            continue
        src = f.read_text(encoding="utf-8")
        if not SEAL_IMPORT_RE.search(src) or not SEAL_CALL_RE.search(src):
            out.append(script)
    return out


def main() -> int:
    unsealed = _unsealed()
    if unsealed:
        print(f"{BOLD}{RED}NOT HERMETIC\033[0m — these suites never call lib.hermetic.seal(), so a "
              "plainkeep invocation in them with no PLAINKEEP_HOME resolves to the developer's REAL "
              "vault:")
        for name in unsealed:
            print(f"  - {name}")
        print("\nAdd, after the imports:\n"
              f"    {SEAL_IMPORT}\n"
              f"    {SEAL_CALL}   # hermetic: an empty throwaway registry, never the developer's "
              "real vault\n")
        return 1

    print(f"\033[1m{'='*60}\nOffline design-validation suites\n{'='*60}\033[0m\n")
    statuses = []
    for label, script in SUITES:
        proc = subprocess.run([sys.executable, str(HERE / script)], capture_output=True, text=True)
        sys.stdout.write(proc.stdout)
        if proc.stderr:
            sys.stdout.write(proc.stderr)
        ok = proc.returncode == 0
        statuses.append((label, ok, _suite_notes(proc.stdout)))
        print(f"\033[2m{'-'*60}\033[0m\n")

    print("\033[1mSUMMARY\033[0m")
    allok = True
    for label, ok, notes in statuses:
        allok = allok and ok
        mark = f"{GREEN}PASS\033[0m" if ok else f"{RED}FAIL\033[0m"
        print(f"  {mark}  {label}")
        for note in notes:
            print(f"        \033[33m! {note}\033[0m")
    print()
    return 0 if allok else 1


if __name__ == "__main__":
    raise SystemExit(main())
