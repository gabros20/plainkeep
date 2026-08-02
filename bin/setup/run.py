#!/usr/bin/env python3
"""Layered installer for the local plainkeep system."""
from __future__ import annotations
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lib import output, setuplib  # noqa: E402

GLYPHS = {
    "ready": "✓",
    "partial": "◐",
    "absent": "○",
    "blocked": "!",
    "not_applicable": "—",
}
AUTO_LAYERS = ("skeleton", "search", "models", "automation", "ui")
# Statuses `--all` neither attempts nor counts as a failure (Task 8): already done, gated on a missing
# prerequisite, or not applicable to this host.
SKIP_STATUSES = ("ready", "blocked", "not_applicable")


def _valid_ids() -> list[str]:
    return [layer.id for layer in setuplib.LAYERS]


def _fake() -> bool:
    return (os.environ.get("PLAINKEEP_SETUP_FAKE") or "").strip().lower() in ("1", "true", "yes", "on")


def _dashboard_rows() -> list[dict]:
    # setuplib is the single source of truth for each layer's `next` remediation string; the
    # dashboard surfaces it verbatim rather than re-deriving a divergent one.
    return setuplib.status()


def _render_dashboard(rows: list[dict]) -> str:
    lines = ["setup layers:"]
    for row in rows:
        glyph = GLYPHS.get(row["status"], "?")
        required = "required" if row["required"] else "optional"
        next_cmd = row.get("next") or "-"
        lines.append(f"  {glyph} {row['id']:<10} {row['title']:<24} {row['status']:<8} {required:<8} {row['detail']}")
        lines.append(f"    next: {next_cmd}")
    return "\n".join(lines)


def _valid_or_fail(layer_id: str) -> None:
    ids = _valid_ids()
    if layer_id not in ids:
        output.fail(output.EXIT_USAGE,
                    f"unknown setup layer '{layer_id}' (valid ids: {', '.join(ids)})",
                    verb="setup")


def _confirm_message(layer_id: str) -> str:
    """What the operator is agreeing to, at the moment they are asked (exit 3).

    `models` gets its own text because it is the one layer that does TWO things of very different
    size, and the generic line ("installs downloads and local dependencies") hid the expensive half:
    `plainkeep models pull --all` is gigabytes of Ollama weights, while the `[models]` pip extra is
    tens of megabytes of wheels. Naming both is the alternative to widening the extra until its name
    is true — which is how packaging silently becomes a downloader (Phase 2 Task 4c).

    The lines come from `setuplib.MODELS_HALVES`, so the prompt and the `--json` payload below are the
    same statement rather than two descriptions that can drift."""
    if layer_id == "models":
        return ("models does TWO things:\n  " + "\n  ".join(setuplib.MODELS_HALVES))
    return f"{layer_id} installs downloads and local dependencies"


def _action_failed(layer_id: str, exc: Exception) -> None:
    layer = next(layer for layer in setuplib.LAYERS if layer.id == layer_id)
    yes = " --yes" if layer.gate == "confirm" else ""
    hint = f"fix the reported setup prerequisite, then re-run: plainkeep setup {layer_id}{yes}"
    if isinstance(exc, subprocess.CalledProcessError):
        cmd = " ".join(str(part) for part in (exc.cmd if isinstance(exc.cmd, (list, tuple)) else [exc.cmd]))
        output.fail(output.EXIT_UNEXPECTED,
                    f"setup layer '{layer_id}' failed while running: {cmd} (exit {exc.returncode})",
                    hint=hint, verb="setup")
    output.fail(output.EXIT_UNEXPECTED, f"setup layer '{layer_id}' failed: {exc}", hint=hint, verb="setup")


def _render_result(layer_id: str, before: dict, res: dict, dry: bool = False) -> str:
    verb = "would run" if dry else "ran"
    lines = []
    if dry:
        lines.append(f"{layer_id}: dry run (nothing installed/written)")
    if before["status"] == "ready":
        lines.append(f"{layer_id}: already ready")
    elif res["confirm_needed"]:
        lines.append(f"{layer_id}: needs confirmation")
    elif res["ran"]:
        lines.append(f"{layer_id}: {'would advance' if dry else 'advanced'}")
        for cmd in res["ran"]:
            lines.append(f"  {verb}: {cmd}")
    elif res["handoff"]:
        lines.append(f"{layer_id}: handoff required")
    else:
        lines.append(f"{layer_id}: no changes")
    for item in res["handoff"]:
        lines.append(f"  handoff: {item}")
    return "\n".join(lines)


