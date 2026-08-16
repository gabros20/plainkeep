#!/usr/bin/env python3
"""
run_pathwall.py — proves the path-wall is ON the write path, by FILESYSTEM SIDE EFFECT.

Why this suite exists, stated plainly because a green suite here is easy to over-read: until
`bin/lib/vaultio.py`, `guardrail.classify()` was never called by any verb. It was reachable from
the test harness and re-exported to plugins through `lib/api.py`, and that was all. The dispatcher
gate (`guardrail.gate`) admitted a verb on its DECLARED RISK CLASS and nothing looked at the path
the verb then wrote to. So "the guardrail refused it" could not be concluded from an exit code, and
a guardrail unit test could not have caught it — the failing region was never exercised.

Hence the shape of every assertion below: **walk the filesystem and count files**. An exit code is
checked too, but it is never the proof.

Offline, stdlib only.
"""
from __future__ import annotations
import ast
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from lib.hermetic import scratch_root, seal
seal()   # hermetic: an empty throwaway registry, never the developer's real vault
# `_seam_report` loads bin/lib/vaultio.py in-process to read the wall's own wording back, and every
# engine module resolves its data root AT IMPORT with no fallback since Task 1b. A marked throwaway
# vault answers that without being anybody's notes — and anything that inherits the variable writes
# its audit log there rather than into the developer's real one.
os.environ.setdefault("PLAINKEEP_HOME", scratch_root())

REPO = Path(__file__).resolve().parents[1]
GREEN, RED, DIM, BOLD, RESET = "\033[32m", "\033[31m", "\033[2m", "\033[1m", "\033[0m"
results: list[tuple[str, bool, str]] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    results.append((name, bool(cond), detail))


def run_verb(home: Path, real_home: Path, verb: str, *args, **envextra):
    """Invoke a verb's run.py directly with PLAINKEEP_HOME=home. `real_home` becomes $HOME so the
    CONVENTIONAL `~/plainkeep` root can never accidentally satisfy the wall during a test."""
    env = {**os.environ, "PLAINKEEP_HOME": str(home), "HOME": str(real_home), **envextra}
    env.pop("PLAINKEEP_TEST_HOME", None)
    return subprocess.run([sys.executable, str(REPO / "bin" / verb / "run.py"), *args],
                          capture_output=True, text=True, env=env)


def files_under(root: Path) -> list[Path]:
    return [p for p in root.rglob("*") if p.is_file()]


# --------------------------------------------------------------------------------------------
# A. The wrong-root side-effect gate: a policy-denied data root must produce ZERO files.
# --------------------------------------------------------------------------------------------
def case_walled_root() -> None:
    with tempfile.TemporaryDirectory() as td:
        h = Path(td)
        # "Mobile Documents" is an absolute wall (guardrail.WALLED_OFF_MARKERS) — a vault synced
        # into iCloud is the one bad root the wall can recognise WITHOUT the ADR-014 root
        # validation, so it is what this suite can honestly prove today.
        root = h / "Library" / "Mobile Documents" / "com~apple~CloudDocs" / "vault"
        root.mkdir(parents=True)
        before = len(files_under(h))
        r = run_verb(root, h, "capture", "this must never reach an iCloud-synced vault")
        after = files_under(h)
        check("walled root: capture refuses with EXIT_DENY (5)", r.returncode == 5,
              f"rc={r.returncode} out={r.stdout.strip()} err={r.stderr.strip()}")
        check("walled root: capture wrote ZERO files (filesystem walk, not an exit code)",
              len(after) == before, f"created: {[str(p.relative_to(h)) for p in after]}")
        check("walled root: the refusal names the wall",
              "walled off" in (r.stdout + r.stderr).lower(), (r.stdout + r.stderr).strip())


# --------------------------------------------------------------------------------------------
# B. Regression: a correctly-resolved root still writes. A wall that denies everything is not a wall.
# --------------------------------------------------------------------------------------------
def case_good_root() -> None:
    with tempfile.TemporaryDirectory() as td:
        h = Path(td)
        root = h / "vault"
        root.mkdir()
        r = run_verb(root, h, "capture", "an ordinary thought")
        caps = list((root / "inbox").glob("cap-*.md"))
        check("good root: capture still succeeds", r.returncode == 0, r.stdout + r.stderr)
        check("good root: the inbox note exists", len(caps) == 1, f"found {caps}")
        check("good root: the journal line was appended",
              any("captured:" in p.read_text(encoding="utf-8")
                  for p in (root / "journal").rglob("*.md")))


# --------------------------------------------------------------------------------------------
# C. Symlink escape: the wall takes the STRICTER of path and realpath, so a subtree symlinked out
#    of the vault is refused even though the unresolved path looks fine.
# --------------------------------------------------------------------------------------------
def case_symlink_escape() -> None:
    with tempfile.TemporaryDirectory() as td:
        h = Path(td)
        root, outside = h / "vault", h / "outside"
        root.mkdir()
        outside.mkdir()
        (root / "inbox").symlink_to(outside, target_is_directory=True)
        r = run_verb(root, h, "capture", "escaping through a symlinked inbox")
        check("symlink escape: refused with EXIT_DENY (5)", r.returncode == 5,
              f"rc={r.returncode} out={r.stdout.strip()} err={r.stderr.strip()}")
        check("symlink escape: ZERO files landed outside the vault",
              len(files_under(outside)) == 0,
              f"created: {[str(p) for p in files_under(outside)]}")


