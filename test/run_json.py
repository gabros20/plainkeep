#!/usr/bin/env python3
"""
run_json.py — the machine-contract suite (proposal Part 1.1/1.2/0.5), offline + stdlib only:
  1. every non-hidden verb's cmd.json declares an `output` block and `hints` (the I/O contract lives
     in plainkeep.json, so a third party never imports lib);
  2. the declared `dry_run` verbs actually declare it;
  3. plainkeep.json/3 top-level shape (schema/ops_version/api_version/json_envelope/capabilities) + the
     plainkeep.json/3 additions: every verb's `group`, and well-formed `actions[]` on compound verbs;
  4. `--json` round-trips: each read-class verb, run against a fixture world, emits the frozen
     envelope and every field its cmd.json `output` block declares;
  5. `--dry-run` on a mutating verb emits a valid envelope and writes NOTHING.
"""
from __future__ import annotations
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from lib.hermetic import seal
seal()   # hermetic: an empty throwaway registry, never the developer's real vault

REPO = Path(__file__).resolve().parents[1]
BIN = REPO / "bin"
GREEN, RED, DIM, BOLD, RESET = "\033[32m", "\033[31m", "\033[2m", "\033[1m", "\033[0m"
results = []


def check(name, cond, detail=""):
    results.append((name, bool(cond), detail))


def run(env, verb, *args):
    return subprocess.run([sys.executable, str(BIN / verb / "run.py"), *args],
                          capture_output=True, text=True, env=env)


def _cmd(verb) -> dict:
    return json.loads((BIN / verb / "cmd.json").read_text(encoding="utf-8"))


def parse_ndjson(out: str):
    return [json.loads(ln) for ln in out.splitlines() if ln.strip()]


def validate_scalar(name, r, verb, fields):
    try:
        objs = parse_ndjson(r.stdout)
    except Exception as e:
        check(f"{name}: valid JSON", False, f"{e}: {r.stdout[:160]}{r.stderr[:160]}"); return
    if len(objs) != 1:
        check(f"{name}: single envelope", False, r.stdout[:200]); return
    e = objs[0]
    ok = (e.get("ops_json") == 1 and e.get("ok") is True and e.get("verb") == verb
          and isinstance(e.get("data"), dict))
    check(f"{name}: scalar envelope", ok, str(e)[:200])
    data = e.get("data", {})
    missing = [f for f in fields if f not in data]
    check(f"{name}: declared fields round-trip", not missing, f"missing {missing}")


def validate_rows(name, r, verb, fields, check_fields=True):
    try:
        objs = parse_ndjson(r.stdout)
    except Exception as e:
        check(f"{name}: valid NDJSON", False, f"{e}: {r.stdout[:160]}{r.stderr[:160]}"); return
    if not objs:
        check(f"{name}: has header", False, r.stdout[:200]); return
    head, rows = objs[0], objs[1:]
    ok = (head.get("ops_json") == 1 and head.get("ok") is True and head.get("verb") == verb
          and head.get("count") == len(rows))
    check(f"{name}: rows header + count", ok, str(head)[:200])
    if check_fields and rows:
        missing = [f for f in fields if f not in rows[0]]
        check(f"{name}: declared row fields round-trip", not missing, f"missing {missing} in {rows[0]}")