def _advance_one(layer_id: str, *, yes: bool, dry: bool = False) -> int:
    _valid_or_fail(layer_id)
    before = setuplib.status(layer_id)[0]
    layer = next(layer for layer in setuplib.LAYERS if layer.id == layer_id)
    # A --dry-run is a READ (the guardrail already downgrades it): it previews the plan with fake=True
    # and NEVER requires --yes, even for a confirm-class layer (Task 7a). The confirm gate fires ONLY
    # for a genuinely-attemptable layer (FIX 4): a blocked/not_applicable (or already-ready) layer is
    # skipped, not installed, so demanding --yes for it would be a spurious exit 3.
    if not dry and before["status"] not in SKIP_STATUSES and layer.gate == "confirm" and not yes:
        output.fail(output.EXIT_CONFIRM, _confirm_message(layer_id),
                    hint=f"re-run: plainkeep setup {layer_id} --yes", verb="setup")
    try:
        res = setuplib.advance(layer_id, yes=(yes or dry), fake=(_fake() or dry))
    except (subprocess.CalledProcessError, FileNotFoundError, OSError) as exc:
        _action_failed(layer_id, exc)
    payload = {**res, "layer": layer_id, "status": before["status"]}
    # The same two lines the confirm prompt carries, on the MACHINE channel too: an agent driving
    # `plainkeep setup models --yes --json` never sees the prompt, and it is the caller most likely to
    # be surprised by a multi-GB download it did not budget for.
    if layer_id == "models":
        payload["halves"] = list(setuplib.MODELS_HALVES)
    if dry:
        payload["dry_run"] = True
    return output.emit(payload, "setup",
                       human=lambda _: _render_result(layer_id, before, res, dry=dry))


def _handoffs() -> list[str]:
    handoffs = []
    by_id = {row["id"]: row for row in setuplib.status()}
    backups = by_id.get("backups")
    if backups and backups["status"] != "ready" and backups.get("next"):
        handoffs.append(backups["next"])
    automation = by_id.get("automation")
    if automation and automation["status"] == "ready":
        handoffs.append("load launchd plists")
    handoffs.append("push git changes")
    return list(dict.fromkeys(handoffs))


def _describe_failure(layer_id: str, exc: Exception) -> str:
    if isinstance(exc, subprocess.CalledProcessError):
        cmd = " ".join(str(part) for part in (exc.cmd if isinstance(exc.cmd, (list, tuple)) else [exc.cmd]))
        return f"{cmd} (exit {exc.returncode})"
    return str(exc)


def _render_all(results: list[dict], handoffs: list[str], dry: bool = False) -> str:
    lines = ["setup --all (dry run — nothing installed/written):" if dry else "setup --all:"]
    verb = "would run" if dry else "ran"
    for res in results:
        layer = res["layer"]
        if res.get("failed"):
            # Surface the steps that DID run before the failure (FIX 5) so partial progress is visible.
            for cmd in res.get("ran") or []:
                lines.append(f"    {verb}: {cmd}")
            lines.append(f"  {layer}: FAILED — {res['failed']}")
        elif res["ran"]:
            lines.append(f"  {layer}: {'would advance' if dry else 'advanced'}")
            for cmd in res["ran"]:
                lines.append(f"    {verb}: {cmd}")
        elif res.get("skipped_reason"):
            lines.append(f"  {layer}: skipped ({res['skipped_reason']})")
        elif res["skipped"]:
            lines.append(f"  {layer}: skipped")
        else:
            lines.append(f"  {layer}: no changes")
    if handoffs:
        lines.append("\noutstanding handoffs:")
        for item in handoffs:
            lines.append(f"  [ ] {item}")
    return "\n".join(lines)


