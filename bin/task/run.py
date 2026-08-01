#!/usr/bin/env python3
"""plainkeep task list|add "<title>"|show <id>|move <id> <status>|done <id> [--dry-run] [--json] —
the task system (§7)."""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lib import filing, output, paths, vaultio  # noqa: E402

STATUSES = filing.STATUSES


def _find(task_id: str):
    for st in STATUSES:
        f = paths.TASKS / st / f"{task_id}.md"
        if f.exists():
            return f, st
    return None, None


def _set_status(f: Path, status: str):
    txt = f.read_text(encoding="utf-8")
    txt = re.sub(r"(?m)^status:.*$", f"status: {status}", txt, count=1)
    txt = re.sub(r"(?m)^updated:.*$", f"updated: {paths.today()}", txt, count=1)
    vaultio.write_text(f, txt, encoding="utf-8")


def cmd_list():
    rows = []
    for st in ("active", "waiting"):
        for f in sorted((paths.TASKS / st).glob("T-*.md")):
            rows.append({"id": f.stem, "title": paths.title_of(f), "status": st})

    def render(rs):
        if not rs:
            return "no active or waiting tasks. add one: plainkeep task add \"<title>\""
        out, cur = [], None
        for r in rs:
            if r["status"] != cur:
                cur = r["status"]
                out.append(f"{cur}/")
            out.append(f"  {r['id']}  {r['title']}")
        return "\n".join(out)

    return output.emit_rows(rows, "task", human=render)


def main(argv):
    _, argv = output.parse_argv(argv)
    dry = "--dry-run" in argv
    argv = [a for a in argv if a != "--dry-run"]
    action = argv[0] if argv else "list"
    if action == "list":
        return cmd_list()
    elif action == "add":
        title = " ".join(argv[1:]).strip()
        if not title:
            output.fail(output.EXIT_USAGE, 'usage: plainkeep task add "<title>"', verb="task")
        if dry:
            tid = filing.next_task_id()
            data = {"dry_run": True, "would_add": tid, "title": title[:70]}
            return output.emit(data, "task",
                               human=lambda _: f"would add {tid}: {title[:70]}  (dry run — nothing written)")
        f = filing.create_task(title, source="manual")
        paths.append_journal(f"task added {f.stem}: {title[:60]}")
        data = {"id": f.stem, "path": str(f.relative_to(paths.PLAINKEEP_HOME)), "title": title[:70]}
        return output.emit(data, "task",
                           human=lambda _: f"added {f.stem} -> {f.relative_to(paths.PLAINKEEP_HOME)}")
    elif action == "show":
        f, st = _find(argv[1]) if len(argv) > 1 else (None, None)
        if not f:
            output.fail(output.EXIT_NOT_FOUND,
                        f"task not found: {argv[1] if len(argv) > 1 else ''}", verb="task")
        content = f.read_text(encoding="utf-8")
        data = {"id": f.stem, "status": st, "path": str(f.relative_to(paths.PLAINKEEP_HOME)), "content": content}
        return output.emit(data, "task", human=lambda _: print(content))
    elif action in ("move", "done"):
        if action == "done":
            tid, status = (argv[1] if len(argv) > 1 else ""), "done"
        else:
            if len(argv) < 3:
                output.fail(output.EXIT_USAGE, "usage: plainkeep task move <id> <status>", verb="task")
            tid, status = argv[1], argv[2]
        if status not in STATUSES:
            output.fail(output.EXIT_USAGE, f"status must be one of {STATUSES}", verb="task")
        f, cur = _find(tid)
        if not f:
            output.fail(output.EXIT_NOT_FOUND, f"task not found: {tid}", verb="task")
        if dry:
            data = {"dry_run": True, "id": tid, "from": cur, "to": status}
            return output.emit(data, "task",
                               human=lambda _: f"would move {tid}: {cur} -> {status}  (dry run — nothing written)")
        dest = paths.TASKS / status
        vaultio.mkdir(dest)
        new = dest / f.name
        f.rename(new)
        _set_status(new, status)
        paths.append_journal(f"task {tid} -> {status}")
        data = {"id": tid, "from": cur, "to": status}
        return output.emit(data, "task", human=lambda _: f"{tid}: {cur} -> {status}")
    else:
        output.fail(output.EXIT_USAGE,
                    "usage: plainkeep task list|add \"<title>\"|show <id>|move <id> <status>|done <id>",
                    verb="task")


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
