# AGENTS.md — operating contract for ~/plainkeep

You are operating Tamas's personal operating system. Read this file first, then read
`skills/operate-plainkeep/SKILL.md` before doing anything. This file is the contract; that
file is the manual.

## What this system is
A local, file-first, git-versioned system with FOUR roots:
- `~/plainkeep` — THE SYSTEM (this repo): knowledge (wiki), tasks, journal, the `plainkeep`
  verbs, skills.
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