# --------------------------------------------------------------------------------------------
# D. The wall reaches the SDK too: a plugin journalling through lib/api.py inherits it.
# --------------------------------------------------------------------------------------------
def case_sdk_journal() -> None:
    with tempfile.TemporaryDirectory() as td:
        h = Path(td)
        root = h / "Library" / "Mobile Documents" / "vault"
        root.mkdir(parents=True)
        script = h / "plug.py"
        script.write_text(
            "import sys\n"
            f"sys.path.insert(0, {str(REPO / 'bin')!r})\n"
            "from lib import api\n"
            "api.append_journal('a plugin writing through the frozen SDK')\n",
            encoding="utf-8")
        env = {**os.environ, "PLAINKEEP_HOME": str(root), "HOME": str(h)}
        env.pop("PLAINKEEP_TEST_HOME", None)
        r = subprocess.run([sys.executable, str(script)], capture_output=True, text=True, env=env)
        check("SDK journal: append_journal through api.py is refused (5)", r.returncode == 5,
              f"rc={r.returncode} err={r.stderr.strip()}")
        check("SDK journal: ZERO files under the walled root",
              len(files_under(root)) == 0, f"created: {files_under(root)}")


# --------------------------------------------------------------------------------------------
# E. The ratchet: no NEW unguarded write may appear in bin/ without joining the exemption list.
#
# Each exemption is a path that the wall as written DENIES but the verb legitimately needs — a
# launchd plist, the ~/.local/bin launcher, a ~/work tree. Those are a POLICY question (does the
# wall's model cover verb-owned writes outside the three roots?), not something to paper over by
# widening the wall. The list may shrink; anything new is a failure.
# --------------------------------------------------------------------------------------------
# `vaultio` (and the `io` alias lib/paths.py binds it to) ARE the seam — a call on them is guarded.
GUARDED_RECEIVERS = {"vaultio", "io"}
METHOD_WRITE = re.compile(r"(\w+)?\.(write_text|write_bytes|mkdir)\s*\(")
SHUTIL_WRITE = re.compile(r"\bshutil\.(?:move|copy2|copytree|copyfile)\s*\(")
PATH_REPLACE = re.compile(r"\.replace\s*\(\s*(?:target|dest|dst|out|path)\b")
OPEN_WRITE = re.compile(r"\bopen\(\s*[^)]*?,\s*[\"'](?:w|a|wb|ab)[\"']")


def _is_raw_write(code: str) -> bool:
    for m in METHOD_WRITE.finditer(code):
        if (m.group(1) or "") not in GUARDED_RECEIVERS:
            return True
    return bool(SHUTIL_WRITE.search(code) or PATH_REPLACE.search(code) or OPEN_WRITE.search(code))


