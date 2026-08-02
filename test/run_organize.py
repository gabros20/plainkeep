#!/usr/bin/env python3
"""run_organize.py — distill + the self-organization loop (proposal Parts 4.2 + 4.4), offline.

No agent is configured (PLAINKEEP_AGENT=none), so this asserts the deterministic zero-LLM paths:
  - `plainkeep files distill` heading-outline fallback -> author:agent/status:draft concept notes,
  - `plainkeep triage drafts` promotion queue (accept -> active, reject -> delete, one commit each),
  - `plainkeep organize scan` emits ONLY closed-catalog typed ops into inbox/organize/<date>.jsonl,
  - review appends accept/reject/defer status lines (audit ledger; latest status wins on replay),
  - apply replays APPROVED ops with one git commit per op, stops at the edit budget, refuses
    protected paths, skips manual (retitle/propose_merge) ops, and rejects off-catalog ops,
  - doctor warns iff a confirm/deny-class verb is scheduled in jobs/registry.json.
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
GREEN, RED, DIM, BOLD, RESET = "\033[32m", "\033[31m", "\033[2m", "\033[1m", "\033[0m"
CATALOG = {"add_link", "refresh_hub", "normalize_tag", "fix_frontmatter",
           "retitle", "flag_duplicate", "propose_merge"}
results: list[tuple[str, bool, str]] = []


def check(name, cond, detail=""):
    results.append((name, bool(cond), "" if cond else str(detail)))


def run(verb, ops, *args, extra=None):
    env = {**os.environ, "PLAINKEEP_HOME": str(ops), "PLAINKEEP_AGENT": "none"}
    if extra:
        env.update(extra)
    return subprocess.run([sys.executable, str(REPO / "bin" / verb / "run.py"), *args],
                          capture_output=True, text=True, env=env)


def git(ops, *args):
    return subprocess.run(["git", "-C", str(ops), *args], capture_output=True, text=True)


def note(p: Path, fm: dict, body="body text with several words here") -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    head = "\n".join(f"{k}: {v}" for k, v in fm.items())
    p.write_text(f"---\n{head}\n---\n# {fm.get('title', p.stem)}\n\n{body}\n", encoding="utf-8")


def fm(text: str, key: str) -> str:
    for ln in text.splitlines():
        if ln.startswith(f"{key}:"):
            return ln.split(":", 1)[1].strip().strip('"').strip("'")
    return ""


# --------------------------------------------------------------------------- distill + triage drafts

def test_distill_and_promotion() -> None:
    with tempfile.TemporaryDirectory() as td:
        ops = Path(td) / "ops"
        (ops / "wiki" / "files").mkdir(parents=True)
        (ops / "wiki" / "notes").mkdir(parents=True)
        (ops / "journal").mkdir()
        git(ops, "init", "-q")
        git(ops, "config", "user.email", "t@x.co")
        git(ops, "config", "user.name", "t")
        extract = ops / "wiki" / "files" / "talk.extract.md"
        extract.write_text(
            "---\ntype: transcript\ntitle: Talk (extract)\nstatus: derived\n"
            "derived_from: \"[[talk]]\"\nsource_sha256: abc\ntool: stdlib-text 1.0\n---\n# Talk\n\n"
            "## Retrieval fusion\nRRF combines BM25 and vector rankings without score scaling.\n\n"
            "## Vector search\nEmbeddings map text into a shared space for cosine similarity.\n",
            encoding="utf-8")
        note(ops / "wiki" / "files" / "talk.md", {"type": "file", "title": "Talk", "path": "/x/talk.mp3"})

        # missing extract -> not-found (exit 4)
        r = run("files", ops, "distill", "nope")
        check("distill of a missing extract is not-found (exit 4)", r.returncode == 4, r.stdout + r.stderr)

        # deterministic outline path: one concept note per ## heading
        r = run("files", ops, "distill", "talk")
        a = ops / "wiki" / "notes" / "retrieval-fusion.md"
        b = ops / "wiki" / "notes" / "vector-search.md"
        check("distill splits the extract by ## headings into concept notes",
              a.exists() and b.exists() and r.returncode == 0, r.stdout + r.stderr)
        at = a.read_text() if a.exists() else ""
        check("concept note is author: agent", fm(at, "author") == "agent", at[:200])
        check("concept note is status: draft", fm(at, "status") == "draft", at[:200])
        check("concept note sources the extract", fm(at, "source") == "[[talk.extract]]", at[:200])
        check("concept note carries the section body", "RRF combines BM25" in at, at)
        check("concept note is type note (via notetype template)", fm(at, "type") == "note", at[:200])

        # doctor must NOT flag these (author:agent HAS a status; no derived_from/tool)
        rd = run("doctor", ops)
        prov = "\n".join(l for l in (rd.stdout + rd.stderr).splitlines() if "provenance:" in l)
        check("doctor does not flag well-formed distilled notes",
              "retrieval-fusion" not in prov and "vector-search" not in prov, prov)

        # --json envelope for distill (dry-run so it doesn't create a second set of drafts)
        r = run("files", ops, "distill", "talk", "--json", "--dry-run")
        try:
            env = json.loads(r.stdout.splitlines()[0])
            ok = env["ops_json"] == 1 and env["ok"] and env["verb"] == "files" and env.get("agent") is False
        except Exception:
            ok = False
        check("distill emits a valid --json header (agent=false without a model)", ok, r.stdout)

        # triage drafts --json lists the two pending drafts
        r = run("triage", ops, "drafts", "--json")
        drafts = [json.loads(l) for l in r.stdout.splitlines() if l.strip()]
        head = drafts[0] if drafts else {}
        check("triage drafts --json lists both drafts", head.get("drafts") == 2, r.stdout)

        # promote all with --yes: status -> active, files kept, one commit each
        before = len(git(ops, "log", "--oneline").stdout.splitlines())
        r = run("triage", ops, "drafts", "--yes")
        check("triage drafts --yes promotes to active",
              fm(a.read_text(), "status") == "active" and fm(b.read_text(), "status") == "active",
              r.stdout + r.stderr)
        after = len(git(ops, "log", "--oneline").stdout.splitlines())
        check("promotion makes one git commit per note", after - before == 2, f"{before}->{after}")

    # reject path deletes the note + commits
    with tempfile.TemporaryDirectory() as td:
        ops = Path(td) / "ops"
        (ops / "wiki" / "notes").mkdir(parents=True)
        (ops / "journal").mkdir()
        git(ops, "init", "-q"); git(ops, "config", "user.email", "t@x.co"); git(ops, "config", "user.name", "t")
        d = ops / "wiki" / "notes" / "draft-x.md"
        note(d, {"type": "note", "title": "Draft X", "author": "agent", "status": "draft",
                 "source": "[[y.extract]]"})
        git(ops, "add", "-A"); git(ops, "commit", "-qm", "seed")
        r = subprocess.run([sys.executable, str(REPO / "bin/triage/run.py"), "drafts"],
                           input="r\n", capture_output=True, text=True,
                           env={**os.environ, "PLAINKEEP_HOME": str(ops), "PLAINKEEP_AGENT": "none"})
        check("triage drafts reject deletes the note", not d.exists(), r.stdout + r.stderr)
        check("reject records a git commit",
              "reject agent draft draft-x" in git(ops, "log", "--oneline").stdout, git(ops, "log", "--oneline").stdout)


# ------------------------------------------------------------------------- organize scan/review/apply

def _seed_vault(ops: Path) -> None:
    for d in ("wiki/notes", "wiki/clients", "wiki/projects", "journal", "inbox", "jobs"):
        (ops / d).mkdir(parents=True, exist_ok=True)
    W = ops / "wiki"
    # normalize_tag candidate: a non-canonical tag in flow form
    note(W / "notes" / "alpha.md", {"type": "note", "title": "Alpha", "status": "active",
                                     "updated": "2026-06-01", "tags": "[ProjectX, ml]"},
         "alpha discusses kubernetes operators and reconcilers in depth")
    # fix_frontmatter candidate: missing status + updated
    note(W / "notes" / "beta.md", {"type": "note", "title": "Beta", "tags": "[]"},
         "beta is about kubernetes operators reconcilers and controllers deeply")
    # flag_duplicate / propose_merge candidates: near-identical titles
    note(W / "notes" / "kubernetes-operators.md",
         {"type": "note", "title": "Kubernetes Operators Guide", "status": "active", "updated": "2026-06-01"},
         "operators guide content about reconcile loops")
    note(W / "notes" / "kubernetes-operators-2.md",
         {"type": "note", "title": "Kubernetes Operators Guide", "status": "active", "updated": "2026-06-01"},
         "operators guide duplicate content about reconcile loops")
    # protected hub with an incoming link that lacks a back-reference -> refresh_hub (review-only)
    note(W / "clients" / "acme.md", {"type": "client", "title": "Acme", "status": "active", "updated": "2026-06-01"},
         "acme hub")
    note(W / "notes" / "acme-meeting.md",
         {"type": "note", "title": "Acme Meeting", "status": "active", "updated": "2026-06-01"},
         "met with [[acme]] about the kubernetes rollout and operators")


def _read_queue(qpath: Path):
    ops, status, order = {}, {}, []
    for ln in qpath.read_text().splitlines():
        o = json.loads(ln)
        if "op" in o:
            order.append(o["id"]); ops[o["id"]] = o; status[o["id"]] = o.get("status")
        elif "status" in o:
            status[o["id"]] = o["status"]
    return ops, status, order


def test_organize() -> None:
    with tempfile.TemporaryDirectory() as td:
        ops = Path(td) / "ops"
        _seed_vault(ops)
        git(ops, "init", "-q"); git(ops, "config", "user.email", "t@x.co"); git(ops, "config", "user.name", "t")
        git(ops, "add", "-A"); git(ops, "commit", "-qm", "seed")

        # --- scan ---
        r = run("organize", ops, "scan")
        qdir = ops / "inbox" / "organize"
        qfiles = list(qdir.glob("*.jsonl"))
        check("scan writes a proposal queue under inbox/organize/", len(qfiles) == 1, r.stdout + r.stderr)
        qpath = qfiles[0]
        opsd, status, order = _read_queue(qpath)
        check("scan emits at least one op", len(order) >= 1, order)
        check("every scanned op is in the closed catalog",
              all(opsd[i]["op"] in CATALOG for i in order),
              {opsd[i]["op"] for i in order})
        kinds = {opsd[i]["op"] for i in order}
        check("scan finds a normalize_tag op (ProjectX)", "normalize_tag" in kinds, kinds)
        check("scan finds a fix_frontmatter op (beta missing status)", "fix_frontmatter" in kinds, kinds)
        check("scan finds a duplicate/merge op for the twin titles",
              "flag_duplicate" in kinds or "propose_merge" in kinds, kinds)
        check("scan finds a refresh_hub op for the client hub", "refresh_hub" in kinds, kinds)
        check("every op line has id/op/target/payload/confidence/rationale/status",
              all(all(k in opsd[i] for k in ("id", "op", "target", "payload", "confidence",
                                             "rationale", "status")) for i in order))

        # --- scan --json is valid + only catalog ops ---
        r = run("organize", ops, "scan", "--json")
        rows = [json.loads(l) for l in r.stdout.splitlines() if l.strip()]
        check("scan --json header is well-formed", rows and rows[0]["ops_json"] == 1 and rows[0]["ok"], r.stdout)
        check("scan --json rows are all catalog ops", all(x["op"] in CATALOG for x in rows[1:]), r.stdout)

        # --- review: ledger append semantics (latest status wins) ---
        tag_id = next(i for i in order if opsd[i]["op"] == "normalize_tag")
        r = run("organize", ops, "review", "--accept", tag_id)
        _, status2, _ = _read_queue(qpath)
        check("review --accept appends an approved status line", status2[tag_id] == "approved", status2)
        # defer then re-approve: latest wins
        run("organize", ops, "review", "--defer", tag_id)
        run("organize", ops, "review", "--accept", tag_id)
        _, status3, _ = _read_queue(qpath)
        check("latest status wins on replay (defer then accept -> approved)", status3[tag_id] == "approved")
        n_lines = len(qpath.read_text().splitlines())
        check("the queue is an append-only ledger (never rewritten)", n_lines == len(order) + 3, n_lines)

        # unapproved ops stay proposed
        check("unreviewed ops stay proposed",
              any(status3[i] == "proposed" for i in order if i != tag_id))

        # --- apply requires --yes (confirm-class self-gate) ---
        r = run("organize", ops, "apply")
        check("apply without --yes is confirm (exit 3)", r.returncode == 3, r.stdout + r.stderr)

        # --- apply --dry-run is a true read: previews the replay, writes nothing (Part 0.5) ---
        before_dry = len(git(ops, "log", "--oneline").stdout.splitlines())
        alpha_before = (ops / "wiki" / "notes" / "alpha.md").read_text()
        r = run("organize", ops, "apply", "--dry-run", "--json")
        rows = [json.loads(l) for l in r.stdout.splitlines() if l.strip()]
        check("apply --dry-run runs without --yes (exit 0)", r.returncode == 0, r.stdout + r.stderr)
        check("apply --dry-run header flags dry_run and counts the approved op",
              rows and rows[0].get("dry_run") is True and rows[0].get("applied") == 1, rows[:1])
        check("apply --dry-run reports would-apply, not applied",
              any(x.get("result") == "would apply" for x in rows[1:])
              and not any(x.get("result") == "applied" for x in rows[1:]), rows[1:])
        check("apply --dry-run makes no commit",
              len(git(ops, "log", "--oneline").stdout.splitlines()) == before_dry)
        check("apply --dry-run leaves the target untouched",
              (ops / "wiki" / "notes" / "alpha.md").read_text() == alpha_before)
        _, status_dry, _ = _read_queue(qpath)
        check("apply --dry-run appends nothing to the ledger", status_dry[tag_id] == "approved", status_dry)

        # --- apply replays only the approved op, one commit ---
        before = len(git(ops, "log", "--oneline").stdout.splitlines())
        r = run("organize", ops, "apply", "--yes")
        after = len(git(ops, "log", "--oneline").stdout.splitlines())
        alpha = (ops / "wiki" / "notes" / "alpha.md").read_text()
        check("apply normalized the approved tag (ProjectX -> projectx)",
              "projectx" in alpha and "ProjectX" not in alpha, alpha[:160])
        check("apply made exactly one commit (only one op approved)", after - before == 1, f"{before}->{after}")
        check("apply commit message carries the op + rationale",
              "organize: normalize_tag" in git(ops, "log", "-1", "--pretty=%s").stdout,
              git(ops, "log", "-1", "--pretty=%s").stdout)
        _, status4, _ = _read_queue(qpath)
        check("applied op is marked applied in the ledger", status4[tag_id] == "applied", status4)

        # re-apply is a no-op (already applied)
        before = len(git(ops, "log", "--oneline").stdout.splitlines())
        run("organize", ops, "apply", "--yes")
        after = len(git(ops, "log", "--oneline").stdout.splitlines())
        check("re-apply of an applied op is a no-op", after == before, f"{before}->{after}")


def test_apply_rails() -> None:
    # budget stops; protected refused; manual skipped; unknown op rejected
    with tempfile.TemporaryDirectory() as td:
        ops = Path(td) / "ops"
        for d in ("wiki/notes", "wiki/clients", "journal", "inbox/organize"):
            (ops / d).mkdir(parents=True, exist_ok=True)
        W = ops / "wiki"
        note(W / "notes" / "n1.md", {"type": "note", "title": "N1", "status": "active", "tags": "[Foo]"})
        note(W / "notes" / "n2.md", {"type": "note", "title": "N2", "status": "active", "tags": "[Bar]"})
        note(W / "clients" / "acme.md", {"type": "client", "title": "Acme", "status": "active", "tags": "[BadTag]"})
        git(ops, "init", "-q"); git(ops, "config", "user.email", "t@x.co"); git(ops, "config", "user.name", "t")
        git(ops, "add", "-A"); git(ops, "commit", "-qm", "seed")
        qpath = ops / "inbox" / "organize" / "2026-07-02.jsonl"

        def op(oid, kind, target, payload, conf=0.9):
            return json.dumps({"id": oid, "op": kind, "target": target, "payload": payload,
                               "confidence": conf, "rationale": "test", "status": "proposed"})
        # non-appliable ops (protected/manual/off-catalog) FIRST so a tight budget still evaluates
        # them (they never consume budget); the two appliable ops come last.
        lines = [
            op("normalize_tag-p", "normalize_tag", "acme", {"from": "BadTag", "to": "badtag"}),   # protected hub
            op("retitle-m", "retitle", "n1", {"to": "New"}),                                       # manual
            op("frobnicate-z", "frobnicate", "n1", {}),                                            # off-catalog
            op("normalize_tag-a", "normalize_tag", "n1", {"from": "Foo", "to": "foo"}),
            op("normalize_tag-b", "normalize_tag", "n2", {"from": "Bar", "to": "bar"}),
            json.dumps({"id": "normalize_tag-p", "status": "approved"}),
            json.dumps({"id": "retitle-m", "status": "approved"}),
            json.dumps({"id": "frobnicate-z", "status": "approved"}),
            json.dumps({"id": "normalize_tag-a", "status": "approved"}),
            json.dumps({"id": "normalize_tag-b", "status": "approved"}),
        ]
        qpath.write_text("\n".join(lines) + "\n", encoding="utf-8")

        # budget = 1 op: only the first approved appliable op runs
        before = len(git(ops, "log", "--oneline").stdout.splitlines())
        r = run("organize", ops, "apply", "--yes", "--max-ops", "1", "--json")
        after = len(git(ops, "log", "--oneline").stdout.splitlines())
        rows = [json.loads(l) for l in r.stdout.splitlines() if l.strip()]
        header = rows[0]
        check("edit budget stops apply at max-ops", header["applied"] == 1 and header["budget_hit"], header)
        check("budget apply made exactly one commit", after - before == 1, f"{before}->{after}")
        outcomes = {x["id"]: x["result"] for x in rows[1:]}
        check("protected hub op is refused (review-only)", outcomes.get("normalize_tag-p") == "skipped", outcomes)
        check("manual op (retitle) is never auto-applied", outcomes.get("retitle-m") == "skipped", outcomes)
        check("off-catalog op is rejected at read time", outcomes.get("frobnicate-z") == "rejected", outcomes)

        # lift the budget: the second approved op applies; protected/manual/unknown still refused
        r = run("organize", ops, "apply", "--yes", "--json")
        rows = [json.loads(l) for l in r.stdout.splitlines() if l.strip()]
        acme = (W / "clients" / "acme.md").read_text()
        check("protected path is never mutated even when approved", "BadTag" in acme, acme[:160])
        n2 = (W / "notes" / "n2.md").read_text()
        check("the remaining approved appliable op applied on the next run", "bar" in n2 and "Bar" not in n2, n2[:160])

        # --safe-only: a low-confidence add_link is excluded
        qp2 = ops / "inbox" / "organize" / "2026-07-03.jsonl"
        qp2.write_text(
            op("add_link-lo", "add_link", "n1", {"to": "n2"}, conf=0.5) + "\n"
            + json.dumps({"id": "add_link-lo", "status": "approved"}) + "\n", encoding="utf-8")
        r = run("organize", ops, "apply", "--yes", "--safe-only", "--json")
        rows = [json.loads(l) for l in r.stdout.splitlines() if l.strip()]
        check("--safe-only excludes a sub-0.8 op", rows[0]["applied"] == 0, rows)


def test_doctor_registry() -> None:
    with tempfile.TemporaryDirectory() as td:
        ops = Path(td) / "ops"
        for d in ("wiki", "journal", "jobs", "bin", "tasks/inbox", "tasks/active", "tasks/waiting",
                  "tasks/done", "inbox", "templates", "skills"):
            (ops / d).mkdir(parents=True, exist_ok=True)
        # a clean registry: only safe_write scan scheduled
        (ops / "jobs" / "registry.json").write_text(json.dumps({
            "jobs": {"organize_scan": {"command": "plainkeep organize scan",
                                       "schedule": {"weekly": "Sun 03:00"}, "risk": "safe_write"}}}),
            encoding="utf-8")
        r = run("doctor", ops)
        out = r.stdout + r.stderr
        check("doctor OKs a registry that schedules only safe verbs",
              "schedules only read/safe_write" in out, out)
        # a bad registry: a job self-declares confirm risk
        (ops / "jobs" / "registry.json").write_text(json.dumps({
            "jobs": {"bad": {"command": "plainkeep organize scan", "schedule": {"daily": "01:00"},
                             "risk": "confirm"}}}), encoding="utf-8")
        r = run("doctor", ops)
        out = r.stdout + r.stderr
        check("doctor warns on a confirm/deny-class scheduled job",
              "confirm/deny-class job(s) scheduled" in out, out)


def main() -> int:
    test_distill_and_promotion()
    test_organize()
    test_apply_rails()
    test_doctor_registry()
    print(f"{BOLD}distill + self-organization loop (4.2/4.4) — {len(results)} checks{RESET}\n")
    passed = sum(1 for _, ok, _ in results if ok)
    for name, ok, detail in results:
        mark = f"{GREEN}PASS{RESET}" if ok else f"{RED}FAIL{RESET}"
        print(f"  {mark} {name:<58}" + (f" {DIM}{detail.strip()[:80]}{RESET}" if (detail and not ok) else ""))
    failed = len(results) - passed
    print(f"\n{BOLD}Result:{RESET} {GREEN}{passed} passed{RESET}, "
          f"{(RED if failed else DIM)}{failed} failed{RESET}, {len(results)} checks")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
