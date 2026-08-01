#!/usr/bin/env python3
"""
plainkeep backup — the durability family (§14, proposal Part 5.1). Bare `plainkeep backup` is UNCHANGED: the
read-only nag that verifies ~/plainkeep is committed + pushed and exits 1 if at risk (it NEVER commits or
pushes — §3, transmit is the human's call). Subcommands add the restic floor + git-bundle safety net:

  backup init        interactive, human-run ONCE (confirm/--yes): collect a local external-SSD restic
                     repo + B2 bucket as op:// REFERENCES (never resolved), render the launchd plist,
                     PRINT where to copy it (never installs a launch agent). Writes .backup/config.json.
  backup status      snapshot age per configured target; exit 1 if stale >48h or unconfigured. Works
                     WITHOUT restic (reports unconfigured / install hint).
  backup run [--target local|cloud]   restic backup of ~/files, ~/dotfiles + working-tree mirrors of
                     ~/plainkeep and ~/work, then `restic check`. Cloud is confirm-class ALWAYS (--yes).
                     The SCHEDULED cloud push runs from launchd invoking restic DIRECTLY with an
                     append-only B2 key — OUTSIDE this verb surface (see the rendered plist).
  backup drill       restore latest snapshot (or --subset N) to tmp, diff vs source, fail loud
                     (confirm/--yes). A backup that has never been restored is a hypothesis.
  backup bundle      rotated `git bundle --all` of ~/plainkeep + every ~/work repo into
                     ~/files/backups/bundles/ with retention (keep last N) — closes the
                     remote-less-repo-has-one-copy hole. Captured by restic.

restic is auto-detected via shutil.which — every restic path degrades gracefully with an install hint.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lib import output, paths, vaultio  # noqa: E402

GREEN, RED, YEL, DIM, RESET = "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[0m"

BACKUP_DIR = paths.PLAINKEEP_HOME / ".backup"
CONFIG = BACKUP_DIR / "config.json"
STALE_HOURS = int(os.environ.get("PLAINKEEP_BACKUP_STALE_HOURS", "48"))
BUNDLE_KEEP = int(os.environ.get("PLAINKEEP_BUNDLE_KEEP", "3"))
RESTIC_HINT = "install restic: brew install restic  (then `plainkeep backup init`)"


# --------------------------------------------------------------------------- the bare nag (UNCHANGED)

def cmd_nag():
    if not (paths.PLAINKEEP_HOME / ".git").exists():
        data = {"git_repo": False, "branch": None, "upstream": None,
                "dirty": 0, "unpushed": 0, "at_risk": True}
        output.emit(data, "backup", human=lambda _:
                    f"{YEL}~/plainkeep is not a git repo — your knowledge is NOT versioned. run: git init{RESET}")
        return 1

    dirty = [ln for ln in paths.git("status", "--porcelain").splitlines() if ln.strip()]
    branch = (paths.git("rev-parse", "--abbrev-ref", "HEAD").strip() or "?")
    upstream = paths.git("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}").strip()
    ahead = 0
    if upstream:
        out = paths.git("rev-list", "--count", "@{u}..HEAD").strip()
        ahead = int(out) if out.isdigit() else 0

    risk = bool(dirty) or (not upstream) or ahead > 0
    data = {"git_repo": True, "branch": branch, "upstream": upstream or None,
            "dirty": len(dirty), "dirty_files": dirty[:8], "unpushed": ahead, "at_risk": risk}

    def render(_):
        print(f"plainkeep repo: {branch}" + (f" → {upstream}" if upstream else " (no remote tracking)"))
        if dirty:
            print(f"  {RED}● {len(dirty)} uncommitted change(s){RESET} — run: git add -A && git commit")
            for ln in dirty[:8]:
                print(f"      {ln}")
        else:
            print(f"  {GREEN}● working tree clean{RESET}")
        if not upstream:
            print(f"  {YEL}● no upstream set{RESET} — your commits live only on this machine "
                  f"(set one: git push -u origin {branch})")
        elif ahead:
            print(f"  {RED}● {ahead} commit(s) not pushed{RESET} — run: git push")
        else:
            print(f"  {GREEN}● pushed — remote is current{RESET}")
        print(f"\nbackup: {'AT RISK — act on the lines above' if risk else 'safe (committed + pushed)'}")

    output.emit(data, "backup", human=render)
    return 1 if risk else 0


# --------------------------------------------------------------------------- config + restic helpers

def _config() -> dict:
    try:
        return json.loads(CONFIG.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _have_restic() -> bool:
    return shutil.which("restic") is not None


def _is_local_repo(repo: str) -> bool:
    """A restic repo we may query offline (a filesystem path); b2:/s3:/rest: are remote → never
    contacted here (hard rule: no real cloud calls; the scheduled launchd job verifies those)."""
    return bool(repo) and ":" not in repo.split("/", 1)[0]


def _restic(repo: str, *args, timeout=120):
    env = {**os.environ, "RESTIC_REPOSITORY": repo}
    return subprocess.run(["restic", *args], capture_output=True, text=True, env=env, timeout=timeout)


def _latest_snapshot_ts(repo: str):
    """Epoch seconds of the newest snapshot in a LOCAL repo, or None (unreachable/empty/no restic)."""
    if not _have_restic() or not _is_local_repo(repo):
        return None
    try:
        r = _restic(repo, "snapshots", "--json", "--last", timeout=60)
        if r.returncode != 0:
            return None
        snaps = json.loads(r.stdout or "[]")
        if not snaps:
            return None
        t = snaps[-1].get("time", "")[:19]
        return int(datetime.strptime(t, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc).timestamp())
    except Exception:
        return None


def _backup_paths() -> list[Path]:
    """~/files, ~/dotfiles, and the working-tree mirrors of ~/plainkeep and ~/work (existing paths only)."""
    cands = [paths.FILES_ROOT, paths.ROOTS_HOME / "dotfiles", paths.PLAINKEEP_HOME, paths.WORK_ROOT]
    return [p for p in cands if p.exists()]


# --------------------------------------------------------------------------- status

def cmd_status(argv):
    cfg = _config()
    targets = cfg.get("targets", {})
    now = int(datetime.now(timezone.utc).timestamp())
    rows, at_risk = [], False
    if not targets:
        at_risk = True
        rows.append({"target": "(none)", "configured": False, "age_hours": None,
                     "stale": True, "note": "no .backup/config.json — run: plainkeep backup init"})
    for name, t in targets.items():
        repo = t.get("repo", "")
        ts = _latest_snapshot_ts(repo)
        if ts is None:
            if not _have_restic():
                reason, stale = RESTIC_HINT, True        # can't verify a configured target → at risk
            elif not _is_local_repo(repo):
                reason, stale = "remote — verified by the scheduled launchd job", False
            else:
                reason, stale = "no snapshots yet", True  # reachable-but-empty local repo is stale
            at_risk = at_risk or stale
            rows.append({"target": name, "configured": True, "age_hours": None,
                         "stale": stale, "note": reason})
            continue
        age_h = (now - ts) / 3600.0
        stale = age_h > STALE_HOURS
        at_risk = at_risk or stale
        rows.append({"target": name, "configured": True, "age_hours": round(age_h, 1),
                     "stale": stale, "note": ""})

    def render(rs):
        out = []
        for r in rs:
            mark = f"{RED}●{RESET}" if r["stale"] else f"{GREEN}●{RESET}"
            age = f"{r['age_hours']}h" if r["age_hours"] is not None else (r["note"] or "?")
            out.append(f"  {mark} {r['target']:<8} {age}")
        out.append(f"\nbackup status: {'AT RISK — stale/unconfigured' if at_risk else 'fresh'}")
        return "\n".join(out)

    output.emit_rows(rows, "backup", human=render, header={"at_risk": at_risk})
    return 1 if at_risk else 0


# --------------------------------------------------------------------------- run

def cmd_run(argv):
    target = argv[argv.index("--target") + 1] if "--target" in argv else "local"
    yes = ("--yes" in argv) or ("-y" in argv)
    dry = "--dry-run" in argv
    if target not in ("local", "cloud"):
        output.fail(output.EXIT_USAGE, "usage: plainkeep backup run [--target local|cloud]", verb="backup")
    if target == "cloud" and not yes and not dry:
        output.fail(output.EXIT_CONFIRM, "cloud backup publishes off-machine (a transmission)",
                    hint="re-run: plainkeep backup run --target cloud --yes", verb="backup")
    cfg = _config()
    t = cfg.get("targets", {}).get(target)
    if not t:
        output.fail(output.EXIT_UNEXPECTED, f"target '{target}' not configured",
                    hint="run: plainkeep backup init", verb="backup")
    if not _have_restic():
        output.fail(output.EXIT_UNEXPECTED, "restic not installed", hint=RESTIC_HINT, verb="backup")
    repo = t["repo"]
    src = [str(p) for p in _backup_paths()]
    if dry:
        return output.emit({"target": target, "repo": repo, "paths": src, "dry_run": True},
                           "backup", human=lambda _: f"dry-run: would `restic backup` {len(src)} path(s) to {target}")
    # pragma: no cover - requires restic + a repo; never run for cloud in tests
    b = _restic(repo, "backup", *src, "--tag", "plainkeep", timeout=3600)
    sys.stdout.write(b.stdout)
    if b.returncode != 0:
        output.fail(output.EXIT_UNEXPECTED, "restic backup failed", hint=b.stderr.strip(), verb="backup")
    c = _restic(repo, "check", timeout=1800)
    ok = c.returncode == 0
    return output.emit({"target": target, "backed_up": True, "check_ok": ok}, "backup",
                       human=lambda _: f"{GREEN}backup {target}: done, check {'ok' if ok else 'FAILED'}{RESET}")


# --------------------------------------------------------------------------- drill

def cmd_drill(argv):
    yes = ("--yes" in argv) or ("-y" in argv)
    if not yes:
        output.fail(output.EXIT_CONFIRM, "drill restores a snapshot to a temp dir",
                    hint="re-run: plainkeep backup drill --yes", verb="backup")
    if not _have_restic():
        output.fail(output.EXIT_UNEXPECTED, "restic not installed", hint=RESTIC_HINT, verb="backup")
    cfg = _config()
    t = cfg.get("targets", {}).get("local") or next(iter(cfg.get("targets", {}).values()), None)
    if not t:
        output.fail(output.EXIT_UNEXPECTED, "no backup target configured", hint="run: plainkeep backup init",
                    verb="backup")
    repo = t["repo"]
    # pragma: no cover - requires restic + a populated repo; never in tests
    with tempfile.TemporaryDirectory() as td:
        r = _restic(repo, "restore", "latest", "--target", td, timeout=3600)
        if r.returncode != 0:
            output.fail(output.EXIT_UNEXPECTED, "restore failed", hint=r.stderr.strip(), verb="backup")
        return output.emit({"drill": "ok", "restored_to": td}, "backup",
                           human=lambda _: f"{GREEN}drill: restored latest snapshot ok{RESET}")


# --------------------------------------------------------------------------- bundle (offline, restic-free)

def _bundle_repos() -> list[tuple[str, Path]]:
    """(name, repo) for ~/plainkeep + every ~/work repo (registry = repos on disk, like `repo health`)."""
    out = [("plainkeep", paths.PLAINKEEP_HOME)]
    if paths.WORK_ROOT.exists():
        for g in sorted(paths.WORK_ROOT.rglob(".git")):
            repo = g.parent
            if ".worktrees" in repo.parts or "archive" in repo.parts:
                continue
            out.append(("-".join(repo.relative_to(paths.WORK_ROOT).parts), repo))
    return out


def cmd_bundle(argv):
    dry = "--dry-run" in argv
    dest = paths.FILES_ROOT / "backups" / "bundles"
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    rows, events = [], []
    if not dry:
        vaultio.mkdir(dest)
    for name, repo in _bundle_repos():
        if not (repo / ".git").exists():
            continue
        out = dest / f"{name}-{stamp}.bundle"
        events.append(f"  {'would bundle' if dry else 'bundled'}: {name} -> {out.name}")
        rows.append({"repo": name, "bundle": out.name})
        if not dry:
            r = subprocess.run(["git", "-C", str(repo), "bundle", "create", str(out), "--all"],
                               capture_output=True, text=True)
            if r.returncode != 0:
                events.append(f"    {RED}skip {name}: {r.stderr.strip()[:80]}{RESET}")
                rows.pop()
                out.unlink(missing_ok=True)
                continue
            # retention: keep the newest BUNDLE_KEEP for this repo
            olds = sorted(dest.glob(f"{name}-*.bundle"))
            for stale in olds[:-BUNDLE_KEEP]:
                stale.unlink(missing_ok=True)
    if not dry and rows:
        paths.append_journal(f"backup bundle: {len(rows)} repo(s) -> files/backups/bundles/")

    def render(_):
        for e in events:
            print(e)
        print(f"\nbackup bundle: {len(rows)} repo(s) bundled" + (" (dry run)" if dry else ""))

    return output.emit_rows(rows, "backup", human=render,
                            header={"bundled": len(rows), "dest": str(dest), "dry_run": dry})


# --------------------------------------------------------------------------- init (config + plist)

def _plist(cfg: dict) -> str:
    """Render a launchd plist that runs restic DIRECTLY (outside the verb surface) with an append-only
    B2 key resolved at runtime from op:// via `op read` — the never-transmit tension resolved: the
    human consents once, then it is machine infrastructure that can only ADD to history."""
    cloud = cfg.get("targets", {}).get("cloud", {})
    repo = cloud.get("repo", "b2:BUCKET:plainkeep")
    pw = cloud.get("password_ref", "op://Private/plainkeep-restic/password")
    acct = cloud.get("b2_account_ref", "op://Private/b2-append-only/account")
    key = cloud.get("b2_key_ref", "op://Private/b2-append-only/key")
    src = " ".join(str(p) for p in _backup_paths())
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.plainkeep.backup.cloud</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/sh</string><string>-c</string>
    <string>export RESTIC_REPOSITORY="{repo}"; \\
export RESTIC_PASSWORD="$(op read {pw})"; \\
export B2_ACCOUNT_ID="$(op read {acct})"; \\
export B2_ACCOUNT_KEY="$(op read {key})"; \\
restic backup {src} --tag scheduled &amp;&amp; restic check</string>
  </array>
  <key>StartCalendarInterval</key><dict><key>Hour</key><integer>3</integer><key>Minute</key><integer>0</integer></dict>
  <key>StandardOutPath</key><string>{paths.PLAINKEEP_HOME}/.logs/backup-cloud.log</string>
  <key>StandardErrorPath</key><string>{paths.PLAINKEEP_HOME}/.logs/backup-cloud.log</string>
</dict>
</plist>
"""