# file -> {exact source line (stripped): why it is NOT behind the wall}.
#
# Keyed by source text, not line number, so an unrelated edit above does not fake a failure — and so
# the reason travels with the code it excuses. EVERY entry here is a write the wall as currently
# written would DENY, for a destination the verb legitimately needs. That is a POLICY gap in the
# wall's model (it was authored for agent actions, not verb-owned ones), and papering over it by
# widening the wall would cost more than it buys.
#
# This used to say "ADR-014 / Phase 2 Task 1 is where it gets decided". Task 1b has since SHIPPED and
# did not decide it — it narrowed `VAULT_ROOTS` to the one selected root (guardrail.py), which is a
# different question. Whether the wall's model should cover verb-owned writes OUTSIDE the vault
# (~/work, ~/.Trash, a human-supplied --out) is still open, and is now a Phase 2 follow-up with no
# task claiming it.
EXEMPT: dict[str, dict[str, str]] = {
    "bin/lib/guardrail.py": {
        'logdir.mkdir(parents=True, exist_ok=True)':
            "the guardrail's own audit log — it records the refusal, so it cannot be subject to it",
        'with open(logdir / "plainkeep.log", "a", encoding="utf-8") as f:':
            "same: the append that logs a DENY must not itself be gated on a DENY",
    },
    "bin/archive/run.py": {
        'dest_dir.mkdir(parents=True, exist_ok=True)':
            "~/work/archive/<year> — the bundle destination for an archived fleet repo",
    },
    "bin/new/run.py": {
        'repo.parent.mkdir(parents=True, exist_ok=True)':
            "~/work/<kind>/<slug> — _write_verdict denies a ~/work write unless it is the current "
            "task's repo (guardrail.py's WORK branch), and `new project` has no task context",
        'shutil.copytree(TEMPLATE, repo)': "same ~/work project tree",
        'p.write_text(t, encoding="utf-8")':
            "_fill() substituting template placeholders in the tree just created — the ~/work repo "
            "for `new project`, and for `new verb` the `.pk-scaffolding-*` staging leaf under the "
            "vault's own plugins/local/, which `vaultio.copytree` already classified on the way in",
    },
    # `bin/files/run.py` and `new client`'s in/out/work `mkdir` USED to sit here, and they were the
    # sharpest entries on the list — a CONTRADICTION rather than an omission: the wall said
    # "~/files/**/in/ originals are read-only evidence" while `files ingest --client` existed
    # precisely to put an original there, so the only verb that writes an original was the one verb
    # the wall never saw. Phase 2 Task 1c redrew the rule (append-only: an original ARRIVES by
    # atomic creation, an existing one is never touched) and both sites went behind the seam.
    # They are GONE rather than reworded, and the stale-exemption check below is what proves it.
    "bin/lib/vaultreg.py": {
        'self.path.parent.mkdir(parents=True, exist_ok=True)':
            "the REGISTRY's config directory ($XDG_CONFIG_HOME/plainkeep) — it lives outside every "
            "vault by design, since it is the thing that knows which vaults exist",
    },
    # ACTIVATION (ADR-022). `plainkeep job enable` installs a rendered plist into the directory
    # launchd actually reads — `~/Library/LaunchAgents`, or `$PLAINKEEP_LAUNCH_AGENTS_DIR` for the
    # suites. That is the machine, not a vault, so `classify()` answers DENY for it exactly the way
    # it answers DENY for `~/.local/bin` and `~/work`: same class as the `backup init` plist this
    # verb's old printed handoff asked the operator to copy by hand, now done by the product.
    #
    # WHAT BOUNDS IT, since the wall cannot — and this reason was WRONG in its first form, which is
    # worth leaving visible. It said "never from an argument", which is true and beside the point: the
    # filename is `com.plainkeep.<registry key>.plist`, and `jobs/registry.json` is VAULT CONTENT, so
    # not caller-controlled is not the same as not attacker-controlled. Review r1 walked a traversing
    # key straight to `vaultio`, which refused it — mid-loop, after earlier jobs were bootstrapped.
    #
    # The operative bound now, in order: a registry key is validated as the identifier it becomes
    # (`bin/job/run.py`'s `_NAME_RE`, checked with the rest of §15 BEFORE anything is rendered); the
    # vault-side render still goes through `vaultio` as an independent second answer; the directory
    # comes from `launchdlib.launch_agents_dir()` and nothing else; the `com.plainkeep.` prefix is
    # glued to the front of whatever survives; and the CONTENT is a fresh `plistlib` render of the
    # registry, never a file copied from wherever. It is confirm-class (`--yes`, or exit 3) and
    # previewable with `--dry-run`, which is the consent this exemption stands in for.
    "bin/job/run.py": {
        'agents.mkdir(parents=True, exist_ok=True)':
            "~/Library/LaunchAgents — created if the operator has never had a launch agent; the "
            "directory is derived from launchdlib.launch_agents_dir(), never from an argument",
        'shutil.copyfile(src, dst)':
            "installing the rendered plist as a COPY (launchd is unreliable with symlinked plists, "
            "and a symlink would let a vault edit change what a privileged loader reads)",
    },
    # The ENGINE INSTALLER (Phase 2 Task 2). Every line below writes into
    # `${XDG_DATA_HOME:-~/.local/share}/plainkeep/engine/<version>/`, which is CODE and is outside
    # every vault by construction — the disjointness check in `vaultroot.validate()` refuses any
    # data root that overlaps it, so an installer write can never land in a vault. It is the same
    # class as `vaultreg`'s config-directory write directly above: this is the machinery that
    # establishes where the wall goes, so it cannot be subject to a wall that has not been
    # positioned yet. Routing it through `vaultio` would mean classifying an engine path against
    # the active DATA root, which answers DENY for every install.
    #
    # It is also not agent-reachable: `enginetree.install()` has no verb, is not in the frozen SDK
    # (`lib/api.py`), and is invoked by `script/setup` and the test harness only.
    #
    # TWO OF THESE KEYS ARE LOOSE, and a reviewer should know which. Matching is `text.startswith(k)`,
    # and `d.parent.mkdir(...)` / `root.mkdir(...)` are not enginetree-specific text: any future line
    # in this file beginning with either is licensed automatically, whatever `d` or `root` has come to
    # mean by then. The other four name a distinctive receiver or argument. Nothing enforces this —
    # it is a note about where the discipline has to come from the reader.
    "bin/lib/enginetree.py": {
        'd.parent.mkdir(parents=True, exist_ok=True)':
            "the installed engine tree's parent directories — engine code, outside every vault",
        'shutil.copytree(s, d, ignore=ignore, symlinks=True)':
            "copying an OWNED_TREES subtree into the staged engine",
        'shutil.copy2(s, d)': "copying an OWNED_FILES file into the staged engine",
        'shutil.copy2(core, d)': "the optional compiled core, if the source checkout has one",
        'root.mkdir(parents=True, exist_ok=True)': "the versions directory itself",
        'staging.mkdir()':
            "the `.incoming-<version>` staging directory — an engine is verified there and only "
            "then renamed into its version name, so a half-copied tree is never reachable",
        # THE DIGEST MANIFEST (Phase 2 Task 4b) and THE PAIR MANIFEST + ACTIVATION STATE (Task 5).
        # `<install-root>/engine/.digests/<version>.json` and `<install-root>/engine/.pairs/`, both
        # BESIDE the versioned trees rather than inside one — a manifest that lives in the tree it
        # covers cannot describe that tree's own removal, and the sealed tree is read-only anyway.
        # Same class and same structural answer as every line above: the destination is derived from
        # the ENGINE root, never from an argument.
        #
        # ONE KEY, TWO SITES. `p.parent.mkdir(...)` appears at enginetree.py:442 (.digests) and
        # :1143 (.pairs); the match is `startswith`, so this single entry licenses both. That is
        # honest here — they are the same write to the same parent directory class — but it is also
        # a LOOSE key in exactly the sense the paragraph above flags, and a third `p.parent.mkdir`
        # meaning something else would be licensed silently.
        'p.parent.mkdir(parents=True, exist_ok=True)':
            "`.digests/` and `.pairs/` beside the installed versions — the digest manifests, the "
            "pair manifests and the activation state, which must live outside the trees they cover",
        'tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\\n", encoding="utf-8")':
            "the digest manifest itself, written to a pid-unique temp name and renamed over",
        '(dst / PROVISION_DIR).mkdir(parents=True, exist_ok=True)':
            "the engine's `tools/` provisioning directory, created empty inside the staged tree",
        # Phase 2 Task 5's PAIR SELF-TEST. These three write into a `tempfile.mkdtemp()` the
        # self-test throws away in its `finally`: a staged pair is proven by dispatching a real verb
        # against a throwaway marked vault, never against the operator's notes.
        '(vault / vaultreg.MARKER_DIR).mkdir(parents=True)':
            "the throwaway marked vault the pair self-test dispatches against, under mkdtemp — the "
            "self-test must never be pointed at the operator's real notes",
        'vaultreg.marker_path(vault).write_text(':
            "its marker, same mkdtemp directory, removed in the `finally`",
        'cfg.mkdir()':
            "the throwaway PLAINKEEP_CONFIG_HOME for the same self-test, so it neither reads nor "
            "writes the real registry",
        'self.path.parent.mkdir(parents=True, exist_ok=True)':
            "the versions directory, so the update lock has somewhere to live",
    },
    # PROVISIONING (Phase 2 Task 4a). Every write below lands under
    # `<engine-root>/tools/`, which is the one writable directory in an installed engine tree and is
    # engine code's neighbour, not a vault's: `vaultroot.validate()` refuses any data root that
    # overlaps the engine root, so none of these can resolve inside a vault — let alone under
    # `~/files/**/in/`. Same class as the installer directly above, and for the same reason it is not
    # routed through `vaultio`: classifying an engine path against the active DATA root answers DENY
    # for every install.
    #
    # Not agent-reachable either: `provision` has no verb, is not in the frozen SDK (`lib/api.py`),
    # and is invoked by `plainkeep setup`, `script/setup`, the compiled core and the harness.
    "bin/lib/provision.py": {
        'with urllib.request.urlopen(req, timeout=timeout) as r, open(dest, "wb") as out:':
            "the pinned uv download, into `tools/.incoming-uv-<version>.<pid>/`",
        'with open(dest, "wb") as out:':
            "the ONE named member extracted from that archive — the archive never names a "
            "destination (see `_extract_member`)",
        'staging.mkdir(parents=True)':
            "that staging directory; it is removed on every failure path, so a refused bootstrap "
            "leaves nothing behind",
        'dest.parent.parent.mkdir(parents=True, exist_ok=True)':
            "`tools/uv/`, the parent the verified binary's version directory is renamed into",
    },
    "bin/vault/run.py": {
        'vaultreg.marker_path(target).write_text(vaultreg.marker_bytes(marker), encoding="utf-8")':
            "the vault MARKER — the one write that establishes where the wall goes. The target is "
            "by definition not yet the active data root, so classifying it against the active root "
            "would refuse every registration but the current vault's. One file, --yes only",
        # LOOSE, in the sense the enginetree block above flags: `d.mkdir(...)` is not distinctive
        # text. It covers two sites today — `register`'s marker directory and `init`'s skeleton
        # directories — and both are the same class.
        'd.mkdir(parents=True, exist_ok=True)':
            "the marker's .plainkeep/ directory (register) and one REQUIRED_DIRS skeleton directory "
            "inside the vault being created (init), same reason",
        # `vault init` (Phase 2 Task 5). Same class as the marker write above and for the identical
        # reason: the wall classifies against the ACTIVE data root, and the vault being CREATED is
        # by definition not it — every one of these lines would be denied for the only directory
        # they are ever aimed at. The destination is not caller-shaped either: it is one canonical
        # path validated by `_init_refusals` (disjoint from the engine, outside a walled/sync tree,
        # not already a vault, not a checkout) before a single byte is written, and the relative
        # paths under it come from `REQUIRED_DIRS` + four literals in this file — never from argv.
        # --yes only.
        'raw.mkdir(parents=True)':
            "the new vault's own directory, created before the location checks so `path_within` has "
            "inodes to compare (see the comment at the call site)",
        'f.write_text(text, encoding="utf-8")':
            ".gitignore / jobs/registry.json / AGENTS.md / CLAUDE.md — the four generated "
            "configuration files, written only when absent",
        'vaultreg.marker_path(target).parent.mkdir(parents=True, exist_ok=True)':
            "the new vault's .plainkeep/ directory",
    },
    # MIGRATION (Phase 2 Task 6). FOUR write sites, and every one of them is OUTSIDE the vault being
    # migrated — which is not a coincidence to be checked line by line but the module's stated design
    # constraint: `migrate.py` never opens a path inside a vault for writing at all, and
    # `test/run_migrate.py`'s AST ratchet enforces that per function against the parse tree (it taints
    # every name derived from a function's `vault` argument and flags a write primitive applied to
    # one). So these four are the module's ENTIRE write surface, and they land under
    # `enginetree.install_root() / "migrations"` — the receipt and the divergence patch.
    #
    # Both live outside the vault ON PURPOSE and the reasons are different. The RECEIPT is what
    # rollback reads: a receipt inside the vault would be removed by the very migration it records,
    # and would then be missing exactly when it is needed. The DIVERGENCE PATCH is emitted while
    # REFUSING to migrate — writing an operator's recoverable diff into the tree it says it will not
    # touch would contradict the refusal. `install_root()` is derived from the engine root alone,
    # never from an argument, and `vaultroot.validate()` refuses any data root overlapping it, so
    # neither can resolve under `~/files/**/in/`. Same class as the installer and provisioner above.
    #
    # LOOSE KEY, in the sense the enginetree block flags: `d.mkdir(...)` is not distinctive text. It
    # covers two sites today, `write_receipt()` and `_divergence()`, and both are the same directory.
    "bin/lib/migrate.py": {
        'd.mkdir(parents=True, exist_ok=True)':
            "`<install-root>/migrations/` — the receipt directory (write_receipt) and the divergence "
            "patch directory (_divergence), which are the same directory outside every vault",
        'tmp.write_text(json.dumps(doc, indent=2, sort_keys=True) + "\\n", encoding="utf-8")':
            "the migration receipt, to a pid-unique temp name and renamed over — rollback's only "
            "record, so it cannot live in the tree the migration removes files from",
        'patch.write_text(body, encoding="utf-8")':
            "the recoverable patch for a DIVERGED engine copy, written while refusing to migrate — "
            "acceptance item 3 requires the refusal to emit it, and it cannot be emitted into the "
            "vault the same call is declining to touch",
    },
    "bin/repo/run.py": {
        'dest.parent.mkdir(parents=True, exist_ok=True)': "~/work fleet clone + adopt destination",
        'shutil.move(str(src), str(dest))': "~/work fleet adopt: moves an existing repo into the fleet",
    },
    "bin/share/run.py": {
        'Path(out).write_bytes(html_bytes)':
            "`--out <path>`: a destination the human named on the command line, deliberately "
            "outside the vault (that is the point of exporting)",
        'Path(out).write_text(md, encoding="utf-8")': "same `--out <path>`",
    },
    "bin/sweep/run.py": {
        'trash.mkdir(parents=True, exist_ok=True)':
            "~/.Trash — outside the three roots BY DESIGN: the recoverable end of the decay machine "
            "(sweep/run.py:34)",
        'shutil.move(str(item), str(tdest))': "same ~/.Trash move",
    },
}


