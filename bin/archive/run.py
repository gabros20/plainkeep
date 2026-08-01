#!/usr/bin/env python3
"""
plainkeep archive <slug> — retire a dead ~/work repo (§4.1, §14). git-bundles the WHOLE repo (all history,
one file) into ~/work/archive/<year>/<slug>.bundle, marks its wiki hub status: archived, and removes
the working tree. The bundle is a complete, restorable repo (`git clone <bundle>`), so nothing is lost.
"""
import shutil
import subprocess
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lib import output, paths, vaultio  # noqa: E402


def _find_repo(slug: str):
    if not paths.WORK_ROOT.exists():
        return None
    for g in paths.WORK_ROOT.rglob(".git"):
        repo = g.parent
        if "archive" in repo.parts or ".worktrees" in repo.parts:
            continue
        if repo.name == slug:
            return repo
    return None


def main(argv):
    _, argv = output.parse_argv(argv)
    dry = "--dry-run" in argv
    argv = [a for a in argv if a != "--dry-run"]
    if not argv:
        output.fail(output.EXIT_USAGE, "usage: plainkeep archive <slug>", verb="archive")
    slug = argv[0]
    repo = _find_repo(slug)
    if not repo:
        output.fail(output.EXIT_UNEXPECTED, f"no ~/work repo named '{slug}'", verb="archive")

    year = str(date.today().year)
    dest_dir = paths.WORK_ROOT / "archive" / year
    bundle = dest_dir / f"{slug}.bundle"
    hub = paths.WIKI / "projects" / f"{slug}.md"

    if dry:
        data = {"dry_run": True, "slug": slug, "repo": str(repo),
                "would_bundle": str(bundle), "hub": str(hub) if hub.exists() else None}
        return output.emit(data, "archive", human=lambda _:
                           f"would archive '{slug}' -> {bundle}  (dry run — nothing written)")

    dest_dir.mkdir(parents=True, exist_ok=True)   # ~/work/archive — see test/run_pathwall.py EXEMPT
    r = subprocess.run(["git", "-C", str(repo), "bundle", "create", str(bundle), "--all"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        output.fail(output.EXIT_UNEXPECTED, f"bundle failed: {r.stderr.strip()}", verb="archive")

    if hub.exists():
        import re
        t = hub.read_text(encoding="utf-8")
        t = re.sub(r"(?m)^status:.*$", "status: archived", t, count=1)
        t = re.sub(r"(?m)^updated:.*$", f"updated: {paths.today()}", t, count=1)
        if "## Timeline" in t:
            t = t.replace("## Timeline", f"## Timeline\n- {paths.today()} archived → {bundle}", 1)
        vaultio.write_text(hub, t, encoding="utf-8")

    shutil.rmtree(repo)
    paths.append_journal(f"archived {slug} -> {bundle}")
    data = {"slug": slug, "bundle": str(bundle), "hub_marked": hub.exists()}

    def render(_):
        print(f"archived '{slug}':")
        print(f"  bundle:  {bundle}  (restore: git clone {bundle} <dir>)")
        print(f"  working tree removed; wiki hub marked status: archived" if hub.exists()
              else "  working tree removed (no wiki hub found)")

    return output.emit(data, "archive", human=render)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
