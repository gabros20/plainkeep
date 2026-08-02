#!/usr/bin/env python3
"""
run_simulation.py — the PROBABILISTIC half. Plug an LLM in as the operator and stress the
agent-judgment surface the guardrail can't cover: misfiling, drift, oversight, bypass attempts,
mishandling of personal/legal material, inventing verbs, skipping brain-first.

For each scenario it: builds the operator prompt from the design doc itself (spec.py), runs the
operator (operator.py — `claude -p` by default, or --dry-run offline), and scores the returned
plan (judge.py). Every proposed action is also replayed through the §5 guardrail.

Agnosticism / drift mode: pass --compare modelA modelB to run each scenario through two models
and flag any scenario where they disagree (divergence = an ambiguous manual, per §12.4).

Usage:
  python3 test/run_simulation.py --dry-run                 # offline plumbing check (no LLM)
  python3 test/run_simulation.py --model sonnet            # real run, one model
  python3 test/run_simulation.py --compare sonnet opus     # two-model agnosticism diff
  python3 test/run_simulation.py --only icloud-tax-doc     # single scenario
  python3 test/run_simulation.py --json report.json        # machine-readable output
"""
from __future__ import annotations
import argparse
import copy
import json
import sys
from pathlib import Path
from lib.hermetic import seal
seal()   # hermetic: an empty throwaway registry, never the developer's real vault

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib.spec import extract_contract, load_world, build_operator_prompt  # noqa: E402
from lib.op_runner import run_operator  # noqa: E402  (named op_runner, not 'operator', to avoid shadowing stdlib)
from lib.judge import judge  # noqa: E402

SCEN = Path(__file__).resolve().parent / "cases" / "scenarios.json"
GREEN, RED, YEL, DIM, BOLD, RESET = "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[1m", "\033[0m"


def run_one(contract, base_world, scenario, model, dry_run):
    world = copy.deepcopy(base_world)
    for k, v in (scenario.get("world_overrides") or {}).items():
        world[k] = v
    prompt = build_operator_prompt(contract, world, scenario["situation"])
    res = run_operator(prompt, model=model, dry_run=dry_run)
    if not res.ok:
        return {"ok": False, "error": res.error, "verdict": None, "plan": {}}
    v = judge(res.plan, scenario["expect"], scenario["name"])
    return {"ok": True, "error": "", "verdict": v, "plan": res.plan, "raw": res.raw}


def print_verdict(name, out):
    if not out["ok"]:
        print(f"  {RED}ERROR{RESET} {name:<26} {DIM}{out['error']}{RESET}")
        return
    v = out["verdict"]
    head = f"{GREEN}PASS{RESET}" if v.passed else f"{RED}FAIL{RESET}"
    print(f"  {head} {name:<26}")
    for c in v.checks:
        mark = f"{GREEN}ok{RESET}" if c.passed else f"{RED}✗{RESET}"
        if not c.passed:
            print(f"        {mark} {c.name}: {DIM}{c.detail}{RESET}")


def replay(report_path: str) -> int:
    """Re-judge plans captured in a prior report with the CURRENT judge — deterministic, free."""
    saved = json.loads(Path(report_path).read_text(encoding="utf-8"))
    expects = {s["name"]: s["expect"] for s in json.loads(SCEN.read_text(encoding="utf-8"))["scenarios"]}
    print(f"{BOLD}Replay{RESET} — re-judging {len(saved['scenarios'])} saved plans from {report_path}\n")
    total_pass = 0
    for entry in saved["scenarios"]:
        name = entry["name"]
        expect = expects.get(name, {})
        print(f"{BOLD}{name}{RESET}  {DIM}{', '.join(entry.get('tags', []))}{RESET}")
        scen_ok = True
        for model, pm in entry["per_model"].items():
            plan = pm.get("plan") or {}
            v = judge(plan, expect, name)
            scen_ok = scen_ok and v.passed
            if len(entry["per_model"]) > 1:
                print(f"    {DIM}[{model}]{RESET}")
            print_verdict(name, {"ok": True, "verdict": v})
        total_pass += 1 if scen_ok else 0
        print()
    print(f"{BOLD}Replay result:{RESET} {GREEN}{total_pass} passed{RESET} / {len(saved['scenarios'])} scenarios")
    return 0 if total_pass == len(saved["scenarios"]) else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="sonnet")
    ap.add_argument("--compare", nargs=2, metavar=("A", "B"))
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--only", default=None)
    ap.add_argument("--tag", default=None, help="run only scenarios carrying this tag")
    ap.add_argument("--json", default=None)
    ap.add_argument("--replay", default=None,
                    help="re-judge saved plans from a prior --json report (no LLM calls)")
    args = ap.parse_args()

    if args.replay:
        return replay(args.replay)

    contract = extract_contract()
    world = load_world()
    scenarios = json.loads(SCEN.read_text(encoding="utf-8"))["scenarios"]
    if args.only:
        scenarios = [s for s in scenarios if s["name"] == args.only]
        if not scenarios:
            print(f"no scenario named {args.only!r}")
            return 2
    if args.tag:
        scenarios = [s for s in scenarios if args.tag in s.get("tags", [])]
        if not scenarios:
            print(f"no scenarios tagged {args.tag!r}")
            return 2

    models = args.compare if args.compare else [args.model]
    mode = "DRY-RUN (offline stub)" if args.dry_run else f"models={models}"
    print(f"{BOLD}Operator simulation{RESET} — {len(scenarios)} scenarios, {mode}\n")

    report = {"mode": mode, "scenarios": []}
    total_pass = 0
    divergences = []

    for s in scenarios:
        print(f"{BOLD}{s['name']}{RESET}  {DIM}{', '.join(s.get('tags', []))}{RESET}")
        per_model = {}
        for m in models:
            out = run_one(contract, world, s, m, args.dry_run)
            per_model[m] = out
            label = f"[{m}] " if len(models) > 1 else ""
            if label:
                print(f"    {DIM}{label}{RESET}")
            print_verdict(s["name"], out)

        # pass = all models pass
        oks = [o["ok"] and o["verdict"] and o["verdict"].passed for o in per_model.values()]
        passed = all(oks)
        total_pass += 1 if passed else 0

        if args.compare:
            results = {m: (per_model[m]["verdict"].passed if per_model[m]["ok"] else None) for m in models}
            if len(set(results.values())) > 1:
                divergences.append(s["name"])
                print(f"    {YEL}DIVERGENCE{RESET} between models: {results}  {DIM}(manual is ambiguous here — §12.4){RESET}")

        report["scenarios"].append({
            "name": s["name"], "tags": s.get("tags", []), "passed": passed,
            "per_model": {m: ({"ok": o["ok"], "error": o["error"],
                               "passed": (o["verdict"].passed if o["ok"] and o["verdict"] else None),
                               "checks": [{"name": c.name, "passed": c.passed, "detail": c.detail}
                                          for c in (o["verdict"].checks if o["ok"] and o["verdict"] else [])],
                               "plan": o.get("plan", {})})
                          for m, o in per_model.items()},
        })
        print()

    print(f"{BOLD}Result:{RESET} {GREEN}{total_pass} passed{RESET} / {len(scenarios)} scenarios"
          + (f"  |  {YEL}{len(divergences)} divergences{RESET}: {divergences}" if args.compare else ""))
    report["summary"] = {"passed": total_pass, "total": len(scenarios), "divergences": divergences}

    if args.json:
        Path(args.json).write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(f"{DIM}wrote {args.json}{RESET}")

    return 0 if total_pass == len(scenarios) else 1


if __name__ == "__main__":
    raise SystemExit(main())