# --------------------------------------------------------------------------------------------
# F. The DELETE ratchet, which is a different question from the write ratchet above.
#
# `_is_raw_write` has no pattern for a removal or a rename, and that is how Task 1c's quality review
# found `move_create_only` unlinking its own source: the wall was destination-only, the validated
# case `originals-in-delete-denied` had no seam, and ingesting a path already under `~/files/**/in/`
# renamed an existing original with exit 0. `vaultio._guard_delete` is now that seam.
#
# These sites are PINNED rather than exempted, and they are deliberately NOT folded into `EXEMPT`
# above: every entry there is a write the wall as written would DENY, and `classify` answers CONFIRM
# — not DENY — for a delete anywhere outside an originals tree. Calling them exemptions would say
# something false. What the pin buys is that a NEW raw removal in `bin/` cannot appear without a
# reviewer looking at it and answering the only question that matters: can this path resolve under
# `~/files/**/in/`? For every line below the answer is no — vault notes, ~/work trees, the registry
# file, a plugin staging dir, a downloaded asset — and the one site that COULD is behind the seam.
METHOD_DELETE = re.compile(r"(\w+)?\.(unlink|rmdir|rename)\s*\(")
OS_DELETE = re.compile(r"\bos\.(remove|rename|unlink|rmdir|replace)\s*\(")
TREE_DELETE = re.compile(r"\bshutil\.rmtree\s*\(")

