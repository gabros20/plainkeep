#!/usr/bin/env python3
"""
plainkeep organize scan | review | apply — the self-organization loop (proposal Part 4.4). The safety
architecture every future autonomous capability reuses: propose-then-approve, never direct edits.

  scan    ZERO-LLM candidate generation (orphan/near-duplicate/link/tag-case/frontmatter heuristics)
          -> a typed proposal queue inbox/organize/<date>.jsonl, one op per line. A model (agent.py,
          scope=read) only RANKS/LABELS when PLAINKEEP_AGENT is set; with none, heuristic confidences. A
          hallucinating model can only mis-rank, never mutate.
  review  triage-style paging showing the EXACT diff each op would produce; accept/reject/defer are
          APPENDED to the queue as new status lines (audit ledger — latest status wins on replay,
          the file is never rewritten in place).
  apply   deterministic replay of APPROVED ops only — NO model at apply time. Hard rails: one git
          commit per op, a per-run edit budget (default 20 ops / 300 lines; flags may only LOWER it),
          protected paths (conventions/index/hubs/pinned) review-only regardless of confidence, and
          retitle / propose_merge / deletions NEVER auto-apply. --safe-only applies only the four
          additive primitives above 0.8 confidence. apply is confirm-class (needs --yes; --dry-run
          previews the exact replay writing nothing, per the Part 0.5 contract) and is NEVER
          schedulable (doctor warns on any confirm/deny verb in jobs/registry.json).

CLOSED op catalog (Mem0 lesson) — validated at BOTH write and read time; anything else is rejected:
  add_link · refresh_hub · normalize_tag · fix_frontmatter · retitle · flag_duplicate · propose_merge
"""
import difflib
import hashlib
import json
import re
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lib import agent, output, paths, vaultio  # noqa: E402

GREEN, RED, YEL, DIM, CYAN, RESET = "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[36m", "\033[0m"

CATALOG = {"add_link", "refresh_hub", "normalize_tag", "fix_frontmatter",
           "retitle", "flag_duplicate", "propose_merge"}
MANUAL = {"retitle", "propose_merge"}                 # never auto-applied even when approved
SAFE = {"add_link", "refresh_hub", "normalize_tag", "fix_frontmatter"}  # --safe-only set
DEFAULT_MAX_OPS = 20
DEFAULT_MAX_LINES = 300

QUEUE_DIR = paths.PLAINKEEP_HOME / "inbox" / "organize"


# --------------------------------------------------------------------------- queue I/O (the ledger)

def _queue_path(argv) -> Path:
    """The queue file to operate on: `--file <path>`, else the newest inbox/organize/*.jsonl."""
    if "--file" in argv:
        i = argv.index("--file")
        return Path(argv[i + 1]) if i + 1 < len(argv) else QUEUE_DIR / "missing.jsonl"
    files = sorted(QUEUE_DIR.glob("*.jsonl")) if QUEUE_DIR.exists() else []
    return files[-1] if files else QUEUE_DIR / f"{date.today().isoformat()}.jsonl"


def _read_queue(path: Path):
    """Replay the append-only ledger. Returns (ops{id:def}, status{id:latest}, order[ids]). A line
    with an `op` key defines an op (status defaults 'proposed'); a line with only `status` overrides
    that op's status — latest wins. Op defs whose `op` is off the catalog are kept but flagged."""
    ops, status, order = {}, {}, []
    if not path.exists():
        return ops, status, order
    for ln in path.read_text(encoding="utf-8").splitlines():
        ln = ln.strip()
        if not ln:
            continue
        try:
            obj = json.loads(ln)
        except Exception:
            continue
        oid = obj.get("id")
        if not oid:
            continue
        if "op" in obj:
            if oid not in ops:
                order.append(oid)
            ops[oid] = obj
            status[oid] = obj.get("status", "proposed")
        elif "status" in obj:
            status[oid] = obj["status"]
    return ops, status, order


