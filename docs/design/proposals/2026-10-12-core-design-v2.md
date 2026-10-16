# Plainkeep: a clearer knowledge core, dependable retrieval, and simple remote operation — v2

Design proposal · v2, 12 October 2026 (revises the v1 proposal of the same date)
Scope: core behavior, everyday ergonomics, multi-machine operation, installation, and an optional remote MCP adapter. No implementation or deployment performed.
Status: **PROPOSED**. Execution order: [`2026-10-12-core-design-v2-plan.md`](2026-10-12-core-design-v2-plan.md). ADRs in [`../../DECISIONS.md`](../../DECISIONS.md) supersede this text as stages land.

## What changed from v1

v1's direction holds. Every factual claim it made about the code was re-checked against commit `d7eb27e` and is correct. v2 changes four things:

1. **Adds a Stage 0 of cheap, high-value fixes** that v1 buried inside large stages. One of them, the Hungarian search bug, is the most damaging defect found, and its fix is one line.
2. **Adds issues found in the code and in the issue tracker** that v1 did not cover:
   - Writes never refresh the search index.
   - The MCP server has no tool annotations.
   - Search settings live in a hand-written `env.sh` that plainkeep never reads.
   - Vault identity does not survive a `git clone` (#16).
   - There is no `sync` verb (#16, and the July remote proposal).
   - `wiki new` cannot take a body (#17).
   - The manual hides the happy path (#15).
   - The triage model default is stale (#8).
3. **Corrects the remote-access facts**:
   - The current MCP spec is **2026-07-28**, and it makes Streamable HTTP stateless.
   - ChatGPT accepts **OAuth or no-auth only**, never a static key.
   - ChatGPT **Pro is read/fetch-only** (developer mode).
   - The OpenAI tunnel works only for OpenAI. Claude connectors **need a publicly reachable URL**.
4. **Reorders delivery**:
   - Stable IDs move later, together with remote editing, which is the first feature that needs them.
   - Multi-machine sync moves earlier, because the Mac mini and VPS plan depends on it.
   - Each reference project is now mapped to a concrete, adopt-or-decline decision.

Philosophy is unchanged: plaintext is the truth, git is the spine, one door, a model proposes and a verb writes, nothing transmits without a human.

---

## Decision

Keep Plainkeep's file-first architecture. Improve the contract between **what is stored, what is current, what can be retrieved, who may change it, and on which machine**.

| Priority | Change | Concrete win | Main cost or risk |
|---|---|---|---|
| 0 | Stage 0 fixes (§2) | Hungarian search works; new notes searchable immediately; truthful health; shorter agent path | Days, not weeks |
| 1 | Complete the content read/write contract | Agents (local and remote) read and write note bodies through verbs, not around them | Extending `wiki open` / `wiki new` / `wiki edit` |
| 1 | Section-level retrieval across all collections | Answers deep inside long documents, tasks and journals become findable | More index rows and embedding work |
| 1 | One vault across machines (`sync`, identity, host config) | Mac mini, laptop and VPS run the same vault without field repair | Small verb family, a committed vault id |
| 2 | Provenance, review state and supersession, minimal | Drafts and obsolete material stop looking authoritative | A few optional frontmatter fields |
| 2 | Remote MCP adapter with tiered surfaces | ChatGPT and Claude use the same core safely | Auth, hosting, an ADR amending principle 6 |
| 2 | Outcome-based install profiles that reuse `node join` | A fresh Mac mini or VPS becomes usable in one guided run | Platform packaging, systemd |

"Better" means measured results on your own work: fewer missed sources, fewer stale conclusions, fewer tool calls to resume, less configuration. It is not a leaderboard claim.

---

## 1. Verified gaps (code at `d7eb27e`)

v1's table, all confirmed, plus new rows (marked **new**).

| Verified behavior | Location | Consequence |
|---|---|---|
| Default content root is `wiki/`. The module docstring says "wiki/tasks/journal" | `bin/lib/indexlib.py:34` vs `:5` | Tasks and journals are invisible to `search` |
| Embeddings use the first 2,000 chars per note, one vector per note | `indexlib.py:169`, `:270` | Long documents are poorly represented semantically |
| Rerank reconstructs the first 1,200 chars per candidate | `indexlib.py:87` | A late relevant passage never reaches the reranker |
| Query tokens are `[A-Za-z0-9]+` | `indexlib.py:294` | **"ügyfél szerződés" becomes `gyf OR szerz` and returns 0 hits** against a note containing those words (reproduced) |
| `test/hu_queries.txt` exists but tests cross-lingual vector retrieval | `test/` | **new:** the Hungarian lexical bug was masked by a test that only exercises vectors |
| `open --json` / `wiki open --json` return a path, never content | `bin/open/run.py:129`, `bin/wiki/run.py:155` | No verb returns a note body. Remote clients cannot read |
| `wiki new` cannot take a body, and doesn't update the index | issue #17 | Agents write bodies around the verb and break frontmatter (`aliases: []---`) |
| **new:** No write verb refreshes the index; only the hourly `index` job does | `jobs/registry.json` | A just-captured or just-created note is unsearchable for up to 60 min |
| MCP exposes a free-form `args` array whose own description cites `--yes` | `cli/src/core/mcp.ts:318-321` | Too broad for any hosted endpoint |
| **new:** MCP sets no tool annotations, no `outputSchema`, and advertises protocol `2024-11-05` | `mcp.ts` | ChatGPT treats every tool as a write and asks for confirmation; spec is two revisions behind |
| `setup search` reports "operational" while `PLAINKEEP_VECTORS`/`RERANK` are advisory exports | `bin/lib/setuplib.py:312-329` | **new evidence:** this machine carries a hand-written `~/.config/plainkeep/env.sh` that no plainkeep code reads; agent and SSH shells don't see it (#16 §4) |
| Optional layers pip-install ranges, not the shipped `uv.lock` | `setuplib.py:219` | "Pinned install" is only true for the engine |
| `orient.backup_age_min` is the last git commit age, printed as "backup" | `bin/orient/run.py:51,80,111` | Version history looks like backup health |
| Concurrent `files ingest` loses shadow notes (15/16 survived) | `docs/followups.md` item 4 | Must be fixed before any remote write |
| `files distill` sends the first 8,000 chars | `bin/files/run.py:646` | Long reports distill partially, silently |
| Duplicate detection compares all title pairs | `bin/organize/run.py:277` | Quadratic: 5×10⁹ pairs at 100k notes |
| Graph expansion happens before the vector arm | `indexlib.py:369-378` | Semantic-only hits never seed link expansion |
| **new:** `.plainkeep/` is gitignored, so each clone mints a new vault id | #16 §3 | Two machines on one remote don't know they share a vault |
| **new:** In-vault skill links are committed as absolute paths | #16 §7 | `doctor` fails on every other machine |
| **new:** No `sync` verb exists (proposed 2026-07-03, still absent) | #16 §8 | Phone capture and second machines depend on hand-run git |
| **new:** The manual's §3 startup ritual does not mention `orient`; §6 never lists `share` | `skills/operate-plainkeep/SKILL.md` (21 KB) | Agents grep and read `plainkeep.json` instead of calling one verb (#15) |
| **new:** `architecture.md` still draws `~/plainkeep` with `bin/` inside the vault | `docs/architecture.md:41` | Documentation contradicts ADR-014/017 |
| **new:** Wikilinks escaped as `[[slug\|label]]` in tables resolve to `slug\` | #17 | Table links silently break |
| **new:** `[[mm:ss]](url)` timestamp links exist in vault notes | found in this session | Obsidian creates empty notes on click; no doctor check catches it |

---

## 2. Stage 0 — cheap fixes that ship first

Each item is small, independent, and testable, and none needs a new verb. Together they remove most day-to-day friction.

| # | Fix | Shape | Test |
|---|---|---|---|
| 0.1 | **Unicode query tokens** | `re.findall(r"\w+", q.lower())` (Unicode by default). FTS5's default `unicode61` tokenizer already folds diacritics on the index side, so `ügyfél` and `ugyfel` both match | Monolingual HU fixtures: `ügyfél`, `szerződés`, `ügyfelek` |
| 0.2 | **Hungarian inflection** | Add prefix matching for tokens ≥5 chars (`"ügyf"*`-style, from the stem candidate) as a second, lower-weighted lexical arm. Do not add a stemmer dependency | `ügyfelek`/`ügyfélnek` find a note that says `ügyfél` |
| 0.3 | **Index all content collections by default** | Index `wiki/`, `tasks/`, `journal/`, `inbox/` under one vault root. Each hit carries `collection` | A task-only fact is found |
| 0.4 | **Index on write** | Every `safe_write` verb that creates or edits a note re-indexes that file's lexical rows before returning. Embeddings queue for the next `index` run and are reported as pending | `capture` then `search` finds it without `plainkeep index` |
| 0.5 | **Host config instead of exports** | The dispatcher reads `~/.config/plainkeep/config.toml` (or `.json`). Precedence: CLI flag → env → config → default. `setup search --yes` writes `search.vectors = true`. Old env vars keep working | A non-interactive SSH shell gets the same search as the terminal |
| 0.6 | **Honest backup age** | Rename the field to `last_commit_age_min` and add `backup_last_ok` from the backup state. Keep `backup_age_min` as a deprecated alias for one major version (the envelope rule) | `orient` stops printing "backup" for a commit |
| 0.7 | **Manual: one start step** | §3 becomes "run `plainkeep orient`". Add a recipe table to §6: find, capture, file a document, file a finished note (`wiki new --body-file`), share (`search` → `share <slug> --yes` when the human asked) | A cold agent asked to "share the OMAD note" makes ≤2 calls (#15 acceptance) |
| 0.8 | **MCP annotations from risk class** | `read` → `readOnlyHint: true, destructiveHint: false, openWorldHint: false`. `safe_write` → `destructiveHint: false`. `confirm` → `destructiveHint: true`. This is free, and it helps local clients too | ChatGPT stops prompting on reads |
| 0.9 | **Wikilink escape** | Treat `\|` as a literal pipe in the resolver | `[[slug\|label]]` in a table resolves |
| 0.10 | **Doctor link lint** | Warn on `[[x]](url)` and on committed absolute symlinks that name another home directory | Both cases in this vault are flagged |
| 0.11 | **Doc drift** | Redraw `architecture.md`'s roots for the engine/vault split; fix the mobile guide's contradiction | — |

**Why first:** 0.1–0.4 fix retrieval for the language you actually write in. 0.5 and 0.7 remove the two biggest sources of agent and operator confusion. None of these changes any principle.

---

## 3. Knowledge structure: a small durable model

Unchanged from v1 in substance. The existing objects already cover the model: source asset, extract, note, entity/project hub, task, journal/timeline, extension record.

A project is a relationship among objects, not a folder. One canonical note, linked from wherever it matters.

### Provenance, review state, and supersession — minimal

Authorship is not a truth score. Add only fields that change behavior, all optional:

| Field | Meaning | Who sets it |
|---|---|---|
| `status` (existing) | `draft` / `active` / `archived`; drafts are labeled in search and excluded from briefings by default | verbs, human |
| `supersedes: [[slug]]` / `superseded_by: [[slug]]` | Explicit replacement of a decision or note | human or a reviewed `organize` op |
| `effective` | When a decision or policy took effect, distinct from `created`/`updated` | optional |
| `source_sha256` (existing on extracts) | The revision a derived note was built from | verbs |

Rules:
- A source change marks dependent extracts and syntheses **needs review**. It does not mark them false.
- A superseded note is **demoted in ranking, never hidden** (gbrain demotes `archive/` 0.5× rather than filtering). Historical questions still find it.
- Formatting-only edits don't trigger review. Compare body hashes, not mtimes.
- Do not add confidence scores, expiry dates, or a claim micro-syntax. Basic Memory's `- [category] fact` grammar is a second language humans stop maintaining.

### Identity — deferred, not dropped

v1 put opaque IDs in Stage A. Slugs are already globally unique (enforced since ADR-004), and Obsidian already rewrites links on rename (`alwaysUpdateLinks: true` in the vault's pack). IDs become necessary when **remote edits and revision-bound citations** arrive, so they ship with that stage (§9, Stage E).

When they come, use Basic Memory's lesson: a `permalink`-style ID in frontmatter that survives `move`, while files and links stay readable.

### Atomicity

Split a note out when it has its own title, evidence, or lifecycle. Keep coherent documents whole and retrieve their **sections**. Do not generate one page per heading. The heading-outline distill fallback is extraction, not synthesis, and should be labeled as such.

### Page shape for hubs

Plainkeep's hubs already follow gbrain's "compiled truth above, timeline below" shape. Make it count in retrieval: the summary section of a hub ranks above its timeline section for "what is true now" queries.

---

## 4. Retrieval: complete, bounded, explainable

Keep SQLite FTS5 plus LanceDB plus the optional cross-encoder. No backend migration. Every surveyed project that works well uses this same shape: files canonical, hybrid BM25 + vector with RRF k=60, an optional reranker.

### Chunking

- **Heading-aware sections**, the existing FTS unit, now shared by FTS and vectors.
- Split a long section at the best break point near ~900 tokens with ~15% overlap. Score break candidates by heading level > code fence > blank line (qmd's method).
- **Never split a code fence, table, or list item.**
- Embed `title › heading breadcrumb | text`. The breadcrumb gives a section its context without re-embedding the whole document.
- Each section row stores collection, path, heading path, `start_line`–`end_line`, token count, body hash, and plane/status.
- Embedding identity is source hash + model + prompt profile + dimensions + chunker version. Changing any of them builds a new index generation, so vectors never mix.

### Pipeline

1. **Exact tier:** slug, title, or alias hits rank first. gbrain and qmd both protect exact matches.
2. Apply collection, plane, status, and (remotely) permission filters **before** taking top-k.
3. Lexical arm (Unicode tokens + prefix arm) and vector arm, fused with RRF at page grain, keeping the best section per page.
4. One-hop link expansion seeded from the **fused** candidates (fixes the ordering bug). Penalize high-degree hubs.
5. Optional rerank on the **actual section text**. Blend by position so the reranker can't bury exact matches: ranks 1–3 are 75% retrieval score, 4–10 are 60%, the rest 40% (qmd).
6. Return results with collection, `path:start-end`, heading, snippet, token count, plane/status labels, and the mode actually used (`keyword` / `hybrid` / `hybrid+rerank`).

**Do not** turn on LLM query expansion by default. gbrain measured equal-weight expansion dropping LongMemEval recall@5 from 93% to 55%.

### Make "find" and "prepare context" different operations

- `search` stays broad and transparent: drafts, history and inbox are discoverable and labeled.
- A **briefing** mode on `orient` (`orient --project <slug> --query "<task>" --budget 2400`, new syntax) assembles the project hub summary, applicable decisions, active tasks, top passages, and optional extension memory. It is deterministic; the client's model synthesizes.
- Every bounded response says what was omitted and the exact budget that would fit ("raise `--budget` to 3100 or narrow the query", Graphify's message).
- "No result" must be distinguishable from "index stale" and from "semantic stage unavailable".

### Freshness

- Lexical rows update on every managed write (Stage 0.4). Embeddings are queued; status reports `pending: N`.
- External edits (Obsidian) are caught by the existing hourly `index --changed`. A watcher is optional acceleration, never the correctness mechanism (Lacuna's mandatory daemon is the counter-example).

### Measure it

Adopt qmd's `bench` idea as `test/` fixtures: a query plus `expected` paths, scored with recall@k and MRR across `keyword`, `hybrid`, and `hybrid+rerank`.

Seed it with about 30 of your real questions, including **monolingual Hungarian**, deep-in-document, task-only, superseded-decision, and unanswerable cases. Run it before and after each retrieval change. Keep a layer only if it shows a reproducible gain.

---

## 5. The content contract: read and write bodies through verbs

Extend existing verbs. Add no new ones, and remove no spellings.

| Capability | Proposed shape | Borrowed from |
|---|---|---|
| **Read content** | `wiki open <slug> --content [--section "<heading>"] [--lines a:b] [--max-tokens N] --json` returns body, frontmatter, `rev` (body hash), `path:start-end`, and token count. A trailing nav block lists sections with token counts, links in/out, and backlinks | qmd `get`, Lacuna nav panel |
| **Create with body** | `wiki new <type> "<title>" --body-file PATH \| --body -` (issue #17). The verb owns path, slug, frontmatter and journal line; it indexes on success | #17 |
| **Duplicate advisory** | On create, return `possible_duplicates: [...]` (title + vector probe) as a question, never a refusal | Basic Memory |
| **Edit** | `wiki edit <slug> --append \| --prepend \| --replace-section "<h>" \| --find X --replace Y [--expect-count N] --expect-rev <rev>`. Refuses on stale `rev` (exit 4 with current rev) or count mismatch. `$EDITOR` stays the default with no flags | Basic Memory `edit_note` |
| **Ambiguity** | A name that matches more than one note returns candidates (exit 4, did-you-mean). It never picks one for a write | Graphify, existing exit-code protocol |

Every write re-enters the dispatcher, so the guardrail, path-wall, `.logs/`, journal and git are unchanged. Frontmatter is always written by the verb, which removes the `aliases: []---` failure class.

---

## 6. Agent experience

- **Bootstrap, not manual.** Cut the always-loaded skill to roughly 4 KB: selected vault, the five hard rules, a recipe table (find, capture, file, share, task, resume), and "run `orient` first". Keep the full manual as on-demand reference. Generate the verb list from `plainkeep.json` rather than duplicating it in prose.
- **The happy path is the documented path.** "Human asked for it" counts as consent for that one confirm-class action (#15). Keep the gate for unsolicited transmission, `share init`, and deploys.
- **Errors carry the next call.** This exists already (exit 3/4/5). Extend it to budgets and stale revisions.
- **Install delivers instructions with tools.** This is already done (`setup agents`). Every surveyed project learned that a tool's existence alone doesn't change agent behavior.
- **Triage model default (#8).** Make `gemma4:e2b` the Ollama default for classification, kept separate from the enrich model. Heuristics remain the fallback.

---

## 7. One vault across machines

This section is new. The Mac mini + VPS + laptop plan depends on it, and issue #16 records the cost of not having it.

**Model:** one authoritative writer per vault at a time. Git is the only transport. There is no multi-master sync.

| Piece | Shape | Risk class |
|---|---|---|
| **Vault identity in git** | Commit `.plainkeep/vault.json`'s id field (keep machine-local registry state ignored). A clone is recognisably the same vault, and the registry name is local and free | — |
| **`sync`** | `sync` = `git pull --ff-only` + `index --changed`. Refuses on divergence with the fix spelled out. `sync --push` commits the vault's own changes and pushes | pull: `safe_write`, schedulable every 15 min; push: `confirm` |
| **`node status [--peer host]`** | Engine version, vault remote, HEAD, dirty/unpushed, default-vault path, effective config, models, UI version, skill-link targets | `read` |
| **`node join <remote> --yes`** | Install or switch the engine to the peer's version; clone and register the vault as default (never the template checkout); write host config; provision the profile; install agent skills; leave schedules as a per-node choice | `confirm` |
| **Doctor** | Fails when the default vault is a template checkout, when a committed link names another home, or when the peer is ahead and dirty (with `--peer`) | — |
| **Skill links** | Stop committing in-vault skill links. `setup agents` already installs user-level links on each machine | — |

The **setup profiles in §8 are `node join` without a peer**. One planner serves both cases.

---

## 8. Installation and operation: one path, progressive control

v1's profile model holds. Changes:

| Profile | Adds |
|---|---|
| **Minimal** | Core, Git-backed vault, lexical search (now Unicode and all collections) |
| **Recommended** | + EmbeddingGemma (pinned), section vectors, lightweight PDF/web extraction, agent skills, Obsidian pack, `sync` job |
| **Server** | + MCP adapter (Reader), chosen connection, supervised services, maintenance jobs. **No** start/close journal automation on a headless server |

- **The planner shows one plan before acting:** packages, model download size, services, network exposure, credentials still needed. Then it executes and prints `ready / degraded / blocked` per capability.
- **Optional layers provision from the shipped `uv.lock`**, not pip ranges (closes the v1 gap). Plugin overlays stay separate.
- **Model readiness is real.** A failed pull fails the layer, names the minimum Ollama version, and does not exit 0 (#16 §5). The UI layer uses the pinned release repo, not a git remote on a sealed engine (#16 §6).
- **Obsidian pack defaults:**
  - Daily notes point at `journal/` with format `YYYY/MM/YYYY-MM-DD`.
  - Templates point at `templates/`.
  - Excluded files hide engine leftovers.
  - Starter Bases: Today, Inbox, Projects, Review.
- **Targets:** qualify Apple Silicon macOS first, then Ubuntu LTS x86_64 with systemd units generated from the existing job registry.
- **Health distinguishes:** core OK, lexical index current, semantic coverage (complete / pending N), remote endpoint reachable and authenticated, last successful backup, last verified restore. A commit is not a backup.

---

## 9. Remote MCP: an adapter with a narrow trust boundary

### Principle 6 needs an ADR, not a footnote

Architecture principle 6 says "no daemon, no HTTP server, ever". The July remote proposal already established the reading that **plainkeep never becomes a server, and a long-lived caller outside it is allowed** (the Telegram bridge, launchd).

Write that down as **ADR-023**:
- The adapter is a separate frontend process.
- It is stateless apart from auth.
- It re-enters `plainkeep <verb>` for every call.
- It owns no index and no write path.
- It is optional, and absent from Minimal and Recommended.

### Tiered surfaces, enforced at dispatch

| Surface | Tools | Default |
|---|---|---|
| **Local (existing)** | Every verb, as today, for stdio clients on the machine | unchanged, plus annotations (Stage 0.8) |
| **Reader** | `search`, `fetch` (OpenAI's schema: `search(query)` returns `{results:[{id,title,url}]}`; `fetch(id)` returns `{id,title,text,url,metadata}`), `open_section`, `briefing`, `status` | remote default |
| **Capture** | Reader + append-only `capture` and `bookmark` (no URL fetch at first) | opt-in |
| **Editor (later)** | Proposals only; apply requires a human-bound approval of the exact patch and revision | after Stage E |

- **Typed schemas** are generated from `plainkeep.json` actions. There is no free-form `args`, `--yes`, vault switch, plugin, setup, job, or model action.
- **Enforcement happens in the adapter's dispatch**, not just in the tool list (gbrain's lesson). Annotations are hints, never the guard.
- Declare `outputSchema` and return `structuredContent` plus the same JSON as text.
- Speak spec **2026-07-28** (stateless; `server/discover`; `Mcp-Method` and `Mcp-Name` headers). Keep accepting `initialize` from older clients.
- Validate `Origin`, bind to 127.0.0.1, and sit behind the auth layer.

### Connection facts (verified 12 Oct 2026)

| Client | Requirement | Implication |
|---|---|---|
| ChatGPT | OAuth (RFC 9728 metadata, S256 PKCE, `resource` → `aud`, CIMD or DCR, refresh tokens) **or no auth**. No static bearer. Business/Enterprise/Edu get full MCP (web). **Pro gets read/fetch only, in developer mode.** Plus is not listed | Reader is exactly what Pro can use; check your plan before building more |
| ChatGPT via Secure MCP Tunnel | Outbound-only client, can wrap the **stdio** server directly. OpenAI-only. Any OAuth server must still be public | Lowest-effort first step for ChatGPT from a Mac mini; plan availability unverified |
| Claude (claude.ai / Desktop / mobile) | Connects from Anthropic's cloud, so the URL **must be public**. OAuth or fixed header credentials | Needs Funnel, Cloudflare Tunnel, or VPS ingress |

### Recommended topologies

- **Mac mini, ChatGPT only:** Secure MCP Tunnel wrapping the stdio adapter, run as a dedicated read-only user. No inbound ports. "No auth" is acceptable only because the tunnel binds to your workspace, and anyone who can drive that ChatGPT account can read the vault. Say so in the setup plan.
- **Mac mini or VPS, ChatGPT + Claude:** Cloudflare Tunnel (custom domain) or Caddy on the VPS → an OAuth gateway (`sigbit/mcp-auth-proxy` with a one-address GitHub/Google allowlist, or a hosted Auth0/Stytch tenant) → adapter on 127.0.0.1. Optionally allowlist OpenAI and Anthropic egress IP ranges. Tailscale Funnel is acceptable for personal use (no custom domain, unpublished bandwidth caps).
- **VPS data:** the VPS holds a `sync`'d clone. If the Mac mini is the writer, the VPS is Reader-only. Promote the VPS to writer only explicitly (§7).

### Safety requirements (unchanged from v1, still mandatory)

- Reader first, scoped to one vault.
- Permission filters are applied before snippets, counts, links, or errors.
- Retrieved text is data, never instructions.
- A model-supplied `--yes` is not human approval, so no confirm-class action is exposed remotely.
- No untrusted plugins over remote.
- Fix the ingest race and add idempotency keys (bound to principal + action + payload, with durable receipts) **before** remote Capture.

---

## 10. References: what to adopt and what to decline

These are design references, not benchmarks run on Plainkeep. Each repo was read in October 2026.

| Reference | Adopt | Decline |
|---|---|---|
| **qmd** | `path:from:count` addressing; position-aware rerank blend; per-collection context strings; `bench` fixture format; Origin/Host checks on HTTP | Silently ignoring unknown params; LLM query expansion on by default; unauthenticated HTTP as a remote story |
| **Basic Memory** | `edit_note` primitives with count guard + checksum precondition; duplicate advisory on create; `move` keeps a stable permalink; annotations on every tool; `search`/`fetch` for ChatGPT; n-gram lexical channel for scripts without spaces | Observation/relation micro-syntax; auto-update and promo telemetry |
| **Graphiti** | Optional `effective` date; invalidate-not-delete; rank by link distance from a focus note | LLM in the write path of every ingest; graph DB as truth |
| **PageWiki** | Explicit per-query token budget; ToC/outline as a cheap first read; cite `file#section` | LLM-navigated retrieval as the primary path (60–300 s per query) |
| **Graphify** | `EXTRACTED` vs `INFERRED` labels on links and suggestions; "raise budget to N" truncation messages; refuse on ambiguity with candidates | Treating inferred edges as retrieval truth |
| **Karpathy LLM Wiki** | Raw sources immutable; "keep this answer" as an explicit action; lint for stale and orphan claims (Plainkeep has `organize`) | "The LLM owns the wiki"; many-page rewrites per ingest |
| **Lacuna** | Token counts on every hit and section; nav footer on page reads; "stale = checked before the newest source arrived" | A mandatory daemon; one overloaded tool |
| **Supermemory / SMFS** | Derived siblings with visible error siblings (Plainkeep's `.extract.md` already matches); `path:start-end` hits; a generated read-only profile/overview as a cheap first read | Hijacking `grep`; opaque cloud memory as a second truth |
| **gbrain** | Compiled truth + timeline weighting; archive demotion; exact/alias tier; tiered MCP surfaces enforced at dispatch; `fix.next` on errors; measured rejection of query expansion | 85-tool sprawl; DB-only state outside files |

**Patterns in 3 or more projects that Plainkeep already has:** files canonical with a rebuildable index, content-hash skip, hybrid + RRF, skills delivered at install.

**Patterns Plainkeep lacks:** section-level vectors, bounded reads with token counts, supersession, a multilingual lexical layer, typed tiered MCP.

---

## 11. Extension boundary: Project Context

Unchanged from v1. Do not merge the repos. A project can reference a Project Context ledger. The briefing composes its bounded, provenance-bearing handoff with Plainkeep's knowledge and tasks. Writes into the vault still go through the dispatcher. Each tool works without the other. Build this one integration before any generic provider interface.

---

## 12. Delivery order and gates

| Stage | Deliver | Gate |
|---|---|---|
| **0: Quick fixes** | §2 items 0.1–0.11 | HU monolingual fixtures pass; capture→search works without `index`; agent "share" recipe ≤2 calls; contract tests green |
| **A: Content contract** | §5: `wiki open --content/--section/--lines`, `wiki new --body-file`, `wiki edit` primitives with `--expect-rev`; ingest race fixed | Stale or ambiguous writes refuse; frontmatter never corrupted; existing JSON envelopes compatible |
| **B: Retrieval** | §4: section chunks for vectors and rerank, exact tier, fused graph seeding, labels, bench fixtures | Recall@k up on deep-document and HU sets; no exact-title regression; lexical-only mode intact |
| **C: Multi-machine** | §7: committed vault id, `sync`, `node status/join`, host config, doctor checks | Fresh laptop joins and comes back green; doctor fails on #16's failure cases |
| **D: Remote Reader** | ADR-023; Reader surface; tunnel or OAuth gateway | Real ChatGPT and Claude clients search, fetch and cite; denied content never leaks; no confirm action reachable |
| **E: Remote Capture + identity** | Idempotency, receipts, stable IDs, revision-bound citations | Concurrent and retried writes keep every item; renames keep citations |
| **F: Profiles + maintenance** | §8 planner (reusing `node join`), systemd, review-state/supersession ranking, reviewed hub refresh, Project Context integration | Fresh-host, reboot, update/rollback, and restore scenarios pass |

Stages 0 and A are independent and can run in parallel. C should land before D, because a VPS Reader needs `sync`.

### Release gates (from v1, kept)

- Every citation resolves to its reported revision, or says it can't.
- No unauthorized content in results, snippets, counts, or expansions.
- Zero silent overwrites in the tested concurrent-write scenarios.
- Model or semantic failure is visible and never disables capture or lexical search.
- Each retrieval layer is measured separately; drop any layer that doesn't pay for itself.

---

## 13. Explicitly rejected

- Autonomous wiki rewriting.
- A mandatory graph database or daemon.
- LLM query expansion by default.
- Saving every answer.
- One file per sentence.
- A new editor.
- A second memory store holding truth.
- Multi-master sync.
- Static-key auth for ChatGPT, which it doesn't support anyway.
- Exposing `--yes` or any administration action remotely.

---

## Open questions for the owner

1. **Which ChatGPT plan is the target?** Pro means remote is Reader-only by platform rule, which matches Stage D exactly. Business would allow Capture later.
2. **Which machine is the writer?** The Mac mini (with the VPS as Reader) is the simpler default.
3. **Should Claude reach the vault remotely too?** If yes, a public HTTPS endpoint with OAuth is required, and the tunnel-only path is not enough.

## Sources

- Plainkeep at `d7eb27e`:
  - Code: `bin/lib/indexlib.py`, `bin/open/run.py`, `bin/wiki/run.py`, `cli/src/core/mcp.ts`, `bin/lib/setuplib.py`, `bin/orient/run.py`, `bin/files/run.py`, `bin/organize/run.py`, `jobs/registry.json`
  - Docs: `docs/followups.md`, `docs/architecture.md`, `docs/DECISIONS.md`, `docs/design/proposals/2026-07-03-remote-and-mobile-operations.md`
  - Issues #8, #15, #16, #17
- MCP spec 2026-07-28: changelog, transports, tools; authorization 2025-11-25
- OpenAI: secure-mcp-tunnels, plugins/build/auth, plugins/build/mcp-server, api/docs/mcp, Help Center "Developer mode and MCP apps"
- Claude Help Center: custom connectors with remote MCP
- Tailscale Funnel KB; Cloudflare MCP authorization; `cloudflare/workers-oauth-provider`; `sigbit/mcp-auth-proxy`
- Reference repos: `tobi/qmd`, `basicmachines-co/basic-memory`, `getzep/graphiti`, `leecg39/pagewiki`, `Graphify-Labs/graphify`, Karpathy gist `442a6bf5…`, `Labhund/lacuna-wiki`, `supermemoryai/supermemory` (+ SMFS), `garrytan/gbrain`