def _advance_all(*, yes: bool, dry: bool = False) -> int:
    """Best-effort orchestration (Task 8): advance every AUTO layer that CAN be attempted, recording
    per-layer outcomes; a failure in one independent layer does NOT abort the rest. Layers that are
    already ready, blocked on a missing prerequisite, or not applicable to this host are skipped (not
    attempted), and never contribute to the exit code. Overall exit: 1 iff some ATTEMPTED layer
    failed; 0 otherwise. `--dry-run` previews the plan (fake=True) and needs no --yes."""
    rows = {row["id"]: row for row in setuplib.status()}
    # Confirm gate (skipped for a dry-run, which is a read): name the confirm-class layers that would
    # actually be ATTEMPTED. Only genuinely-attemptable layers count (FIX 4) — a layer that is already
    # ready, blocked on a missing prerequisite, or not_applicable is skipped by the attempt loop below,
    # so it must not force a --yes it will never use (that was a spurious exit 3 on, e.g., a host with
    # no ollama demanding --yes for a blocked search layer).
    if not dry:
        confirm = [layer.id for layer in setuplib.LAYERS
                   if layer.id in AUTO_LAYERS and layer.gate == "confirm"
                   and rows[layer.id]["status"] not in SKIP_STATUSES]
        if confirm and not yes:
            output.fail(output.EXIT_CONFIRM,
                        f"setup layers need --yes: {', '.join(confirm)}",
                        hint="re-run: plainkeep setup --all --yes", verb="setup")
    results = []
    attempted_failed = False
    for layer_id in AUTO_LAYERS:
        st = rows[layer_id]["status"]
        if st in SKIP_STATUSES:
            res = setuplib._result()
            res["skipped"].append(layer_id)
            if rows[layer_id].get("next"):
                res["handoff"].append(rows[layer_id]["next"])
            results.append({**res, "layer": layer_id, "skipped_reason": st})
            continue
        try:
            res = setuplib.advance(layer_id, yes=(yes or dry), fake=(_fake() or dry))
            results.append({**res, "layer": layer_id})
        except (subprocess.CalledProcessError, FileNotFoundError, OSError) as exc:
            attempted_failed = True
            # FIX 5: keep the steps that DID run before the failure (advance attaches them to the
            # exception), so a layer that ran N commands then failed on N+1 doesn't report `ran: []`.
            partial = list(getattr(exc, "ops_partial_ran", []))
            results.append({"layer": layer_id, "ran": partial, "skipped": [], "handoff": [],
                            "confirm_needed": False, "failed": _describe_failure(layer_id, exc)})
    # FIX 5: aggregate every layer's remediation into the top-level handoff list ("refusals teach") —
    # each blocked/not_applicable/skipped AUTO layer carried its `next` into its own res["handoff"]
    # above; fold those in alongside the standing handoffs (backups, launchd load, git push), deduped.
    handoffs = [h for res in results for h in res.get("handoff", [])]
    handoffs = list(dict.fromkeys(h for h in [*handoffs, *_handoffs()] if h))
    payload = {"results": results, "handoff": handoffs}
    if dry:
        payload["dry_run"] = True
    output.emit(payload, "setup", human=lambda _: _render_all(results, handoffs, dry=dry))
    # Exit 1 is a semantic "some attempted layer failed" (machine-contract §2), not a crash — the
    # envelope's ok stays true; the aggregate `results` carries each failure.
    return output.EXIT_UNEXPECTED if attempted_failed else output.EXIT_OK


# Safe defaults for the interactive wizard (Task 11 / roadmap 5.4 "≤5 skippable prompts, vectors/jobs
# OFF"): the required, safe-write skeleton is pre-selected ON; the heavy/opt-in layers default OFF (no
# vectors, no model pulls, no scheduled jobs). backups is never a yes/no here — it's a human handoff
# (gate="blocked"), surfaced as a printed next-step, never auto-run.
# The `ui` layer defaults ON: the wizard's audience is exactly who the TUI serves, and the install
# is a small, sha256-verified binary download into the vault's own .local/bin (no system writes).
WIZARD_DEFAULTS = {"skeleton": True, "search": False, "models": False, "automation": False, "ui": True}


def _ask_yes_no(prompt: str, default: bool, ask) -> bool:
    """One stdlib yes/no prompt, factored so the wizard is unit-testable without a real tty:
    `ask(text)->str` supplies the raw line (real path: builtin `input`). An empty line (just Enter)
    takes the SAFE DEFAULT; a closed stdin (EOFError) also takes it. Only an explicit y/yes or n/no
    overrides."""
    suffix = "[Y/n]" if default else "[y/N]"
    try:
        raw = ask(f"  {prompt} {suffix} ").strip().lower()
    except EOFError:
        return default
    if not raw:
        return default
    return raw in ("y", "yes")


