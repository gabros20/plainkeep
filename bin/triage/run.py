#!/usr/bin/env python3
"""
plainkeep triage [--dry-run|--yes] | drafts [--dry-run|--yes] — PROPOSE filing of inbox/ items into a task
or a wiki note; the human approves (§4.1, §10). Interactive by default; --dry-run shows proposals
only; --yes accepts all.

Classification here is the deterministic pure-shell fallback (no agent required). When an agent is
wired (PLAINKEEP_AGENT), it would improve the proposal — but the system works without it.
On an override (you pick differently than proposed), it offers to record a one-line rule in
wiki/conventions.md ## Filing rules — the plaintext learning loop.

`plainkeep triage drafts` (proposal Part 4.2) pages the agent-drafted concept notes (`author: agent`,
`status: draft`) that `plainkeep files distill` produced as a PROMOTION queue: accept -> status active,
reject -> delete, one git commit each — the human is the gate on machine-authored knowledge.
"""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lib import agent, filing, output, paths, vaultio  # noqa: E402

# An item is a TASK when its first word is an imperative action verb (matched as a whole word,
# so "merges"/"writing" don't false-trigger), or it's an explicit todo/checkbox line.
ACTION_VERBS = {"fix", "call", "email", "send", "ask", "schedule", "review", "ping", "pay", "buy",
                "book", "draft", "update", "write", "check", "reply", "chase", "invoice", "deploy",
                "merge", "rename", "do", "make", "add", "remove", "create", "investigate", "prep",
                "prepare", "ship", "test", "refactor", "migrate", "follow"}


def parse_item(p: Path) -> str:
    t = p.read_text(encoding="utf-8")
    if t.startswith("---"):
        e = t.find("\n---", 3)
        if e != -1:
            t = t[e + 4:]
    return t.strip()


def classify(text: str) -> str:
    if not text.strip():
        return "note"
    # §6: if an agent is configured, borrow its judgment; otherwise fall through to the shell rule.
    if agent.available():
        ans = agent.run_agent(
            "Classify this captured item as exactly one word — 'task' (an action to do) or "
            f"'note' (a fact/idea to keep). Reply with only the word.\n\n{text}", scope="read")
        if ans:
            a = ans.strip().lower()
            if "task" in a and "note" not in a:
                return "task"
            if "note" in a and "task" not in a:
                return "note"
    first = text.splitlines()[0].lower()
    if first.startswith(("- [ ]", "todo", "[]")) or "follow up" in first:
        return "task"
    words = re.findall(r"[a-z]+", first)
    if words and words[0] in ACTION_VERBS:
        return "task"
    return "note"


def make_task(text: str) -> Path:
    title = text.splitlines()[0][:70] if text.strip() else "task"
    return filing.create_task(title, intent=text, source="triage")


def make_note(text: str) -> Path:
    return filing.create_note(text)


def record_rule(rule: str):
    conv = paths.WIKI / "conventions.md"
    if not conv.exists():
        return
    txt = conv.read_text(encoding="utf-8")
    if "## Filing rules" in txt:
        txt = txt.replace("## Filing rules", f"## Filing rules\n- {rule}", 1)
    else:
        txt += f"\n## Filing rules\n- {rule}\n"
    vaultio.write_text(conv, txt, encoding="utf-8")


def items():
    if not paths.INBOX.exists():
        return []
    return sorted(p for p in paths.INBOX.iterdir() if p.suffix in (".md", ".txt") and p.name != ".gitkeep")


def _draft_notes() -> list:
    """Agent-drafted concept notes awaiting promotion (author: agent + status: draft)."""
    if not paths.WIKI.exists():
        return []
    return sorted(p for p in paths.WIKI.rglob("*.md")
                  if paths.fm_field(p, "author") == "agent" and paths.fm_field(p, "status") == "draft")


def _commit(rel: str, msg: str) -> None:
    paths.git("add", "-A", "--", rel)
    paths.git("commit", "-m", msg)


def cmd_drafts(argv, dry, yes, js):
    notes = _draft_notes()
    if js:
        rows = [{"slug": p.stem, "title": paths.fm_field(p, "title") or p.stem,
                 "source": paths.fm_field(p, "source"),
                 "path": str(p.relative_to(paths.PLAINKEEP_HOME))} for p in notes]
        return output.emit_rows(rows, "triage", header={"drafts": len(notes)})
    if not notes:
        print("no agent-drafted notes to promote (run `plainkeep files distill <slug>` first).")
        return 0
    print(f"triage drafts: {len(notes)} agent-drafted note(s) awaiting promotion\n")
    for p in notes:
        title = paths.fm_field(p, "title") or p.stem
        src = paths.fm_field(p, "source")
        rel = str(p.relative_to(paths.PLAINKEEP_HOME))
        print(f"• {p.stem}: \"{title}\"" + (f"  from {src}" if src else ""))
        if dry:
            continue
        choice = "a" if yes else (input("    [a]ccept -> active / [r]eject -> delete / [s]kip ? ")
                                  .strip().lower() or "a")
        if choice in ("r", "reject"):
            p.unlink()
            _commit(rel, f"organize: reject agent draft {p.stem}")
            paths.append_journal(f"triage drafts: rejected {p.stem} (deleted)")
            print("    rejected (deleted)")
        elif choice in ("s", "skip"):
            print("    skipped")
        else:
            text = p.read_text(encoding="utf-8")
            vaultio.write_text(p, re.sub(r"(?m)^status:\s*draft\s*$", "status: active", text, count=1),
                         encoding="utf-8")
            _commit(rel, f"organize: promote agent draft {p.stem} -> active")
            paths.append_journal(f"triage drafts: promoted {p.stem} -> active")
            print("    promoted -> active")
    if dry:
        print("\n(dry run — nothing changed)")
    return 0