def mkrepo(path: Path):
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(path)], check=False)
    (path / "README.md").write_text("# demo\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(path), "add", "-A"], check=False)
    subprocess.run(["git", "-C", str(path), "-c", "user.email=t@t", "-c", "user.name=t",
                    "-c", "commit.gpgsign=false", "commit", "-qm", "init"], check=False,
                   capture_output=True)


def main() -> int:
    # --- static contract: every non-hidden verb declares output + hints (+ dry_run where required) ---
    DRY = {"capture", "task", "wiki", "triage", "files", "archive", "sweep",
           "invoice", "index", "consolidate", "job", "new", "bookmark", "setup",
           "start", "close", "week"}
    verbs = []
    for cj in sorted(BIN.glob("*/cmd.json")):
        d = json.loads(cj.read_text(encoding="utf-8"))
        if d.get("hidden"):
            continue
        verbs.append(d["verb"])
        out = d.get("output")
        check(f"{d['verb']}: cmd.json has output block",
              isinstance(out, dict) and isinstance(out.get("fields"), dict) and out.get("mode") in ("scalar", "rows"),
              str(out))
        check(f"{d['verb']}: cmd.json has hints", bool(d.get("hints")))
    for v in sorted(DRY):
        check(f"{v}: declares dry_run", _cmd(v).get("dry_run") is True)

    # --- plainkeep.json/3 top-level shape ---
    with tempfile.TemporaryDirectory() as td:
        env0 = {**os.environ, "PLAINKEEP_HOME": td, "PLAINKEEP_ROOTS_HOME": td}
        run(env0, "help")  # regenerates plainkeep.json into the temp PLAINKEEP_HOME
        doc = json.loads((Path(td) / "plainkeep.json").read_text(encoding="utf-8"))
    for key in ("schema", "ops_version", "api_version", "json_envelope", "capabilities", "verbs"):
        check(f"plainkeep.json top-level: {key}", key in doc)
    check("plainkeep.json schema is plainkeep.json/3", doc.get("schema") == "plainkeep.json/3")
    caps = doc.get("capabilities", {})
    check("capabilities keys present", all(k in caps for k in ("vectors", "rerank", "agent", "plugins")))
    check("every verb tagged source=engine", all(v.get("source") == "engine" for v in doc.get("verbs", [])))

    # --- plainkeep.json/3: every verb carries a display `group`; declared `actions[]` are well-formed ---
    ARG_TYPES = {"string", "int", "enum", "slug", "path", "flag"}
    RISK_ENUM = {"read", "safe_write", "draft_only", "confirm", "deny"}
    COMPLETE_PROVIDERS = {"note-slug", "asset-slug", "task-id", "hub", "note-type", "status", "layer"}
    dverbs = doc.get("verbs", [])
    check("every verb carries a non-empty group",
          all(isinstance(v.get("group"), str) and v.get("group") for v in dverbs),
          str([v.get("verb") for v in dverbs if not v.get("group")]))

    def _actions_wellformed(v) -> tuple[bool, str]:
        acts = v.get("actions")
        if not isinstance(acts, list) or not acts:
            return False, "actions is not a non-empty list"
        for a in acts:
            if not isinstance(a.get("name"), str) or not a["name"]:
                return False, f"action missing name: {a}"
            if not isinstance(a.get("args"), list):
                return False, f"action {a.get('name')} missing args list"
            if "risk" in a and a["risk"] not in RISK_ENUM:
                return False, f"action {a['name']} bad risk {a['risk']}"
            if "dry_run" in a and not isinstance(a["dry_run"], bool):
                return False, f"action {a['name']} dry_run not bool"
            if "default" in a and not isinstance(a["default"], bool):
                return False, f"action {a['name']} default not bool"
            if "tty" in a and not isinstance(a["tty"], bool):
                return False, f"action {a['name']} tty not bool"
            for arg in a["args"]:
                if not isinstance(arg.get("name"), str) or not arg["name"]:
                    return False, f"{a['name']}: arg missing name: {arg}"
                if arg.get("type") not in ARG_TYPES:
                    return False, f"{a['name']}/{arg.get('name')}: bad type {arg.get('type')}"
                if "complete" in arg and arg["complete"] not in COMPLETE_PROVIDERS:
                    return False, f"{a['name']}/{arg['name']}: bad complete {arg['complete']}"
                if arg["type"] == "enum" and not (isinstance(arg.get("enum"), list) and arg["enum"]):
                    return False, f"{a['name']}/{arg['name']}: enum type without enum list"
        return True, ""

    for v in dverbs:
        if "actions" in v:
            ok, why = _actions_wellformed(v)
            check(f"{v['verb']}: actions[] well-formed (plainkeep.json/3)", ok, why)
            ndefault = sum(1 for a in v["actions"] if a.get("default"))
            check(f"{v['verb']}: at most one default action", ndefault <= 1, f"{ndefault} defaults")
    # every compound verb (wave 1 + wave 2) carries a non-empty actions[]
    by_verb = {v["verb"]: v for v in dverbs}
    for v in ("task", "wiki", "files", "organize", "share", "repo", "job", "backup",
              "models", "plugin", "new"):
        acts = by_verb.get(v, {}).get("actions")
        check(f"{v}: declares a non-empty actions[]", isinstance(acts, list) and len(acts) > 0,
              str(acts)[:120])
    # the two tokenless-default verbs mark their default action
    for v in ("share", "backup"):
        acts = by_verb.get(v, {}).get("actions", [])
        check(f"{v}: has a default:true action (tokenless default)",
              any(a.get("default") for a in acts), str([a["name"] for a in acts]))

    # --- the completion contract verb (plainkeep complete): visible, read-class, right output shape ---
    comp = by_verb.get("complete")
    check("complete: verb present in plainkeep.json/3", isinstance(comp, dict), "missing")
    if comp:
        check("complete: risk read", comp.get("risk") == "read", str(comp.get("risk")))
        cfields = comp.get("output", {}).get("fields", {})
        check("complete: output rows {value,description,kind}",
              comp.get("output", {}).get("mode") == "rows"
              and all(f in cfields for f in ("value", "description", "kind")), str(cfields))

    # --- live --json round-trip against a seeded fixture world ---
    with tempfile.TemporaryDirectory() as td:
        plainkeep_home = Path(td) / "plainkeep"
        roots = Path(td) / "roots"
        plainkeep_home.mkdir(); roots.mkdir()
        env = {**os.environ, "PLAINKEEP_HOME": str(plainkeep_home), "PLAINKEEP_ROOTS_HOME": str(roots),
               "PLAINKEEP_NO_OPEN": "1"}
        # seed content
        run(env, "wiki", "new", "note", "Alpha Note About Testing")
        run(env, "task", "add", "Fix the alpha widget")
        png = Path(td) / "pic.png"
        png.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 64)
        run(env, "files", "ingest", str(png))
        # jobs registry (user-owned; copy the repo's for the fixture)
        (plainkeep_home / "jobs").mkdir(parents=True, exist_ok=True)
        (plainkeep_home / "jobs" / "registry.json").write_text(
            (REPO / "jobs" / "registry.json").read_text(encoding="utf-8"), encoding="utf-8")
        mkrepo(roots / "work" / "labs" / "demo")
        run(env, "index")

        # canonical read-class round-trips (verb, args) validated against their cmd.json output block
        canon = [("backup", []), ("status", []), ("help", []), ("search", ["alpha"]),
                 ("task", ["list"]), ("wiki", ["list"]), ("files", ["list"]),
                 ("repo", ["health"]), ("job", ["list"]), ("doctor", [])]
        for verb, args in canon:
            r = run(env, verb, *args, "--json")
            block = _cmd(verb)["output"]
            fields = list(block["fields"])
            if block["mode"] == "scalar":
                validate_scalar(f"{verb} {' '.join(args)}".strip(), r, verb, fields)
            else:
                validate_rows(f"{verb} {' '.join(args)}".strip(), r, verb, fields)

        # other read sub-actions: envelope must be well-formed even if row shape differs from the block
        for verb, args in [("wiki", ["stale"]), ("wiki", ["orphans"])]:
            r = run(env, verb, *args, "--json")
            validate_rows(f"{verb} {' '.join(args)}".strip(), r, verb, [], check_fields=False)

        # PLAINKEEP_JSON env toggles JSON too (no --json flag)
        r = run({**env, "PLAINKEEP_JSON": "1"}, "status")
        validate_scalar("status via PLAINKEEP_JSON=1", r, "status", list(_cmd("status")["output"]["fields"]))

        # --dry-run emits a valid envelope AND writes nothing
        before = len(list((plainkeep_home / "tasks" / "active").glob("T-*.md")))
        r = run(env, "task", "add", "Should not persist", "--dry-run", "--json")
        objs = parse_ndjson(r.stdout) if r.stdout.strip() else []
        check("task add --dry-run: ok envelope",
              len(objs) == 1 and objs[0].get("ok") is True and objs[0]["data"].get("dry_run") is True, r.stdout[:200])
        after = len(list((plainkeep_home / "tasks" / "active").glob("T-*.md")))
        check("task add --dry-run writes nothing", before == after, f"{before} -> {after}")

        inbox_before = len(list((plainkeep_home / "inbox").glob("cap-*.md")))
        r = run(env, "capture", "ephemeral", "--dry-run", "--json")
        check("capture --dry-run writes nothing",
              len(list((plainkeep_home / "inbox").glob("cap-*.md"))) == inbox_before, r.stdout[:160])

        # the daily/weekly verbs (start/close/week) now honour --dry-run: valid envelope, no journal write
        def _journal_md():
            jd = plainkeep_home / "journal"
            return len(list(jd.rglob("*.md"))) if jd.exists() else 0
        for verb in ("start", "close", "week"):
            before = _journal_md()
            r = run(env, verb, "--dry-run", "--json")
            objs = parse_ndjson(r.stdout) if r.stdout.strip() else []
            check(f"{verb} --dry-run: ok envelope + dry_run flag",
                  len(objs) == 1 and objs[0].get("ok") is True and objs[0]["data"].get("dry_run") is True,
                  r.stdout[:200])
            check(f"{verb} --dry-run writes no journal", _journal_md() == before, f"{before} -> {_journal_md()}")

        # error path: fail() emits the error envelope + protocol exit code under --json
        r = run(env, "search", "--json")  # empty query -> usage (2)
        errs = parse_ndjson(r.stdout) if r.stdout.strip() else []
        check("search usage error: JSON error envelope",
              r.returncode == 2 and errs and errs[0].get("ok") is False and errs[0]["error"].get("code") == 2,
              f"rc={r.returncode} {r.stdout[:160]}")

        # human rendering unchanged when --json is absent (spot check)
        r = run(env, "task", "list")
        check("human mode still renders (no envelope)", "active/" in r.stdout and "ops_json" not in r.stdout, r.stdout[:160])

        # setup layer status enum (machine-contract §7): every dashboard row's `status` is one of the
        # documented values, now including `not_applicable` (Task 8).
        SETUP_STATUS_ENUM = {"ready", "partial", "absent", "blocked", "not_applicable"}
        r = run(env, "setup", "--json")
        srows = parse_ndjson(r.stdout) if r.stdout.strip() else []
        check("setup --json rows carry a documented status enum value",
              len(srows) > 1 and all(row.get("status") in SETUP_STATUS_ENUM for row in srows[1:]),
              f"rc={r.returncode} {r.stdout[:200]}")

        # ui shim (Wave 3): with plainkeep-ui absent, a blocked-style envelope (installed:false, next hint),
        # exits cleanly — never a crash. Point PLAINKEEP_UI_BIN at a nonexistent path so `_resolve` short-
        # circuits on the override and never consults the host PATH — deterministic even the day
        # plainkeep-ui is installed globally (the host-sensitive-path CI lesson).
        r = run({**env, "PLAINKEEP_UI_BIN": "/nonexistent/plainkeep-ui"}, "ui", "--json")
        uobjs = parse_ndjson(r.stdout) if r.stdout.strip() else []
        check("ui --json (plainkeep-ui absent): blocked envelope, clean exit",
              r.returncode == 0 and len(uobjs) == 1 and uobjs[0].get("ok") is True
              and uobjs[0]["data"].get("installed") is False
              and uobjs[0]["data"].get("status") == "blocked"
              and bool(uobjs[0]["data"].get("next")), r.stdout[:200])

    print(f"{BOLD}Machine contract: --json envelope + plainkeep.json/3 + dry-run — {len(results)} checks{RESET}\n")
    passed = sum(1 for _, ok, _ in results if ok)
    for name, ok, detail in results:
        mark = f"{GREEN}PASS{RESET}" if ok else f"{RED}FAIL{RESET}"
        print(f"  {mark} {name:<48}" + (f" {DIM}{detail.strip()[:80]}{RESET}" if (detail and not ok) else ""))
    failed = len(results) - passed
    print(f"\n{BOLD}Result:{RESET} {GREEN}{passed} passed{RESET}, "
          f"{(RED if failed else DIM)}{failed} failed{RESET}, {len(results)} checks")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
