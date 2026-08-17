# AGENTS.md — operating contract for a plainkeep vault

You are operating someone's personal operating system: their knowledge, tasks and work records.
Read this file first, then read the **operating manual** — the `operate-plainkeep` skill — before
doing anything. This file is the contract; that skill is the manual.

`plainkeep setup agents --yes` installs the manual into every agent skills directory on the
machine (`~/.claude/skills/`, `~/.agents/skills/`, `~/.hermes/skills/`, `~/.grok/skills/`), so it
is normally already loaded as one of your skills. If it is not, ask the tool where it lives —
`plainkeep vault status` reports the engine root, and the manual is
`<engine>/skills/operate-plainkeep/SKILL.md`. Do NOT look for `skills/` beside these notes: since
ADR-017 the engine is a versioned tree OUTSIDE every vault, and that path resolves to nothing.

**If you cannot read the manual, say so and stop — do not improvise.** Working without it is how
an agent ends up grepping the notes and writing scripts to do what one verb already does.

## What this system is
A local, file-first, git-versioned system with FOUR roots:
- **the vault** — THE SYSTEM: knowledge (wiki), tasks, journal, and the human's own plugins.
  Its path is not fixed and not assumed: run `plainkeep vault status` to learn which vault you
  are on and where it is. Everywhere below, `<vault>/…` means that root.
- `~/work`    — CODE: every project is its own git repo. NOT a repo itself.
- `~/files`   — BINARY ASSETS: client docs, deliverables, research PDFs. NOT in git.
- `~/dotfiles`— THE MACHINE: configs. You inspect, you don't change without being asked.

You do everything through ONE command surface: `plainkeep <verb>`. The authoritative list of
verbs is `plainkeep.json` (run `plainkeep help`). NEVER invent a verb or work around the surface.

## Absolute rules (the guardrails enforce these; violating them is a system failure)
1. Operate ONLY inside the SELECTED vault, `~/files`, and the ONE `~/work` repo your task
   concerns. The selected vault is the one `plainkeep vault status` reports and no other.
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

## How to find anything (the rule that replaces improvising)
- To FIND a note, a task or an asset: `plainkeep search "<query>"`. Never `grep`, `find` or `ls`
  your way through the vault — the index exists so you don't, and trawling is how notes get
  misfiled and duplicated.
- To learn the surface: `plainkeep help`, or `plainkeep complete --json <partial>` for the
  candidate values of any argument. Never read or parse `plainkeep.json` with a script.
- To act: the verb owns the placement, the naming and the links. You supply content and judgement.

## Where to go next
- How to do anything → the `operate-plainkeep` skill (loaded as a skill; see above if it is not)
- What you can do → `plainkeep help` / `plainkeep.json`
- The shape of notes → `wiki/conventions.md`
- Inside a `~/work` repo → that repo's `AGENTS.md` (and use its `script/*`)
