---
title: Personal Operating System — Unified Design (v3.7)
status: design-v3.7 (v3.6 + gbrain-informed: compiled-truth/timeline, brain-first, iron-law, auto-backlinks, routing-eval, dream-lite)
owner: Tamas
updated: 2026-06-17
supersedes: [PERSONAL_OS_DESIGN.md (design-v3.6), OPS_CORE_DESIGN.md (design-v2), Agent-Agnostic Local-First Personal Operating System (v1.0)]
tags: [meta, plainkeep, architecture, agent-agnostic, local-first]
---

# Personal Operating System — Unified Design (v3.7)

> **v3.7 changelog (gbrain-informed).** After studying `garrytan/gbrain` (a Postgres-native
> "compiled intelligence" knowledge runtime), six ideas were transposed onto this plaintext +
> git + shell system — kept only where they fit the first principles, never importing gbrain's
> runtime weight (no Postgres, no embeddings stack, no agent daemon). Added: **compiled-truth /
> timeline two-zone notes** (§7.2, §10.1), **brain-first lookup** as a hard rule (§1, §12.3),
> the **Iron Law — the model picks WHAT, the system guarantees WHERE/HOW** (§1, §5, §12.3),
> **zero-LLM auto-backlinks on write** (§10), **routing-eval fixtures** as the checkable form of
> the agnosticism test (§11, §12.4), and a **`consolidate` dream-lite nightly job** (§15). What
> was deliberately *rejected* from gbrain is recorded in Appendix A. gbrain's own cautionary
> tale — a 147k-token agent context file, "the exact anti-pattern it exists to fix" — is why
> these are minimal additions, not a rewrite.

> **What this is.** The single, merged design for a local-first, agent-agnostic personal
> operating system. It takes the **topology** of the v1.0 document (system / code /
> assets / machine roots) and the **mechanism** of the v2 document (one verb-based command
> surface, a generated capability manifest, guardrails enforced by the system itself,
> one-point agent indirection) — and deletes everything that doesn't earn its complexity.
>
> **The contract in one paragraph.** Files are truth. Git is the spine. One command
> surface (`plainkeep <verb>`) is the only way anything *gets done*, identical for you in a
> terminal, for a cron job, and for any agent. One skill file teaches any agent to drive
> it. Code projects live as independent git repos outside the system repo. Dotfiles
> rebuild the machine. Indexes are disposable accelerators. Swap the agent any time;
> the system remains.

---

## 1. First principles

When anything is ambiguous, re-derive the answer from these. They are ordered.

1. **Plaintext is forever.** Markdown + YAML frontmatter outlives every app, diffs in
   git, and is the native read/write format of both humans and LLMs.
2. **Git is the spine.** Versioning, undo, audit, backup, and "new Mac" all collapse to
   git operations. Anything that writes, writes inside a repo.
3. **The agent is interchangeable labor.** The system is the folders, the commands, and
   the skill file. Claude Code today, anything else tomorrow. If every AI vanished, the
   system still works by hand.
4. **One command surface for everyone.** Human, script, scheduler, and agent all run the
   *same* `plainkeep <verb>` lines. No private agent API. This is the consistency guarantee.
5. **Safety lives in the system, not in prompts.** Guardrails are enforced by the
   dispatcher per-verb, so they hold no matter what is driving.
6. **Reversibility over cleverness.** One SQLite file over a server. Wikilinks over a
   graph DB. Shell scripts over a framework. Choices you can walk away from.
7. **Don't automate chaos.** Manual → checklist → SOP → script → verb → scheduled.
   Nothing skips steps. Complexity must be earned by repeated friction.
8. **Low memory burden.** You memorize ~10 verbs. Everything else is discoverable via
   `plainkeep help` (and, for agents, `plainkeep.json`). If you need a cheat sheet, the design failed.
9. **The system answers before the world.** (brain-first.) Any driver checks `~/plainkeep` —
   `plainkeep search` first — before reaching for an external API, the web, or its own memory.
   The system is the cheapest, fastest, most personal source you have; external lookups
   only fill gaps it can't. An agent that web-searches before searching the system is
   giving a worse, more expensive answer from stale context.
10. **The model picks WHAT; the system guarantees WHERE and HOW.** (The Iron Law.) The
   agent supplies content and judgment — *which* note, *what* summary. The system, in code,
   supplies structure — the slug, the path, the `[[wikilinks]]`, the backlinks, the
   `source:` citation. Never let the model compose a path or a link by hand; derive them
   deterministically from a verb. This makes whole classes of misfiling and broken-link
   drift structurally impossible, not merely discouraged.

**The agnosticism contract.** Any agent needs exactly three things: (a) the path
`~/plainkeep`, (b) the skill file `~/plainkeep/skills/operate-plainkeep/SKILL.md`, (c) a shell. Everything
it can do is discoverable from `plainkeep.json`. Swapping agents = point the new one at the
folder and the skill file. Nothing else changes.

---

## 2. Topology — four roots (the key structural decision)

This is taken from the v1.0 document because it cleanly solves the problem the v2
document glossed over: **your code projects are their own git repos and must never live
inside the system repo.**

```
~/plainkeep          # THE SYSTEM. Knowledge, tasks, journal, verbs, skills, templates, jobs.
               # Private remote. Never contains code repos. ONE git repo at small scale;
               # ONE git repo even at 100k+ notes: keep it fast with notes/<aa>/ subdirectory
               # fanout + git large-repo tuning; binaries already live in ~/files (ADR-006, §10.2).

~/work         # YOUR CODE. A plain directory tree (NOT a git repo itself). Every
               # project inside is its own independent git repo with its own remote.
               # The plainkeep wiki holds *notes about* these projects and knows where
               # their remotes are, so they can be re-cloned in bulk.

~/files        # THE ASSETS. Binary/heavy material: client-provided docs, contracts,
               # generated PDFs and deliverables, research papers, exports, recordings.
               # NOT a git repo — backed up encrypted via restic (§9). Mirrors the
               # same client/project slugs as ~/work and the wiki.

~/dotfiles     # THE MACHINE. chezmoi repo: shell, Brewfile, tool configs, agent CLI
               # configs, PATH setup. One command rebuilds any Mac.
```

**The one sorting rule** (this replaces all judgment calls about where a thing goes):
**if it's plaintext it lives in a git repo; if it's binary it lives in `~/files`.**
Markdown, code, configs, templates → `~/plainkeep` or `~/work`. PDFs, images, design files,
recordings, archives, datasets → `~/files`, with a markdown note in the wiki linking to
it. `~/plainkeep` stays plaintext-only by policy (a doctor check flags tracked binaries).

How the three relate:

- `~/plainkeep` *references* `~/work` (project notes carry `repo:` and `remote:` frontmatter)
  but never *contains* it. No submodules, no nested repos, no `.gitignore` gymnastics.
- `~/files` mirrors the same slugs (`~/files/clients/acme/acme-webapp/…`) so the wiki,
  the repo, and the assets of one project always share one name. Wiki notes link into
  it; it never links back. Git never sees it.
- `~/dotfiles` installs the tools and puts `~/plainkeep/bin` on PATH; it owns zero business
  knowledge.
- Restore order on a fresh machine: dotfiles → plainkeep → work (bulk re-clone from the
  project registry) → files (restic restore). Full procedure in §14.

Hard boundaries:

- **iCloud is walled off.** `~/plainkeep`, `~/work`, `~/files`, `~/dotfiles` are never inside iCloud
  Drive (iCloud sync corrupts `.git`). iCloud holds family/photos/personal only and is
  off every command path — enforced as a `deny` guardrail, not a convention.
- **Secrets never touch any of the roots.** References only
  (`op://Private/acme-vercel-token/credential`), resolved at runtime via 1Password CLI.

Naming: lowercase (`~/plainkeep`, `~/work`, `~/dotfiles`) — terminal-friendly, and Finder
doesn't care. No Johnny.Decimal number prefixes (cut from v2): numeric prefixes buy
Finder sort order but break links when renumbered, and neither `plainkeep search` nor an agent
needs them. Use Finder sidebar favorites instead (§13).

---

## 3. The `~/plainkeep` repo layout

Merged: v2's lean core + v1.0's task system, SOPs/runbooks, and jobs registry — with
v1.0's parallel folders for vectors/graph/five-agent-prompts removed (they were
scaffolding for complexity not yet earned).

```
plainkeep/
├── README.md                  # the map, for humans: what this is, quick commands, recovery
├── AGENTS.md                  # the short contract for ANY agent (entry point; ~1 page; full text §12.2)
│                              #   read natively by Codex, Hermes, OpenClaw
├── CLAUDE.md                  # one-line bridge: `@AGENTS.md` — because Claude Code reads only this (§12.5)
├── GEMINI.md                  # optional: Antigravity/Gemini global-context bridge (also → AGENTS.md) (§12.6)
├── .claude/                   # Claude Code (+ Grok) adapter: settings.json + skills→../skills symlink
├── .codex/                    # Codex adapter: config.toml (sandbox+approval) + skills→../skills symlink (#22869)
├── .agents/skills → ../skills # cross-tool skills path: Antigravity, pi, open standard (symlink, §12.6)
├── .factory/skills → ../skills# Factory Droid skills path (symlink, §12.6)
│                              #   opencode/Cursor/Hermes/OpenClaw reach ../skills via config — no symlink
├── plainkeep                        # the dispatcher script (`plainkeep <verb>`); symlinked onto PATH
├── plainkeep.json                   # GENERATED capability manifest (committed; rebuilt by `plainkeep index --manifest`)
├── .gitignore
│
├── bin/                       # THE VERBS — one folder per verb + metadata sidecar
│   ├── lib/                   # shared helpers: guardrail.sh, agent.sh, log.sh, manifest.sh
│   └── <verb>/                # e.g. capture/, search/, task/, start/, close/ ...
│       ├── run.sh             # the implementation (bash or python; see script rules §16)
│       └── cmd.json           # self-description: usage, args, risk, reads/writes
│
├── skills/                    # CANONICAL skills (SKILL.md standard). All TEN supported agents discover
│   │                          #   THIS one dir via their own path — symlinks (.claude/.codex/.agents/
│   │                          #   .factory) or config (opencode/Cursor/Hermes/OpenClaw). One source of truth (§12.5–12.6).
│   ├── operate-plainkeep/SKILL.md   # ← THE system skill: drives everything (full text §12.3)
│   ├── invoice-hu/SKILL.md    # draft Hungarian invoice + accountant packet
│   ├── triage/SKILL.md        # how to file inbox items
│   └── weekly-review/SKILL.md
│
├── wiki/                      # durable knowledge (the "LLM wiki")
│   ├── index.md               # map of content: entry points by area
│   ├── conventions.md         # naming, frontmatter, linking rules (normative)
│   ├── decisions.md           # append-only ADR log: why things are the way they are
│   ├── clients/               # one note per client relationship
│   ├── projects/              # one note per project — ABOUT the project; code is in ~/work
│   ├── areas/                 # ongoing responsibilities, no end date
│   │   └── business-admin/    # tax-formula.md (átalányadó gross-up), invoicing, NAV, bookkeeper
│   ├── people/
│   ├── tools/                 # evaluated tools, configs, gotchas
│   ├── notes/                 # atomic knowledge notes — one idea per note, linked
│   ├── runbooks/              # how to operate/recover a SPECIFIC system
│   └── archive/               # done/inactive notes (move, don't delete)
│
├── tasks/                     # folder = status; file = task record
│   ├── inbox/                 # captured, not yet triaged
│   ├── active/                # being worked
│   ├── waiting/               # blocked on someone/something, or awaiting your approval
│   └── done/                  # completed (swept to done/<year>/ yearly)
│
├── journal/                   # the shared activity record — every driver appends here
│   └── 2026/06/2026-06-10.md
│
├── inbox/                     # raw capture zone: dumped text, dropped files; `plainkeep triage` empties it
│
├── templates/                 # task.md, client.md, project.md, daily.md, weekly.md,
│                              # runbook.md, skill/SKILL.md, project-repo/ (for ~/work, §8)
│
├── jobs/
│   ├── registry.yaml          # scheduler-neutral job definitions
│   └── launchd/               # rendered .plist files (generated; macOS adapter only)
│
├── .index/                    # search indices, GITIGNORED, rebuildable from markdown:
│                              #   plainkeep.sqlite (FTS5 keyword + wikilink/edge graph) +
│                              #   vectors.lance/ (LanceDB ANN, stage-2, PLAINKEEP_VECTORS=1; ADR-006)
└── .logs/                     # append-only run logs per verb/job. GITIGNORED
#   .gitignore also excludes agent-written cruft: skills/**/.system/, .claude/**/cache, etc.
#   The adapter files themselves (CLAUDE.md, .claude/settings.json, .codex/config.toml, the
#   two skills symlinks) ARE committed — they restore with the repo, no extra setup on a new Mac.
```

What got cut from v1.0 and why:

| Cut | Why |
|---|---|
| `bin/plainkeep-task`, `plainkeep-wiki`, … (8 sub-binaries) | One dispatcher + verb folders does the same with one pattern to learn |
| `agent/prompts/` (5 role prompts) | One system skill file; roles are an agent-side concern, not the system's |
| 10 pre-made agent skill folders | Skills are created when a workflow has actually repeated (§1.7); start with 4 |
| `policy.yaml` | Duplicate source of truth; risk lives in each verb's `cmd.json` (§6) |
| `indexes/{vectors,graph,snapshots}` upfront | Scaffolding for unearned complexity; FTS5 first, vectors when needed (§10) |
| cron + systemd adapters | This is a Mac; launchd only, `plainkeep job run` as the universal fallback |
| `failed/`, `blocked/`, `waiting-approval/` task statuses | Collapsed into `waiting/` + a `why:` line; fewer states = less bookkeeping |
| `scratch/` in the repo | Use `~/work/sandbox/` or `/tmp`; the system repo stays signal-only |
| Separate `sop/` vs `agent/skills/` | Merged: a skill IS an SOP written to be executable by human or agent (§11) |

