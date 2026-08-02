#!/usr/bin/env python3
"""plainkeep search "<query>" [--author human|agent] [--open] [--json] — ranked file#heading hits with an
FTS5 snippet excerpt (§10.2 stage 1, proposal Part 3.4). `--author human` excludes agent + derived
material, `--author agent` keeps only agent notes (provenance planes, Part 4.3). `--open` jumps to the
top hit via `plainkeep open`; bare `plainkeep search` in a tty with fzf opens a live-reload search session."""
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lib import enginetree, output, paths, render  # noqa: E402
from lib.indexlib import search, snippets  # noqa: E402

DIM, RESET = "\033[2m", "\033[0m"


def _open(slug: str) -> int:
    return subprocess.run([str(enginetree.launcher()), "open", slug]).returncode


def _live_session() -> int:
    """fzf live-reload search: each keystroke re-runs `plainkeep search`; enter opens the hit via `plainkeep open`.
    Re-enters through the dispatcher (guardrail applies); no verb/lib import shortcut (anti-roadmap #2)."""
    pk = str(enginetree.launcher())   # the engine's launcher, not a vault-local shim
    reload_cmd = f'PLAINKEEP_RENDER=raw "{pk}" search {{q}} 2>/dev/null'
    preview = f'PLAINKEEP_RENDER=plain "{pk}" open {{2}} 2>/dev/null'
    argv = ["fzf", "--ansi", "--reverse", "--disabled", "--height", "80%", "--nth", "2..",
            "--prompt", "search> ", "--preview", preview, "--preview-window", "right:60%:wrap",
            "--bind", f"change:reload({reload_cmd})", "--bind", f"start:reload({reload_cmd})"]
    sel = subprocess.run(argv, capture_output=True, text=True).stdout.strip()
    if not sel:
        return output.EXIT_OK
    parts = sel.split()
    slug = Path((parts[1] if len(parts) > 1 else sel).split("#", 1)[0]).stem
    return _open(slug)


def main(argv):
    _, argv = output.parse_argv(argv)
    do_open = "--open" in argv
    argv = [a for a in argv if a != "--open"]
    author = None
    if "--author" in argv:
        i = argv.index("--author")
        author = argv[i + 1] if i + 1 < len(argv) else None
        del argv[i:i + 2]
        if author not in ("human", "agent"):
            output.fail(output.EXIT_USAGE, "usage: plainkeep search \"<query>\" --author human|agent",
                        verb="search")
    query = " ".join(argv).strip()

    if not query:
        if sys.stdin.isatty() and sys.stdout.isatty() and shutil.which("fzf"):
            return _live_session()
        output.fail(output.EXIT_USAGE, 'usage: plainkeep search "<query>"', verb="search")

    # every real search is logged to .logs/queries.jsonl (ADR-002); --author filters the plane (4.3)
    hits = search(query, log=True, author=author)
    snips = snippets(query, {p for p, _h, _s in hits})
    rows = [{"path": p, "heading": h, "score": round(s, 6), "snippet": snips.get(p, "")}
            for p, h, s in hits]

    if do_open:
        if not hits:
            output.fail(output.EXIT_NOT_FOUND, f"no hit to open for '{query}'", verb="search")
        return _open(Path(hits[0][0]).stem)

    def render_hits(rs):
        if not rs:
            return "(no hits — try `plainkeep index` first, or broaden the query)"
        out = []
        for r in rs:
            out.append(f"{r['score']:6.4f}  {r['path']}#{r['heading']}")
            if r["snippet"]:
                out.append(f"        {DIM}{r['snippet']}{RESET}")
        return "\n".join(out)

    return output.emit_rows(rows, "search", human=render_hits)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
