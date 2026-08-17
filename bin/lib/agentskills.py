"""Deliver the operating manual to the directories AI agents actually read.

THE PROBLEM THIS EXISTS TO END. `skills/operate-plainkeep/SKILL.md` is engine-owned and ships inside
the engine tree, which is the one place no agent looks. Until this module the only thing pointing at
it was PROSE — a vault's `AGENTS.md` saying "read `skills/operate-plainkeep/SKILL.md`" — and since
ADR-017 moved the engine out of the vault, that relative path resolves to nothing in a data-only
vault. An agent that obeys the contract's first instruction gets ENOENT, has no manual, and falls
back to its own judgement: grep the notes, read `plainkeep.json` with a script, improvise a
placement. Every rule about HOW to operate the vault lives in the file it could not open.

WHY A SYMLINK PER AGENT AND NOT A CONFIG FLAG. `SKILL.md` in `<skills-dir>/<name>/` is an open
standard (agentskills.io, Dec 2025) that every agent here implements, and each one scans its OWN
directory. The manual already satisfies the spec — `name:` matches its folder, `description:` is
present — so delivery, not authoring, was the whole gap. Telling an operator to hand-edit N agent
configs to point at one exotic path is the arrangement that produced this bug; putting the file
where each tool already looks is the arrangement that cannot.

    ~/.claude/skills/     Claude Code
    ~/.agents/skills/     the cross-tool directory — Codex CLI, OpenClaw, Grok Build
    ~/.hermes/skills/     Hermes
    ~/.grok/skills/       Grok Build (reads this in ADDITION to ~/.agents)

SYMLINKS, NOT COPIES, and they name the engine through `current` (never the active version — see
`enginetree.stable_launcher`). One engine update refreshes every agent at once, a rollback follows
the same link back, and there is no second copy to drift. Claude Code and Codex document symlink
following explicitly; the rest resolve them through the filesystem like any other directory.

WHAT THIS WILL NOT DO. It replaces a symlink it owns and nothing else. A REAL directory sitting at a
target name is somebody's hand-installed skill, possibly edited; this module reports it and stops,
because silently deleting a directory to install a link is not a setup step, it is data loss.

Offline, stdlib only. The `PLAINKEEP_AGENT_HOME` override exists so the suite can point the whole
surface at a throwaway tree — the same reason `PLAINKEEP_LAUNCH_AGENTS_DIR` exists for launchd, and
it is sealed in `test/lib/hermetic.py` for the same reason.
"""
from __future__ import annotations
from dataclasses import dataclass
import os
from pathlib import Path

from lib import enginetree

# The skill delivered. One name, because it is the manual — a vault's own plugins are not shipped
# into an agent's global directory, where they would outlive the vault they describe.
SKILL_NAME = "operate-plainkeep"


@dataclass(frozen=True)
class Target:
    agent: str                 # human name, for the status row
    markers: tuple[str, ...]   # config dirs that mean "this agent is on the machine"
    rel: str                   # the skills dir it reads, relative to the home root


# A TOOL'S CONFIG DIR AND THE DIRECTORY IT READS SKILLS FROM ARE NOT THE SAME PATH, which is why
# `markers` is a set rather than the single directory this started as. Codex keeps its config in
# `~/.codex` and loads skills from `~/.agents/skills`; keying delivery on `~/.agents` existing meant
# that on a machine with Codex installed and no `~/.agents` yet — a real one, measured — the manual
# was never delivered and Codex went on operating without it. That is the same bug this whole module
# exists to end, one level up: a check for the wrong directory is as good as no delivery at all.
#
# `~/.agents/skills` is the cross-tool directory, so ANY of the three tools that read it is reason
# enough to create it. Grok Build reads both it and `~/.grok/skills`; both are targeted, because a
# skill listed twice under one name is cosmetic where a skill missing entirely is the failure.
TARGETS: list[Target] = [
    Target("Claude Code", (".claude",), ".claude/skills"),
    Target("Codex · OpenClaw · Grok", (".agents", ".codex", ".grok", ".openclaw", ".config/openclaw"),
           ".agents/skills"),
    Target("Hermes", (".hermes",), ".hermes/skills"),
    Target("Grok Build", (".grok",), ".grok/skills"),
]


def agent_home() -> Path:
    """The root the agent directories hang off — `$HOME`, or the test override.

    Read at CALL time, never cached at import: the suite re-points it between fixtures, and a module
    constant would defend the first one for the life of the process."""
    override = os.environ.get("PLAINKEEP_AGENT_HOME", "").strip()
    if override:
        return Path(os.path.abspath(os.path.expanduser(override)))
    return Path(os.path.expanduser("~"))


def source_dir() -> Path:
    """The manual's directory, named through `current` so the link survives an update.

    `paths.SKILLS` resolves THROUGH `current` to the active version (`…/engine/4.0.5-dev/skills`),
    which is right for reading a file now and wrong for a link that outlives the invocation — it
    would keep naming the old pair after the next activation and become a broken link the moment
    that version is pruned. This is `stable_launcher`'s argument, applied to the sibling directory."""
    return enginetree.stable_launcher().parent / "skills" / SKILL_NAME


def link_path(target: Target) -> Path:
    return agent_home() / target.rel / SKILL_NAME


def installed(target: Target) -> bool:
    """Is any tool that reads this directory on the machine?

    A CONFIG dir existing is the signal, never the skills dir — a freshly installed agent has the
    former and not yet the latter, and that is precisely the machine that needs the manual. Any one
    marker is enough: `~/.agents/skills` is shared, so one of its readers being present is reason
    enough to create it."""
    home = agent_home()
    return any((home / m).is_dir() for m in target.markers)


def detected() -> list[Target]:
    return [t for t in TARGETS if installed(t)]


def state(target: Target) -> str:
    """`linked` (ours, pointing at the manual) · `stale` (our link, wrong target) ·
    `foreign` (a real file or directory we must not touch) · `absent`."""
    p = link_path(target)
    if p.is_symlink():
        try:
            return "linked" if os.path.realpath(p) == os.path.realpath(source_dir()) else "stale"
        except OSError:
            return "stale"
    if p.exists():
        return "foreign"
    return "absent"


def link(target: Target) -> str:
    """Point this agent's skills directory at the manual. Returns the action taken.

    Idempotent, and the ONLY destructive step is `unlink()` of a symlink this module's own naming
    scheme produced — see the module docstring on why a real directory is reported rather than
    replaced. The delete is pinned in `test/run_pathwall.py` with that bound written out."""
    src, dst = source_dir(), link_path(target)
    st = state(target)
    if st == "linked":
        return f"already linked: {dst}"
    if st == "foreign":
        raise FileExistsError(
            f"{dst} is a real file or directory, not a link this created — move it aside first")
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.is_symlink():          # `stale`: our name, pointing at a pruned or previous engine
        dst.unlink()
    os.symlink(src, dst)
    return f"linked {dst} -> {src}"


def rows() -> list[dict]:
    """One row per DETECTED agent, in `TARGETS` order. An agent that is not installed is not a gap,
    so it never appears — a machine with only Claude Code is fully ready with one link."""
    return [{"id": t.rel, "title": f"{t.agent} — {t.rel}/{SKILL_NAME}", "agent": t.agent,
             "state": state(t), "ok": state(t) == "linked"} for t in detected()]