# `.replace()` is TWO different functions sharing a name, and the first version of this ratchet
# matched neither: `os.replace(tmp, f)` and `Path(src).replace(p)` are `rename(2)`, which destroys a
# name exactly as `os.rename` does, while `s.replace(a, b)` and `dt.replace(tzinfo=…)` are string and
# datetime methods that destroy nothing. A source scan has no types, so they are told apart by the
# only signal that is actually in the text: ARITY. The filesystem method takes exactly ONE positional
# argument; the string method takes two; the datetime one is passed by keyword. `os.replace` is
# unambiguous and is matched by name above regardless of arity.
PATH_REPLACE_DELETE = re.compile(r"(\w+)?\.replace\s*\(\s*[^,()=]+\s*\)")


def _is_raw_delete(code: str) -> bool:
    """A removal or rename that is NOT going through the guarded seam. Receiver-aware for the same
    reason `_is_raw_write` is: `vaultio.replace(...)` IS the seam, so it is not a raw delete."""
    for pat in (METHOD_DELETE, PATH_REPLACE_DELETE):
        for m in pat.finditer(code):
            if (m.group(1) or "") not in GUARDED_RECEIVERS:
                return True
    return bool(OS_DELETE.search(code) or TREE_DELETE.search(code))


PINNED_DELETES: dict[str, set[str]] = {
    "bin/archive/run.py": {"shutil.rmtree(repo)"},
    "bin/backup/run.py": {"out.unlink(missing_ok=True)", "stale.unlink(missing_ok=True)"},
    # `plainkeep job disable` removing the installed launch agent (ADR-022). The question this
    # ratchet asks — can this path resolve under `~/files/**/in/`? — is answered NO structurally: the
    # name is `<launch_agents_dir()>/com.plainkeep.<registry key>.plist`, and neither half comes from
    # an argument. It removes only what `enable` installed; the vault-side rendered plist is left.
    # The bare `dst.unlink()` is `enable` replacing a PRE-ADR-022 SYMLINK before installing its copy
    # (the old guidance was `ln -sf` out of jobs/launchd/; copying onto that raises SameFileError or
    # writes through the link) — same structurally-derived path, guarded by `dst.is_symlink()`, so
    # what it removes is only ever the link, never a file's bytes.
    "bin/job/run.py": {"dst.unlink(missing_ok=True)", "dst.unlink()"},
    "bin/lib/setuplib.py": {"shutil.rmtree(venv, ignore_errors=True)",
                            "asset_path.unlink(missing_ok=True)",
                            "checksums_path.unlink(missing_ok=True)"},
    "bin/lib/vaultreg.py": {"self.path.unlink(missing_ok=True)",
                            # the atomic write of the registry file itself, which lives outside every
                            # vault by design — so it can never resolve under `~/files/**/in/`
                            "os.replace(tmp, f)"},
    # The ENGINE INSTALLER (Phase 2 Task 2). Every removal below targets a path under
    # `${XDG_DATA_HOME:-~/.local/share}/plainkeep/engine/`, and the question this ratchet exists to
    # ask — "can this path resolve under `~/files/**/in/`?" — is answered NO structurally rather
    # than by inspection: the destination is derived from `enginetree.versions_dir()` alone, never
    # from an argument, and a data root that overlaps that tree is refused by
    # `vaultroot.validate()`. Nothing an agent can reach calls any of them.
    "bin/lib/enginetree.py": {"shutil.rmtree(d, ignore_errors=True)",
                              "shutil.rmtree(staging, ignore_errors=True)",
                              # the age-gated sweep of ABANDONED `.incoming-<version>.<pid>` trees;
                              # same destination, same structural answer
                              "shutil.rmtree(p, ignore_errors=True)",
                              # the staged tree becomes the version directory; the version
                              # directory it would overwrite was removed by remove_version() first
                              "os.rename(staging, dst)",
                              # replacing the `current` symlink, atomically: a uniquely named link
                              # is created beside it and renamed over the old one
                              "tmp.unlink()",
                              "os.replace(tmp, link)",
                              # `os.replace(tmp, p)` covers TWO sites, both an atomic manifest write
                              # under a directory derived from the engine root alone: the digest
                              # manifest (Task 4b, `digests_path(root)`) and the pair manifest /
                              # activation state (Task 5, `<install-root>/engine/.pairs/`). The
                              # rmtree is the pair self-test's own `tempfile.mkdtemp()` sandbox. The
                              # three `unlink`s remove a manifest for a version that has just been
                              # removed or refused, so a manifest never outlives the tree it
                              # describes.
                              "os.replace(tmp, p)",
                              "digests_path(d).unlink()",
                              "shutil.rmtree(td, ignore_errors=True)",
                              "pair_manifest_path(version).unlink(missing_ok=True)",
                              "pair_manifest_path(v).unlink(missing_ok=True)"},
    # PROVISIONING (Phase 2 Task 4a) — all under `<engine-root>/tools/`, derived from the engine root
    # and never from an argument, so the question this ratchet asks ("can this resolve under
    # `~/files/**/in/`?") is answered NO structurally. The two `unlink`s are the point of the pin
    # rather than an exception to it: an artifact that fails its checksum is DELETED so that nothing
    # later can reach for it.
    "bin/lib/provision.py": {"dest.unlink()",
                             "archive.unlink()",
                             "shutil.rmtree(dest.parent, ignore_errors=True)",
                             "shutil.rmtree(staging, ignore_errors=True)",
                             "os.rename(staging, dest.parent)"},
    # Phase 2 Task 5. The ONE removal `vault init` can reach, and it is bounded twice over: it runs
    # only when this same call created the directory moments earlier, and `rmdir` refuses a
    # non-empty one — so a path that acquired any content between the two lines survives. It cannot
    # resolve under `~/files/**/in/` for the same reason the rest of init cannot: the target is
    # validated disjoint and unmarked before anything is written.
    "bin/vault/run.py": {"target.rmdir()"},
    # MIGRATION (Phase 2 Task 6). This is the ONE module on this list whose removals are aimed INSIDE
    # a directory holding a person's notes, so it is the one that needs reading rather than
    # classifying, and it splits in two.
    #
    # The first two lines ARE the migration — `_remove_engine_path` deletes a file and
    # `_remove_empty_dir` removes a directory the deletions emptied. Neither takes a path a caller
    # chose. Both refuse anything not in `_VERIFIED`, the set `verify_candidate()` produces from a
    # `git diff-tree` between HEAD and a candidate tree built in a TEMPORARY index, and that tree is
    # itself refused unless every path in it is inside the engine allowlist (`enginetree`'s ownership
    # manifest plus `script/` and `.plainkeep-engine-ref`) and every change is a deletion. So the
    # ratchet's question — can this resolve under `~/files/**/in/`? — is answered no by construction:
    # `bin/`, `script/`, `templates/verb` and `VERSION` inside a vault are engine code, an originals
    # tree is not on the allowlist, and there is no `--force` to widen it. `_remove_empty_dir` adds a
    # second bound on top: it must be an ANCESTOR of a verified deletion, and `os.rmdir` itself
    # refuses a directory anything is still inside. `test/run_migrate.py` drives both refusals
    # through the real CLI and mutation-tests the AST ratchet that keeps them the only two.
    #
    # The rest are outside every vault, same class as the installer's: two atomic symlink swaps of
    # `<bin-dir>/plainkeep` (one repointing it off the vault-local shim, one putting it back on
    # rollback — a single pin covers both, the text is identical), the receipt's own rename, the
    # scratch `mkdtemp` the candidate tree is built in, and the receipt a rollback deletes because a
    # vault that is no longer migrated must not read as one.
    "bin/lib/migrate.py": {"os.remove(p)",
                           "os.rmdir(vault / rel)",
                           "tmp.unlink()",
                           "os.replace(tmp, link)",
                           "os.replace(tmp, p)",
                           "shutil.rmtree(scratch, ignore_errors=True)",
                           'receipt_path(marker["id"]).unlink(missing_ok=True)'},
    # `new verb` scaffolds through a `.pk-scaffolding-<verb>.<pid>` staging leaf and renames it into
    # place, so that a scaffold which fails halfway (it did — the engine seal made every copied file
    # read-only, and `_fill` could not substitute) leaves nothing behind instead of an unwritable verb
    # full of `{{name}}` that the resolver then dispatched. The removal targets that leaf and only it:
    # it is `paths.PLAINKEEP_HOME / "plugins" / "local" / ".pk-scaffolding-*"`, which cannot resolve
    # under `~/files/**/in/` — plugins/ is a vault-owned code tree, not an originals tree. The move
    # INTO place goes through the seam (`vaultio.replace`), which classifies both ends.
    # The second removal is the SWEEP of leaves an earlier run was killed before it could rename: same
    # glob, same directory, and it is age-gated (older than the installer's `STALE_STAGING_SECONDS`)
    # so it can never take a leaf a concurrent scaffold is still filling. `p` comes from
    # `parent.glob(".pk-scaffolding-*")` where `parent` is that same `plugins/local/`, so the target
    # set is the dot-prefixed staging namespace and nothing else — a user's verb has no leading dot.
    "bin/new/run.py": {"shutil.rmtree(staging, ignore_errors=True)",
                       "shutil.rmtree(p, ignore_errors=True)"},
    "bin/plugin/run.py": {"shutil.rmtree(staging, ignore_errors=True)",
                          "shutil.rmtree(dest, ignore_errors=True)"},
    "bin/repo/run.py": {"shutil.rmtree(nm); freed += 1"},
    "bin/sweep/run.py": {"b.rmdir()"},
    "bin/task/run.py": {"f.rename(new)"},
    "bin/triage/run.py": {"p.unlink()"},
    "bin/week/run.py": {"f.rename(dest / f.name)"},
}


