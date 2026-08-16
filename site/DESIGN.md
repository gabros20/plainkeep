---
version: alpha
name: plainkeep site — Paper & Terminal
description: >
  Design contract for the plainkeep marketing/explainer site (site/, deployed at
  plainkeep.vercel.app). Brand kept, visual system evolved, IA restructured to four pages.
colors:
  paper: "#FAFAF7"
  paper-soft: "#F1F0EB"
  paper-bright: "#FFFFFF"
  ink: "#1D1B17"
  ink-soft: "#45413A"
  stone: "#6F6B60"
  line: "#D9D6CC"
  rust: "#C96442"
  rust-deep: "#A44E30"
  rust-bright: "#E28A68"
  moss: "#4F8A6B"
  moss-bright: "#7FB89B"
  term-bg: "#1D1B17"
  term-fg: "#EAE7DE"
  term-dim: "#928E82"
  term-str: "#E4C580"
typography:
  display: { fontFamily: "Bricolage Grotesque", fontWeight: 750, letterSpacing: "-0.025em" }
  heading: { fontFamily: "Bricolage Grotesque", fontWeight: 650, letterSpacing: "-0.01em" }
  body: { fontFamily: "Public Sans", fontWeight: 400, lineHeight: 1.65 }
  mono: { fontFamily: "IBM Plex Mono", fontWeight: 400 }
rounded:
  sm: 4px
  md: 8px
  lg: 12px
---

## Overview

**Register:** design IS the product (a marketing/explainer site) — but the audience is people who
may never have opened a terminal, so clarity outranks spectacle. Every explainer must survive the
"my non-technical friend gets it" test. Copy rules: plain verbs, second person, no unexplained
jargon; a term of art (vault, guardrail) is allowed only immediately after its plain-English
definition. Banned words on first use without definition: envelope, MicroVM, provenance,
capability URL, idempotent, launchd (say "the Mac's scheduler").

**Brand path: KEEP.** Logo (slot-mark), terracotta accent, Bricolage display, Public Sans body,
IBM Plex Mono. No rebrand. (The near-white warm ground is a considered token — "Vault Modern" —
not the cream default; the warmth lives in the accent and the terminal panels.)

**The concept: Paper & Terminal.** The product has exactly two material worlds and the site is
built from them. *Paper* (light, warm-white, cards, the file tree) is your notes — plaintext you
own. *Terminal* (dark ink panels) is the one command that runs them. Every section is set in one
world or shows the two touching; section rhythm alternates paper → terminal → paper.

**The signature: sessions that play themselves.** One terminal panel per page types its commands
character-by-character and prints real output, scroll-triggered, with a replay control. Everything
it shows is real product output — never invented flags, never fake UI. Static-complete without
JS; instant-complete under reduced motion.

## Colors

One accent: terracotta (`rust`). Moss is *semantic only* — it always means "safe / ok / passing",
never decoration. Yellow (`term-str`) exists only inside terminal panels as string/warn tint.
Diagram part-coloring (command anatomy) may use the terminal token tints, because there it *is*
semantics — the same colors the panels already use.

## Typography

Display = Bricolage 700–750, tight (-0.02 to -0.025em), used huge exactly once per page (the page
hero). Headings sentence-case. Mono is reserved for things you could actually type or read back
from the tool; UI copy never set in mono. Body measure ≤ 68ch.

## Layout

Four pages, cleanUrls:

- `/` — what it is, in 60 seconds, and install. Hero (played session) → "it's just files" bento →
  the one-door diagram → the feature map (links into /features) → humans/agents split →
  quickstart → why-different → closing.
- `/how-it-works` — the command line, demystified: command anatomy (interactive) → two ways to
  drive it (menus vs typing) → what happens when you press Enter (step flow) → you can't get it
  wrong (help/complete/dry-run/refusals-teach) → a day with it (played session) → the safety
  rules → where things live (four roots).
- `/features` — the full catalog, one section per capability, automation/schedules FIRST (newest,
  least covered): schedule → capture/triage → tasks → wiki → search → documents pipeline →
  bookmarks → backup & business → plugins → terminal UI → health/updates.
- `/agents` — for AI agents: same door/same rules story → the machine contract (JSON, exit codes,
  plainkeep.json) → MCP hookup → which agents → the rules agents follow → the 18/18 proof.

Left prompt-gutter rail stays (per-page markers). Sticky header with cross-page nav +
`aria-current`. Shared closing CTA band on sub-pages.

## Elevation & Depth

Terminal panels float on `shadow-panel` (12/32 soft); paper cards sit nearly flat (1/2). Depth
comes from the two worlds layering — a dark panel overlapping a paper section edge — never from
glows or glass.

## Shapes

Radii 4/8/12 only. No pills except the guardrail-class chips (existing). No over-rounding.

## Components

Existing: terminal-panel, pill, root-card, exit-strip, data tables, btn primary/secondary.
New: `term-play` (the played session), `anatomy` (labeled command diagram), `door-flow` (who → one
door → what happens), `states-strip` (rendered → installed → loaded), `feature-map` (link grid),
`split-doors` (humans/agents pair), `page-hero` (sub-page opener), `cta-band` (shared closer),
`tree-card` (the vault as a real file tree on paper).

## Do's and Don'ts

- DO show real commands and real output, verified against the repo docs — the site is a promise.
- DO alternate paper/terminal section rhythm; a page of only paper cards is flat and wrong.
- DO keep exactly one played session per page; other panels are static.
- DON'T use eyebrow chips, numbered section markers, or three identical feature cards.
- DON'T let moss appear as decoration, or any second accent appear at all.
- DON'T write "not just another…" copy, or sell — explain.
- DON'T break the no-build rule: hand-written HTML/CSS/vanilla JS, self-hosted fonts, no CDN.