def cmd_decide(argv):
    """Non-interactive: apply ONE decision to ONE inbox item (the JSON/agent apply path). The
    interactive/list flows can't be driven headless, so a frontend calls this per item."""
    if len(argv) < 2:
        output.fail(output.EXIT_USAGE, "usage: plainkeep triage decide <item> task|note|skip", verb="triage")
    item, decision = argv[0], argv[1].lower()
    if decision not in ("task", "note", "skip"):
        output.fail(output.EXIT_USAGE, "decision must be one of task|note|skip", verb="triage")
    cands = [q for q in items() if q.name == item or q.stem == item]
    p = cands[0] if cands else None
    if not p or not p.exists():
        output.fail(output.EXIT_NOT_FOUND, f"no inbox item '{item}' (see `plainkeep triage --json`)", verb="triage")
    if decision == "skip":
        return output.emit({"item": p.name, "decision": "skip", "filed": None}, "triage",
                           human=lambda _: f"skipped {p.name}")
    text = parse_item(p)
    new = make_task(text) if decision == "task" else make_note(text)
    p.unlink()
    filed = str(new.relative_to(paths.PLAINKEEP_HOME))
    paths.append_journal(f"triaged {p.name} -> {filed} (decide {decision})")
    return output.emit({"item": p.name, "decision": decision, "filed": filed}, "triage",
                       human=lambda _: f"filed {p.name} -> {filed}")


def cmd_drafts_decide(argv):
    """Non-interactive: apply ONE decision to ONE agent-drafted note (the JSON/agent promote path)."""
    if len(argv) < 2:
        output.fail(output.EXIT_USAGE, "usage: plainkeep triage drafts decide <slug> accept|reject|skip", verb="triage")
    slug, decision = argv[0], argv[1].lower()
    if decision not in ("accept", "reject", "skip"):
        output.fail(output.EXIT_USAGE, "decision must be one of accept|reject|skip", verb="triage")
    p = next((q for q in _draft_notes() if q.stem == slug), None)
    if not p:
        output.fail(output.EXIT_NOT_FOUND, f"no agent-draft '{slug}' (see `plainkeep triage drafts --json`)", verb="triage")
    rel = str(p.relative_to(paths.PLAINKEEP_HOME))
    if decision == "skip":
        return output.emit({"slug": slug, "decision": "skip"}, "triage", human=lambda _: f"skipped {slug}")
    if decision == "reject":
        p.unlink()
        _commit(rel, f"organize: reject agent draft {slug}")
        paths.append_journal(f"triage drafts: rejected {slug} (deleted)")
        return output.emit({"slug": slug, "decision": "reject", "deleted": True}, "triage",
                           human=lambda _: f"rejected {slug} (deleted)")
    text = p.read_text(encoding="utf-8")
    vaultio.write_text(p, re.sub(r"(?m)^status:\s*draft\s*$", "status: active", text, count=1), encoding="utf-8")
    _commit(rel, f"organize: promote agent draft {slug} -> active")
    paths.append_journal(f"triage drafts: promoted {slug} -> active")
    return output.emit({"slug": slug, "decision": "accept", "status": "active"}, "triage",
                       human=lambda _: f"promoted {slug} -> active")


def main(argv):
    js, argv = output.parse_argv(argv)
    dry = "--dry-run" in argv
    yes = "--yes" in argv or "-y" in argv
    if argv and argv[0] == "decide":
        return cmd_decide(argv[1:])
    if argv and argv[0] == "drafts":
        if len(argv) > 1 and argv[1] == "decide":
            return cmd_drafts_decide(argv[2:])
        return cmd_drafts(argv[1:], dry, yes, js)
    its = items()

    if js:  # machine mode: emit proposals as rows, file nothing (agent files via task/wiki)
        rows = []
        for p in its:
            text = parse_item(p)
            kind = classify(text)
            title = (text.splitlines()[0] if text.strip() else p.stem)[:70]
            dest = "tasks/active/" if kind == "task" else f"wiki/notes/{paths.slugify(title)}.md"
            rows.append({"item": p.name, "title": title, "proposal": kind, "dest": dest})
        return output.emit_rows(rows, "triage", header={"items": len(its)})

    if not its:
        print("inbox is empty — nothing to triage.")
        return 0
    print(f"triage: {len(its)} item(s) in inbox/\n")
    for p in its:
        text = parse_item(p)
        kind = classify(text)
        title = (text.splitlines()[0] if text.strip() else p.stem)[:70]
        dest = "tasks/active/" if kind == "task" else f"wiki/notes/{paths.slugify(title)}.md"
        print(f"• {p.name}: \"{title}\"")
        print(f"    proposal: {kind.upper()} -> {dest}")
        if dry:
            continue
        choice = kind
        if not yes:
            ans = input("    [a]ccept / [t]ask / [n]ote / [s]kip ? ").strip().lower() or "a"
            choice = {"a": kind, "t": "task", "n": "note", "s": "skip"}.get(ans, kind)
        if choice == "skip":
            print("    skipped")
            continue
        new = make_task(text) if choice == "task" else make_note(text)
        p.unlink()
        paths.append_journal(f"triaged {p.name} -> {new.relative_to(paths.PLAINKEEP_HOME)}")
        print(f"    filed -> {new.relative_to(paths.PLAINKEEP_HOME)}")
        if (not yes) and choice != kind:  # override -> offer to learn the rule (§10)
            if input("    record a filing rule for next time? [y/N] ").strip().lower() == "y":
                record_rule(f"items like \"{title[:40]}\" -> {choice}")
                print("    rule recorded in wiki/conventions.md")
    if dry:
        print("\n(dry run — nothing changed)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
