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
import os
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lib import output, paths, resolver, vaultio  # noqa: E402

# `templates/project-repo` is USER data — a vault's own house style for a scaffolded repo, editable
# and versioned with the notes. `templates/verb` is a CODE scaffold that ships with the engine, and
# since Phase 2 Task 2 it resolves from the engine tree rather than from the vault: it renders a
# plugin `run.py` whose bootstrap line has to match the engine that will run it, so a stale copy left
# in someone's vault would scaffold plugins that cannot find `lib`.
TEMPLATE = paths.PLAINKEEP_HOME / "templates" / "project-repo"
VERB_TEMPLATE = paths.VERB_TEMPLATES
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
    try:
        vaultio.mkdir(dest.parent)
        _scaffold_from_template(dest, {"{{name}}": name, "{{risk}}": risk,
                                       "{{summary}}": summary or f"TODO: describe {name}"})
    except OSError as e:
        # An unwritable `plugins/local/`, a full volume, a template that vanished mid-copy. Atomicity
        # is already handled inside `_scaffold_from_template` (the staging leaf is removed and nothing
        # half-made appears); what was missing is the SHAPE — a raw traceback where every other
        # surface in this codebase, `enginetree.main()` included, prints one line the operator can act
        # on. The failure is genuinely unexpected, so the code stays EXIT_UNEXPECTED; only the
        # rendering changes.
        output.fail(output.EXIT_UNEXPECTED, f"scaffolding verb '{name}' failed: {e}", verb="new")
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


def _scaffold_from_template(dest: Path, repl: dict) -> None:
    """Render `templates/verb/` into `dest` — STAGED, MODE-NORMALISED, then moved into place.

    Two things make the obvious `copytree` + `_fill` wrong here, and both only bite through a
    NORMALLY INSTALLED engine — which is why the suite, which scaffolds out of the writable
    repository checkout, never saw either:

    1. MODE. `templates/verb` is engine-owned, and `enginetree.install()` seals the engine tree at
       0444/0555. `shutil.copytree` PRESERVES source modes, so the scaffold landed in the user's
       vault read-only and `_fill`'s `write_text` could not substitute a single placeholder. Every
       copied file is normalised here; the executable bit is the only one carried across.
    2. ATOMICITY. That failure happened halfway, leaving an unwritable `plugins/local/<name>/` whose
       files still held `{{name}}` — and the resolver DISPATCHED it, exit 0, printing the raw
       template text. A half-created verb the resolver happily serves is worse than a refusal. So
       the tree is built under a dot-prefixed sibling and renamed in only once it is complete: what
       appears under the verb's name is finished, or nothing appears at all.

    The staging leaf is pid-unique and lives in `plugins/local/`, i.e. inside the vault and inside
    the wall — the same shape `enginetree.install()` and `bin/plugin/run.py` already use."""
    staging = dest.parent / f".pk-scaffolding-{dest.name}.{os.getpid()}"
    if staging.exists():
        shutil.rmtree(staging, ignore_errors=True)
    _sweep_stale_scaffolding(dest.parent)
    try:
        vaultio.copytree(VERB_TEMPLATE, staging)
        for p in [staging, *staging.rglob("*")]:
            if p.is_dir():
                p.chmod(0o755)
            elif p.is_file():
                p.chmod(0o755 if (p.stat().st_mode & stat.S_IXUSR) else 0o644)
        _fill(staging, repl)
        vaultio.replace(staging, dest)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def _sweep_stale_scaffolding(parent: Path) -> None:
    """Remove ABANDONED `.pk-scaffolding-*` leaves under `plugins/local/` — never a live one.

    The `except` above and the same-pid `rmtree` clean every path this process can see; a `SIGKILL`
    between `_fill` and the rename is the one that cannot be caught, and it left the staging leaf
    behind forever. Debris rather than a contract leak — it is dot-prefixed, so `plainkeep.json`, the
    completion catalog, `help` and `plugin list` all omit it, and only `resolver.known_verbs()` sees
    it — but `enginetree.install()` grew `_sweep_stale_staging()` for exactly this shape and this did
    not, so a vault accumulated one leaf per interrupted scaffold with nothing to remove them.

    Same age rule as the installer's sweep, imported rather than restated: a leaf younger than the
    cutoff may belong to a run happening right now, and the previous line has already dealt with this
    process's own. Best effort — debris this process cannot remove is not a reason to refuse to
    scaffold a verb."""
    import time
    from lib.enginetree import STALE_STAGING_SECONDS
    cutoff = time.time() - STALE_STAGING_SECONDS
    try:
        leaves = list(parent.glob(".pk-scaffolding-*"))
    except OSError:
        return
    for p in leaves:
        try:
            if not p.is_dir() or p.is_symlink() or p.stat().st_mtime > cutoff:
                continue
            shutil.rmtree(p, ignore_errors=True)
        except OSError:
            continue


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
