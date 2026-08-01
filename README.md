# plainkeep — a personal operating system (`~/plainkeep`)

**plainkeep turns a folder of Markdown files into a system that runs your knowledge, tasks, and
work — operable by you, by schedules, and by AI agents, all through one command.**

Everything is plaintext in your own git repo. One `plainkeep <verb>` command is the only door: it
knows where things go, a guardrail classifies every call before it runs, and nothing leaves your
machine without you. No server, no cloud, no lock-in — you can grep your notes or walk away at any
time.

There are two faces, one door:

- **Humans** run `plainkeep ui` — a guided terminal UI. Point-and-pick menus, forms generated from
  the machine contract, nothing to memorize.
- **Agents and scripts** run `plainkeep <verb> --json` — a frozen JSON envelope and a strict
  exit-code protocol.

Both go through the same dispatcher, so the guardrail and the logs see every action identically.

[![License: MIT](https://img.shields.io/badge/License-MIT-C96442.svg)](LICENSE) · verbs generated (see [`plainkeep.json`](plainkeep.json)) · offline test suites in CI · macOS / Linux · Python 3.10+ + git · zero required deps

> [!TIP]
> Want the 2-minute tour first? Open [`docs/how-it-works.html`](docs/how-it-works.html) in a
> browser — a self-contained interactive walkthrough, no install needed.

## Quick start

This repo is a **template, not your data**. The installer clones your own copy to `~/plainkeep` and
wires the machine:

```sh
# 1. Download the installer, verify it, read it
curl --proto '=https' --tlsv1.2 -fsSL \
  https://raw.githubusercontent.com/gabros20/plainkeep/main/script/get -o get.sh
shasum -a 256 get.sh        # compare against script/get.sh.sha256 in the repo

# 2. Install (lean vault, non-interactive, no sudo; refuses to overwrite an existing ~/plainkeep)
sh get.sh

# 3. Finish with the guided first-run
plainkeep setup --wizard
```

The wizard walks the optional layers with safe defaults: vault skeleton and the terminal UI **on**;
semantic search, local models, and scheduled jobs **off** until you opt in. Prefer flags?
`plainkeep setup --all --yes` does the same non-interactively.

Then try it:

```sh
plainkeep ui                                 # the guided terminal UI
plainkeep capture "an idea worth keeping"    # a thought → inbox/
plainkeep orient                             # where am I? tasks, journal, inbox, health
```

Not ready to commit? `sh get.sh --demo` installs a throwaway vault with example notes into a tmp
dir — one `rm -rf` removes it completely. Manual install (GitHub template + `script/setup`) and the
full layer reference: [`docs/setup.md`](docs/setup.md).

**Requirements:** macOS or Linux (Windows via WSL2), `git`, Python 3.10+. Everything else is
optional and auto-detected — search, models, OCR, and backup tools each degrade gracefully with a
one-line install hint when missing.

## How your files are organized

Four roots, separated by location. Siblings sit *next to* `~/plainkeep`, never inside it — that
keeps the knowledge repo small and fast, and the safety wall is rooted in this layout:

| Root | Holds | In git? |
|---|---|---|
| `~/plainkeep` | knowledge (wiki), tasks, journal, the verbs, your plugins | ✅ your own repo |
| `~/work` | code — each project its own repo (`products/ labs/ tools/ clients/`) | each separately |
| `~/files` | binary assets — client docs, PDFs, datasets | ❌ (restic backup) |
| `~/dotfiles` | machine config; puts `plainkeep` on PATH | ✅ separately |

Inside `~/plainkeep`, everything is a Markdown file. Indexes and embeddings are disposable caches
rebuilt from the text — never the source of truth.

## Daily use

The rhythm is five verbs — `start` → `capture` / `triage` / `task` → `close`, with `week` on
Fridays. Everything else is discoverable:

```sh
plainkeep help                      # the whole surface, grouped, with summaries
plainkeep ui                        # or just point-and-pick
```

| Group | Verbs |
|---|---|
| Flow | `capture` · `triage` · `start` · `close` · `week` |
| Knowledge | `search` · `open` · `wiki` · `bookmark` · `enrich` · `organize` |
| Tasks | `task` (folder = status: inbox / active / waiting / done) |
| Work | `new` · `repo` · `archive` · `files` · `sweep` |
| Business | `invoice` (draft only) · `share` (capability links) |
| Jobs | `job` (schedule the nightly verbs) |
| System | `status` · `orient` · `doctor` · `setup` · `backup` · `index` · `models` · `plugin` · `ui` · `help` |

You never memorize paths or formats — the verb owns placement. And you never memorize flags either:

- **`plainkeep ui`** generates its menus and forms from the machine contract, so every verb,
  action, and argument is a guided pick.
- **Tab-completion (zsh)** completes verbs with summaries, plus your *actual* note slugs and task
  IDs, pulled live from your content.
- **`--dry-run`** on any mutating verb prints what would happen and writes nothing — it counts as a
  read, so no `--yes` needed.
- **`fzf` (optional)** turns bare `plainkeep search` / `plainkeep open` into live fuzzy pickers.

## Using it with AI agents

Point any capable agent at `~/plainkeep` and it drives the same system behind the same guardrail:

- **The contract it reads:** [`AGENTS.md`](AGENTS.md) (the open standard), with the detailed
  manual in [`skills/operate-plainkeep/SKILL.md`](skills/operate-plainkeep/SKILL.md). `CLAUDE.md`
  bridges for Claude Code.
- **The contract it parses:** every verb speaks `--json` with one frozen envelope, and exit codes
  are a protocol (`0` ok · `2` usage · `3` needs `--yes` · `4` not found · `5` denied). Refusals
  carry the exact corrected command — a refusal teaches the next call. The generated
  [`plainkeep.json`](plainkeep.json) describes the full surface: args, schemas, risk classes,
  per-action grammar, completion providers. Spec: [`docs/machine-contract.md`](docs/machine-contract.md).
- **The transport:** `plainkeep mcp` is a stateless MCP stdio server whose tool list is generated
  from `plainkeep.json`. Register it with `plainkeep mcp --setup`.

An agent never gets more power than you do: same guardrail, same logs, same confirm gates.

## How it stays safe

The guardrail classifies every invocation before it runs — yours, a cron job's, a plugin's, or an
agent's:

| Class | Meaning |
|---|---|
| `read` | pure read — runs freely |
| `safe_write` | writes inside the roots — every change is a revertible git diff |
| `draft_only` | produces a draft (e.g. `invoice`) — the system never transmits |
| `confirm` | needs an explicit `--yes` (exit 3 with the exact re-run when missing) |
| `deny` | always refused: force-push, `rm -rf`, secrets, paths outside the wall |

New and untrusted verbs default to `confirm`. It's all git underneath — even a mistaken write is
one `git revert` away.

## Finding anything

Three local search stages, no server. Keyword works out of the box; the semantic stages are opt-in:

```sh
plainkeep index                                  # build/refresh (SQLite FTS5 + wikilink graph)
plainkeep search "how do I stop a runaway agent" # ranked file#heading hits with snippets
```

Enable vectors (`PLAINKEEP_VECTORS=1`, local embeddings + LanceDB) and reranking
(`PLAINKEEP_RERANK=1`) via `plainkeep setup search --yes`. The index is disposable:
`rm -rf .index && plainkeep index` rebuilds it.

## Extending it

Your verbs live in `plugins/`, never in the engine — updates can't overwrite them:

```sh
plainkeep new verb standup                   # scaffold your own verb (run.py + cmd.json)
plainkeep plugin add you/plainkeep-pomodoro --yes  # install a pack
plainkeep plugin trust plainkeep-pomodoro --yes    # lift its trust ceiling (capped at confirm until you do)
```

A plugin verb appears in `plainkeep help`, `plainkeep.json`, completion, `plainkeep ui`, and MCP
automatically, and the guardrail gates it identically. How to write one:
[`docs/plugins.md`](docs/plugins.md).

## Staying current

```sh
./script/update    # pull engine improvements — your notes are never touched
```

`script/update` 3-way-merges only the engine files (listed in `script/engine.txt`): unmodified
files fast-forward, locally patched ones merge, conflicts get markers. When an update expects a
newer terminal UI, `plainkeep setup` shows "update available" and `plainkeep setup ui --yes`
fetches the exact pinned release.

## More

- **Frontends:** Obsidian as the zero-cost editor/graph/mobile app, Raycast script commands, JSON
  Canvas maps — [`docs/architecture.md`](docs/architecture.md) and
  [`docs/obsidian-compat.md`](docs/obsidian-compat.md). The terminal UI:
  [`docs/terminal-ui.md`](docs/terminal-ui.md).
- **Knowledge pipeline:** PDFs, audio, images, and URLs become searchable, provenance-labeled notes
  (`files ingest/extract/distill` → `organize`) — machine material can never quietly become truth.
- **Backup & share:** restic with an append-only cloud key, git-bundle sweeps, and expiring
  capability-URL sharing — [`docs/backup-and-share.md`](docs/backup-and-share.md).
- **Docs map:** [`docs/README.md`](docs/README.md) · ADR log: [`docs/DECISIONS.md`](docs/DECISIONS.md)
  · changelog: [`CHANGELOG.md`](CHANGELOG.md) · contributing: [`CONTRIBUTING.md`](CONTRIBUTING.md)

## Project layout

```
plainkeep            # the dispatcher: a shim over the compiled core, with the bash floor inside it
plainkeep.json       # GENERATED — the machine contract
AGENTS.md  CLAUDE.md # the agent contract
bin/                 # engine verbs (lib/ = shared code + the frozen plugin SDK)
cli/                 # the core binary + terminal UI source (ships to vaults compiled, never as source)
plugins/             # YOUR verbs + installed packs — never touched by updates
frontends/raycast/   # zero-build Raycast script commands
skills/operate-plainkeep/  # the operating manual any agent loads
wiki/ tasks/ journal/ inbox/   # your content
templates/ jobs/ script/ docs/ test/
```

## Status

Every verb is built, guardrail-gated, documented in the machine contract, and covered by offline
test suites that run in CI (stdlib-only, no network). An 18-scenario two-operator simulation passed
with zero divergences — the agent contract is unambiguous.

```sh
python3 test/run_all.py    # run everything locally
```

## License

[MIT](LICENSE) © 2026 Tamás Gábor. Use it, fork it, make it yours.
