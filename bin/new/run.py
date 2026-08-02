#!/usr/bin/env python3
"""
plainkeep new project "<name>" [--kind labs|products|tools] | new client "<name>"
    | new verb <name> [--risk <class>] [--summary "<text>"]
— scaffold a unit of work (§4.1, §12.3) or a new command. A PROJECT gets a wiki hub AND a ~/work repo
scaffolded from templates/project-repo/ (git-initialized). A CLIENT gets a wiki hub AND a
~/files/clients/<slug>/ material tree. A VERB stamps out plugins/local/<name>/{run.py,cmd.json} from
templates/verb/ (defaulting to confirm-class, per §5) and regenerates the manifest — turning "add a
verb" into a one-command, guardrailed act instead of hand-wiring the surface. User verbs land in the
user-owned plugins/ tree (Part 2.1), never in bin/ (the `script/update` boundary). Slugs are globally
unique and IDENTICAL across wiki ↔ ~/work ↔ ~/files (§2).
"""
import re
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lib import output, paths, resolver, vaultio  # noqa: E402

TEMPLATE = paths.PLAINKEEP_HOME / "templates" / "project-repo"
VERB_TEMPLATE = paths.PLAINKEEP_HOME / "templates" / "verb"
RISK_CLASSES = ("read", "safe_write", "draft_only", "confirm", "deny")


def _new_verb(rest, dry=False):
    name = rest[0] if rest else ""
    risk, summary = "confirm", ""
    i = 1
    while i < len(rest):
        if rest[i] == "--risk" and i + 1 < len(rest):
            risk = rest[i + 1]; i += 2; continue
        if rest[i] == "--summary" and i + 1 < len(rest):
            summary = rest[i + 1]; i += 2; continue
        i += 1
    if not re.fullmatch(r"[a-z][a-z0-9-]*", name or ""):
        output.fail(output.EXIT_USAGE,
                    "verb name must be lowercase [a-z0-9-], starting with a letter", verb="new")
    if risk not in RISK_CLASSES:
        output.fail(output.EXIT_USAGE, f"--risk must be one of {RISK_CLASSES}", verb="new")
    # Part 0.2 — user verbs scaffold into plugins/local/ (user-owned, survives `script/update`),
    # NEVER into bin/ (the engine checkout boundary). Engine names stay reserved (Part 2.1).
    if resolver.is_engine_verb(name):
        output.fail(output.EXIT_UNEXPECTED, f"'{name}' is a reserved engine verb — choose another name", verb="new")
    dest = paths.PLAINKEEP_HOME / "plugins" / "local" / name
    rel = f"plugins/local/{name}"
    if dest.exists():
        output.fail(output.EXIT_UNEXPECTED, f"plugin verb '{name}' already exists ({rel})", verb="new")
    if not VERB_TEMPLATE.is_dir():
        output.fail(output.EXIT_UNEXPECTED, f"missing template: {VERB_TEMPLATE}", verb="new")
    if dry:
        return output.emit({"dry_run": True, "type": "verb", "name": name, "risk": risk, "code": f"{rel}/run.py"}, "new",
                           human=lambda _: f"would scaffold verb '{name}' ({rel}/, risk: {risk})  (dry run — nothing written)")
    vaultio.mkdir(dest.parent)
    vaultio.copytree(VERB_TEMPLATE, dest)
    _fill(dest, {"{{name}}": name, "{{risk}}": risk,
                 "{{summary}}": summary or f"TODO: describe {name}"})
    from lib.manifest import write_manifest  # regenerate plainkeep.json so `plainkeep help` shows the new verb
    write_manifest()
    paths.append_journal(f"new verb: {name} (risk {risk})")
    data = {"type": "verb", "name": name, "risk": risk, "code": f"{rel}/run.py"}

    def render(_):
        print(f"scaffolded verb '{name}':")
        print(f"  code:   {rel}/run.py  (+ cmd.json, risk: {risk})")
        print(f"  source: plugin:local — user-owned, survives `script/update` (never in engine.txt)")
        print(f"  next:   implement main() in {rel}/run.py, then `plainkeep {name}`"
              + ("" if risk != "confirm" else "  (confirm-class → runs with --yes until you lower risk)"))

    return output.emit(data, "new", human=render)


def _all_slugs() -> set:
    return {p.stem for p in paths.WIKI.rglob("*.md")} if paths.WIKI.exists() else set()


def _hub(folder: str, typ: str, slug: str, name: str) -> Path:
    d = paths.WIKI / folder
    vaultio.mkdir(d)
    f = d / f"{slug}.md"
    vaultio.write_text(f, f"---\ntype: {typ}\ntitle: {name}\nstatus: active\ncreated: {paths.today()}\n"
                 f"updated: {paths.today()}\ntags: []\naliases: []\nremote:\n---\n# {name}\n\n"
                 f"## Timeline\n- {paths.today()} created via `plainkeep new {typ}`\n", encoding="utf-8")
    return f


