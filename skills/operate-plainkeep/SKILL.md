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
The **vault** (git) is the single source of truth for knowledge and work records. `~/work`
holds code (each project its own repo). `~/files` holds binaries (find them via their
shadow notes, never by trawling). You act ONLY through `plainkeep <verb>` and by reading and
writing plaintext files. You add no capabilities of your own.

**The vault is DATA. plainkeep itself is not in it.** The engine — every verb, `bin/lib`,
the launcher — is a separate, versioned, READ-ONLY tree installed at
`${XDG_DATA_HOME:-~/.local/share}/plainkeep/engine/<version>/`, and a vault that carries a
`bin/` directory of its own is carrying inert files that nothing will ever run. Two
consequences you must act on:

* **There is no `~/plainkeep`.** The vault is wherever it is, it may not be the only one,
  and its path is not part of your instructions. Run `plainkeep vault status` (or
  `--json`) at the start of a session to learn which vault you are on and where it is; the
  same command reports the engine root beside it. Every path in this document written
  `<vault>/…` means that root.
* **You cannot change plainkeep by editing files.** Fixing a verb is not a vault edit — the
  installed tree is read-only, and editing a copy of it inside a vault changes nothing. A
  change to the engine is a change to its SOURCE checkout, reviewed and installed as a new
  version. What you CAN add without touching the engine is a plugin: `plainkeep new verb
  <name>` scaffolds one into `<vault>/plugins/local/<name>/`, which is user-owned, survives
  an engine upgrade, and re-enters through the dispatcher so the guardrail still gates it.
* **Creating a vault and installing an engine are two different acts.** `plainkeep vault
  init <path> --yes` makes a new DATA-ONLY vault (content dirs, configuration, `plugins/`,
  a generated `plainkeep.json`, the marker, a registry entry — and no code). `plainkeep
  vault register <path> --yes` ADOPTS a directory that already exists. Neither installs an
  engine, and `init` refuses a directory that already carries engine code — that is a
  source checkout, and `register` is what adopts one.
* **Never update or roll back an engine on a human's behalf without being asked.** Both are
  `bin/lib/enginetree.py`, not verbs, and they are deliberately outside the surface you
  drive. If an engine looks wrong, report what `plainkeep doctor` says — its `engine:` rows
  name the version, whether the tree is sealed, and which pair a rollback would reach — and
  hand the operator the command. The rollback is
  `python3 <engine>/bin/lib/enginetree.py --rollback`; the previous pair is retained
  precisely so a human has that option (docs/DECISIONS.md ADR-021).

## 2. The one rule about capabilities
`plainkeep.json` (or `plainkeep help`) is the authoritative, complete list of what you can do.
NEVER invent a verb. If a task needs something not in the manifest, say so and propose
adding a verb — do not script around the surface, do not reach for raw tools to do what a
verb already does.