# --------------------------------------------------------------------------------------------
# F2. The seam ITSELF, checked structurally rather than by grepping the file for a string.
#
# Every primitive in `vaultio.py` that TAKES A NAME AWAY must classify that name first. The list is
# spelled out here so it reads as the claim it makes, and so a NEW destroying primitive cannot be
# added without a reviewer putting it on one of these two lists.
SEAM = REPO / "bin" / "lib" / "vaultio.py"

# function -> the syscall it makes on somebody else's name. Each MUST call `_guard_delete`.
SOURCE_DESTROYING = {
    "move_create_only": "unlinks its source once the original has arrived",
    "_unlink_arrived_source": "os.unlink — the syscall itself",
    "move": "shutil.move takes the source away",
    "replace": "rename(2) destroys the source name and whatever occupied the destination",
}

# function -> {exact removal call: why it needs NO verdict}. Every one of these unlinks a leaf THIS
# module just created, that nothing else has ever had a name for — so there is no original to
# classify and no append-only question to ask. Pinned rather than allowed by pattern: a new removal
# inside the seam cannot join them silently.
SELF_CLEANUP = {
    "_direct_create_only_copy": {"os.unlink(dst)": "the half-written leaf this call just created"},
    "_create_only_copy": {"os.unlink(tmp)": "this call's own staging leaf"},
    "move_create_only": {"os.unlink(p)": "backs out a destination this call created moments ago"},
}