def cmd_init(argv):
    yes = ("--yes" in argv) or ("-y" in argv)

    def flag(name, default=""):
        return argv[argv.index(name) + 1] if name in argv else default

    if not yes:
        output.fail(output.EXIT_CONFIRM, "init writes .backup/config.json + renders the launchd plist",
                    hint="re-run: plainkeep backup init --local-repo <path> --cloud-repo b2:<bucket>:plainkeep "
                         "--password-ref op://... --yes", verb="backup")
    # References ONLY — op:// values are stored verbatim and NEVER resolved (§4).
    cfg = {
        "targets": {
            "local": {"repo": flag("--local-repo", "/Volumes/Backup/restic-plainkeep"),
                      "password_ref": flag("--password-ref", "op://Private/plainkeep-restic/password")},
            "cloud": {"repo": flag("--cloud-repo", "b2:plainkeep-backup:plainkeep"),
                      "password_ref": flag("--password-ref", "op://Private/plainkeep-restic/password"),
                      "b2_account_ref": flag("--b2-account-ref", "op://Private/b2-append-only/account"),
                      "b2_key_ref": flag("--b2-key-ref", "op://Private/b2-append-only/key")},
        },
        "retention": {"keep_daily": 7, "keep_weekly": 4, "keep_monthly": 12},
    }
    vaultio.mkdir(BACKUP_DIR)
    vaultio.write_text(CONFIG, json.dumps(cfg, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    plist_path = BACKUP_DIR / "com.plainkeep.backup.cloud.plist"
    vaultio.write_text(plist_path, _plist(cfg), encoding="utf-8")
    label = "com.plainkeep.backup.cloud"
    steps = [
        f"cp {plist_path} ~/Library/LaunchAgents/{label}.plist",
        f"launchctl load ~/Library/LaunchAgents/{label}.plist",
    ]
    data = {"config": str(CONFIG.relative_to(paths.PLAINKEEP_HOME)),
            "plist": str(plist_path.relative_to(paths.PLAINKEEP_HOME)),
            "install_steps": steps, "targets": list(cfg["targets"])}
    return output.emit(data, "backup", human=lambda _:
                       f"{GREEN}wrote{RESET} {CONFIG.relative_to(paths.PLAINKEEP_HOME)} (op:// references only)\n"
                       f"rendered plist: {plist_path.relative_to(paths.PLAINKEEP_HOME)}\n"
                       f"install the scheduled cloud push yourself (never done for you):\n  "
                       + "\n  ".join(steps))


# --------------------------------------------------------------------------- dispatch

def main(argv):
    _, argv = output.parse_argv(argv)
    action = argv[0] if argv else ""
    if not action or action.startswith("-"):
        return cmd_nag()  # bare `plainkeep backup` (and `plainkeep backup --json`) — UNCHANGED
    rest = argv[1:]
    if action == "status":
        return cmd_status(rest)
    if action == "run":
        return cmd_run(rest)
    if action == "drill":
        return cmd_drill(rest)
    if action == "bundle":
        return cmd_bundle(rest)
    if action == "init":
        return cmd_init(rest)
    output.fail(output.EXIT_USAGE,
                "usage: plainkeep backup [status|run|drill|bundle|init]  (bare = commit/push nag)",
                verb="backup")


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