def _run_wizard(rows, ask, say) -> dict:
    """The guided first-run loop (Task 11), factored so it is testable with injected I/O:
    `ask(text)->str` supplies answers (real path: `input`), `say(text)` receives each printed line
    (real path: `print`). Walks setuplib.LAYERS order, ONE skippable prompt per attemptable layer with
    a safe default. Accepted layers advance through the SAME `setuplib.advance(id, yes=True, ...)` the
    dashboard/`--all` use (no second advance path). Already-ready layers are noted and skipped;
    blocked/not_applicable layers show their reason + handoff and are never prompted to install.
    Returns {advanced, skipped, handoff}."""
    advanced: list[str] = []
    skipped: list[str] = []
    handoffs: list[str] = []
    say("plainkeep setup — guided first run")
    say("  safe defaults are pre-selected; press Enter to accept each.")
    say("  search / models / automation default to OFF (no vectors, no model pulls, no jobs).")
    say("  the terminal UI (plainkeep ui) defaults to ON — a small verified binary download.")
    for row in rows:
        lid, status = row["id"], row["status"]
        if status == "ready":
            say(f"  {GLYPHS['ready']} {lid}: already ready — skipping")
            continue
        if status in ("blocked", "not_applicable"):
            say(f"  {GLYPHS.get(status, '—')} {lid}: {row['detail']}")
            nxt = row.get("next") or ""
            if nxt:
                say(f"      do this yourself: {nxt}")
                handoffs.append(nxt)
            skipped.append(lid)
            continue
        # Attemptable (partial/absent): the one skippable prompt, pre-set to the safe default.
        if not _ask_yes_no(f"set up {lid} — {row['title']}?", WIZARD_DEFAULTS.get(lid, False), ask):
            say(f"  skip {lid}")
            skipped.append(lid)
            continue
        try:
            res = setuplib.advance(lid, yes=True, fake=_fake())
        except (subprocess.CalledProcessError, FileNotFoundError, OSError) as exc:
            say(f"  {lid}: FAILED — {_describe_failure(lid, exc)}")
            skipped.append(lid)
            continue
        if res["ran"]:
            advanced.append(lid)
            say(f"  {lid}: advanced")
            for cmd in res["ran"]:
                say(f"    ran: {cmd}")
        else:
            skipped.append(lid)
            say(f"  {lid}: no changes")
        for item in res.get("handoff", []):
            handoffs.append(item)
    handoffs = list(dict.fromkeys(handoffs))
    say("")
    say("summary:")
    say(f"  advanced: {', '.join(advanced) if advanced else '(none)'}")
    say(f"  skipped:  {', '.join(skipped) if skipped else '(none)'}")
    if handoffs:
        say("  outstanding handoffs:")
        for item in handoffs:
            say(f"    [ ] {item}")
    say("")
    say("next steps (yours to run — setup never pushes):")
    say("  • point origin at your GitHub and push:  git push -u origin main")
    say("  • configure encrypted backups:           plainkeep backup init")
    say("  • re-check state anytime:                plainkeep setup   (read-only dashboard)")
    return {"advanced": advanced, "skipped": skipped, "handoff": handoffs}


def _wizard(*, json_on: bool, dry: bool) -> int:
    # tty-guard (Task 11): the wizard is interactive-only. No tty, or a machine/preview mode
    # (--json / --dry-run) paired with it, means we must NOT prompt — fail with exit 2 and print the
    # exact non-interactive alternatives ("refusals teach"). --dry-run is refused rather than silently
    # advancing: the wizard's advance path is real (fake only under PLAINKEEP_SETUP_FAKE), so a --dry-run
    # here could not preview without a second code path; point at the dashboard's own preview instead.
    if json_on or dry or not sys.stdin.isatty():
        output.fail(output.EXIT_USAGE,
                    "plainkeep setup --wizard is interactive-only (needs a tty; not with --json or --dry-run)",
                    hint="non-interactive instead: `plainkeep setup --all --yes` to apply, "
                         "`plainkeep setup --json` to inspect, `plainkeep setup --all --dry-run` to preview",
                    verb="setup")
    _run_wizard(setuplib.status(), input, print)
    return output.EXIT_OK


USAGE = "usage: plainkeep setup [<layer> [--yes] | --all [--yes] | --wizard] [--dry-run]"


def main(argv: list[str]) -> int:
    json_on, argv = output.parse_argv(argv)
    yes = "--yes" in argv or "-y" in argv
    all_ = "--all" in argv
    dry = "--dry-run" in argv  # a true preview: advance with fake=True, write nothing, never need --yes
    wizard = "--wizard" in argv
    argv = [a for a in argv if a not in ("--yes", "-y", "--all", "--dry-run", "--wizard")]
    if wizard:
        if all_ or argv:
            output.fail(output.EXIT_USAGE,
                        "plainkeep setup --wizard walks every layer — pass no <layer> and not --all",
                        hint="run: plainkeep setup --wizard", verb="setup")
        return _wizard(json_on=json_on, dry=dry)
    if all_ and argv:
        output.fail(output.EXIT_USAGE, USAGE, verb="setup")
    if all_:
        return _advance_all(yes=yes, dry=dry)
    if not argv:
        return output.emit_rows(_dashboard_rows(), "setup", human=_render_dashboard)
    if len(argv) > 1:
        output.fail(output.EXIT_USAGE, USAGE, verb="setup")
    return _advance_one(argv[0], yes=yes, dry=dry)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