_DESTROYING_CALLS = {"os.unlink", "os.remove", "os.rename", "os.replace", "os.rmdir",
                     "shutil.move", "shutil.rmtree"}


def _is_destroying_call(n: ast.Call) -> bool:
    if _callname(n.func) in _DESTROYING_CALLS:
        return True
    # `<anything>.replace(one_positional_arg)` is `rename(2)`; `s.replace(a, b)` is the string method
    # and `dt.replace(tzinfo=…)` the datetime one. Same arity signal `PATH_REPLACE_DELETE` uses, and
    # here it is read off the parse tree rather than guessed from the text.
    return (isinstance(n.func, ast.Attribute) and n.func.attr == "replace"
            and len(n.args) == 1 and not n.keywords)


def _callname(node: ast.AST) -> str:
    """`os.unlink` for `os.unlink(x)`, `_guard_delete` for `_guard_delete(x)`, `.replace` for
    `Path(s).replace(p)` — enough to recognise a removal without resolving types."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _callname(node.value)
        return f"{base}.{node.attr}" if base else f".{node.attr}"
    return ""


def _seam_report() -> list[tuple[str, bool, str]]:
    tree = ast.parse(SEAM.read_text(encoding="utf-8"))
    src = SEAM.read_text(encoding="utf-8").splitlines()
    funcs = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}
    out: list[tuple[str, bool, str]] = []

    missing = []
    for name, what in SOURCE_DESTROYING.items():
        fn = funcs.get(name)
        guarded = fn is not None and any(
            isinstance(n, ast.Call) and _callname(n.func) == "_guard_delete" for n in ast.walk(fn))
        if not guarded:
            missing.append(f"vaultio.{name}() — {what} — does not call _guard_delete"
                           if fn is not None else f"vaultio.{name}() has GONE from the seam")
    out.append(("delete ratchet: every source-destroying primitive in vaultio classifies its source",
                not missing, "\n        " + "\n        ".join(missing)))

    # A removal ANYWHERE in the seam is either inside a function that classifies, or pinned above.
    stray = []
    for name, fn in funcs.items():
        pinned = SELF_CLEANUP.get(name, {})
        classifies = any(isinstance(n, ast.Call) and _callname(n.func) == "_guard_delete"
                         for n in ast.walk(fn))
        for n in ast.walk(fn):
            if not (isinstance(n, ast.Call) and _is_destroying_call(n)):
                continue
            text = src[n.lineno - 1].strip()
            if any(text.startswith(k) for k in pinned) or classifies:
                continue
            stray.append(f"vaultio.{name}():{n.lineno}  {text}")
    out.append(("delete ratchet: no removal inside the seam is unclassified and unpinned",
                not stray, "\n        " + "\n        ".join(stray)))

    # The reason `_guard_delete` falls back on when the `.env` rule pre-empts `classify`'s delete
    # branch must still be the wall's OWN words — otherwise the seam refuses with a message the
    # validated model no longer uses, which is how the wrong-reason bug got in.
    # Anything the seam does not supply is a FAILED CHECK, never an exception: a ratchet that dies
    # reports the crash instead of the damage, and this one is loaded against modified trees by
    # design (that is how it gets mutation-tested).
    try:
        sys.path.insert(0, str(REPO / "bin"))
        import importlib.util
        spec = importlib.util.spec_from_file_location("pw_vaultio", SEAM)
        vio = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(vio)
        said = vio.guardrail.classify(
            {"kind": "delete", "path": "~/files/clients/acme/in/brief.pdf",
             "realpath": "~/files/clients/acme/in/brief.pdf"}).reason
        ok, why = said == vio._APPEND_ONLY, f"classify={said!r} seam={vio._APPEND_ONLY!r}"
    except BaseException as e:   # noqa: BLE001
        ok, why = False, f"could not read the seam's wording back: {type(e).__name__}: {e}"
    out.append(("delete ratchet: the seam's append-only reason is still the wall's own wording",
                ok, why))
    return out


def scan_raw_deletes() -> dict[str, dict[int, str]]:
    found: dict[str, dict[int, str]] = {}
    for f in sorted((REPO / "bin").rglob("*.py")):
        rel = str(f.relative_to(REPO))
        if rel == "bin/lib/vaultio.py":       # the seam itself; its one source unlink is classified
            continue
        for i, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
            code = line.split("#", 1)[0]
            if _is_raw_delete(code):
                found.setdefault(rel, {})[i] = line.strip()
    return found


def case_delete_ratchet() -> None:
    found = scan_raw_deletes()
    new = [f"{rel}:{ln}  {text}" for rel, lines in found.items() for ln, text in lines.items()
           if text not in PINNED_DELETES.get(rel, set())]
    check("delete ratchet: no raw removal or rename in bin/ that is not on the pinned list",
          not new, "\n        " + "\n        ".join(new[:40]))
    gone = [f"{rel}: {t}" for rel, texts in PINNED_DELETES.items() for t in texts
            if t not in set(found.get(rel, {}).values())]
    check("delete ratchet: no stale pins (a removed site must leave the list)", not gone, str(gone))

    # And the seam the pin exists to protect. This check USED to be `'"kind": "delete"' in seam and
    # "_guard_delete" in seam` — a substring grep over the whole file, and both of those strings live
    # inside `_guard_delete`'s own `def` and docstring. Deleting every CALL to the guard while leaving
    # the helper defined kept it GREEN: the one check whose name claimed the property was the one that
    # did not test it. It is structural now — the call sites are read out of the AST, per function, so
    # removing one is exactly what makes this fail.
    for name, ok, why in _seam_report():
        check(name, ok, why)


def scan_raw_writes() -> dict[str, dict[int, str]]:
    found: dict[str, dict[int, str]] = {}
    for f in sorted((REPO / "bin").rglob("*.py")):
        rel = str(f.relative_to(REPO))
        if rel == "bin/lib/vaultio.py":       # the seam itself is where the real writes live
            continue
        for i, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
            code = line.split("#", 1)[0]
            if _is_raw_write(code):
                found.setdefault(rel, {})[i] = line.strip()
    return found


def case_ratchet() -> None:
    found = scan_raw_writes()
    unexpected = []
    for rel, lines in found.items():
        allowed = EXEMPT.get(rel, {})
        for ln, text in lines.items():
            if not any(text.startswith(k) for k in allowed):
                unexpected.append(f"{rel}:{ln}  {text}")
    check("ratchet: every raw write in bin/ is either guarded or a listed exemption",
          not unexpected, "\n        " + "\n        ".join(unexpected[:40]))

    stale = [f"{rel}: {k}" for rel, lines in EXEMPT.items() for k in lines
             if not any(t.startswith(k) for t in found.get(rel, {}).values())]
    check("ratchet: no stale exemptions (a fixed site must leave the list)", not stale, str(stale))


def main() -> int:
    case_walled_root()
    case_good_root()
    case_symlink_escape()
    case_sdk_journal()
    case_ratchet()
    case_delete_ratchet()

    print(f"{BOLD}Path-wall enforcement (bin/lib/vaultio.py) — {len(results)} checks{RESET}\n")
    passed = sum(1 for _, ok, _ in results if ok)
    for name, ok, detail in results:
        mark = f"{GREEN}PASS{RESET}" if ok else f"{RED}FAIL{RESET}"
        print(f"  {mark} {name:<62}" + (f" {DIM}{detail}{RESET}" if (detail and not ok) else ""))
    failed = len(results) - passed
    exempt_total = sum(len(v) for v in EXEMPT.values())
    if exempt_total:
        print(f"\nSUITE-NOTE: {exempt_total} raw write site(s) in bin/ are NOT behind the wall — "
              f"~/work fleet trees, ~/.Trash, a human-supplied `--out`, the guardrail's own audit "
              f"log, and the vault marker/registry (the writes that ESTABLISH where the wall goes). "
              f"The wall as written DENIES all of them; whether its model should cover verb-owned "
              f"writes outside the three roots is a policy decision, not a wiring fix.")
    print("SUITE-NOTE: this suite invokes each verb's run.py DIRECTLY with PLAINKEEP_HOME set, so it "
          "never runs discovery and cannot judge whether the root was resolved correctly — the wall "
          "is anchored to the root it is handed. What changed with ADR-014 Task 1b (shipped) is who "
          "hands it over: in a real invocation both dispatchers now VALIDATE the root before the "
          "gate runs, and guardrail.py's VAULT_ROOTS is that one root and nothing else. Discovery "
          "itself is gated by test/run_discovery.py, not here.")
    print(f"\n{BOLD}Result:{RESET} {GREEN}{passed} passed{RESET}, "
          f"{(RED if failed else DIM)}{failed} failed{RESET}, {len(results)} checks")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
