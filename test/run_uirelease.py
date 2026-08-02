#!/usr/bin/env python3
"""
run_uirelease.py — the two release gates for the `plainkeep-ui` binary, run on every batch.

Both gates existed before this suite. Neither RAN.

**Gate 1 — the three-way version check.** `plainkeep setup ui --yes` downloads the release named by
the engine-owned pin and then compares what it installed against `plainkeep-ui --version`, so three
things must agree at release time: the git tag, the pin, and the constant compiled into the binary.
That rule lived as an inline shell snippet inside `.github/workflows/release-ui.yml`, reachable only
by pushing a `ui-v*` tag — which is to say it was never executed by anything that could report on it,
and its own parser (a `sed` regex over a TypeScript file) had no test at all. A parser that silently
yields the empty string makes every comparison pass. The rule now lives here, in code, and this suite
proves on every run that it FAILS on each of the three ways the three can disagree.

**Gate 2 — the bun floor.** `cli/package.json`'s `check:bun` refuses to build below bun 1.2.21,
because older bun DROPS empty-string entries when spawning a child and the dispatcher would then eat
an empty verb argument. `build` and `test` ran it; `build:ui` — the script that produces the artifact
a floor user actually installs — did not. Fixed in the same commit as this suite; what is checked
here is the property rather than the one script: EVERY script that runs `bun build --compile` passes
through the gate, so the next compile script is covered on the run that adds it.

**Where the pin lives is not hardcoded here.** `bin/ui/version.txt` is engine-owned content: it
travels inside an installed engine tree (ADR-017) and `enginetree.NAMED_CONTENT` is the manifest that
says so. This suite derives the pin's path FROM that manifest, so moving the file means editing the
manifest — one place — rather than editing a workflow, a bun test and a suite that each spelled the
path out and could each be missed.

Usage:
    python3 test/run_uirelease.py                  # both gates, no tag leg (the batch run)
    python3 test/run_uirelease.py --tag ui-v0.2.0  # the same, plus the tag leg (the release run)

Offline, stdlib only. Green from the repo root and from inside `test/`.
"""
from __future__ import annotations
import importlib.util
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from lib.hermetic import seal
seal()   # hermetic: an empty throwaway registry, never the developer's real vault

REPO = Path(__file__).resolve().parents[1]
PKG = REPO / "cli" / "package.json"
TS_VERSION = REPO / "cli" / "src" / "tui" / "version.ts"
WORKFLOW = REPO / ".github" / "workflows" / "release-ui.yml"
GREEN, RED, DIM, BOLD, YELLOW, RESET = ("\033[32m", "\033[31m", "\033[2m", "\033[1m",
                                        "\033[33m", "\033[0m")
results: list[tuple[str, bool, str]] = []
notes: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    results.append((name, bool(cond), detail))


