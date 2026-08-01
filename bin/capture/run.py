#!/usr/bin/env python3
"""plainkeep capture "<text>" [--dry-run] [--json] — zero-decision capture of a thought into inbox/
(also reads stdin)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lib import output, paths, vaultio  # noqa: E402


def main(argv):
    _, argv = output.parse_argv(argv)
    dry = "--dry-run" in argv
    argv = [a for a in argv if a != "--dry-run"]
    text = " ".join(argv).strip()
    if not text and not sys.stdin.isatty():
        text = sys.stdin.read().strip()
    if not text:
        output.fail(output.EXIT_USAGE, 'usage: plainkeep capture "<text>"   (or pipe text via stdin)',
                    verb="capture")
    f = paths.INBOX / f"cap-{paths.now_stamp()}.md"
    rel = f.relative_to(paths.PLAINKEEP_HOME)
    if dry:
        data = {"dry_run": True, "would_write": str(rel), "text": text}
        return output.emit(data, "capture",
                           human=lambda _: f"would capture -> {rel}  (dry run — nothing written)")
    vaultio.mkdir(paths.INBOX)
    vaultio.write_text(f, f"---\ntype: capture\ncreated: {paths.today()}\nsource: capture\n---\n{text}\n",
                 encoding="utf-8")
    paths.append_journal(f"captured: {text[:70]}{'…' if len(text) > 70 else ''}")
    data = {"path": str(rel), "text": text}
    return output.emit(data, "capture",
                       human=lambda _: f"captured -> {rel}  (triage it with `plainkeep triage`)")


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