What got kept from v1.0 that v2 lacked: the three-root topology (§2), the task system
(§7), project repo standards + worktrees (§8), `doctor`/`status`/restore-check (§14),
the jobs registry (§15), runbooks as a distinct type, and the weekly review loop.

---

## 4. The command surface — `plainkeep <verb>`

Taken from v2 verbatim in mechanism; verb set merged from both documents and capped.
**Everything the system can do is a verb.** Same line whether you type it, launchd fires
it, or an agent runs it.

### 4.1 The verb set (v1 — complete, ~16 verbs)

```
SYSTEM
  plainkeep help [verb]           # list verbs / usage for one (rendered from plainkeep.json)
  plainkeep status                # repo states, active/waiting tasks, last close/backup/index
  plainkeep doctor                # self-check: tools, PATH, folders, templates, index, remotes
  plainkeep backup                # verify plainkeep+dotfiles+work repos committed/pushed; warn loudly
  plainkeep index                 # rebuild/refresh search index (+ regenerate plainkeep.json manifest)
  plainkeep consolidate           # dream-lite: refresh backlinks, flag stale/orphans, journal digest (§15)

FLOW
  plainkeep capture "<text>"      # thought → inbox (also: pipe stdin, or drop a file in inbox/)
  plainkeep triage                # PROPOSE filing of inbox items into tasks/wiki; human approves
  plainkeep start                 # daily start: today's journal note, active tasks, carry-forward
  plainkeep close                 # daily close: summarize day into journal, flag uncommitted work
  plainkeep week                  # weekly review: shipped/stalled, repo health, SOP candidates

KNOWLEDGE
  plainkeep search "<query>"      # hybrid local search → ranked file#heading hits
  plainkeep wiki <action>         # open <slug> | new <type> <name> | stale | orphans

TASKS
  plainkeep task <action>         # list | add "<title>" | show <id> | move <id> <status> | done <id>

WORK
  plainkeep new <type> "<name>"   # scaffold: project | client  (wiki note + templates; project
                            #   additionally scaffolds the repo in ~/work from template)
  plainkeep repo <action>         # health (all ~/work repos: dirty/unpushed/stale) | clone <project> | clone --all
                            #   | adopt <path> --kind <tools|labs|clients|products> (move an ALREADY-cloned
                            #     repo into the routing-tree destination + write its wiki note/registry; §12.3 #4a)
                            #   | nuke-modules --stale <days> (delete node_modules untouched N+ days; §15 job)
  plainkeep archive <project>     # strip artifacts, git-bundle a dead repo into ~/work/archive/<year>/
  plainkeep files <action>        # open <slug> (reveal in Finder) | ingest (route inbox files into ~/files + shadow notes)
  plainkeep sweep                 # manually trigger the Desktop/Downloads decay sweep (also a nightly job)

BUSINESS
  plainkeep invoice <client>      # DRAFT invoice + accountant packet (reads tax-formula.md; never sends)

JOBS
  plainkeep job <action>          # list | run <name> | apply   (render+load launchd plists)
```

Design rules for the surface:

- **Flat verbs, shallow subactions.** A verb may take one subaction word
  (`plainkeep task add …`) but never nests deeper. Ten memorable verbs for daily life
  (`capture`, `search`, `start`, `close`, `task`, `triage`, `status`, `new`, `invoice`,
  `backup`); the rest are discoverable.
- **A new verb costs one folder.** Drop `bin/<verb>/{run.sh,cmd.json}`, run
  `plainkeep index --manifest`, done — instantly visible to `plainkeep help`, every agent, and the
  guardrail layer. Nothing else to wire.
- **No verb sprawl.** Quarterly review prunes verbs unused for 90 days (the `.logs/`
  make this measurable). Target: stay under ~20 verbs indefinitely.

### 4.2 The dispatcher

A ~30-line script. Reads the verb, checks the guardrail, execs the verb's `run.sh`.

```sh
#!/usr/bin/env bash
# plainkeep — the single entrypoint. ~/plainkeep/bin on PATH via dotfiles.
set -euo pipefail
PLAINKEEP="${PLAINKEEP_HOME:-$HOME/plainkeep}"
VERB="${1:-help}"; shift || true
[ "$VERB" = "help" ] && exec "$PLAINKEEP/bin/lib/help.sh" "$@"
CMD="$PLAINKEEP/bin/$VERB/run.sh"
[ -x "$CMD" ] || { echo "unknown verb: $VERB (try: plainkeep help)" >&2; exit 1; }
"$PLAINKEEP/bin/lib/guardrail.sh" "$VERB" "$@"      # enforce risk class (§6)
"$PLAINKEEP/bin/lib/log.sh" "$VERB" "$@"            # append invocation to .logs/
exec "$CMD" "$@"
```

### 4.3 Self-describing verbs → the manifest

Every verb carries a `cmd.json` sidecar; `bin/lib/manifest.sh` concatenates them into
`plainkeep.json`. That one generated file is the contract between the system and any driver:
`plainkeep help` renders from it, agents learn the full surface from it, the scheduler reads
`risk` from it. **Agents never hardcode the command set and never invent verbs.**

```jsonc
// bin/invoice/cmd.json
{
  "verb": "invoice",
  "summary": "Draft a Hungarian invoice (átalányadó) + accountant packet for a client",
  "usage": "plainkeep invoice <client> [period]",
  "args": [
    { "name": "client", "required": true },
    { "name": "period", "required": false, "default": "previous month" }
  ],
  "risk": "draft_only",
  "reads": ["wiki/clients/", "wiki/areas/business-admin/tax-formula.md"],
  "writes": ["wiki/areas/business-admin/invoices/<year>/"],
  "agent": "optional"        // pure-shell fallback exists; agent improves the draft text
}
```

---

## 5. Guardrails — safety enforced by the system

Taken from v2 unchanged (it is the single best idea in either document) and made the
*only* policy mechanism: v1.0's separate `policy.yaml` is deleted — **risk lives in each
verb's `cmd.json`, enforced by `bin/lib/guardrail.sh` before any verb runs.** One source
of truth; holds identically for you at 2am, a launchd job, and any agent.

| Risk class | Meaning | Example verbs | Dispatcher behavior |
|---|---|---|---|
| `read` | no writes anywhere | `search`, `status`, `help`, `repo health` | run freely; log |
| `safe_write` | writes only inside `~/plainkeep`, reversible via git | `capture`, `start`, `close`, `task`, `new`, `index` | run; every change is a revertible git diff |
| `draft_only` | produces files/drafts; never transmits | `invoice` | run; a human sends |
| `confirm` | irreversible or external side effects | `git push`, deploy, send email, spend money, delete | refuse without an explicit human `--yes` |
| `deny` | never | paths outside the three roots, iCloud/family tree, secret values, `rm -rf`, `--force` push | refuse + log + notify |

Enforcement principles:

- **Git is the undo button.** Verbs write inside repos; mistakes are diffs.
- **The wall is by path, not by trust.** Guardrail resolves every target path; anything
  escaping `~/plainkeep` / registered `~/work` repos / `~/files` — or entering the
  iCloud/family tree — hits `deny` regardless of who asked. Within `~/files`, the
  `in/` folders (client-provided originals) are APPEND-ONLY for every verb: an original may
  ARRIVE by ATOMIC CREATION (`files ingest` — evidence has to get in somehow), and one already
  there is never overwritten, replaced, mutated or deleted. The create-only guarantee is the
  filesystem's — `link(2)` / `O_CREAT|O_EXCL`, which fail EEXIST — never an `exists()` test the
  verb ran first, because that is a window rather than a guard (Phase 2 Task 1c).
- **The sweep zone is the one sanctioned write area outside the roots.** `plainkeep sweep`
  (§9.4) operates on the macOS inboxes `~/Desktop` and `~/Downloads` in **move-only** mode —
  it relocates stale items *within* those dirs into `_swept/`, never into the three roots, and
  never deletes on the 7-day pass (trashing is the separate 60-day pass). This is `safe_write`,
  not a wall breach: it only shuffles already-non-secret user files in place and writes nothing
  into `~/plainkeep`/`~/work`/`~/files`. No other verb may write outside the roots. (Found by the
  `test/` jobs suite: without this carve-out the path wall would `deny` the sweep job itself.)
- **Nothing transmits without a human `--yes` — except the pre-authorized backup.** Email,
  push, deploy, payment: the verb produces a draft and stops (`confirm`). The lone exception is
  a scheduled `restic backup` to the **pre-configured** encrypted bucket (key in 1Password): it
  makes no *per-run* external decision and sends nothing you chose, so it is `read`-class, not
  `confirm`. A transmit is `confirm` precisely when a human is choosing *what* leaves and *to
  whom*; an unattended dedup-backup to a fixed destination is not that. (Also surfaced by the
  jobs suite: `files_backup` was flagged `read`-but-transmits until this distinction was made explicit.)
- **Worktrees are sanctioned; "invent a verb" means the `plainkeep` surface** (reliability, v3.7).
  `~/work/.worktrees/<project>-<task-id>` is a first-class `safe_write` zone — it is a disposable,
  gitignored checkout of the task's repo and the *required* place for agent code work (§8.4), so
  the wall allows it for the current task. And the "never invent a verb" rule (§4, §12.3) governs
  the **`plainkeep` command surface only**: raw shell tools the design already endorses — `git`,
  `script/*`, `rg`, `$EDITOR` (§13) — are not "invented verbs." They are bounded by the path wall
  (their writes still get classified), the transmit/secret sniffers, and each agent's adapter
  tool-scoping (§12.5) — not by the verb allowlist. (Both clarified after the `test/`
  task-lifecycle simulation flagged legitimate `git worktree`/`script/test` use as violations.)
- **Resolve before you wall** (reliability, v3.7). The guardrail resolves every target to its
  real path (`realpath`, following symlinks) AND matches **case-insensitively** before deciding —
  because macOS filesystems are case-insensitive (`IN/` ≡ `in/`) and a symlink inside `~/plainkeep`
  could otherwise point at iCloud or at a `~/files/**/in/` original. The wall is on where a write
  *lands*, not on the string the agent typed. (Three escapes — symlink-to-iCloud, symlink-to-originals,
  uppercase `IN/` — were found by the `test/` adversarial suite and closed here.)
- **Transmit means *any* tool, not just git** (reliability, v3.7). The `confirm` wall on external
  side effects fires for `git push`, `vercel/netlify/fly deploy`, `npm/yarn publish`, `aws s3` /
  `gsutil`, `scp` / `rsync` to a remote, `gh pr merge` / `gh api -X POST`, and `curl/wget` POST/PUT
  — anything that sends data off the machine or spends money. Recognizing only `git push` was a
  hole (an agent could exfiltrate via `curl` or deploy via `vercel` unchecked); the dispatcher
  classifies by *effect*, sniffing the command, and forced variants (`--force`, `rm -rf`/`-fr`/`-r -f`)
  are `deny`, not `confirm`.
- **Everything logs; failure is loud.** Append-only `.logs/`, non-zero exits, macOS
  notification on failure.
- **New verbs default to `confirm`** until deliberately classified lower. Safe by default.
- **The Iron Law is structural, not advisory** (principle 10). Paths, slugs, `[[wikilinks]]`,
  backlinks, and `source:` citations are generated *by the verb*, never accepted as free text
  from the agent. A verb that files a note computes its destination from the routing rules
  (§4a / the MAP) and rejects an agent-supplied absolute path the same way it rejects any
  out-of-root path. The agent proposes the *content*; the code owns *placement and linkage*.
  This is what makes filing testable: given the same inputs, the destination is deterministic.

Net effect: an agent can be given *broad* freedom to operate, because the things that
could actually hurt are structurally impossible without you. Freedom for the agent,
control at the boundaries.

---

## 6. Agent indirection — the one swappable point

From v2, unchanged. Exactly **one** place in the entire system names an agent:

```sh
# bin/lib/agent.sh — the ONLY file that references any agent.
#   export PLAINKEEP_AGENT="claude"     # set in dotfiles; or "codex", "opencode", "ollama", ...
run_agent() {
  local prompt="$1"; local tools="${2:-read-only}"   # caller declares the scope it needs
  case "${PLAINKEEP_AGENT:-claude}" in
    claude)   claude -p "$prompt" --output-format text \
                --allowedTools "$([ "$tools" = read-only ] && echo Read,Bash || echo Read,Write,Bash)" ;;
    codex)    codex exec "$prompt" ;;             # scope per each agent's own flags
    opencode) opencode run "$prompt" ;;
    ollama)   ollama run "${PLAINKEEP_MODEL:-llama3.1}" "$prompt" ;;
    none)     return 2 ;;                         # explicit no-agent mode
    *)        echo "set PLAINKEEP_AGENT" >&2; return 1 ;;
  esac
}
```

Two distinct ways agents and the system meet — keep them straight:

1. **Verbs that call an agent** (`triage`, `close` summaries, `invoice` prose): the
   *script* calls `run_agent` internally. The caller never knows. Every such verb has a
   pure-shell fallback (e.g. `plainkeep close` without an agent assembles the day's git log +
   completed tasks into the journal template, unsummarized). The system works with
   `PLAINKEEP_AGENT=none`.
