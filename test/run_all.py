#!/usr/bin/env python3
"""
run_all.py — run every OFFLINE suite (no LLM, no cost) and summarize.

Covers: guardrail (§5), jobs registry (§15), sweep decay machine (§9.4), wiki integrity (§10),
and searchability/vector analysis (§10.2). The LLM-operator simulation (run_simulation.py) is
separate because it needs a model and network.

Usage:  python3 test/run_all.py
"""
from __future__ import annotations
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
GREEN, RED, BOLD, DIM, RESET = "\033[32m", "\033[31m", "\033[1m", "\033[2m", "\033[1m\033[0m"

SUITES = [
    ("guardrail model (§5)", "run_deterministic.py"),
    ("guardrail enforcement (§5)", "run_guardrail.py"),
    ("path-wall on the write path (§5 Iron Law)", "run_pathwall.py"),
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
    ("trust wave: exit codes + update merge + doctor (0.1/0.3/0.4)", "run_trust.py"),
    ("machine contract: --json + plainkeep.json/3 + dry-run (1.1/1.2/0.5)", "run_json.py"),
    ("multi-root verb resolution: plugins + PLAINKEEP_PATH (2.1/0.2)", "run_resolver.py"),
    ("core-parity: TS<->Python resolver differential oracle (hybrid-core)", "run_core_parity.py"),
    ("core-fuzz: TS<->Python difflib + bool()/str() differential fuzz (hybrid-core)", "run_fuzz.py"),
    ("core-tui: in-core terminal UI on a real pty (hybrid-core)", "run_tui_pty.py"),
    ("frozen SDK + plainkeep plugin: api.py + trust ceiling (2.2/2.3)", "run_plugin.py"),
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


def main() -> int:
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