# --- where the pin lives, asked of the ownership manifest ------------------------------------------
def _load_enginetree():
    """Import `bin/lib/enginetree.py` BY FILE LOCATION, never by name off `sys.path`.

    The cwd is not part of this: the suite is green from the repo root and from `test/`, and a
    `sys.path`-based import is precisely how a previous check ended up reading the caller's directory
    instead of the tree it meant (see `test/README.md`)."""
    spec = importlib.util.spec_from_file_location("_pk_enginetree",
                                                  REPO / "bin" / "lib" / "enginetree.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)                                          # type: ignore[union-attr]
    return mod


def pin_rel_from_manifest() -> str | None:
    """The pin's repo-relative path, taken from the engine ownership manifest.

    None when the manifest names zero or more than one such path — an ambiguous manifest must fail
    the gate rather than have this function pick a winner."""
    named = [p for p in _load_enginetree().NAMED_CONTENT if p.endswith("ui/version.txt")]
    return named[0] if len(named) == 1 else None


# --- the rule itself, as one implementation --------------------------------------------------------
_TS_VERSION_RE = re.compile(r'^export const VERSION = "([^"]*)"', re.M)
_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$")


def read_pin(repo: Path, pin_rel: str) -> tuple[str | None, str]:
    """The engine-owned pin, or (None, why)."""
    f = repo / pin_rel
    if not f.is_file():
        return None, f"{pin_rel} is missing — the engine's own stale-install check reads it"
    raw = f.read_text(encoding="utf-8").strip()
    if not _VERSION_RE.match(raw):
        return None, f"{pin_rel} does not hold a version: {raw!r}"
    return raw, ""


def read_ts(repo: Path) -> tuple[str | None, str]:
    """The constant compiled into the binary, or (None, why).

    The failure this returns rather than swallows is the one the shell snippet had: a `version.ts`
    whose declaration is reformatted yields NO match, and an unmatched extraction that is allowed to
    become the empty string compares equal to nothing and unequal to everything — either way the
    result is not about the versions."""
    rel = "cli/src/tui/version.ts"
    f = repo / rel
    if not f.is_file():
        return None, f"{rel} is missing — it is what `plainkeep-ui --version` serves"
    m = _TS_VERSION_RE.search(f.read_text(encoding="utf-8"))
    if not m:
        return None, (f"{rel} has no `export const VERSION = \"...\"` line this check can read — "
                      "the declaration was reformatted, and the check cannot see the version at all")
    if not _VERSION_RE.match(m.group(1)):
        return None, f"{rel} declares VERSION = {m.group(1)!r}, which is not a version"
    return m.group(1), ""


def ui_version_problems(repo: Path, tag: str | None, pin_rel: str) -> list[str]:
    """Every way the tag, the pin and the compiled constant can disagree. Empty list == the gate is
    green. `tag` is None on a batch run (there is no tag yet) and the `ui-v*` ref name on a release."""
    problems: list[str] = []
    pin, why = read_pin(repo, pin_rel)
    if why:
        problems.append(why)
    src, why = read_ts(repo)
    if why:
        problems.append(why)
    if pin is not None and src is not None and pin != src:
        problems.append(f"{pin_rel}={pin} but cli/src/tui/version.ts={src} — the engine would "
                        "download a release the binary does not claim to be")
    if tag is not None:
        want = tag[len("ui-v"):] if tag.startswith("ui-v") else None
        if want is None:
            problems.append(f"tag {tag!r} is not a `ui-v<version>` tag")
        else:
            if pin is not None and want != pin:
                problems.append(f"tag={want} but {pin_rel}={pin} — vaults pin their download to "
                                "that file, so the release would be unreachable")
            if src is not None and want != src:
                problems.append(f"tag={want} but cli/src/tui/version.ts={src} — the binary would "
                                "self-report a version no release carries")
    return problems


# --- A. the gate is anchored, and it is green here -------------------------------------------------
def case_anchor_and_green(tag: str | None) -> str | None:
    pin_rel = pin_rel_from_manifest()
    check("the pin's home comes from enginetree.NAMED_CONTENT, not from a literal in this file",
          pin_rel is not None, "the manifest names zero or several `ui/version.txt` paths")
    if pin_rel is None:
        return None
    check(f"...and it resolves to a real file ({pin_rel})", (REPO / pin_rel).is_file())
    # The pin is engine-owned CONTENT: an installed tree without it fails `enginetree.verify()`, which
    # is why the manifest is the right anchor and a repo-relative literal is not.
    problems = ui_version_problems(REPO, tag, pin_rel)
    check("the three-way check is GREEN at HEAD" + (f" (tag {tag})" if tag else " (no tag leg)"),
          not problems, "; ".join(problems))
    return pin_rel


# --- B. it FAILS on each way the three can disagree ------------------------------------------------
def _mutant(tmp: Path, name: str, pin_rel: str, *, pin: str | None = None,
            ts_body: str | None = None) -> Path:
    """A two-file copy of the repo's version sources, one leg optionally mutated."""
    root = tmp / name
    (root / Path(pin_rel).parent).mkdir(parents=True, exist_ok=True)
    (root / "cli" / "src" / "tui").mkdir(parents=True, exist_ok=True)
    (root / pin_rel).write_text(
        pin if pin is not None else (REPO / pin_rel).read_text(encoding="utf-8"), encoding="utf-8")
    (root / "cli" / "src" / "tui" / "version.ts").write_text(
        ts_body if ts_body is not None else TS_VERSION.read_text(encoding="utf-8"), encoding="utf-8")
    return root


def case_drift_is_detected(tmp: Path, pin_rel: str) -> None:
    """A gate that has never been seen to fail is a green test of nothing. Each cell below mutates ONE
    leg on a copy and requires the checker to say so — and to say WHICH pair disagrees, because a
    release that goes red at 3am is read by whoever is holding the tag."""
    true_v = (REPO / pin_rel).read_text(encoding="utf-8").strip()
    other = "9.9.9"

    r = _mutant(tmp, "pin-drift", pin_rel, pin=other + "\n")
    probs = ui_version_problems(r, None, pin_rel)
    check("RED: pin != compiled constant, with no tag in play",
          any(pin_rel in p and "version.ts" in p for p in probs), f"{probs}")

    r = _mutant(tmp, "tag-vs-pin", pin_rel)
    probs = ui_version_problems(r, f"ui-v{other}", pin_rel)
    check("RED: tag != pin", any(f"tag={other}" in p and pin_rel in p for p in probs), f"{probs}")

    r = _mutant(tmp, "tag-vs-src", pin_rel, pin=other + "\n",
                ts_body=f'export const VERSION = "{other}";\n')
    probs = ui_version_problems(r, f"ui-v{true_v}", pin_rel)
    check("RED: tag != compiled constant (both other legs agreeing with each other)",
          any(f"tag={true_v}" in p and "version.ts" in p for p in probs), f"{probs}")

    # The defect the shell snippet could not have caught: its `sed` yields nothing on a reformatted
    # declaration, and nothing compared against nothing is a pass.
    r = _mutant(tmp, "unreadable-ts", pin_rel,
                ts_body='export const VERSION =\n  "0.2.0";\n')
    probs = ui_version_problems(r, f"ui-v{true_v}", pin_rel)
    check("RED: the compiled constant cannot be READ (reformatted declaration) — not a silent pass",
          any("cannot see the version at all" in p for p in probs), f"{probs}")

    r = _mutant(tmp, "missing-pin", pin_rel)
    (r / pin_rel).unlink()
    probs = ui_version_problems(r, f"ui-v{true_v}", pin_rel)
    check("RED: the pin file is gone", any("is missing" in p for p in probs), f"{probs}")

    r = _mutant(tmp, "junk-pin", pin_rel, pin="latest\n")
    probs = ui_version_problems(r, None, pin_rel)
    check("RED: the pin holds something that is not a version",
          any("does not hold a version" in p for p in probs), f"{probs}")

    r = _mutant(tmp, "bad-tag", pin_rel)
    probs = ui_version_problems(r, "v0.2.0", pin_rel)
    check("RED: a tag that is not `ui-v<version>`",
          any("is not a `ui-v" in p for p in probs), f"{probs}")

    # ...and the same fixture, unmutated, is GREEN — otherwise every cell above would pass for a
    # checker that simply always complains.
    r = _mutant(tmp, "clean", pin_rel)
    check("GREEN: the unmutated fixture, tag included",
          not ui_version_problems(r, f"ui-v{true_v}", pin_rel),
          f"{ui_version_problems(r, f'ui-v{true_v}', pin_rel)}")


# --- C. the release actually CALLS this, and the rule has exactly one implementation ---------------
def case_the_workflow_calls_it() -> None:
    """The pattern this whole suite is an answer to (ADR-019): a rule that is written down, agreed,
    and model-tested, and that nothing in the product ever consults. So: prove the consumer runs it."""
    wf = WORKFLOW.read_text(encoding="utf-8") if WORKFLOW.is_file() else ""
    check("release-ui.yml exists", bool(wf))
    check("release-ui.yml runs THIS suite, with the tag",
          bool(re.search(r"run_uirelease\.py\s+--tag", wf)), wf[:200])
    check("...passing the ref name, so the tag leg is the real tag",
          "GITHUB_REF_NAME" in wf and "run_uirelease.py" in wf)
    # One rule, one implementation. The old inline snippet is what drifted from reality for a whole
    # phase without anyone noticing, because nothing ran it.
    check("release-ui.yml no longer carries its own copy of the comparison",
          "export const VERSION" not in wf and "version drift:" not in wf,
          "the inline shell check is back")
    runner = (REPO / "test" / "run_all.py").read_text(encoding="utf-8")
    check("run_all.py runs this suite, so the batch executes the gate every time",
          "run_uirelease.py" in runner)
    # The bun-side guard is a second, faster reader of the SAME pin. It is allowed to exist; it is not
    # allowed to point somewhere else.
    ts_test = REPO / "cli" / "src" / "tui" / "version.test.ts"
    if ts_test.is_file():
        m = re.search(r'new URL\("([^"]+)", import\.meta\.url\)', ts_test.read_text(encoding="utf-8"))
        target = (ts_test.parent / m.group(1)).resolve() if m else None
        check("cli/src/tui/version.test.ts reads the same pin file this gate does",
              target is not None and target == (REPO / (pin_rel_from_manifest() or "")).resolve(),
              f"{target}")


# --- D. the bun floor gates every compile script ---------------------------------------------------
def case_bun_floor() -> None:
    pkg = json.loads(PKG.read_text(encoding="utf-8"))
    scripts: dict[str, str] = pkg.get("scripts", {})
    compilers = {k: v for k, v in scripts.items() if "bun build --compile" in v}
    check("cli/package.json has compile scripts to gate", bool(compilers), f"{list(scripts)}")
    for name, body in sorted(compilers.items()):
        # Prefix, not merely presence: a gate that runs after the build has already produced the
        # artifact is not a gate.
        gated = re.match(r"\s*bun run check:bun\s*&&", body) is not None
        check(f"`{name}` passes through check:bun BEFORE it compiles", gated, body[:90])
    check("`build:ui` is one of the gated scripts", "build:ui" in compilers)

    # The gate's own predicate, proved rather than trusted — the shipped expression, with Bun.version
    # replaced by the version under test. Nothing here re-implements the comparison.
    src = scripts.get("check:bun", "")
    m = re.match(r"^bun -e '(.*)'$", src, re.S)
    if not m:
        check("check:bun is a `bun -e '<expr>'` one-liner this suite can drive", False, src[:120])
        return
    check("check:bun is a `bun -e '<expr>'` one-liner this suite can drive", True)
    if shutil.which("bun") is None:
        notes.append("bun is not installed here, so the FLOOR PREDICATE cells (does check:bun "
                     "actually refuse an old bun?) were SKIPPED. CI's `ui` job has bun and runs "
                     "them; the offline-suites job does not. 4 checks skipped.")
        return
    for version, want_ok in (("1.1.45", False), ("1.2.0", False), ("1.2.21", True), ("1.3.14", True)):
        expr = m.group(1).replace("Bun.version", f'"{version}"')
        rc = subprocess.run(["bun", "-e", expr], capture_output=True, text=True).returncode
        check(f"check:bun {'accepts' if want_ok else 'REFUSES'} bun {version}",
              (rc == 0) is want_ok, f"rc={rc}")


def main() -> int:
    tag = None
    argv = sys.argv[1:]
    if argv:
        if argv[0] != "--tag" or len(argv) != 2:
            print("usage: run_uirelease.py [--tag <ui-v...>]", file=sys.stderr)
            return 2
        tag = argv[1]

    pin_rel = case_anchor_and_green(tag)
    if pin_rel is not None:
        with tempfile.TemporaryDirectory(prefix="pk-uirelease-") as td:
            case_drift_is_detected(Path(td), pin_rel)
    case_the_workflow_calls_it()
    case_bun_floor()

    print(f"{BOLD}ui release gates: version three-way + bun floor — {len(results)} checks{RESET}\n")
    passed = sum(1 for _, ok, _ in results if ok)
    for name, ok, detail in results:
        mark = f"{GREEN}PASS{RESET}" if ok else f"{RED}FAIL{RESET}"
        print(f"  {mark} {name:<78}"
              + (f" {DIM}{detail.strip()[:110]}{RESET}" if (detail and not ok) else ""))
    failed = len(results) - passed
    if tag is None:
        notes.append("no --tag was supplied, so the TAG leg of the three-way check was exercised "
                     "only against fixtures. `.github/workflows/release-ui.yml` supplies the real "
                     "one; a batch run has no tag to supply.")
    for note in notes:
        print(f"\nSUITE-NOTE: {note}")
    print(f"\n{BOLD}Result:{RESET} {GREEN}{passed} passed{RESET}, "
          f"{(RED if failed else DIM)}{failed} failed{RESET}, {len(results)} checks")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