def _fill(root: Path, repl: dict):
    for p in root.rglob("*"):
        if p.is_file():
            t = p.read_text(encoding="utf-8")
            for k, v in repl.items():
                t = t.replace(k, v)
            p.write_text(t, encoding="utf-8")   # inside the ~/work repo — see test/run_pathwall.py EXEMPT


def main(argv):
    _, argv = output.parse_argv(argv)
    dry = "--dry-run" in argv
    argv = [a for a in argv if a != "--dry-run"]
    if len(argv) < 2 or argv[0] not in ("project", "client", "verb"):
        output.fail(output.EXIT_USAGE,
                    'usage: plainkeep new project "<name>" [--kind labs|products|tools] | new client "<name>"\n'
                    '       | new verb <name> [--risk <class>] [--summary "<text>"]', verb="new")
    if argv[0] == "verb":
        return _new_verb(argv[1:], dry)
    typ = argv[0]
    kind = "labs"
    rest = []
    i = 1
    while i < len(argv):
        if argv[i] == "--kind" and i + 1 < len(argv):
            kind = argv[i + 1]; i += 2; continue
        rest.append(argv[i]); i += 1
    name = " ".join(rest).strip()
    if not name:
        output.fail(output.EXIT_USAGE, "a name is required", verb="new")
    slug = paths.slugify(name)
    if slug in _all_slugs():
        output.fail(output.EXIT_UNEXPECTED, f"slug '{slug}' already exists — slugs are unique (§10.1)", verb="new")

    if typ == "client":
        tree = paths.FILES_ROOT / "clients" / slug
        if dry:
            data = {"dry_run": True, "type": "client", "slug": slug,
                    "hub": f"wiki/clients/{slug}.md", "tree": str(tree)}
            return output.emit(data, "new", human=lambda _:
                               f"would create client '{name}' (wiki/clients/{slug}.md + {tree}/)  (dry run — nothing written)")
        hub = _hub("clients", "client", slug, name)
        for sub in ("in", "out", "work"):
            # `exist_ok=False` makes each of these an ATOMIC create, which is the only write shape
            # the append-only wall admits for `in/` (Task 1c) — `new client` creates the empty
            # container, it never puts an original in it. An already-present tree is not an error:
            # nothing is mutated either way, and this has always tolerated one.
            try:
                vaultio.mkdir(tree / sub, parents=True, exist_ok=False)
            except FileExistsError:
                pass
        paths.append_journal(f"new client: {slug}")
        data = {"type": "client", "slug": slug, "hub": str(hub.relative_to(paths.PLAINKEEP_HOME)), "tree": str(tree)}

        def render_c(_):
            print(f"created client '{name}':")
            print(f"  wiki hub:  {hub.relative_to(paths.PLAINKEEP_HOME)}")
            print(f"  material:  {tree}/  (in/ = originals, append-only, out/ = deliverables, work/ = drafts)")
        return output.emit(data, "new", human=render_c)

    # project
    if kind not in paths.WORK_KINDS:
        output.fail(output.EXIT_USAGE, f"--kind must be one of {paths.WORK_KINDS}", verb="new")
    if not TEMPLATE.is_dir():
        output.fail(output.EXIT_UNEXPECTED, f"missing template: {TEMPLATE}", verb="new")
    repo = paths.WORK_ROOT / kind / slug
    if repo.exists():
        output.fail(output.EXIT_UNEXPECTED, f"repo already exists: {repo}", verb="new")
    if dry:
        data = {"dry_run": True, "type": "project", "slug": slug, "kind": kind,
                "hub": f"wiki/projects/{slug}.md", "repo": str(repo)}
        return output.emit(data, "new", human=lambda _:
                           f"would create project '{name}' (wiki/projects/{slug}.md + {repo}/, kind: {kind})  (dry run — nothing written)")
    hub = _hub("projects", "project", slug, name)
    repo.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(TEMPLATE, repo)
    _fill(repo, {"{{name}}": name, "{{slug}}": slug, "{{date}}": paths.today()})
    subprocess.run(["git", "init", "-q", str(repo)], check=False)
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=False)
    subprocess.run(["git", "-C", str(repo), "-c", "commit.gpgsign=false",
                    "commit", "-qm", f"scaffold {name} via plainkeep new"], check=False,
                   capture_output=True)
    paths.append_journal(f"new project: {slug} ({kind})")
    data = {"type": "project", "slug": slug, "kind": kind,
            "hub": str(hub.relative_to(paths.PLAINKEEP_HOME)), "repo": str(repo)}

    def render_p(_):
        print(f"created project '{name}':")
        print(f"  wiki hub:  {hub.relative_to(paths.PLAINKEEP_HOME)}")
        print(f"  repo:      {repo}/  (git-initialized from templates/project-repo)")
        print(f"  next:      cd {repo} && script/setup   ·   set the hub's remote: field when you push")

    return output.emit(data, "new", human=render_p)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