2. **Agents that call verbs** (interactive Claude Code session, future supervisor): the
   agent reads the skill file and drives `plainkeep <verb>` like any human. This is the main
   driving plane you described — the gardener/maintainer — and it needs *zero* code in
   the system. A Telegram supervisor, when you build it, is just another caller of the
   same surface (per v2 Appendix C: deliberately out of scope here).

Two different agent axes, kept separate: this section (§6) is the *runtime* swap — which
agent a verb shells out to. §12.5 is the *onboarding* config — how each real agent
(Codex, Claude Code, Hermes, OpenClaw) is wired to read the entry point. Setting
`PLAINKEEP_AGENT=codex` and configuring Codex's adapter are independent steps.

---

## 7. The task system

From v1.0, slimmed. Tasks are markdown files; **the folder is the status** — visible in
Finder, moved with `mv` or `plainkeep task move`, no database required.

### 7.1 Statuses (four, not seven)

```
tasks/inbox/      captured, not yet looked at
tasks/active/     being worked (keep under ~7 — if more, you're hiding a backlog)
tasks/waiting/    blocked on someone/something OR awaiting your approval (why: in frontmatter)
tasks/done/       finished; swept into done/<year>/ by `plainkeep week`
```

v1.0's `blocked`, `failed`, and `waiting-approval` are all `waiting/` with a `why:` —
three folders fewer to glance at, zero information lost. A failed attempt is a `done`
task with `outcome: failed` and recovery notes (the record matters more than the folder).

### 7.2 Task record

ID `T-YYYYMMDD-NN`, file `tasks/<status>/T-20260610-01.md`:

```markdown
---
type: task
id: T-20260610-01
status: active            # mirrors the folder; folder wins on conflict
created: 2026-06-10
updated: 2026-06-10
source: capture           # capture | manual | agent | job
risk: green               # green | yellow | red — red requires your eyes before work starts
client: acme              # optional
project: acme-webapp      # optional → links to wiki/projects/acme-webapp.md
why:                      # required when status: waiting
---

# Fix Acme staging webhook timeout

## Intent
## Plan
## Outcome                <!-- COMPILED TRUTH: current best state, REWRITTEN as it changes -->
## Log                    <!-- TIMELINE: append-only; every driver adds commands run, files changed -->
```