`plainkeep.json` is schema `plainkeep.json/3`: besides each verb's top-level `args`/`risk`, a compound
verb carries an `actions[]` array — the full subcommand grammar (per-action name, typed
`args` with `enum`/`complete` provider/`required`, per-action `risk` and `dry_run`). Read it
to know a subcommand's exact shape instead of guessing from prose. `plainkeep complete --json
<partial words…>` returns candidate `{value, description, kind}` rows for any argument (the
same brain shell tab-completion uses). Per-action `risk` is descriptive (the verb self-gates
`confirm` subactions on `--yes`, exit 3) — never pre-append `--yes`; let the verb refuse and
read the exact re-run from its error. `plainkeep ui` is the HUMAN terminal UI — not for you; you
drive verbs by flags + `--json`, which is the better surface for an agent.

## 3. Orientation — run this at the start of every session
1. Read today's and yesterday's `journal/YYYY/MM/*.md` to see what already happened.
   The day's bookends are SCHEDULED by default (ADR-022): journal lines like
   `started the day (automated)` / `closed the day (automated)` mean launchd ran
   `start`/`close`, not a human — do not re-run `start` because nobody typed it; the note is
   already seeded and a second run is a no-op you'd be logging for nothing.
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
| A passing thought / note (text) | `<vault>/inbox/` then triaged to wiki or a task | `plainkeep capture "<text>"` |
| Durable knowledge (something learned, a decision, a how-to) | `<vault>/wiki/` (notes/ for atomic ideas; the entity hub for client/project facts) | edit the note; `plainkeep wiki new` for a new one |
| Multi-step work to track | `<vault>/tasks/<status>/` | `plainkeep task add` |
| A binary doc you RECEIVED (brief, contract draft, asset, research PDF) | `~/files/<area>/…/in/` or `research/`, + a shadow note in the wiki | `plainkeep files ingest` |
| An ingested PDF/audio/video/image you want to search or read as text | a sibling derived note `wiki/files/<slug>.extract.md` (markdown/transcript/OCR) | `plainkeep files extract <slug>` |
| A link/article/URL you want saved and searchable | `wiki/bookmarks/` (a `type: bookmark` note; readable text pulled in) | `plainkeep bookmark <url>` |
| A binary you PRODUCED (invoice, export, report) | the project's `~/files/.../out/`, linked from its wiki note | the producing verb writes it there |
| A personal/legal/family doc (tax, medical, ID, signed master) | iCloud — **you do NOT file this; you PROPOSE the destination and stop** | report the suggested path |
| **A code repo** | see the routing tree below — this is where misfiling happens | `plainkeep new project` / by hand |
| A new plainkeep CAPABILITY | `<vault>/plugins/local/<name>/` — never the engine tree, which is read-only | `plainkeep new verb <name>` |

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
  - *Driving plainkeep from an agent terminal (Hermes, dispatch, cron)?* Keyword search always works,
    but **semantic** search (`PLAINKEEP_VECTORS=1`) needs the agent's shell to use the same `python3`
    (venv + PATH) as your interactive shell — a common silent gap. If `plainkeep doctor` says lancedb is
    missing though you installed it, see [`docs/agent-terminal-search.md`](../../docs/agent-terminal-search.md).
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
  (start/close run on a schedule by default — `plainkeep job status` shows rendered/installed/
  loaded per job; run them by hand only when the schedule didn't, or the human asks)
- Tasks:             `plainkeep task list|add "<title>"|show <id>|move <id> <status>|done <id>`
- Knowledge:         `plainkeep wiki open <slug>|new <type> <name>|backlinks <slug>|stale|orphans`
- Filing:            `plainkeep triage` (text → tasks/wiki) · `plainkeep files ingest` (binaries → ~/files + shadow note)
  - `plainkeep triage` walks the inbox interactively; to apply a decision programmatically (with `--json`),
    use `plainkeep triage decide <item> task|note|skip` and `plainkeep triage drafts decide <slug> accept|reject|skip`
    — the human still owns the call, but this is the non-interactive way to record it.
  - `plainkeep files extract <slug>` turns an ingested source into a searchable derived note
    (`wiki/files/<slug>.extract.md`) — tier auto-detected by media type: PDF → markdown
    (pymupdf4llm, `--heavy` for docling), audio → transcript (`--lang`, `--diarize` for speaker
    labels), video/URL → transcript via yt-dlp captions, txt/md → plain text. Each tier is an
    optional dep that degrades to a one-line install hint instead of crashing; same-bytes+same-tool
    re-run is a no-op, `--reextract` forces.
    - `plainkeep files extract <img> [--describe]` OCRs an image (+ optional VLM description) — needs models
      installed/pulled, see [`docs/image-reading.md`](../../docs/image-reading.md); degrades to
      deterministic OCR (`ocrmac`/`tesseract`) without them, never crashes.
    - the video/URL tier needs a shadow note whose `path:` is already the URL — there's no
      `ingest <url>` support yet, so a bare link/YouTube URL doesn't have a verb-based way in;
      save it with `plainkeep bookmark` instead (below).
  - `plainkeep files distill <slug>` compiles an extract note into 1-N draft concept notes in
    `wiki/notes/` (agent-typed with an agent configured, heading-outline fallback without); promote
    with `plainkeep triage drafts`.
  - `plainkeep bookmark <url> [--archive]` saves an article/link as a searchable `type: bookmark` note
    (readable text via trafilatura when installed, crude tag-strip fallback otherwise); `--archive`
    also snapshots the page HTML to `~/files/bookmarks/` against link-rot. Any http(s) URL is
    accepted as a generic link — there's no special x.com/tweet handling.
    - both `files extract` and `bookmark` auto-call `plainkeep enrich` afterward (best-effort, unless
      `PLAINKEEP_ENRICH=off`) to generate `description`/`keywords` for search — see below.
  - `plainkeep enrich <slug> [--reenrich] | --all` generates `{description, keywords}` for a note's
    derived text and writes it to frontmatter, feeding both keyword and semantic search; idempotent
    via a content hash, `--reenrich` forces. Needs a local model pulled (`ollama pull gemma4:e4b`)
    or it degrades to a deterministic stdlib keyword floor — never crashes. See
    [`docs/search-enrichment.md`](../../docs/search-enrichment.md).
  - `plainkeep models list|status|stop|pull|test` — see/download/offload/A-B-test the model behind any
    stage (stt/ocr/vlm/enrich/embed/rerank); `pull`/`test` are confirm-gated (`--yes`). Same doc.
- Work scaffolding:  `plainkeep new project|client "<name>"` (asks/uses the routing tree above)
- Repo lifecycle:    `plainkeep repo health|clone <p>|clone --all` · `plainkeep archive <project>` (dead repo → bundle)
- Business:          `plainkeep invoice <client>` (DRAFT only; reads tax-formula.md; never sends)
- System:            `plainkeep status` · `plainkeep doctor` · `plainkeep setup [<layer>] [--all] [--yes|--wizard]`
  (layered installer; paired with `plainkeep doctor` as checker; see
  [`docs/setup.md`](../../docs/setup.md)) · `plainkeep vault status|list` (which vault am I on,
  which engine is running, how was it chosen) · `plainkeep backup` · `plainkeep index` · `plainkeep sweep`
  · `plainkeep job list|status|run <name>|apply|enable|disable` — the §15 schedule. `status` is a
  read (rendered / installed / loaded / drift); `run <name>` is the manual fallback for any job.
  `enable`/`disable` MUTATE THE HUMAN'S LAUNCHD SESSION (`~/Library/LaunchAgents`) and are
  confirm-class (`--yes`): never run them on your own initiative — only on the human's explicit
  ask, and prefer showing them `--dry-run` output first. Doctor WARNs (never fails) on a
  rendered-but-unloaded schedule or drift; the remedy it names is the human's to run.
  · `plainkeep complete --json` (completion candidates) · `plainkeep ui` (human TUI — not for agents;
  installed by `plainkeep setup ui --yes` as a compiled binary into `.local/bin/`)

`triage`, `files ingest`, and `invoice` PROPOSE; the human approves. `archive`, `sweep`,
`start`/`close`/`week`, and anything mutating support `--dry-run` — use it when unsure what a
verb will do (a dry-run is a read, so it needs no `--yes`).

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
- Operate ONLY inside the SELECTED vault, `~/files`, and the ONE `~/work` repo of the current
  task. The vault is the one `plainkeep vault status` reports and no other — registering a
  second vault does not widen the wall to it, and an invocation acts on exactly one.
  NEVER touch iCloud or family/personal paths.
- NEVER edit the engine tree. It is code, it lives outside every vault, it is installed
  read-only, and a write there is not a note you can revert — it is a change to the tool
  that is about to act on somebody's notes. Propose an engine change; do not make one. A
  capability you can add safely is a PLUGIN (`plainkeep new verb`), which lives in the vault
  and is yours to write.
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