def _append_status(path: Path, oid: str, st: str) -> None:
    from datetime import datetime, timezone
    rec = {"id": oid, "status": st, "ts": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    with vaultio.open_append(path, encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


# ------------------------------------------------------------------------------------- vault model

def _notes() -> dict:
    if not paths.WIKI.exists():
        return {}
    out = {}
    for p in sorted(paths.WIKI.rglob("*.md")):
        if any(part in (".obsidian", ".trash", ".smart-env") for part in p.parts):
            continue
        out.setdefault(p.stem, p)
    return out


def _resolve(slug: str):
    return _notes().get(slug)


_WORD = re.compile(r"[a-z0-9]{3,}")


def _tokens(text: str) -> set:
    return set(_WORD.findall(text.lower()))


def _jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _protected(path: Path):
    """(is_protected, why) — conventions, index files, entity hubs, and pinned notes are review-only
    regardless of confidence (Part 4.4)."""
    rel = path.relative_to(paths.PLAINKEEP_HOME).as_posix()
    if rel == "wiki/conventions.md":
        return True, "wiki/conventions.md"
    if path.name == "index.md":
        return True, "index file"
    if rel.startswith("wiki/clients/") or rel.startswith("wiki/projects/"):
        return True, "entity hub"
    if str(paths.fm_field(path, "pinned")).lower() in ("true", "yes", "1"):
        return True, "pinned: true"
    return False, ""


# --------------------------------------------------------------------------- deterministic transforms

def _add_related(text: str, link: str) -> str:
    line = f"- [[{link}]]"
    if f"[[{link}]]" in text:
        return text
    if "## Related" in text:
        out, done = [], False
        for ln in text.splitlines():
            out.append(ln)
            if ln.strip() == "## Related" and not done:
                out.append(line)
                done = True
        return "\n".join(out) + ("\n" if text.endswith("\n") else "")
    return text.rstrip("\n") + f"\n\n## Related\n{line}\n"


def _set_field(text: str, field: str, value: str) -> str:
    if re.search(rf"(?m)^{re.escape(field)}:", text):
        return re.sub(rf"(?m)^{re.escape(field)}:.*$", f"{field}: {value}", text, count=1)
    lines = text.splitlines(keepends=True)
    if lines and lines[0].strip() == "---":
        for i in range(1, len(lines)):
            if lines[i].strip() == "---":
                lines.insert(i, f"{field}: {value}\n")
                return "".join(lines)
    return f"---\n{field}: {value}\n---\n" + text


def _normalize_tag(text: str, frm: str, to: str):
    def repl(m):
        items = [x.strip() for x in m.group(1).split(",") if x.strip()]
        items = [to if x == frm else x for x in items]
        seen = []
        for x in items:
            if x not in seen:
                seen.append(x)
        return "tags: [" + ", ".join(seen) + "]"
    new = re.sub(r"(?m)^tags:\s*\[([^\]]*)\]", repl, text, count=1)
    return new if new != text else None


def _transform(op: dict, text: str):
    """Return (new_text, note) — new_text is None for a no-op or a manual op (never auto-applied)."""
    kind, payload = op["op"], op.get("payload", {}) or {}
    if kind in MANUAL:
        return None, "manual — never auto-applied (review-only)"
    if kind == "add_link":
        new = _add_related(text, payload.get("to", ""))
        return (new, "added link") if new != text else (None, "already linked")
    if kind == "refresh_hub":
        new = text
        for s in payload.get("add", []):
            new = _add_related(new, s)
        return (new, "hub refreshed") if new != text else (None, "hub already lists these")
    if kind == "normalize_tag":
        new = _normalize_tag(text, payload.get("from", ""), payload.get("to", ""))
        return (new, "tag normalized") if new else (None, "tag not present")
    if kind == "fix_frontmatter":
        field, val = payload.get("field", ""), payload.get("to", "")
        if not field:
            return None, "no field"
        new = _set_field(text, field, val)
        return (new, f"{field} set") if new != text else (None, "already set")
    if kind == "flag_duplicate":
        dup = payload.get("duplicate_of", "")
        if f'duplicate_of: "[[{dup}]]"' in text:
            return None, "already flagged"
        return _set_field(text, "duplicate_of", f'"[[{dup}]]"'), "duplicate flagged"
    return None, "unknown op"


def _difflines(old: str, new: str) -> int:
    return sum(1 for ln in difflib.ndiff(old.splitlines(), new.splitlines())
               if ln[:1] in ("+", "-"))


def _diff_str(path: Path, new: str) -> str:
    old = path.read_text(encoding="utf-8")
    rel = path.relative_to(paths.PLAINKEEP_HOME).as_posix()
    d = list(difflib.unified_diff(old.splitlines(), new.splitlines(),
                                  fromfile=rel, tofile=rel, lineterm="", n=1))
    return "\n".join(d[:20])


# ----------------------------------------------------------------------------- scan (candidate gen)

def _oid(op: str, target: str, payload: dict) -> str:
    raw = op + "|" + target + "|" + json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return op.split("_")[0] + "-" + hashlib.sha1(raw.encode()).hexdigest()[:8]


def _mk(op: str, target: str, payload: dict, conf: float, why: str) -> dict:
    return {"id": _oid(op, target, payload), "op": op, "target": target, "payload": payload,
            "confidence": round(conf, 2), "rationale": why, "status": "proposed"}


def _candidates() -> list:
    notes = _notes()
    texts = {s: p.read_text(encoding="utf-8") for s, p in notes.items()}
    toks = {s: _tokens(texts[s]) for s in notes}
    title = {s: (paths.fm_field(notes[s], "title") or paths.title_of(notes[s])) for s in notes}
    ttoks = {s: _tokens(title[s]) for s in notes}
    typ = {s: paths.fm_field(notes[s], "type") or "note" for s in notes}
    outn = {s: set(paths.link_targets(texts[s])) for s in notes}
    inbound = {s: 0 for s in notes}
    for s in notes:
        for t in outn[s]:
            if t in inbound and t != s:
                inbound[t] += 1

    ops, seen = [], set()

    def add(o):
        if o["id"] not in seen:
            seen.add(o["id"])
            ops.append(o)

    concept = [s for s in notes if typ[s] == "note" and s not in ("index", "conventions")]

    # tag-case variants -> normalize_tag (deterministic, high confidence)
    for s in notes:
        for tg in paths.fm_list(notes[s], "tags"):
            canon = re.sub(r"[^a-z0-9]+", "-", tg.lower()).strip("-")
            if canon and canon != tg:
                add(_mk("normalize_tag", s, {"from": tg, "to": canon}, 0.92,
                        f"tag '{tg}' is not lowercase-hyphenated"))

    # missing frontmatter -> fix_frontmatter
    for s in concept:
        if not paths.fm_field(notes[s], "status"):
            add(_mk("fix_frontmatter", s, {"field": "status", "to": "active"}, 0.85,
                    "note is missing a status: field"))
        if not paths.fm_field(notes[s], "updated"):
            add(_mk("fix_frontmatter", s, {"field": "updated", "to": paths.today()}, 0.8,
                    "note is missing an updated: field"))

    # near-duplicate title overlap -> flag_duplicate (0.6-0.8) / propose_merge (>=0.8)
    cs = sorted(concept)
    for i, a in enumerate(cs):
        for b in cs[i + 1:]:
            jt = _jaccard(ttoks[a], ttoks[b])
            if jt >= 0.8:
                add(_mk("propose_merge", b, {"merge_with": a}, jt,
                        f"title nearly identical to [[{a}]] ({jt:.0%})"))
            elif jt >= 0.6:
                add(_mk("flag_duplicate", b, {"duplicate_of": a}, jt,
                        f"title overlaps [[{a}]] ({jt:.0%})"))

    # orphans -> add_link from the best-overlapping non-orphan (gives the orphan an inbound link)
    for o in concept:
        if inbound[o] != 0:
            continue
        best, bs = None, 0.0
        for s in concept:
            if s == o or o in outn[s]:
                continue
            j = _jaccard(toks[o], toks[s])
            if j > bs:
                best, bs = s, j
        if best and bs >= 0.15:
            add(_mk("add_link", best, {"to": o}, min(bs + 0.2, 0.95),
                    f"[[{o}]] is an orphan; [[{best}]] shares {bs:.0%} of its terms"))

    # hub back-references -> refresh_hub (protected -> review-only, but still proposed)
    for h in notes:
        if not (typ[h] in ("client", "project", "area")
                or notes[h].relative_to(paths.PLAINKEEP_HOME).as_posix().startswith(("wiki/clients/", "wiki/projects/"))):
            continue
        missing = [s for s in notes if h in outn[s] and s not in outn[h] and s != h]
        if missing:
            add(_mk("refresh_hub", h, {"add": sorted(missing)}, 0.7,
                    f"{len(missing)} note(s) link to [[{h}]] without a back-reference"))

    return ops


def _rank(ops: list) -> bool:
    """Optional LLM RANK/LABEL pass (scope=read): adjust confidences only, never mutate. Best-effort;
    returns True if a model was consulted. With PLAINKEEP_AGENT=none this is skipped entirely."""
    if not ops or not agent.available():
        return False
    listing = "\n".join(f"{o['id']}\t{o['op']}\t{o['target']}\t{o['rationale']}" for o in ops[:60])
    ans = agent.run_agent(
        "Rank these proposed knowledge-base maintenance ops by how safe+useful each is. Reply with "
        "ONLY a JSON object mapping id -> a confidence float in [0,1]. No prose.\n\n" + listing,
        scope="read")
    try:
        scores = json.loads(re.sub(r"```(?:json)?", "", ans or "").strip())
        if isinstance(scores, dict):
            for o in ops:
                if o["id"] in scores:
                    o["confidence"] = round(max(0.0, min(1.0, float(scores[o["id"]]))), 2)
            return True
    except Exception:
        pass
    return False


def cmd_scan(argv, dry):
    ops = _candidates()
    # write-time catalog guard (Mem0 lesson): a generated op MUST be in the catalog.
    ops = [o for o in ops if o["op"] in CATALOG]
    ranked = _rank(ops)
    ops.sort(key=lambda o: -o["confidence"])
    qpath = QUEUE_DIR / f"{date.today().isoformat()}.jsonl"
    rel = qpath.relative_to(paths.PLAINKEEP_HOME).as_posix()
    rows = [{"id": o["id"], "op": o["op"], "target": o["target"], "confidence": o["confidence"],
             "rationale": o["rationale"], "status": "proposed"} for o in ops]
    if not dry:
        vaultio.mkdir(QUEUE_DIR)
        vaultio.write_text(qpath, "".join(json.dumps(o, ensure_ascii=False) + "\n" for o in ops), encoding="utf-8")
        paths.append_journal(f"organize scan -> {len(ops)} proposed op(s) in {rel}")

    def render(rs):
        out = [f"{'would write' if dry else 'wrote'} {len(rs)} proposed op(s) -> {rel} "
               f"{DIM}({'agent-ranked' if ranked else 'heuristic confidences'}){RESET}:"]
        for r in rs:
            out.append(f"  {DIM}{r['confidence']:.2f}{RESET} {r['op']:<15} {CYAN}{r['target']:<24}{RESET} "
                       f"{DIM}{r['rationale'][:50]}{RESET}")
        out.append(f"\n  review:  plainkeep organize review        apply:  plainkeep organize apply --yes"
                   + ("  (dry run — nothing written)" if dry else ""))
        return "\n".join(out)

    return output.emit_rows(rows, "organize", human=render,
                            header={"queue": rel, "proposed": len(rows), "agent": ranked, "dry_run": dry})


# ----------------------------------------------------------------------------------------- review

def cmd_review(argv, dry, yes, js):
    qpath = _queue_path(argv)
    ops, status, order = _read_queue(qpath)
    rel = qpath.relative_to(paths.PLAINKEEP_HOME).as_posix() if _under_home(qpath) else str(qpath)
    pending = [oid for oid in order if status.get(oid) == "proposed"]

    def _ids(flag):
        if flag not in argv:
            return []
        i = argv.index(flag)
        v = argv[i + 1] if i + 1 < len(argv) else ""
        return order if v == "all" else [x for x in v.split(",") if x]

    accept, reject, defer = _ids("--accept"), _ids("--reject"), _ids("--defer")
    if accept or reject or defer:                       # non-interactive ledger append
        applied = []
        for oid, st in (*[(i, "approved") for i in accept], *[(i, "rejected") for i in reject],
                        *[(i, "deferred") for i in defer]):
            if oid in ops:
                _append_status(qpath, oid, st)
                applied.append({"id": oid, "status": st})
        return output.emit_rows(applied, "organize", header={"queue": rel, "updated": len(applied)},
                                human=lambda rs: "\n".join(f"  {r['status']:<9} {r['id']}" for r in rs)
                                or "no matching op ids")

    if js:
        rows = [{"id": oid, "op": ops[oid]["op"], "target": ops[oid]["target"],
                 "confidence": ops[oid].get("confidence"), "status": status.get(oid),
                 "diff": _preview_diff(ops[oid])} for oid in order]
        return output.emit_rows(rows, "organize", header={"queue": rel, "pending": len(pending)})

    if not order:
        print(f"no proposal queue (run `plainkeep organize scan`).")
        return 0
    print(f"organize review: {len(pending)} pending / {len(order)} op(s) in {rel}\n")
    for oid in pending:
        o = ops[oid]
        print(f"• {oid}  {o['op']}  ->  {o['target']}   {DIM}({o.get('confidence')}: {o['rationale']}){RESET}")
        diff = _preview_diff(o)
        for ln in (diff or "(no textual change / manual op)").splitlines():
            print(f"    {ln}")
        if dry:
            continue
        choice = "a" if yes else (input("    [a]ccept / [r]eject / [d]efer / [s]kip ? ").strip().lower() or "s")
        st = {"a": "approved", "r": "rejected", "d": "deferred"}.get(choice)
        if st:
            _append_status(qpath, oid, st)
            print(f"    -> {st}")
        else:
            print("    skipped")
    if dry:
        print("\n(dry run — no status recorded)")
    return 0


def _under_home(p: Path) -> bool:
    try:
        p.relative_to(paths.PLAINKEEP_HOME)
        return True
    except ValueError:
        return False


def _preview_diff(op: dict) -> str:
    if op["op"] in MANUAL:
        return "(manual op — never auto-applied)"
    path = _resolve(op["target"])
    if not path:
        return "(target not found)"
    prot, why = _protected(path)
    new, note = _transform(op, path.read_text(encoding="utf-8"))
    if new is None:
        return f"({note})"
    d = _diff_str(path, new)
    return (f"[protected: {why} — review-only]\n" + d) if prot else d


# ------------------------------------------------------------------------------------------ apply

def _commit(rel: str, msg: str) -> None:
    paths.git("add", "-A", "--", rel)
    paths.git("commit", "-m", msg)


def _int_flag(argv, name, default):
    """A budget flag may only LOWER the default (a run can never widen its own budget)."""
    if name in argv:
        i = argv.index(name)
        try:
            return min(int(argv[i + 1]), default)
        except Exception:
            return default
    return default


def cmd_apply(argv, dry: bool = False):
    yes = "--yes" in argv or "-y" in argv
    if not yes and not dry:                             # confirm-class: self-gate for direct calls too
        output.fail(output.EXIT_CONFIRM,
                    "plainkeep organize apply is confirm-class — re-run with --yes to replay approved ops"
                    " (or preview with --dry-run)",
                    hint="re-run: plainkeep organize apply --yes", verb="organize")
    safe_only = "--safe-only" in argv
    max_ops = _int_flag(argv, "--max-ops", DEFAULT_MAX_OPS)
    max_lines = _int_flag(argv, "--max-lines", DEFAULT_MAX_LINES)
    qpath = _queue_path(argv)
    ops, status, order = _read_queue(qpath)
    rel_q = qpath.relative_to(paths.PLAINKEEP_HOME).as_posix() if _under_home(qpath) else str(qpath)

    applied = skipped = rejected = 0
    changed_lines = 0
    results, budget_hit = [], False
    for oid in order:
        o = ops[oid]
        def note(st, msg):
            results.append({"id": oid, "op": o["op"], "target": o["target"], "result": st, "detail": msg})
        if o["op"] not in CATALOG:                      # read-time catalog guard
            rejected += 1; note("rejected", f"off-catalog op '{o['op']}'"); continue
        if status.get(oid) != "approved":
            skipped += 1; note("skipped", f"status is {status.get(oid)}"); continue
        if o["op"] in MANUAL:
            skipped += 1; note("skipped", "manual op — never auto-applied"); continue
        conf = float(o.get("confidence") or 0)
        if safe_only and (o["op"] not in SAFE or conf < 0.8):
            skipped += 1; note("skipped", "excluded by --safe-only (<0.8 or not additive)"); continue
        path = _resolve(o["target"])
        if not path:
            skipped += 1; note("skipped", "target not found"); continue
        prot, why = _protected(path)
        if prot:
            skipped += 1; note("skipped", f"protected: {why} (review-only)"); continue
        new, msg = _transform(o, path.read_text(encoding="utf-8"))
        if new is None:
            skipped += 1; note("skipped", msg); continue
        nlines = _difflines(path.read_text(encoding="utf-8"), new)
        if applied >= max_ops or changed_lines + nlines > max_lines:
            budget_hit = True
            note("deferred", f"edit budget reached ({applied}/{max_ops} ops, {changed_lines}/{max_lines} lines)")
            break
        if not dry:                                     # a true dry-run IS a read: no write/commit/ledger
            rel = path.relative_to(paths.PLAINKEEP_HOME).as_posix()
            vaultio.write_text(path, new, encoding="utf-8")
            _commit(rel, f"organize: {o['op']} {o['target']} — {o['rationale']}")
            _append_status(qpath, oid, "applied")
        applied += 1; changed_lines += nlines
        note("would apply" if dry else "applied", msg)
    if not dry:
        paths.append_journal(f"organize apply -> {applied} op(s) applied, {skipped} skipped, {rejected} rejected"
                             + (" (budget reached)" if budget_hit else ""))

    def render(rs):
        verbed = "would apply" if dry else "applied"
        out = [f"organize apply{' (dry run)' if dry else ''}: {GREEN}{applied} {verbed}{RESET}, "
               f"{skipped} skipped, {rejected} rejected  {DIM}({rel_q}){RESET}"]
        for r in rs:
            c = {"applied": GREEN, "would apply": GREEN, "rejected": RED, "deferred": YEL}.get(r["result"], DIM)
            out.append(f"  {c}{r['result']:<11}{RESET} {r['op']:<15} {r['target']:<24} {DIM}{r['detail'][:44]}{RESET}")
        if budget_hit:
            out.append(f"  {YEL}stopped at the edit budget — re-run to continue{RESET}")
        if dry:
            out.append(f"  {DIM}(dry run — nothing written; apply for real: plainkeep organize apply --yes){RESET}")
        return "\n".join(out)

    return output.emit_rows(results, "organize", human=render,
                            header={"queue": rel_q, "applied": applied, "skipped": skipped,
                                    "rejected": rejected, "budget_hit": budget_hit, "dry_run": dry})


def main(argv):
    _, argv = output.parse_argv(argv)
    dry = "--dry-run" in argv
    yes = "--yes" in argv or "-y" in argv
    js = output.json_mode()
    action = argv[0] if argv else "scan"
    rest = argv[1:]
    if action == "scan":
        return cmd_scan(rest, dry)
    if action == "review":
        return cmd_review(rest, dry, yes, js)
    if action == "apply":
        return cmd_apply(rest, dry)
    output.fail(output.EXIT_USAGE,
                "usage: plainkeep organize scan [--dry-run] | review [--accept|--reject|--defer <ids|all>] "
                "[--dry-run|--yes] | apply [--yes|--dry-run] [--safe-only] [--max-ops N] [--max-lines N]",
                verb="organize")


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