**Two zones, one discipline (from gbrain's "compiled truth + timeline").** A record has a
*compiled* top — `Intent`/`Plan`/`Outcome` are the current synthesis, **rewritten in place**
when the situation changes — and an *append-only* bottom (`## Log`) that is the immutable
evidence trail. You never edit the Log; you rewrite the Outcome. The point: the top stays
short and current (read it to know where things stand), while the bottom never loses history
(read it to know how you got there). Every claim in the compiled top should trace to a Log
line below. This is the same shape entity hubs use (§10.1), so one habit covers tasks and wiki.

### 7.3 Task rules

- Every multi-step piece of work gets a task file; the task file is where any driver
  (you, an agent, a job) logs what it did. Single-step trivia doesn't (journal line only).
- Agents must update the task's `## Log` as they work and write `## Outcome` before
  declaring done — this is in the skill file, so it holds across agents.
- `plainkeep start` lists `active/` + `waiting/`; `plainkeep close` flags active tasks with no log
  entry today; `plainkeep week` sweeps `done/`.

---

## 8. `~/work` and project repo standards

From v1.0, kept almost whole — this is what makes any repo instantly operable by you
*and* any agent.

### 8.1 Layout

```
work/
├── clients/<client>/<project>/      # paid work; each project = its own git repo
│   └── designatives/<project>/      # your main relationship is just a client folder
├── products/                        # your own products you ship and maintain
├── labs/                            # experiments with a future
├── tools/                           # forked repos + self-hosted apps you actually RUN
├── sandbox/                         # cloned repos to test, throwaways (NOT backed up)
├── archive/<year>/                  # dead repos, git-bundled (see plainkeep archive, §8.5)
└── .worktrees/                      # agent/risky-work worktrees (gitignored everywhere)
```

**Where does a new repo go? Four questions, top to bottom, first hit wins:**
1. Am I getting paid for it? → `clients/<client>/` (or `clients/designatives/`)
2. Is it mine and I intend to ship/keep it? → `products/`
3. Do I just *run* it (a fork I maintain, a self-hosted app)? → `tools/`
4. Am I only trying something? → `labs/` (or `sandbox/` if truly throwaway)

`plainkeep new project "<name>"` asks (or takes a `--kind`) and files accordingly. This is the
one decision the routing rule removes from your day.

`~/work` itself is **not** a git repo. Each project repo has its own remote. The link
back to the system: every project has a note `~/plainkeep/wiki/projects/<slug>.md` whose
frontmatter carries `repo:` (local path) and `remote:` (clone URL). That registry is what
makes `plainkeep repo health` and `plainkeep repo clone --all` possible — and it is how the whole of
`~/work` is restored on a new machine without ever nesting repos (§14). `tools/` and
`labs/` repos opt in by adding the same `remote:` frontmatter; throwaways in `sandbox/`
intentionally don't, so they're never backed up or re-cloned.

### 8.2 Every serious project repo contains

```
README.md          # what, why, how to run
AGENTS.md          # the agent contract for THIS repo (template below)
CLAUDE.md          # → symlink to AGENTS.md (Claude Code reads CLAUDE.md; others read AGENTS.md;
                   #   one file of truth, two names)
script/            # scripts-to-rule-them-all: setup, dev, test, lint, typecheck, build, deploy, doctor
.env.example       # every needed var, no values
mise.toml          # pinned tool versions
docs/decisions.md  # repo-local ADR log (architecture decisions stay with the code)
```

`plainkeep new project "<name>"` scaffolds all of this from `~/plainkeep/templates/project-repo/`,
initializes git, and creates the wiki note — so the standard costs nothing to follow.

### 8.3 Project `AGENTS.md` (template)

```markdown
# Agent instructions — <project>

## Commands (always use these; never invent)
setup `script/setup` · dev `script/dev` · test `script/test` · lint `script/lint` · build `script/build`

## Rules
- Work on a branch or worktree; never on main directly.
- Do not read `.env`. Do not push, deploy, or touch prod without explicit approval.
- Prefer small diffs. Behavior change ⇒ test change. Architecture change ⇒ docs/decisions.md entry.
- Before finishing: run the smallest meaningful check; for broad changes run lint+typecheck+test.

## Report when done
branch · files changed · commands run · verification results · risks · next action
```

### 8.4 Worktree policy (agent coding)

Agent-driven or risky code work happens in a worktree, never the main checkout:

```
git worktree add ~/work/.worktrees/<project>-<task-id> -b agent/<task-id>-short-title
```

Main stays clean; a bad attempt is `git worktree remove` away; the branch name carries
the task ID so every change traces to a task record.

### 8.5 Archiving dead repos — `plainkeep archive <project>`

A repo that's done shouldn't rot in `labs/` taking disk and attention, but deleting it
loses history. `plainkeep archive`: strips build artifacts (`node_modules`, `dist`, …),
`git bundle`s the whole repo into `~/work/archive/<year>/<project>.bundle` (a single
file containing all history), updates the project's wiki note to `status: archived` with
the bundle path, then removes the working tree. Reviving it is
`git clone <bundle> <path>`. This is the *decay* story for code: dead projects leave on
their own command, history intact, one file, restic-backed like the rest of `~/files`
if you point the bundle there — or kept in `~/work/archive/` and caught by Time Machine
(§14). `plainkeep week` surfaces `labs/` repos untouched for 90 days as archive candidates.

---

## 9. Files & assets — `~/files`, the binary plane

The plaintext system above runs your *knowledge*; this plane stores the *stuff* —
everything a developer / web-design / AI-consulting / teaching practice accumulates that
git handles badly: client briefs and asset dumps, signed contracts, generated PDFs and
invoices, design exports, slide decks, research papers, recordings, datasets. Same
principles apply: plain folders, Finder-browsable, one naming convention, rebuildable
accelerators, agent-operable through the same surface.

### 9.1 Layout — mirrors the slugs you already have

```
files/
├── clients/<client>/<project>/      # acme/acme-webapp — IDENTICAL slug to wiki + ~/work
│   ├── in/                          # client-provided originals — APPEND-ONLY (arrive, never change)
│   ├── out/                         # what you delivered: final PDFs, exports, packages
│   ├── work/                        # working files: drafts, design sources, comps
│   └── research/                    # gathered material for this project
├── products/<product>/              # course recordings, rendered decks, lesson exports
│                                    #   (course *source* — markdown, slides-as-code — is a
│                                    #    git repo in ~/work/products/; only renders live here)
├── areas/
│   └── business-admin/<year>/       # signed contracts, NAV documents, accountant packets
├── research/                        # cross-project: papers, reports, reference PDFs
└── archive/                         # closed clients/projects, moved whole
```

Three structural rules, then no further rules:

1. **`in/` is append-only.** Client originals are evidence — never edited, never renamed,
   never replaced. An original ARRIVES (`files ingest`) and after that it is fixed. Need to
   change one? Copy to `work/`. (Enforced by guardrail §5 + `lib/vaultio.move_create_only`,
   whose EEXIST is the filesystem's answer rather than the verb's.)
2. **`out/` is what left the building.** If a client has it, a copy is here. Your
   delivery history is a folder listing.
3. **Filenames carry their date:** `2026-06-10--acme-homepage-v2.pdf`. Sortable in
   Finder, citable in notes, no "final-final-v3" archaeology.
4. **File by what it IS, not what it's FOR.** A signed NDA goes under
   `areas/business-admin/`, not under the client it concerns; a research PDF goes in
   `research/`, not in each project that cites it (the shadow note does the linking). One
   physical home per file; links create the "for" relationships.

### 9.2 The shadow-note convention — how binaries become searchable

`plainkeep search` indexes plaintext; binaries are invisible to it — unless they have a
**shadow note**: a small markdown note in the wiki that links to the asset and carries
the extracted essence.

```markdown
---
type: document
title: Acme webapp brief v2
file: ~/files/clients/acme/acme-webapp/in/2026-06-08--brief-v2.pdf
created: 2026-06-10
tags: [acme, brief]
---
# Acme webapp brief v2
Summary: …          ← agent-extracted, 5–10 lines
Key requirements: … ← the parts you'll actually search for
```

This is the agent-processing answer in one move: **agents don't trawl `~/files`; they
read shadow notes.** The pipeline is `plainkeep files ingest` — for each new file in `inbox/`
(or pointed at directly), it: files the asset into the right `~/files` path (proposing,
like triage) → extracts text (`pdftotext` etc.) → `run_agent` writes the summary →
creates the shadow note → the next `plainkeep index` makes it findable. No agent configured?
The verb still files the asset and stubs the note; you fill in one line by hand. Only
documents worth finding later get shadow notes — a 40-file asset dump gets one note for
the dump, not forty.

Generated assets flow the other way: a verb or agent produces a PDF (invoice, proposal,
report) → writes it to the project's `out/` → appends the link to the project's wiki
note and the journal. Drafting stays `draft_only`; sending stays human.

### 9.3 Backup, security, remote access

`~/files` is not in git and not in iCloud. It gets the boring, proven answer:

- **Backup: restic → a cloud bucket** (Backblaze B2 or any S3). Encrypted client-side
  by restic, deduplicated, snapshotted, point-in-time restorable. One job in the
  registry (§15) runs `restic backup ~/files` nightly; `plainkeep backup` checks snapshot
  freshness and yells if the last one is >48h old; `plainkeep doctor` verifies the repo is
  reachable. Restore on a new machine: `restic restore latest --target ~/files`.
- **Security.** Client confidentiality is the same guardrail story: no verb transmits
  anything without a human `--yes`, agents under the skill file's hard rules never
  upload or externally share file contents, and the off-site copy is encrypted with a
  key that lives in 1Password — not in any repo.
- **Remote access: Tailscale to your Mac.** SSH in, run `plainkeep` verbs, browse `~/files`
  remotely — zero exposed ports, nothing new to design, and any future always-on
  supervisor uses the same path. (For pure file emergencies, restic can restore any
  single file from the bucket to wherever you are.)

What deliberately does NOT get this treatment: your photography library (already its own
managed world — keep it there; a wiki note can link to it) and `~/work/sandbox/`
(throwaway by definition).

### 9.4 The macOS inboxes & automatic decay

There are three "dump now, sort later" zones, at two levels — keep them straight:

| Zone | Level | Holds | Promoted by | Decays? |
|---|---|---|---|---|
| `~/Desktop`, `~/Downloads` | macOS (can't move them) | downloads, screenshots, AirDrops, received files | `plainkeep files ingest` / `plainkeep capture` | **yes — auto-swept** |
| `~/plainkeep/inbox/` | system | text/notes destined for the knowledge base | `plainkeep triage` | no (you empty it) |

You can't stop downloads and screenshots landing on Desktop/Downloads, so don't fight it
— **let them be the macOS inboxes and give them automatic decay** so they self-clean:

```
~/Desktop/   ─7 days untouched→  ~/Desktop/_swept/YYYY-MM/  ─60 more days→  Trash
~/Downloads/ ─ same timers ───→  (same _swept safety net) ───────────────→ Trash
```

A nightly `sweep` job (§15) moves anything untouched for 7 days into `_swept/YYYY-MM/`
(not deleted — a dated holding pen) and trashes `_swept/` items older than 60 days. So
you have a week to `plainkeep files ingest` what matters into `~/files`, then a 60-day net
before anything is actually gone. Promotion is deliberate; decay is automatic; nothing
important is lost because ingesting *is* the act of saying "this matters." This is the
hygiene layer the rest of the design assumed but never specified — the machine stays
clean without you tidying it.

**Rescue is by ingest, not by reopening** (reliability, v3.7). Once a file is in `_swept/`,
merely *touching* it (opening/previewing) does **not** reset the 60-day trash timer — only
`plainkeep files ingest` pulls it back out of the decay path. This keeps the timer predictable (a
re-touch can't silently keep junk alive forever), but it means a file you reopen from `_swept`
and forget to ingest will still be trashed on schedule. The 60-day window and the `_swept`
folder being in plain sight are the safety net. (Surfaced by `test/run_sweep.py`, which models
this explicitly so it's a deliberate choice, not an accident.)

**The iCloud line, made operational.** A real ingest run finds two kinds of file: work
material (a client brief, a generated invoice) and irreplaceable personal/family/legal
docs (a tax return, a signed master contract, a baby medical record). The wall (§5) says
no verb writes into iCloud — so ingest does NOT split-brain into managing iCloud. Instead
it **auto-files work material into `~/files`** (`safe_write`) and, for anything it
classifies as personal/legal/family, **only proposes the iCloud destination and stops** —
e.g. *"this looks like a NAV tax document → move to iCloud Személyes/pénzügyek/adó/ (do
this yourself)"*. You make the one move. This keeps the single cleanest safety property
intact (agents never touch irreplaceable originals) while still getting the classification
help — it's the same "propose, human executes" contract as `triage`, applied at the wall.

---

## 10. Knowledge & search

### 10.1 The wiki substrate

Plain Markdown + YAML frontmatter + `[[wikilinks]]`. Obsidian-*compatible*, never
Obsidian-*dependent* — Obsidian (or anything else) is a disposable viewer.

Conventions (normative copy lives in `wiki/conventions.md`):

- Frontmatter on every note: `type`, `title`, `status`, `created`, `updated`, `tags`,
  `aliases`. Types: `client | project | area | person | tool | note | runbook | skill |
  decision | meeting | research | prediction`.
- One idea per knowledge note (`wiki/notes/`); link generously; filenames are stable,
  human-readable slugs (`stripe-webhook-retries.md`). **Slugs are globally unique** across all
  folders — because `[[bare-slug]]` links resolve by basename, two notes sharing a basename
  (`clients/acme` and `notes/acme`) make every `[[acme]]` ambiguous. `plainkeep doctor`/`consolidate`
  flag slug collisions; `plainkeep new`/`plainkeep wiki rename` refuse to create one. Link syntax allows
  `[[slug#heading]]` and `[[slug|Display text]]` — the resolver strips the `#`/`|` part.
  Renames go through `plainkeep wiki rename` (later) so backlinks update. (Collision + alias handling
  verified by `test/run_wiki_edges.py`.)
- **Entity notes (client/project) are *hubs* with two zones** (from gbrain): a *compiled
  truth* top — short, current, rewritten as facts change — over a `## Timeline` section that
  is append-only evidence. The hub IS the context: read the top to know the current state,
  read the timeline to know how it got there. Hubs are heavily linked, never essays.
- **Backlinks are generated, never hand-typed** (the Iron Law, principle 10). When any verb
  writes a note, it extracts `[[wikilinks]]` with a pure regex — *zero LLM calls* — and
  updates the backlink index. The wikilink graph grows for free on every write; `rg
  '\[\[slug\]\]'` is the ground-truth backlink query and `plainkeep wiki backlinks` wraps it.
  (gbrain's benchmark showed typed-graph traversal is its single biggest retrieval lift —
  which is why backlinks are not a "later" nicety here; see §10.2.)
- **A `prediction` note** records a belief you can later be graded on: a claim, a confidence
  (0–1), a resolution date, and (once known) the outcome. Cheap to write, plaintext, and it
  unlocks a future "calibration" review — *how often were you right?* — with no database
  (gbrain's takes-grading idea, reduced to one note type). Optional; write them when a real
  call is being made.
- No secrets, ever. References only.
- Archive by moving to `wiki/archive/`; never delete knowledge.

**The system learns from your corrections (cheaply).** When you override a `triage` or
`files ingest` proposal — "no, Designatives invoices go *here*" — the verb offers to
append a one-line rule to `wiki/conventions.md` (a `## Filing rules` section). Every
classifying verb reads that file into its prompt, so next time it files that case
correctly. This is the plaintext, local-first version of a "learning agent": no model
fine-tuning, no `.json` rules engine, no per-agent memory — just an append to a markdown
file that any agent reads. The corrections compound; the file stays human-editable and
git-versioned; and because it's the *same* conventions file humans read, there's one
source of truth, not a hidden agent brain. Start with zero rules; let friction write them.

**The graph question, settled (from v2), sharpened by gbrain:** no graph *database* — but
treat the wikilink graph as a first-class retrieval signal, not an afterthought. gbrain's
public benchmark showed typed-link graph traversal was its single biggest retrieval lift
(P@5 ~18 → ~49), far more than vectors. The lesson for a plaintext system: the cheap win is
already in hand. Wikilinks form a traversable graph (`rg '\[\[slug\]\]'` *is* the query,
wrapped as `plainkeep wiki backlinks`), generated for free on every write (§10.1). So **stage 1
includes backlink traversal, and stage 2 (vectors) is deferred further** — chase link
coverage before embeddings. Vector+keyword hybrid still covers most retrieval; a graph DB
adds extraction cost and a second source of truth that goes stale. Because everything is
plaintext-with-explicit-links, adding the **LanceDB** vector layer (ADR-006) is additive, never a migration.

### 10.2 Search — scale-ready from day one (target: 100k–500k+ notes), each stage rebuildable

**Scale target (ADR-006).** This system is built for **hundreds of thousands of notes** — single
machine, **no server**, because the embedded-ANN era makes that possible. The stages below are a
*bring-up path on one scale-ready architecture*, not "add it if you ever need it": each stage is
built small but on the components that hold at 1M+ chunks, so there is **never a re-platforming**.

| Stage | What | Role |
|---|---|---|
| 0 | `rg` / `fd` / `fzf` raw | bootstrap; always works |
| 1 | **SQLite FTS5** (keyword) + **wikilink/edge graph**, chunked by heading, incremental by file hash → `plainkeep search`. FTS5 scales to millions of rows. | the lexical + graph spine |
| 2 | **LanceDB** vectors (embedded, file-based, disk **IVF-PQ ANN**, larger-than-RAM, billions-scale) + local **EmbeddingGemma** embeddings (ADR-005), fused with FTS5+graph via RRF | semantic, **scale-grade from the start** — flat index small, IVF-PQ as it grows, no migration |
| 3 | local **cross-encoder rerank** (`bge-reranker-v2-m3` via Ollama/llama.cpp) over the fused candidates | precision layer (gbrain's zerank role, run locally) |

**NOT `sqlite-vec` for vectors** — it is brute-force and fails past ~1M vectors; at our target that's
a non-starter, so vectors live in **LanceDB** from day one (still embedded/file-based/no-server).
SQLite stays the keyword/metadata/graph engine. **Rejected outright (servers / 2nd source of truth):**
Postgres+pgvector, FalkorDB, LightRAG, Qdrant-as-service, RDF — see §10.2.1.

**Storage at scale.** Indices live under `.index/` — `.index/plainkeep.sqlite` (FTS5 + graph) and
`.index/vectors.lance/` (LanceDB) — all gitignored and **rebuildable from markdown**
(`rm -rf .index && plainkeep index`; an index that can't be rebuilt from files is a bug). The markdown
**system of record stays ONE git repo, even at 100k+ notes — "git is the spine" holds, unbent.**
A single repo of *plaintext* is fine at this scale; what keeps it fast is (1) **subdirectory fanout**
(`notes/<aa>/<slug>.md`) so no folder holds 100k entries, and (2) **git large-repo tuning**
(`feature.manyFiles`, `core.fsmonitor`, `core.untrackedCache`, `commit-graph`) — `git status` stays
near-constant-time regardless of file count (Microsoft runs the 3.5M-file Windows repo on git).
The thing that actually chokes git is **binary size**, not text file count — and the
plaintext→git / binary→`~/files` rule (§2) already keeps binaries out of the repo (this is exactly
what gbrain hit: its "2.3GB wiki" was ~300KB/file of embedded media, not 7k text files). **Multiple
repos are an optional, much-later lever** (millions of files, or separating a noisy auto-ingest
feed's cadence, or per-machine selective sync) — never a day-one requirement, and truth never moves
into a DB. Initial indexing of a large vault is a **resumable, checkpointed, batched** backfill
(incremental by file-hash thereafter). `plainkeep index` also regenerates `plainkeep.json` (the manifest).

#### 10.2.1 The retrieval add-on test — embedded scales, servers don't (ADR-006)

The foundation is **Karpathy's LLM Wiki** pattern (compile knowledge into interlinked markdown,
maintain index files + backlinks, let the agent navigate the filesystem) — which is what this design
already is (§3, §10.1). At **gbrain scale (100k–500k+ notes)** that foundation is *kept*, and the
vector + graph + rerank layers are built **from day one** on top of it, on components that hold at
1M+ chunks. The point is no longer "add vectors if earned" — it's "scale the right way."

**The one test for any retrieval component** (a corollary of principle 6): *is it embedded —
file-based, locally-computed, rebuilt-from-plaintext, no daemon — or is it a server / a second
source of truth?* Embedded is allowed (and now scales to billions of vectors); a server is rejected.

- **Allowed — embedded, scale-grade:** SQLite **FTS5** (keyword/graph, millions of rows) +
  **LanceDB** (vectors; disk IVF-PQ ANN, larger-than-RAM, <20ms @1M, billions single-node) + local
  **EmbeddingGemma** embeddings + a local cross-encoder reranker. All file-based under `.index/`,
  gitignored, rebuilt from markdown — no server, no second source of truth. This is how we hit
  gbrain *capability* without gbrain's Postgres: the embedded-ANN era (LanceDB) is what newly makes
  100k+ notes searchable on one laptop. **Measured** (`test/run_search.py`, real local embeddings):
  on queries whose wording shares no vocabulary with the target note, keyword+graph scores 0.00
  recall@5 and local vectors recover **1.00** — vectors recover exactly what keyword structurally
  cannot. (`sqlite-vec` is excluded *for vectors* — brute-force, dies past ~1M; LanceDB replaces it.)
- **Rejected — the server tier:** gbrain's Postgres + pgvector; FalkorDB / LightRAG graph+vector;
  Qdrant/Weaviate-as-service; RDF/semantic-web layers. Each is a *server* or an *LLM-extracted second
  graph that goes stale* — both violate principle 6. We scale by **embedded** ANN, not by standing up
  a database service. The graph is the `[[wikilink]]`/edge table (recursive-CTE multi-hop), not a
  graph-DB daemon.

**Empirical, even at scale** (principle 7). The stages bring up on the same architecture; the query
log (`.logs/queries.jsonl`) + `test/run_search.py` keep tuning honest (which mode, which model, where
rerank helps). And the storage spine does **not** bend: one git repo of plaintext, kept fast with
`notes/<aa>/` subdirectory fanout + git large-repo tuning, carries 100k–500k notes — because the
plaintext→git / binary→`~/files` rule already keeps the size-and-binary cause of git slowness out of
the repo. Sharding into multiple repos is an optional much-later lever, not a requirement.

**Measured on a fair vault (2026-06-19).** A real plainkeep-shaped vault (58 notes, 13 area hubs, 435
wikilinks, built from an LLM/agents KB; `vault/`) was queried with 25 realistic queries (11
exact-term, 14 natural-language) via real local embeddings. Exact-term queries: keyword+graph and
vectors agree (keyword suffices). Natural-language queries: ~8 clear vector wins, and on 5 of them
**keyword+graph missed the right note entirely even at rank 3** while vectors got it at #1 (~32%
clear wins, 40% divergence). So for **conceptual/natural-language** retrieval, stage-2 vectors are
*earned*; for **entity/proper-noun** lookups, keyword+graph already suffices. Conclusion: build
stage 1 now, keep query-logging on (`.logs/queries.jsonl`), and bring up stage 2 (**LanceDB ANN** +
local Ollama embeddings) on the scale architecture — the natural-language share already justifies it
and only grows with the corpus. (See `DECISIONS.md` ADR-002/006 and `test/vault_queries.txt`.)

**Engine split (ADR-003 + ADR-006).** *Keyword / metadata / graph* → plain **SQLite FTS5** (already
present via Python `sqlite3` stdlib + CLI, public domain, scales to millions of rows) — chosen over
Turso's libSQL fork because libSQL's reason-for-being (embedded replicas / sync / cloud) is the
server gravity this design walls off; libSQL stays a file-compatible fallback only. *Vectors* →
**LanceDB**, NOT `sqlite-vec`: sqlite-vec is brute-force and fails past ~1M vectors, which our
100k–500k-note target blows through; LanceDB is embedded, file-based, and disk-ANN (IVF-PQ) to
billions on one node — scale without a server. Both stores live under `.index/`, are gitignored, and
rebuild from the markdown. Net: two embedded engines, zero servers, principle 6 intact at scale.

---

## 11. Skills = SOPs (merged concept)

v1.0 kept SOPs (human procedure) and agent skills (agent procedure) as separate trees —
that's two documents to keep in sync, describing one workflow. Merged: **a skill is an
SOP written so that either a human or an agent can execute it.** One folder per skill,
`skills/<name>/SKILL.md`, frontmatter `name` + `description` (when to use it), body with
trigger, preconditions, steps, done-criteria, failure modes, and which `plainkeep` verbs /
scripts it uses. Runbooks (recover a *specific system*) stay in `wiki/runbooks/` — they
are knowledge, not capability.

The lifecycle (principle 7): a workflow earns a skill only after it has repeated, and
earns a verb only after the skill is stable. `plainkeep week` asks: *what did I do by hand
twice this week?* — those are the skill/verb candidates. Start with exactly four skills:
`operate-plainkeep`, `triage`, `invoice-hu`, `weekly-review`.

**"Stable" is checkable, not vibes (from gbrain's routing-evals).** A skill is stable when
its routing is *tested*. Each skill ships a `skills/<name>/routing-eval.jsonl` — ≥5 lines of
`{"intent": "<paraphrased user phrasing>", "expected_skill": "<name>"}`, including a few
**adversarial** cases that look like a neighbor skill but must NOT match it
(`{"intent": "...", "expected_skill": "<other>", "ambiguous_with": ["<this-skill>"]}`). A
tiny checker (`plainkeep skill check`, later) confirms every intent routes to the expected skill
and that no two skills claim the same trigger (MECE). This is the cheap, plaintext form of
gbrain's eval gate — no judge models, just substring/description matching — and it is the
concrete pass/fail the agnosticism test (§12.4) runs.

The skill set is tiered: `operate-plainkeep` is the **always-load** system manual (§12);
the others are **load-on-demand** recipes an agent reads only when the task matches their
`description`. This keeps the agent's working context small — it carries the system
manual always, and pulls a specific recipe only when invoicing, triaging, etc.

---

## 12. The agent entry point — onboarding chain, AGENTS.md, and the system skill

This is the most load-bearing part of the design: it is where any agent learns what the
system is and, just as important, what it must **not** do. An underspecified entry point
is exactly how an agent misfiles a cloned tool into `projects/`, writes to iCloud, or
invents a workflow that bypasses a guardrail. So this section is deliberately exhaustive.

### 12.1 The onboarding chain (which document teaches what)

An agent meets the system through a fixed, layered chain. Each layer has one job; none
duplicates another.

| Order | Document | Role | Stability |
|---|---|---|---|
| 1 | `~/plainkeep/AGENTS.md` | The **contract**: what this is, the absolute rules, where to go next. Read first, every time. ~1 page. | Rarely changes |
| 2 | `~/plainkeep/skills/operate-plainkeep/SKILL.md` | The **operating manual**: the map, the routing/filing decisions, how to traverse the wiki, every workflow. The driving license. | Changes as the system grows |
| 3 | `~/plainkeep/plainkeep.json` | The **capability truth**: every verb, its args, what it reads/writes, its risk. Generated. The agent never hardcodes verbs; it reads this. | Auto-generated |
| 4 | `~/plainkeep/wiki/conventions.md` | The **normative conventions**: frontmatter, naming, linking — plus the `## Filing rules` the system learns over time (§10). | Grows with corrections |
| 5 | `<repo>/AGENTS.md` (in a `~/work` repo) | The **repo contract**: this project's commands and rules. Read on entering any repo. | Per-repo |

The rule the agent internalizes: **AGENTS.md says what you may do; the skill file says
how; plainkeep.json says with which verbs; conventions.md says in what shape; the repo's
AGENTS.md governs once you're inside it.** Nothing is improvised around this chain.

### 12.2 `~/plainkeep/AGENTS.md` (full text — the contract)

```markdown
# AGENTS.md — operating contract for ~/plainkeep

You are operating Tamas's personal operating system. Read this file first, then read
`skills/operate-plainkeep/SKILL.md` before doing anything. This file is the contract; that
file is the manual.

## What this system is
A local, file-first, git-versioned system with FOUR roots:
- `~/plainkeep`     — THE SYSTEM (this repo): knowledge (wiki), tasks, journal, the `plainkeep` verbs, skills.
- `~/work`    — CODE: every project is its own git repo. NOT a repo itself.
- `~/files`   — BINARY ASSETS: client docs, deliverables, research PDFs. NOT in git.
- `~/dotfiles`— THE MACHINE: configs. You inspect, you don't change without being asked.

You do everything through ONE command surface: `plainkeep <verb>`. The authoritative list of
verbs is `plainkeep.json` (run `plainkeep help`). NEVER invent a verb or work around the surface.

## Absolute rules (the guardrails enforce these; violating them is a system failure)
1. Operate ONLY inside `~/plainkeep`, `~/files`, and the ONE `~/work` repo your task concerns.
2. NEVER touch iCloud or any family/personal path. It is walled off by location. If a
   file belongs there (tax, legal, medical, family), you may only PROPOSE the move and
   tell the human to do it — you never write there.
3. NEVER transmit anything externally — no email, push, deploy, post, or payment. You
   produce DRAFTS. The human sends. Verbs that could transmit refuse without a human `--yes`.
4. NEVER read `.env` files or print secret values. Secret references (`op://…`) may be
   named, never resolved.
5. `~/files/**/in/` (client originals) is APPEND-ONLY. A new original may ARRIVE there (that is what
   `plainkeep files ingest` is for); one already there is never edited, renamed, replaced or
   deleted. To change one, copy it to `work/`.
6. Everything is in git — safe edits are revertible, so prefer doing the safe write over
   asking. But STOP and ask for anything classified `confirm`, and STOP and report on any
   failure or ambiguity. Never guess.

## Where to go next
- How to do anything → `skills/operate-plainkeep/SKILL.md`
- What you can do → `plainkeep help` / `plainkeep.json`
- The shape of notes → `wiki/conventions.md`
- Inside a `~/work` repo → that repo's `AGENTS.md` (and use its `script/*`)
```

### 12.3 `~/plainkeep/skills/operate-plainkeep/SKILL.md` (full text — the manual)

This is the driving license. It is normative and complete; if a behavior isn't covered
here, the agent treats that as a gap to report, not a license to improvise.

```markdown
---
name: operate-plainkeep
description: >
  How to operate Tamas's personal operating system (~/plainkeep), code workspace (~/work),
  and asset store (~/files). Read this whenever asked to capture, search, organize,
  triage, manage tasks, file documents, scaffold or archive projects/clients, draft
  invoices, review the day/week, or do coding work in any ~/work repo. This is the
  full operating manual; pair it with plainkeep.json (the verb list) and wiki/conventions.md.
---

# Operating the system

## 1. What this is, in one breath
`~/plainkeep` (git) is the single source of truth for knowledge and work records. `~/work`
holds code (each project its own repo). `~/files` holds binaries (find them via their
shadow notes, never by trawling). You act ONLY through `plainkeep <verb>` and by reading and
writing plaintext files. You add no capabilities of your own.

## 2. The one rule about capabilities
`plainkeep.json` (or `plainkeep help`) is the authoritative, complete list of what you can do.
NEVER invent a verb. If a task needs something not in the manifest, say so and propose
adding a verb — do not script around the surface, do not reach for raw tools to do what a
verb already does.

## 3. Orientation — run this at the start of every session
1. Read today's and yesterday's `journal/YYYY/MM/*.md` to see what already happened.
2. Run `plainkeep status` (active/waiting tasks, last close/backup/index).
3. If your work is multi-step, open or create its task: `plainkeep task add "<title>"`.
Do not start acting until you've oriented; the journal exists so you never repeat or
contradict the last session.

## 4. THE MAP — where every kind of thing lives (consult before placing ANYTHING)
This table is the anti-misfiling rule. When you are about to put something somewhere,
find its row first. When in doubt, capture to `inbox/` and let `triage` decide — never
guess a permanent home.

| You have… | It goes… | Via |
|---|---|---|
| A passing thought / note (text) | `~/plainkeep/inbox/` then triaged to wiki or a task | `plainkeep capture "<text>"` |
| Durable knowledge (something learned, a decision, a how-to) | `~/plainkeep/wiki/` (notes/ for atomic ideas; the entity hub for client/project facts) | edit the note; `plainkeep wiki new` for a new one |
| Multi-step work to track | `~/plainkeep/tasks/<status>/` | `plainkeep task add` |
| A binary doc you RECEIVED (brief, contract draft, asset, research PDF) | `~/files/<area>/…/in/` or `research/`, + a shadow note in the wiki | `plainkeep files ingest` |
| A binary you PRODUCED (invoice, export, report) | the project's `~/files/.../out/`, linked from its wiki note | the producing verb writes it there |
| A personal/legal/family doc (tax, medical, ID, signed master) | iCloud — **you do NOT file this; you PROPOSE the destination and stop** | report the suggested path |
| **A code repo** | see the routing tree below — this is where misfiling happens | `plainkeep new project` / by hand |

### 4a. Repo routing tree (the cloned-tool-in-projects trap)
A directory with code is NOT automatically a "project." Decide with these questions, top
to bottom, FIRST hit wins — never skip to a lower one:

1. Am I (Tamas) getting PAID to build/maintain it? → `~/work/clients/<client>/` (his main
   client is `clients/designatives/`).
2. Is it HIS OWN product he intends to ship and keep? → `~/work/products/`.
3. Is it something he only RUNS — a fork he maintains, a self-hosted app, someone else's
   tool he cloned to use? → `~/work/tools/`.  ← cloned tools land HERE, never in clients/products.
4. Is it an EXPERIMENT with a possible future? → `~/work/labs/`.
5. Is it a throwaway clone just to look at / test? → `~/work/sandbox/` (never backed up).

If you cannot answer #1–#5 with confidence, STOP and ask. Misfiling a repo is worse than
asking, because it pollutes the registry that drives backup and restore.

Once the destination is decided, **place the repo with a verb, never by hand** (the Iron Law).
For an already-cloned repo sitting outside `~/work`, that verb is `plainkeep repo adopt <path>
--kind <tools|labs|clients|products>`: it moves the clone to the routing-tree destination,
computes the final path and slug, and writes the wiki note + `repo:`/`remote:` registry
frontmatter. (`plainkeep new project` scaffolds a *new blank* repo; `plainkeep repo clone` pulls from a
*registered remote*; `adopt` is the missing third case — an existing local clone.) Do NOT `mv`
the directory yourself: a hand-composed `~/work/...` write hits the path wall, and an
unregistered repo is invisible to `plainkeep repo health` / `clone --all` and so silently drops out
of backup and restore. If no verb yet covers the placement you need, STOP and propose adding
one — never work around the surface.
<!-- v3.7: the `adopt` verb + this note close a gap the agnosticism simulation found
     (test/ cloned-tool-trap): two agents agreed WHERE a cloned tool goes but diverged on HOW
     to place it — one hand-moved it (path-wall violation), one asked for a flag. -->


## 5. How to traverse and use the wiki (the LLM knowledge core)
The wiki is Markdown + YAML frontmatter + `[[wikilinks]]`. Treat it like a graph of
hubs, not a pile of files. Rules of traversal:

- **Search first; never trawl.** To find anything, run `plainkeep search "<query>"` → it
  returns ranked `file#heading` hits. Open the file and read the relevant heading. Do not
  `cat` your way through folders; the index exists precisely so you don't.
- **Read frontmatter before body.** `type`, `status`, `related`, and (for projects)
  `repo:`/`remote:` tell you what a note is and where it connects before you read a word
  of prose.
- **Entity notes are HUBS.** `wiki/clients/<c>.md` and `wiki/projects/<p>.md` are short,
  current, heavily linked summaries — the entry point to everything about that thing. To
  understand a project: open its hub, read its frontmatter and summary, then follow its
  `[[wikilinks]]` outward to the specific notes. Don't reconstruct context from scratch;
  the hub IS the context.
- **Atomic notes hold one idea.** `wiki/notes/<slug>.md` is a single concept, linked from
  the hubs that need it. When you learn something durable, write it as one atomic note and
  link it from the relevant hub — don't bloat the hub.
- **From wiki to code:** a project hub's `repo:` field is the path in `~/work`. That's how
  you cross from knowledge to the actual codebase.
- **From wiki to assets:** a shadow note's `file:` field is the path in `~/files`. Search
  finds the note (it's text); the note points you to the binary. You never search binaries.
- **Backlinks / relationships:** `plainkeep wiki backlinks <slug>` (what links here);
  `plainkeep wiki stale` (notes untouched too long), `plainkeep wiki orphans` (unlinked notes). Use
  these when asked to assess or tidy the knowledge base.
- **Stay scoped.** Pinpoint with search, read the hub plus its immediate neighbors, act.
  Reading the entire wiki to answer one question is a failure mode, not thoroughness.

## 6. How to DO things (the full surface — grouped)
Discover the live, authoritative set with `plainkeep help`; this is the working summary.

- Capture / find:    `plainkeep capture "<text>"` · `plainkeep search "<query>"`
- Daily / weekly:    `plainkeep start` · `plainkeep close` · `plainkeep week`
- Tasks:             `plainkeep task list|add "<title>"|show <id>|move <id> <status>|done <id>`
- Knowledge:         `plainkeep wiki open <slug>|new <type> <name>|backlinks <slug>|stale|orphans`
- Filing:            `plainkeep triage` (text → tasks/wiki) · `plainkeep files ingest` (binaries → ~/files + shadow note)
- Work scaffolding:  `plainkeep new project|client "<name>"` (asks/uses the routing tree above)
- Repo lifecycle:    `plainkeep repo health|clone <p>|clone --all` · `plainkeep archive <project>` (dead repo → bundle)
- Business:          `plainkeep invoice <client>` (DRAFT only; reads tax-formula.md; never sends)
- System:            `plainkeep status` · `plainkeep doctor` · `plainkeep backup` · `plainkeep index` · `plainkeep job …` · `plainkeep sweep`

`triage`, `files ingest`, and `invoice` PROPOSE; the human approves. `archive`, `sweep`,
and anything mutating support `--dry-run` — use it when unsure what a verb will do.

## 7. Working inside a ~/work repo
- FIRST read that repo's `AGENTS.md` and use its `script/*` commands; never invent build
  steps a `script/` already provides.
- All code changes happen on a worktree, never on main:
  `git worktree add ~/work/.worktrees/<project>-<task-id> -b agent/<task-id>-<slug>`
- Log progress in the task file's `## Log`; write `## Outcome` before declaring done.
- Report when finished: branch · files changed · commands run · verification results ·
  risks · next action.

## 8. The journal is the shared memory
After any meaningful action, append ONE line to today's `journal/YYYY/MM/YYYY-MM-DD.md`:
what you did, when, the result. This is how the human and the next agent/session know what
happened. It is not optional — an action no one can see in the journal effectively didn't
happen for the next operator.

## 9. Conventions, and how the system learns from you
- Every note: YAML frontmatter (`type, title, status, created, updated, tags, aliases`).
  Bump `updated:` on every edit. Link generously with `[[wikilinks]]`.
- Full rules live in `wiki/conventions.md`. Read its `## Filing rules` section before
  triaging/ingesting — it contains the human's accumulated corrections.
- When the human OVERRIDES one of your filing/triage proposals, offer to append the new
  rule as one line under `## Filing rules`. That is how the system gets smarter; do it,
  and you (and the next agent) will file that case correctly next time.

## 10. Hard rules (the guardrails enforce these; respecting them keeps your freedom broad)
- Operate ONLY inside `~/plainkeep`, `~/files`, and the ONE `~/work` repo of the current task.
  NEVER touch iCloud or family/personal paths.
- iCloud-bound documents (tax, legal, medical, ID, family, signed masters): PROPOSE the
  destination, never write there yourself.
- `~/files/**/in/` is APPEND-ONLY (client originals are evidence): an original may arrive,
  none already there may be changed or removed. Copy to `work/` to change one.
- Deliverables you produce go to the project's `~/files/.../out/` and get linked from the
  project hub and the journal.
- NEVER transmit externally (email, push, deploy, post, payment). Drafts only; human sends.
- NEVER read `.env` or print secrets. References (`op://…`) may be named, never resolved.
- NEVER hardcode tax rates — read `wiki/areas/business-admin/tax-formula.md`. Currency HUF.
- BRAIN-FIRST: search the system (`plainkeep search`) before any web/external lookup or relying on
  your own memory. The system is the cheapest, most current, most personal source; external
  calls only fill gaps it genuinely lacks. State when you fell back to an external source.
- DON'T hand-compose paths, slugs, or `[[wikilinks]]` (the Iron Law). You supply content and
  judgment; the verb computes the destination and the links. If no verb covers a placement,
  STOP and propose one — never write to an absolute path you composed yourself.
- Prefer the safe, revertible edit over asking; but STOP and ask for any `confirm` action,
  and STOP and report on any failure, missing file, or ambiguous convention. Never guess.
```

### 12.4 The agnosticism check, and how task-skills nest

Validate the entry point the way the design intends (from v2): hand `AGENTS.md` +
`operate-plainkeep/SKILL.md` to **two different agents** and confirm they file the same repo to
the same place, traverse the wiki the same way, and refuse the same actions. Divergence
means the manual is ambiguous *there* — fix the manual, not the agent. That test passing
is what "agent-agnostic" means in practice.

**This check is now mechanized** (see `test/` — the design's own simulation harness). The
deterministic side is a guardrail model exercised by adversarial path/risk cases (does the
path-wall actually deny every escape?); the probabilistic side plugs a real LLM in as the
*operator*, feeds it the contract + manual + a simulated world + a scenario, and a judge
scores whether it filed/refused/proposed correctly — run across two models to surface
divergence. Skills' `routing-eval.jsonl` fixtures (§11) are the routing slice of the same
harness. The rule holds: a failure is a defect in the *manual or the guardrail spec*, fixed
there, not papered over in a prompt.

Task-specific skills (`triage`, `invoice-hu`, `weekly-review`, and any you grow later) do
NOT repeat this material. Each is a thin recipe: frontmatter `description` (when to load
it), then trigger → preconditions → steps → done-criteria → failure modes, naming the
exact `plainkeep` verbs it uses. The agent loads one only when its `description` matches the
task, so the always-on context stays just AGENTS.md + operate-plainkeep. When you add a verb,
update `operate-plainkeep/SKILL.md` §6 (the surface summary) and, if it changes how things are
filed or traversed, §4–§5 — those two sections are what keep the agent from drifting.

### 12.5 Wiring real agents to the entry point (Codex · Claude Code · Hermes · OpenClaw)

The agnosticism contract (§1) promises any agent drives the system through the same two
files. In practice the four agents you'll actually use **disagree on the filename of the
contract and on where skills live**, so two of them silently fail to load the entry point
if you do nothing. The fix keeps the design's spirit exactly: **one source of truth
(`AGENTS.md` + `skills/operate-plainkeep/SKILL.md`), plus a thin per-agent adapter that points
each agent at it.** No content is duplicated — adapters are two symlinks, a one-line
bridge file, and a few config lines, all committed to the plainkeep repo so they restore for
free. This is the §6 "adapters, not forks" principle applied to onboarding.

**What each agent actually does (from the research), and the adapter it needs:**

| Agent | Reads the contract as | Finds the skill via | Lock shell to `plainkeep` | Adapter (committed in `~/plainkeep`) |
|---|---|---|---|---|
| **Codex CLI** | `AGENTS.md` — **native** (run from `~/plainkeep` or `--cd ~/plainkeep`) | can't add skill dirs (#22869) → **symlink** | `config.toml`: `sandbox_mode="workspace-write"`, `approval_policy="on-request"` | `.codex/config.toml` + `.codex/skills → ../skills` |
| **Claude Code** | **`CLAUDE.md` only — NOT `AGENTS.md`** | `.claude/skills/` only | `settings.json`: `permissions.allow:["Bash(plainkeep:*)"]`, deny rest | `CLAUDE.md` (bridge) + `.claude/settings.json` + `.claude/skills → ../skills` |
| **Hermes** | `AGENTS.md` — native, **but only if the gateway's cwd is `~/plainkeep`** | configurable external skill dir | `approvals.mode: smart`; pre-approve `plainkeep` | none in-repo; configured in `~/.hermes/` via `~/dotfiles` |
| **OpenClaw** | `AGENTS.md` — native (workspace = `~/plainkeep`) | `<workspace>/skills/` — **native** | **`exec` is YOLO by default** → must set allowlist | none in-repo; `~/.openclaw/` + `exec-approvals.json` via `~/dotfiles` |

**The canonical-source rule.** `~/plainkeep/skills/` is the only real copy of every skill.
Codex and Claude reach it by a committed symlink (`.codex/skills` and `.claude/skills`
both → `../skills`); Hermes is configured with `~/plainkeep/skills` as an external skill dir;
OpenClaw finds it natively because its workspace *is* `~/plainkeep`. Four discovery paths, one
directory. (Gitignore `skills/**/.system/` — Claude Code writes internal files into its
skills dir; harmless once ignored.)

**The two in-repo adapter files, in full.** They are tiny by design:

```markdown
<!-- ~/plainkeep/CLAUDE.md  — the entire file. Claude Code won't read AGENTS.md, so bridge it. -->
@AGENTS.md

Before any plainkeep action, read and follow `skills/operate-plainkeep/SKILL.md`.
Operate only through the `plainkeep <verb>` surface it defines. Never invent a verb.
```

```toml
# ~/plainkeep/.codex/config.toml  — adapter, not a second source of truth
sandbox_mode   = "workspace-write"   # writes confined to ~/plainkeep; ~/files & repos added per task
approval_policy = "on-request"       # ask before anything outside the sandbox
# [profiles.plainkeep] may layer model/approval choices; the AGENTS.md it reads is ~/plainkeep/AGENTS.md
```

`.claude/settings.json` carries only `permissions` (allow `Bash(plainkeep:*)` and the repo's
own `script/*`; deny `Read(.env*)`, `Bash(git push:*)`, etc.) — the guardrail surface in
Claude's own vocabulary. Hermes and OpenClaw configs live in their home dirs, which are
machine config, so they belong in **`~/dotfiles`** (chezmoi), not the plainkeep repo: for
Hermes, set the gateway working directory to `~/plainkeep` (`terminal.cwd`/`MESSAGING_CWD`) and
add `~/plainkeep/skills` as an external skill dir; for OpenClaw, set
`agents.defaults.workspace: "~/plainkeep"` and **tighten `exec`** (`tools.exec.security:
allowlist` + an `exec-approvals.json` permitting only the `plainkeep` binary) — its ungated
default is the single biggest risk for an always-on Telegram agent.

**One content rule that matters for all four.** Every agent pre-loads only each skill's
*name + description* at session start and reads the body on demand. So the contract file
must explicitly say *"load `operate-plainkeep` before any plainkeep action"* (the §12.2 AGENTS.md and
the CLAUDE.md bridge both do), and the skill's `description` (§12.3) stays keyword-rich so
implicit matching also fires. Without that line, the agent has the index but never opens
the manual.

**`plainkeep doctor` checks the wiring.** Doctor verifies the adapters resolve: `CLAUDE.md`
exists and imports `AGENTS.md`; `.codex/skills` and `.claude/skills` symlinks point at
`skills/`; `.codex/config.toml` and `.claude/settings.json` parse. A broken adapter is
how an agent silently reverts to improvising — so it's a first-class health check, not an
afterthought.

**What each agent cannot do (design hedges, not assumptions).**
- **Claude Code** has no native daemon/Telegram — it's a coding tool. Use it as the
  *headless executor* an `plainkeep` verb calls (`claude -p`, scoped `--allowedTools`), not as
  the chat face. If you want a Claude-driven Telegram surface, it rides inside Hermes/OpenClaw.
- **OpenClaw**'s workspace is "default cwd, not a hard sandbox" — absolute paths can still
  escape it; the `plainkeep`-only allowlist and `~/plainkeep`/`~/files` path wall (§5) are what
  actually contain it, not the workspace setting.
- **Hermes**'s native `AGENTS.md` load depends on gateway cwd; verify after install with a
  probe ("which instruction files are loaded?").
- **Codex** can't register arbitrary skill dirs (#22869) — hence the symlink; and its
  `experimental_instructions_file` is broken on GPT‑5‑class models, so don't rely on it to
  inject the contract — `AGENTS.md` is the supported path.

### 12.6 The extended agent roster (six more, mostly native AGENTS.md)

The first four agents (§12.5) spanned the hard cases. A second pass across six more —
**Antigravity (Google's Gemini-CLI successor), Grok Build (xAI), opencode, Cursor CLI,
Factory Droid, and pi (pi-mono)** — confirms the design's bet: **the industry converged
on `AGENTS.md` as the project-root contract.** Five of the six read it natively; only
Antigravity needs a one-file bridge (and even that bridge already exists as a side effect
of how it loads global context). The canonical-source-plus-thin-adapter pattern (§12.5)
covers all of them with no structural change to `~/plainkeep` — at most a symlink and a config
block per agent.

| Agent | Reads contract as | Finds the skill via | Lock shell to `plainkeep` | Adapter (committed in `~/plainkeep`) |
|---|---|---|---|---|
| **opencode** | `AGENTS.md` — **native** (first-match beats `CLAUDE.md`; global `~/.config/opencode/AGENTS.md` too) | `skills/` + `instructions` glob in `opencode.json`; native agents/skills | `permission: {bash: ask}` + allow `plainkeep`; tools filtered pre-model | `.opencode/` config (or none — uses `~/plainkeep/AGENTS.md` directly) |
| **Cursor CLI** (`cursor-agent`) | `AGENTS.md` + `CLAUDE.md` + `.cursor/rules` — **native, all at root** | `.cursor/rules` (skills injected via rules) | `~/.cursor/cli-config.json` / `.cursor/cli.json`: `Shell(plainkeep)` allow, deny rest | `.cursor/cli.json` (permissions) |
| **Grok Build** (`grok`) | `AGENTS.md` family + `CLAUDE.md` + `.claude/` — **native, zero-config** | `.claude/skills/` (reads the Claude tree) | plan-mode + approval; `~/.grok/config.toml` | reuse the `.claude/` adapter (§12.5) |
| **Factory Droid** (`droid`) | `AGENTS.md` — **native** (nearest-wins, auto-read before any change) | `.factory/skills/**` (native SKILL.md) | `droid exec` defaults to read-only **spec-mode**; `--auto low\|medium\|high` to escalate | `.factory/skills → ../skills` symlink |
| **pi** (pi-mono) | `AGENTS.md` — **native** | `.agents/skills/` + `.pi/skills/` + global; native SKILL.md (inlines top-3) | **no built-in permission system** → `--no-shell`, or sandbox/containerize, or an `exec`-gating extension | `.agents/skills → ../skills` symlink |
| **Antigravity CLI** (`agy`) | `GEMINI.md` **and** `AGENTS.md` from workspace — **native** (global only via `~/.gemini/GEMINI.md`) | `.agents/skills/` (workspace) | bidirectional allowlist shared with Antigravity 2.0 | `.agents/skills → ../skills` symlink (+ optional `GEMINI.md`) |

**What this confirms and what to add:**

- **`AGENTS.md` is the safe bet.** opencode, Cursor, Grok, Droid, pi, and Antigravity all
  honor a project-root `AGENTS.md` natively. So `~/plainkeep/AGENTS.md` *is* the portable
  contract; Claude Code (§12.5) remains the lone holdout needing the `CLAUDE.md` bridge.
- **`.agents/skills/` is emerging as the cross-tool skills path** (Antigravity, pi, and
  the open standard use it; Droid uses `.factory/skills/`). So alongside the `.claude` and
  `.codex` symlinks, add **`.agents/skills → ../skills`** and **`.factory/skills → ../skills`**.
  Now every agent reaches the one canonical `~/plainkeep/skills/` — Claude/Codex via their
  private dirs, Antigravity/pi via `.agents/skills`, Droid via `.factory/skills`, opencode
  via config glob, Hermes via external-dir, OpenClaw natively. **One directory, eight
  discovery paths.** All symlinks are committed and `plainkeep doctor` checks them.
- **Two new guardrail watch-items:**
  - **pi has *no* permission system** — it runs with the user's full rights and only gates
    *project trust*, not commands. For an `plainkeep`-only surface, run it `--no-shell` (then it
    can't call `plainkeep` either — so pi is best as a *read/plan* driver), or wrap it in a
    sandbox, or install an exec-gating extension. Treat pi like OpenClaw: powerful, but
    safe only after deliberate containment. (Fittingly, pi is the SDK that *powers*
    OpenClaw, so they share this trait.)
  - **Grok reads the `.claude/` tree** for skills/agents/rules — convenient (no new
    adapter), but it means your Claude adapter does double duty; keep it clean.
- **Antigravity caveat:** Google replaced Gemini CLI with the Go-based Antigravity CLI
  (`agy`); consumer Gemini-CLI access sunsets June 18, 2026. Both read workspace
  `AGENTS.md`, but **global** context is `~/.gemini/GEMINI.md` only — so a global baseline
  must go in `GEMINI.md`, not `AGENTS.md`. Workspace skills moved to `.agents/skills/`
  (hence the symlink above). Don't rely on `~/.gemini/GEMINI.md` for project rules; keep
  those in `~/plainkeep/AGENTS.md`.

**Verdicts (extended roster).** **opencode, Cursor CLI, Factory Droid — best-in-class fit:**
native `AGENTS.md`, native skills, real permission/approval controls; Droid even defaults
to read-only. **Grok Build — native and zero-config**, rides the `.claude/` adapter.
**Antigravity — native with a `.agents/skills` symlink and a `GEMINI.md` global baseline.**
**pi — operable but must be contained** (no permission system); ideal as the
read/plan/“gardener-of-knowledge” driver rather than the one holding the keys to `plainkeep`.

Across all ten agents now surveyed, the design needs **zero structural change** — only a
small, committed adapter set (`CLAUDE.md` bridge; `GEMINI.md` optional; and the symlinks
`.claude/skills`, `.codex/skills`, `.agents/skills`, `.factory/skills` → `../skills`).
That the entry point survived contact with ten independent agents is the strongest
evidence the agnosticism contract (§1) is real and not aspirational.

These products iterate weekly; treat the exact key names as current-as-of-research and
re-verify against installed versions before shipping. The *structure* — one canonical
entry point plus committed per-agent adapters — is what's durable, regardless of which
config key a given release uses.

---

## 13. The four planes of operation (all equivalent)

1. **Finder (manual).** Browse `~/plainkeep`; open markdown anywhere; drag a file into
   `inbox/`; drag a task file between status folders. Nothing breaks — it's just files.
   Sidebar favorites: `~/plainkeep`, `~/plainkeep/tasks/active`, `~/plainkeep/inbox`, `~/work/clients`.
2. **Terminal (manual).** `plainkeep <verb>` for system operations; raw `git`/`rg`/`$EDITOR`
   for everything else. No intermediary. (A Raycast/Alfred hotkey that runs `plainkeep capture`,
   `plainkeep files ingest`, or `plainkeep new` is just an ergonomic shortcut to this plane — an
   optional human launcher, not a fifth plane; it calls the same verbs.)
3. **Scheduler (automated, no judgment).** launchd fires the same `plainkeep` lines for
   `read`/`safe_write` jobs (§15).
4. **Agent (automated, with judgment).** Your main driving plane: the gardener. It gets
   the skill file, the folder, a shell — and drives the identical surface. Swap it
   whenever something better ships.

Same commands, same files, same guardrails on all four planes. That equivalence *is*
the design.

---

## 14. Backup, restore, doctor

### 14.1 What is backed up (git remotes) vs regenerated

Four backup layers, each with one job:

| Layer | Protects | Mechanism |
|---|---|---|
| **git remotes** | `~/plainkeep`, `~/dotfiles`, every `~/work` repo (incl. opted-in `tools/`/`labs/`) | `git push` |
| **restic → encrypted bucket** | `~/files` (versioned, off-site, remote-restorable) | nightly job (§9.3) |
| **iCloud** | irreplaceable personal/legal/family originals — passport, certificates, health, signed master contracts, baby/family docs | always synced; **walled off from every `plainkeep` path** |
| **Time Machine** | the catch-all: app state, creative-app project files, anything outside the four roots | automatic on home Wi-Fi |

The tiebreaker when you're unsure where something belongs: **can you regenerate it?**
No, and it's irreplaceable/personal → iCloud. No, but it's work material → `~/files`.
Needs version history → a git repo. Everything else → Time Machine catches it. Note the
deliberate split: iCloud holds the *irreplaceable master* of a signed contract; `~/files`
may hold a *working copy* for an active project (with a shadow note) — agents touch the
latter, never the former.

Regenerated, never backed up: `.index/`, `.logs/`, rendered launchd plists, worktrees,
`~/work/sandbox/`, `~/Desktop/_swept/`.

### 14.2 New machine / disaster restore — the ordered sequence

The order matters: **you cannot clone private repos or restore restic until auth exists**,
and the empty `~/work` / `~/files` skeleton folders live in no repo, so something must
create them. The correct sequence:

```sh
# 1. Toolchain
xcode-select --install
sh -c "$(curl -fsLS get.chezmoi.io)" -- init --apply <you>   # Brewfile, configs, PATH, macOS defaults
exec $SHELL -l                                               # reload so ~/plainkeep/bin is on PATH

# 2. AUTH FIRST — nothing private clones or restores without this
op signin                                                   # 1Password CLI (installed by Brewfile)
#   1Password SSH agent now serves your git SSH key; restic key is op://… (see §9.3)

# 3. The brain, then the skeleton, then the contents
git clone <plainkeep-remote> ~/plainkeep
plainkeep doctor --init            # creates ~/work + ~/files skeleton folders; verifies everything
plainkeep repo clone --all         # re-clone every ~/work repo from the wiki registry
op run -- restic restore latest --target ~/files   # assets back, point-in-time, key from 1Password
```

Two things make this actually work on a bare machine: project notes carry `remote:`
frontmatter (**the plainkeep repo is the manifest of your work repos**, so `~/work` rebuilds
without ever being one repo), and **auth is established before any clone or restore** —
the single most common reason a "just git clone it" plan fails in practice. `plainkeep doctor
--init` is idempotent: it creates any missing skeleton folder (`~/work/{clients,products,
tools,labs,sandbox,archive}`, `~/files/{clients,products,areas,research,archive}`,
`~/Desktop/_swept`) and is safe to run any time, not only at restore.

### 14.3 `plainkeep backup` (run weekly by a job; loud)

Checks plainkeep + dotfiles for uncommitted/unpushed changes; walks the project registry and
reports dirty/unpushed work repos; verifies remotes are configured; checks the last
restic snapshot of `~/files` is <48h old; records the check in the journal. It never
pushes or uploads by itself (`confirm` class) — it nags.

### 14.4 `plainkeep doctor`

Verifies: required tools present (git, rg, fd, fzf, jq, sqlite3, chezmoi, restic, 1password-cli;
ollama only if stage-2 search is on) · auth works (`op` signed in, git SSH key served) ·
`plainkeep` on PATH · folder structure + templates intact · `plainkeep.json` parses and matches
`bin/` · index rebuilds · job registry parses · remotes configured · restic repo
reachable · no secret-looking files tracked · no binaries tracked in `~/plainkeep` · **agent
adapters resolve** (`CLAUDE.md` imports `AGENTS.md`; the skill symlinks `.claude/skills`,
`.codex/skills`, `.agents/skills`, `.factory/skills` all point at `skills/`; adapter
configs parse — §12.5–12.6). The `--init` flag
additionally *creates* any missing skeleton folder and re-links broken adapter symlinks
(idempotent). First command after any restore; also run by `plainkeep week`.

Machine-level reproducibility that isn't a config file — Finder/dock/macOS `defaults`
(show hidden files, path bar, sort-folders-first) — lives as a chezmoi `run_once_` script
in `~/dotfiles`, so a fresh Mac comes up configured. Keep the Brewfile current with
`brew bundle dump --force` (the yearly cadence, §16); a stale Brewfile is the quiet way a
"one-command rebuild" silently stops being one command.

---

## 15. Jobs — scheduler-neutral definitions, one adapter

Definitions live once in `jobs/registry.yaml`; the launchd adapter renders plists
(`plainkeep job apply`); `plainkeep job run <name>` runs anything manually (the universal fallback —
also the migration path if the host ever isn't a Mac).

```yaml
jobs:
  index:        { command: "plainkeep index",          schedule: { interval_minutes: 60 }, risk: read }
  close_nudge:  { command: "plainkeep close --automated", schedule: { daily: "18:30" },    risk: safe_write }
  backup_check: { command: "plainkeep backup",         schedule: { weekly: "Fri 17:00" },  risk: read }
  files_backup: { command: "restic backup ~/files", schedule: { daily: "03:00" },    risk: read }   # reads ~/files, writes only to the bucket
  sweep:        { command: "plainkeep sweep",          schedule: { daily: "02:00" },       risk: safe_write } # Desktop/Downloads → _swept → trash
  nuke_modules: { command: "plainkeep repo nuke-modules --stale 30", schedule: { monthly: "1 04:00" }, risk: safe_write } # only node_modules untouched 30+ days — never an active project's
  consolidate:  { command: "plainkeep consolidate",    schedule: { daily: "02:30" },       risk: safe_write } # dream-lite: refresh backlinks, flag stale/orphan notes, draft a "what changed" journal digest
```

**`plainkeep consolidate` — the dream-lite nightly cycle (from gbrain's "dream", stripped to
deterministic safe writes).** gbrain runs a ~20-phase LLM-heavy nightly cycle that synthesizes
transcripts, clusters facts into "takes", recomputes salience, and probes for contradictions.
The plaintext equivalent keeps only the phases that are *deterministic and reversible*: (1)
regenerate the backlink index (§10.1); (2) flag stale notes (untouched past a threshold) and
orphans (no inbound links) into a digest; (3) append a one-paragraph **"what changed today"**
summary to the journal from the day's git log + closed tasks. It is `safe_write` (every change
is a git diff), runs unattended, and — like every other job — calls a verb, never inline logic.
The LLM-judgment phases gbrain runs (contradiction probing, take-grading) stay **manual verbs
you run with eyes on**, never scheduled, per §15's rule that nothing surprising runs mid-work.

Job rules: jobs call verbs, never inline logic; only `read`/`safe_write` risk classes
may be scheduled; everything logs to `.logs/jobs/`; every job is manually runnable.
A scheduled job must be safe to run *while you're mid-work*: `nuke-modules` only touches
modules untouched for 30+ days (regenerable anyway via install); `sweep` moves, never
deletes, on the 7-day pass. Anything that could surprise you mid-task is not scheduled —
it's a manual verb you run with eyes on it.

---

## 16. Operating loops & rules of thumb

- **Daily (~5 min).** `plainkeep capture` all day as thoughts arrive (zero-decision capture —
  routing is triage's job, not capture's). `plainkeep start` in the morning; `plainkeep close` at
  day's end (the 18:30 job nags if you forget).
- **Weekly (~20 min).** `plainkeep week`: approve triage filings, scan `plainkeep repo health` and
  `plainkeep backup`, sweep done tasks, and answer the one compounding question: *what did I
  do by hand twice that should become a skill or verb?*
- **Monthly (~30 min).** Review and send the drafted invoices + accountant packet for
  Mária. Machine hygiene runs itself (`nuke-modules`, sweep); just glance at `tools/` —
  anything you no longer run? `plainkeep archive` it.
- **As-needed.** `plainkeep files ingest` from Desktop/Downloads when something worth keeping
  arrives; `plainkeep archive` a lab repo the moment you know it's done.
- **Quarterly (~1 hr).** Prune unused verbs/skills (the logs say which), review `plainkeep week`'s
  archive candidates, update `conventions.md`, append to `decisions.md`.
- **Yearly (~30 min).** Update the Brewfile in `~/dotfiles`, prune dead GitHub remotes,
  sweep `done/` and `archive/` into the year folders.

Script rules (apply to every `run.sh`): idempotent where possible · `--dry-run` on
anything mutating · absolute paths from `$PLAINKEEP_HOME` · fail fast, loud, non-zero ·
no embedded secrets · works from any cwd.

---

## 17. Build order

Each phase is independently useful. Do not skip ahead; do not start phase N+1 until
phase N is in daily use.

1. **Roots + git + Finder.** Create `~/plainkeep` per §3 (folders, README, AGENTS.md,
   conventions.md, templates), push to a private remote. Create `~/work` tree. Usable by
   hand immediately.
2. **Dispatcher + first pure-shell verbs.** `plainkeep`, `bin/lib/`, then `capture`, `task`,
   `help`, `status`, `search` (FTS5), `index`. Put on PATH via dotfiles. *This phase is
   80% of daily value.*
3. **Manifest.** `cmd.json` sidecars + manifest build → `plainkeep.json`. `plainkeep help` and
   agents now learn the surface from one file.
4. **Guardrails.** `guardrail.sh`, classify every verb, new verbs default `confirm`.
   Broad agent freedom is now structurally safe.
5. **The agent entry point + adapters + agent test.** Write `AGENTS.md` (§12.2) and
   `operate-plainkeep/SKILL.md` (§12.3) — especially the routing tree (§12.3 #4a) and the
   wiki-traversal rules (§12.3 #5). Add the adapters (§12.5): `CLAUDE.md` bridge,
   `.codex/` and `.claude/` with their symlinks and configs; configure Hermes/OpenClaw in
   `~/dotfiles` if used. Run the two-agent test on **two genuinely different agents**
   (e.g. Codex + Claude Code, since they sit at opposite ends — native `AGENTS.md` vs
   `CLAUDE.md`-only): have each file a sample repo, a sample document, and answer a wiki
   question; fix the manual until behavior is identical. Start with Codex (lowest-risk,
   native everything), then add others. The entry point is done when it *is*, not when it
   merely exists.
6. **Flow verbs.** `start`, `close`, `triage`, `week` — with `run_agent` where judgment
   helps and shell fallbacks throughout.
7. **Work standards.** `templates/project-repo/`, `plainkeep new`, `plainkeep repo health/clone`,
   worktree policy. Retrofit AGENTS.md + script/* onto your 2–3 most active repos.
8. **Business + jobs.** `plainkeep invoice` (reading tax-formula.md), `jobs/registry.yaml` +
   launchd adapter, `plainkeep backup`, `plainkeep doctor`.
8b. **The files plane.** Create `~/files` per §9.1, init restic + nightly job, move the
   2–3 active clients' material in, extend `plainkeep new` to scaffold the files folder, add
   `plainkeep files ingest` with shadow notes. (Can run in parallel with 6–8; folders + restic
   alone are already most of the value.)
9. **Stage-2 hybrid search (core at scale, ADR-006).** Local Ollama **EmbeddingGemma** (per-model
   prompts) + **LanceDB** ANN, fused with FTS5+graph via weighted RRF (`PLAINKEEP_VECTORS=1`). Built on
   LanceDB from the first note so there is no re-platforming at 100k. Then **stage-3**: a local
   cross-encoder reranker (`bge-reranker-v2-m3`) over the fused candidates for precision.
10. **Scale-out plumbing (when the corpus grows).** Keep the ONE repo fast: `notes/<aa>/`
   subdirectory fanout + git large-repo tuning (`feature.manyFiles`, `core.fsmonitor`,
   `core.untrackedCache`, `commit-graph`); make `plainkeep index` a **resumable, checkpointed, batched**
   backfill (done) with a file-watcher for live incremental (or the hourly `plainkeep index` job, §15).
   Binaries already live in `~/files`, so the repo stays plaintext and git stays fast. *Optional,
   much later:* split into multiple git repos only if you hit millions of files or want to separate a
   noisy auto-ingest feed's cadence / per-machine selective sync. The architecture (embedded FTS +
   LanceDB, markdown truth, one repo) does not change — only indexing throughput and (optionally) repo
   layout do.

---

## 18. Definition of done (v1)

- `~/plainkeep` and `~/dotfiles` are git repos with remotes; `~/work` tree exists.
- The ~16 verbs of §4.1 work; `plainkeep help` renders from `plainkeep.json`.
- Guardrails enforce risk classes; new verbs default to `confirm`.
- Capture → triage → task → done flows end-to-end; journal records every day.
- `AGENTS.md` + `operate-plainkeep/SKILL.md` pass the two-agent agnosticism check: two
  different agents file the same repo to the same root, traverse the wiki the same way,
  and refuse the same actions.
- Per-agent adapters (§12.5) are committed and `plainkeep doctor` confirms they resolve, so a
  fresh clone makes Codex/Claude/Hermes/OpenClaw operable with no extra wiring.
- A new machine reaches full function via §14.2 in under an hour.
- You can find any note in <10 seconds via `plainkeep search`, and any file by eye in Finder.
- `~/files` exists with restic backing it up nightly; `plainkeep backup` notices a stale snapshot.
- Important documents are findable via `plainkeep search` through their shadow notes.
- Nothing in any repo is a secret; nothing outside the four roots is ever touched.

---

## Appendix A — merge decisions at a glance

| Concern | Source | Decision |
|---|---|---|
| Topology: plainkeep / work / dotfiles split | v1.0 | **Adopted** — solves code-repos-outside-the-system cleanly |
| Verb dispatcher + `cmd.json` → `plainkeep.json` manifest | v2 | **Adopted** — capability discovery without hardcoding |
| Guardrails as per-verb risk in the dispatcher | v2 | **Adopted**; v1.0's `policy.yaml` **deleted** (duplicate truth) |
| Agent indirection (`PLAINKEEP_AGENT` + `agent.sh`) | v2 | **Adopted** — exactly one swap point |
| Task system (folder = status, ID'd records) | v1.0 | **Adopted, slimmed** 7 statuses → 4 |
| Journal as shared activity record | v2 (= v1.0 daily notes) | **Adopted** — the cross-driver memory |
| Project standards: AGENTS.md + script/* + worktrees | v1.0 | **Adopted**; CLAUDE.md becomes a symlink |
| Work-repo registry in wiki frontmatter → `plainkeep repo clone --all` | new (implied by both) | **Adopted** — makes ~/work reconstructable |
| Skills vs SOPs as separate trees | v1.0 | **Merged** into one skill concept |
| 8 sub-CLIs, ~50 commands | v1.0 | **Cut** → ~16 flat verbs, shallow subactions |
| 5 agent role prompts, 10 prebuilt skills | v1.0 | **Cut** → 1 system skill + 3, grown on demand |
| PARA numeric prefixes (10-, 20-, …) | v2 | **Cut** — plain semantic folder names |
| Graph DB / vector DB upfront | both flirted | **Cut** — wikilinks + FTS5 now; vectors when earned; graph probably never |
| cron/systemd adapters | v1.0 | **Cut** — launchd + `plainkeep job run` fallback |
| Telegram supervisor / Mac mini host / always-on agent | earlier drafts | **Out of scope** — future *callers* of the same surface; core unchanged |
| iCloud wall, secrets-as-references, chezmoi rebuild | both | **Adopted** unchanged |
| `~/files` assets root (plaintext→git, binary→files rule) | v3.1 | **Added** — binaries out of git, slugs mirrored, in/out/work/research |
| Shadow notes (`plainkeep files ingest`) | v3.1 | **Added** — binaries become searchable via wiki notes, not asset trawling |
| restic → encrypted bucket for `~/files`; Tailscale for remote | v3.1 | **Added** — boring, proven; key in 1Password, never in a repo |
| `~/work` lifecycle: `tools/`, routing rule, `plainkeep archive` (git-bundle) | Daddy Dev OS | **Added** — fills the repo birth→death story the design lacked |
| Desktop/Downloads as macOS inboxes + auto-sweep decay (7d→_swept→60d→trash) | Daddy Dev OS | **Added** — the missing machine-hygiene/decay layer |
| "File by what it IS, not what it's FOR" | Daddy Dev OS | **Added** — one physical home per file; links do the "for" |
| Time Machine catch-all + regenerate/version tiebreaker + explicit iCloud wall contents | Daddy Dev OS | **Added** — sharpens the four-layer backup story |
| Full iCloud Hungarian folder tree; per-area resources/; separate photo/design/3d/video roots | Daddy Dev OS | **Rejected** — personal/family + creative-app domains are out of scope; importing them confuses the four-root boundary |
| Raycast/CleanShot/Obsidian/Ghostty tool specifics | Daddy Dev OS | **Rejected as core** — tooling preferences belong in `~/dotfiles`; Raycast noted only as an optional launcher (§13) |
| Restore auth-ordering (1Password SSH/restic key *before* clones) | Automation doc (bootstrap/SSH notes) | **Added** — fixed a restore that wouldn't actually run on a bare machine (§14.2) |
| `~/work` + `~/files` skeleton via `plainkeep doctor --init`; macOS `defaults` as chezmoi run-script | Automation doc (bootstrap) | **Added** — the empty folders + Finder prefs were in no repo (§14.2/§14.4) |
| iCloud ingest = propose-only at the wall (agent never writes iCloud) | reconciles Automation doc's tidy-into-iCloud vs our wall | **Added** — keeps the wall intact while still getting classification help (§9.4) |
| `nuke-modules --stale 30` (never an active project's modules) | Automation doc (script targets 30-day-old only) | **Added** — made the unattended job safe mid-work (§15) |
| Corrections accrete into `conventions.md` "Filing rules" | Automation doc (tidy evolution v3 learning file) | **Added (minimal)** — plaintext learning loop; no JSON engine, no agent memory (§10) |
| Concrete agent tool-scoping in `agent.sh` | Automation doc (`--allowedTools "Bash,Read"`) | **Added** — read verbs get read-only scope; one line in the one adapter (§6) |
| Full bash script bodies, launchd XML, Raycast launcher files | Automation doc | **Rejected as core** — implementation, not design; they live in `~/plainkeep/bin/` + `~/dotfiles`, generated/edited there, not pasted into the design |
| Per-agent adapters: `CLAUDE.md` bridge + `.codex/`/`.claude/` config & skill symlinks | agent research (Codex/Claude/Hermes/OpenClaw) | **Added** — Claude won't read `AGENTS.md`; Codex/Claude won't find a bare `skills/`. One canonical source + thin committed adapters (§12.5) |
| Canonical `~/plainkeep/skills/` reached by 4 discovery paths (native/external-dir/symlink) | agent research | **Added** — keeps one source of truth while satisfying each agent's fixed skill location |
| Hermes/OpenClaw home-dir config in `~/dotfiles`, not the plainkeep repo | agent research | **Added** — those configs are machine config (chezmoi); plainkeep repo holds only the project-scoped Codex/Claude adapters |
| OpenClaw `exec` default-ungated → mandatory allowlist; doctor checks adapters | agent research | **Added** — closed the biggest always-on-agent risk; broken adapter is now a health-check failure |
| Extended roster: opencode, Cursor CLI, Grok, Factory Droid, pi, Antigravity | agent research (round 2) | **Added (§12.6)** — 5/6 read `AGENTS.md` natively; only `.agents/skills`/`.factory/skills` symlinks + optional `GEMINI.md` needed. Validates the contract across 10 agents with zero structural change |
| pi has no permission system; Antigravity global = `GEMINI.md` only | agent research (round 2) | **Noted as caveats** — pi runs contained (read/plan driver); global baseline goes in `GEMINI.md`, project rules stay in `AGENTS.md` |

### Appendix A.1 — gbrain merge decisions (v3.7)

| Concern | Source | Decision |
|---|---|---|
| Compiled-truth + timeline two-zone notes (rewrite the top, append the bottom) | gbrain | **Adopted** — applied to task records (§7.2) and entity hubs (§10.1); pure plaintext, one habit |
| Brain-first lookup (check the system before the web/memory) | gbrain | **Adopted** — first principle 9 + a hard rule in `operate-plainkeep` (§12.3) |
| Iron Law: model picks WHAT, code guarantees WHERE/HOW (paths/slugs/links deterministic) | gbrain | **Adopted** — principle 10 + guardrail enforcement (§5) + skill hard rule (§12.3) |
| Zero-LLM auto-backlinks generated on every write; graph as first-class retrieval | gbrain | **Adopted** — §10.1/§10.2; pulls backlink traversal into stage 1, defers vectors further |
| `routing-eval.jsonl` fixtures as the checkable "stable skill" gate | gbrain | **Adopted, slimmed** — substring/description matching, no judge models (§11, §12.4) |
| `prediction` note type → future calibration ("how often was I right?") | gbrain (takes-grading) | **Adopted, minimal** — one optional note type, no DB (§10.1) |
| `plainkeep consolidate` dream-lite nightly job (deterministic phases only) | gbrain ("dream" cycle) | **Adopted, stripped** — backlinks/stale/orphan/digest; LLM-judgment phases stay manual (§15) |
| Postgres + pgvector + HNSW + cross-encoder reranker retrieval stack | gbrain | **Rejected as core** — violates reversibility (principle 6); FTS5→vectors staging unchanged (§10.2) |
| Retrieval add-on test (file-based+local+rebuildable = OK; server/2nd-source = reject); stage-2 = **LanceDB** ANN + local Ollama embeddings + RRF; graph stays wikilinks | X research (Karpathy LLM Wiki) + real-embedder measurement (`test/run_search.py`) | **Added (§10.2.1)** — settles the vector question: local file-based vectors fit the philosophy and recover the semantic-bucket misses; server/graph-DB tiers (pgvector, FalkorDB, LightRAG, RDF) rejected |
| Scale to 100k–500k+ notes, single-machine, no server: LanceDB ANN + ONE git repo (fanout + tuning) + resumable backfill + local rerank | owner's ambition + research (sqlite-vec brute-force limit, LanceDB scale, real git scaling facts) | **Added (ADR-006, §10.2/§17)** — reaches gbrain capability via embedded engines, no Postgres; git-spine stays one repo (gbrain's git pain was binary *size*, excluded here by plaintext→git/binary→files) |
| Skillopt self-optimizing skills + 3-model judge panels + eval gates on every change | gbrain | **Rejected** — real plainkeep/$$ cost for ~4 skills; routing-eval is the right-sized substitute |
| Schema-packs / lens-packs / skillpack registry (versioned, distributable brain shape) | gbrain | **Rejected** — `wiki/conventions.md` "Filing rules" is the lightweight learning loop already (§10) |
| Minions job queue / autopilot daemon / push-"volunteer"-context / MCP-HTTP server | gbrain | **Rejected / deferred** — agent supervisor was already out of scope; keep the four planes (§13) |
| Single-DB "sources" model for separating repos | gbrain | **Rejected** — the four-root topology (code repos as independent git repos, §2) is cleaner for a dev |
| Cautionary tale: gbrain's 147k-token agent context file | gbrain (self-described anti-pattern) | **Heeded** — v3.7 stays minimal-additions; `operate-plainkeep` must stay ~one driving manual, not a brain dump |
