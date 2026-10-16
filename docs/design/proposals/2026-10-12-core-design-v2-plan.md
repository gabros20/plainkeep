# Implementation plan — core design v2

Plan · 12 October 2026 · base commit `d7eb27e` (engine 4.0.8)
Design: [`2026-10-12-core-design-v2.md`](2026-10-12-core-design-v2.md). Section numbers like "§4"
refer to that document. Step IDs (`0.1`, `A2`, …) are stable, so use them in branch names, commits
and CHANGELOG entries.

## How to use this plan

- **Execute steps in order inside a stage.** Stages run in the order given in "Stage order" below.
  A step is done only when its **Done when** line holds and `python3 test/run_all.py` is green.
- **One step is one branch and one PR** (`feat/v2-0.1-unicode-tokens`). Small steps that touch the
  same file may share a PR when this plan says so.
- **Every step follows the same rules:**
  - Add a `CHANGELOG.md` `[Unreleased]` entry.
  - Update `bin/<verb>/cmd.json`, `plainkeep.json` and completion whenever the surface changes.
    The contract stays `plainkeep.json/3`: new fields are **additive only**, and a renamed field
    keeps its old name as a deprecated alias until the next major version.
  - Write an ADR whenever this plan marks one. Numbers are assigned when each ADR is written,
    starting at ADR-023. The design calls the principle-6 amendment "ADR-023"; it takes whatever
    number is next when D0 lands.
  - Dogfood each change on the real vault by reinstalling the engine
    (`python3 bin/lib/enginetree.py --install . --force`) and running the step's check against
    `~/plainkeep`.
- **Release at the end of each stage.**

  | Stage | Release |
  |---|---|
  | 0 | 4.1.0 |
  | A | 4.2.0 |
  | B | 4.3.0 |
  | C | 4.4.0 |
  | D | 4.5.0 |
  | E | 4.6.0 |
  | F | 4.7.0 |

  Deprecated aliases are removed at 5.0.
- **Stop and decide** at each ◆ marker. Each one is a decision this plan does not make.

## Stage order and dependencies

```
0 (quick fixes) ──┬─► A (content contract) ──┐
                  ├─► B (retrieval)          ├─► E (remote capture + identity) ─► F (profiles, maintenance)
                  └─► C (multi-machine) ─► D (remote Reader) ┘
```

- **Stages A, B and C** depend only on Stage 0, so they can overlap if two people or agents work in
  parallel. Run serially, the order is A, B, C.
- **Stage D** needs C, because a VPS reader needs `sync`. It also needs A1 (`fetch` reads bodies).
- **Stage E** needs both A and D.

---

## Stage 0 — quick fixes (§2)

Ordering rationale: build the measuring stick first (0.0), make the test harness safe to run in
parallel (0.14), then fix retrieval (0.1–0.4), then fix
correctness and linting (0.9, 0.10, 0.6, 0.12, 0.13, 0.15), the consent flag (0.16), then
configuration (0.5), then MCP (0.8), then
docs last (0.7, 0.11), so the docs describe the finished state.

### 0.0 Retrieval bench harness
- **What:** Add `test/bench/queries.json` (JSON, stdlib only — decision D1) with entries of the form `{query, expected: [paths],
  tags}`. Add `test/run_bench.py`, which reports recall@5, recall@10 and MRR per mode (`keyword`,
  `hybrid`, `hybrid+rerank`). Modes whose dependencies are missing are skipped.
- **Seed set:** about 30 queries over a fixture vault in `test/fixtures/bench/`:
  - 8 monolingual Hungarian queries, including inflected forms (`ügyfelek`, `ügyfélnek`)
  - 5 queries whose answer is deep inside a long document
  - 4 task-only and 3 journal-only queries
  - 3 queries whose correct answer is a superseded decision
  - 3 unanswerable queries
  - 4 exact title or alias queries
- **Real queries:** add a `--vault` flag so the same harness runs against `~/plainkeep` with a
  private, git-ignored query file.
- **Placement:** `run_all.py` runs the bench in report mode (no threshold) until 0.1 lands. After
  0.1, the keyword-mode Hungarian subset becomes a gate.
- **Done when:** the bench runs and records the baseline. The expected baseline is roughly 0 on
  the Hungarian lexical subset and partial on the task and journal subsets.

### 0.14 Test harness hygiene (follow-up, found while orchestrating 0.0/0.1)
Do this next, right after 0.0 and 0.1. It is what made parallel agent runs pile up.
- **Bug 1 — no timeout, orphaned children:**
  - `test/run_all.py:156` runs each suite with `subprocess.run(...)`, which has no timeout.
  - Stopping `run_all` leaves the suite it was running alive as an orphan.
  - On 2026-10-12, two abandoned `run_all` runs and their `run_migrate.py` children kept running
    for 14 minutes. Nothing would have stopped them.
- **Bug 2 — environment leaks into the suites:**
  - `test/lib/hermetic.py` `seal()` isolates the config home, launchd and the agent home, but not
    `PLAINKEEP_VECTORS`, `PLAINKEEP_RERANK`, `PLAINKEEP_CONTENT`, `PLAINKEEP_ENRICH_MODEL` or
    `PLAINKEEP_AGENT_MODEL`.
  - Example: with your shell's exports, `python3 test/run_search_impl.py` fails its
    `wikilink-graph` check when run alone, but passes under `run_all`.
- **What:**
  - **Per-suite timeout.** Each suite gets a timeout: a default of 600 s, overridable per suite in
    `SUITES`, and globally with `PLAINKEEP_TEST_TIMEOUT`. A timeout counts as a FAIL with the
    suite name and the elapsed time.
  - **Clean shutdown.** Start each suite in its own process group
    (`start_new_session=True`). On timeout, Ctrl-C or SIGTERM, `run_all` stops the whole group,
    first with TERM and then with KILL after 5 s. `run_all` installs the SIGTERM/SIGINT handler
    that does this.
  - **One run per checkout.** `run_all` takes a lock (`fcntl.flock` on a file under
    `test/.run_all.lock`, git-ignored). A second concurrent run in the same checkout exits 2 with
    the holder's PID. Separate worktrees stay independent.
  - **Environment isolation.** `seal()` pops the search and model variables listed above. Suites
    that need them (`run_search_vec.py`, `run_bench.py` hybrid modes) set them explicitly after
    `seal()`.
- **Tests:** a meta-suite case with a fixture suite that sleeps forever. `run_all` with a 2 s
  timeout reports FAIL, and no process from that suite survives. Two concurrent runs: the second
  refuses. `run_search_impl.py` passes with `PLAINKEEP_VECTORS=1 PLAINKEEP_RERANK=1` exported.
- **Done when:** the meta cases pass, and every suite passes both standalone and under `run_all`
  with your real shell environment.

### 0.1 Unicode query tokens
- **Where:** `bin/lib/indexlib.py:293-295` (`_fts_query`).
- **What:** Change the token regex to `re.findall(r"\w+", q.lower())` and keep the `len > 1`
  filter. Check every other `[A-Za-z0-9]` tokenizer in `bin/lib` (snippets, logging) and fix
  each one the same way.
- **Tests:** Add a unit test in `run_search_impl.py` covering `ügyfél szerződés`, `ugyfel` and
  `szerződés`.
- **Done when:** the Hungarian bench subset reaches recall@5 ≥ 0.6 in keyword mode, and the
  English bench does not regress.

### 0.2 Hungarian inflection prefix arm
- **Where:** `indexlib.py`, in the lexical arm of `search()`.
- **What:**
  - For each token of 5 or more characters, derive the stem candidate `tok[:max(4, len(tok)-3)]`.
  - Run a second FTS query, `"stem"*` OR-joined.
  - Fuse it into the existing RRF at **half weight**, so exact-form hits still outrank prefix hits.
  - Add no stemmer dependency.
- **Tests:** `ügyfelek` and `ügyfélnek` find a note containing `ügyfél`. An exact-form query still
  ranks that note first.
- **Done when:** the Hungarian subset reaches recall@5 ≥ 0.85, and English MRR drops by no more
  than 0.02. ◆ If English drops more, restrict the arm to tokens containing non-ASCII letters, or
  to queries detected as Hungarian.

### 0.3 Index every content collection
- **Where:**
  - `indexlib.py:34` (`CONTENT`)
  - `index()`, `_ignored()`
  - the search result shape
  - `bin/search/cmd.json`
- **What:**
  - Default the root to the vault root, with an allowlist: `wiki/`, `tasks/`, `journal/`,
    `inbox/`. Store `collection` per row.
  - Add a `--collection` filter to `search`, applied **before** top-k.
  - Every hit carries `collection`, an additive envelope field.
  - Keep the `PLAINKEEP_CONTENT` override working.
- **Migration:** a schema change triggers a full rebuild through the existing rebuild rule. State
  this in the CHANGELOG.
- **Done when:** the task-only and journal-only bench queries pass, and `search --collection wiki`
  returns the old behavior.

### 0.4 Index on write
- **Where:**
  - New `indexlib.index_paths(paths)` for lexical rows only. Its embeddings are marked pending.
  - Called from the note-writing verbs: `capture`, `new`, `wiki new`, `task`, `bookmark`,
    `files ingest` (shadow notes), and `close`/`start` journal writes.
  - `status` and `orient` report `pending_embeddings: N`.
- **What:** Refresh the written file's FTS rows before the verb returns. If indexing fails, warn
  and leave the write in place, because a write must never fail because of the index.
- **Tests:** in a fresh fixture vault, `capture` then `search` finds the capture with no
  `plainkeep index` in between.
- **Done when:** the test above passes, and the cost added per write is under 50 ms, measured.

### 0.9 Escaped pipe in wikilinks (same PR as 0.10)
- **Where:**
  - `indexlib.py:144` (`_norm_link`)
  - `bin/lib/paths.py:112-115`
  - `bin/lib/render.py:32`
  - `bin/lib/sharelib.py:19`
- **What:** Strip a trailing `\` before splitting on `|`. Better: centralise link parsing in
  `paths.py` so the four sites share it.
- **Tests:** add `[[slug\|label]]` inside a table to `run_wiki_edges.py`.

### 0.10 Doctor link lint
- **Where:** `bin/doctor/run.py`.
- **What:** Warn, not fail, on two patterns:
  - `[[x]](url)` in any note
  - a committed symlink whose target is an absolute path outside the vault and the engine
- **Done when:** a fixture with both patterns produces both warnings. `~/plainkeep` (whose 65 links
  are already fixed) is clean.

### 0.6 Honest backup age
- **Where:**
  - `bin/orient/run.py:51,80,109-111`
  - `bin/orient/cmd.json:25`
  - the backup state reader in `bin/backup/`
- **What:**
  - Add `last_commit_age_min` and `backup_last_ok`: an ISO timestamp, or null when backup is not
    configured.
  - Keep `backup_age_min` as a deprecated alias whose value equals `last_commit_age_min`.
  - Change the human output to say "last commit" and "backup", each truthfully.
- **Done when:** on a vault with no restic setup, `orient` says the backup is not configured.

### 0.12 Triage model default (#8)
- **Where:** `bin/lib/agent.py:11,42`.
- **What:** Change the default from `gemma3:1b` to `gemma4:e2b`, kept separate from the enrich
  default (`gemma4:e4b`). Update `setup models` so it pulls the new tag.
- **Done when:** heuristics remain the fallback when Ollama is absent, and the suite is green.

### 0.13 Accent-folding slugs (follow-up, found by the 0.0 architect)
- **Bug:** `bin/lib/paths.py:107-109` `slugify` keeps only `[a-z0-9]`, so accented letters are
  dropped, not folded: `Ügyfél szerződés` becomes `gyf-l-szerz-d-s`. Every verb that names a file
  from a title is affected: `wiki new`, `new`, `bookmark`, `triage`, `files`, `invoice`, `repo`,
  `filing.py`, and the plugin SDK (`bin/lib/api.py`).
- **What:**
  - Fold before filtering: NFKD-normalize, drop combining marks, then apply the existing
    `[a-z0-9]` rule. `Ügyfél szerződés` becomes `ugyfel-szerzodes`. Slugs stay ASCII, so file
    names and existing links are unaffected.
  - Fold `ő`/`ű` the same way (to `o`/`u`). Add explicit cases for letters NFKD does not decompose
    (`ß`, `ø`, `æ`, `ł`), or document that they are dropped.
  - **Never rename existing notes.** Only new slugs change. `doctor` gets an informational check
    listing existing slugs that a fold would have produced differently (for example `gyf-l`), so the
    human can choose to `move` them.
- **Compatibility:** `api.slugify` is part of the frozen plugin SDK (ADR-018). Changed output for
  non-ASCII input is a behavior change: note it in the CHANGELOG under "Changed". If the SDK
  contract forbids it, keep `api.slugify` on the old behavior and add `api.slugify_folded`.
  ◆ Decide at implementation time after reading ADR-018.
- **Tests:** a `run_new.py`/`run_wiki.py` case with `wiki new note "Ügyfél szerződés"` producing
  `ugyfel-szerzodes.md`. ASCII titles must produce byte-identical slugs to today's.
- **Why before B5:** the exact tier matches slugs, so Hungarian titles need folded slugs to hit it.

### 0.15 Disclose the result cap; exhaustive tag listing (#18, part 1)
- **Bug:**
  - `search` returns the top 10 (`indexlib.search(k=10)`) with no truncation notice, and
    `bin/search/run.py` has no limit flag.
  - In #18, a 35-note collection that all share one tag showed 10 notes. The operator could only
    conclude that the other 25 did not exist.
- **What:**
  - **`search --limit N` and `--all`.** `--all` removes the cut on the lexical arm: every FTS match
    is ranked, plus the fused vector and graph additions. It is not "a bigger k".
  - **Cap disclosure.** Human output ends with `showing 10 of ≥N — use --limit/--all` when
    truncated. JSON adds `returned`, `limit`, `truncated`, and `lexical_total` (an exact count of
    FTS-matching notes). Never present a vector candidate count as an exhaustive total.
  - **`wiki list --tag <tag>`.** Exhaustive, deterministic membership:
    - It matches whole normalized frontmatter `tags` values, with an optional leading `#`, and
      hyphenated tags count as one value.
    - It never matches body text or wikilinks, and a prefix or suffix of another tag does not
      match.
    - Results are sorted by slug, and each row has title, slug, type and path, with `--json`.
    - Reuse the tag-membership code from `wiki canvas '#tag'` (`bin/wiki/run.py`, `_canvas_model`).
  - Update `cmd.json`, `plainkeep.json`, completion, and the manual: use `wiki list --tag` for
    "everything in a collection", and `search` for discovery.
- **Tests:** #18 acceptance criteria 1–3, 6 and 7, on a fixture with 35 tagged notes, prose-only
  mentions, and similar-prefix tags. Add a bench query tagged `exhaustive`.
- **Why Stage 0:** it is cheap, it prevents a wrong conclusion ("only 10 exist"), and it needs no
  retrieval redesign. The filtered and exact parts of #18 are B10.

### 0.16 `--approved`: name the consent flag for what it means
Decided by the owner on 2026-10-16. `--yes` reads as "skip the prompt", but what it actually
asserts is that **a human approved this exact action**. `--approved` says that.
- **What:**
  - `--approved` becomes the canonical consent flag for every confirm-class action.
  - `--yes` and `-y` stay as permanent aliases, because spellings are never removed (contract
    rule).
  - The exit-3 remediation, `cmd.json`, `plainkeep.json`, completion, the manual, and every doc
    example switch to `--approved`.
  - The guardrail log and the journal line record which spelling was used.
  - Both guards change in the same PR, with a parity test:
    - Python: `bin/lib/guardrail.py`, plus the verbs that parse `--yes` themselves (≈19 files
      under `bin/`).
    - TypeScript: `cli/src/core/guardrail.ts:319,358`.
- **Semantics (unchanged, now explicit in the manual):**
  - An agent passes `--approved` only when the human asked for *that* action in this
    conversation (the 0.7 / #15 rule).
  - It never passes it for an action it decided on itself, a scheduled job never can, and the
    MCP surfaces never expose it (D1).
  - The flag records a claim and does not prove one. That is why the remote surfaces have no
    confirm actions at all.
- **Tests:** in both guards, `--approved` and `--yes` behave identically; `--approved` never clears
  a deny; and the exit-3 message names `--approved`.
- **Done when:** a grep of the docs and manual finds no remaining `--yes` example outside the
  alias note.

### 0.5 Host config file
- **Where:**
  - New `bin/lib/hostconfig.py`
  - `_vectors_on`/`_rerank_on` (`indexlib.py:58,83`)
  - `setuplib.py:312-329`
  - `enrichlib.py:25-26`
  - `agent.py`
- **What:**
  - Read `~/.config/plainkeep/config.toml` with stdlib `tomllib`.
  - Precedence: CLI flag → env → config → default.
  - Keys: `search.vectors`, `search.rerank`, `models.enrich`, `models.triage`, `ollama.host`.
  - `setup search --yes` writes `search.vectors = true` instead of advising exports, and
    `setup status` prints the **effective** value and its source.
- **Migration:** if `~/.config/plainkeep/env.sh` exists, `doctor` suggests deleting it once the
  config holds the same values. Never delete it automatically.
- **Tests:** a subprocess with a clean environment gets vectors on through the config alone.
- **Done when:** a non-interactive `ssh host plainkeep search …` gets hybrid mode.

### 0.8 MCP annotations
- **Where:** `cli/src/core/mcp.ts`, where tools are built from `plainkeep.json`.
- **What:** Map each verb's risk class to annotations:

  | Risk class | `readOnlyHint` | `destructiveHint` | `openWorldHint` |
  |---|---|---|---|
  | `read` | true | false | false |
  | `safe_write` | false | false | false |
  | `draft_only` | false | false | false |
  | `confirm` | false | true | — |

  Also set `openWorldHint: true` on verbs that fetch URLs (`bookmark`). Leave `PROTOCOL_VERSION`
  alone here; Stage D handles it.
- **Tests:** in `run_mcp_protocol.py`, `tools/list` carries annotations that match the risk
  classes.

### 0.7 Manual: one start step and recipes (#15)
- **Where:** `skills/operate-plainkeep/SKILL.md`.
- **What:**
  - §3 becomes "run `plainkeep orient`".
  - Add a recipe table to §6: find, capture, file a document, file a finished note, share, task,
    resume. The "file a finished note" recipe says `wiki new --body-file` once A2 lands; until
    then it shows the current shape.
  - State that "the human asked for it" counts as consent for that one confirm-class call.
- **Done when:** the operator simulation (`run_simulation.py`) solves "share the OMAD note" in ≤2
  plainkeep calls.

### 0.11 Doc drift
- `docs/architecture.md:41`: redraw the roots for the engine/vault split (ADR-014/017).
- Fix the contradiction in the mobile guide.
- `docs/README.md`: link this plan and the v2 design.

**Stage 0 exit:** all Done-when lines hold, the bench baseline is recorded against the post-Stage 0
numbers, and 4.1.0 is tagged.

---

## Stage A — content contract (§5)

### A1 `wiki open --content`
- **Where:**
  - `bin/wiki/run.py:155`
  - `bin/open/run.py:129` (`open` gets the same flags by delegation)
  - `bin/wiki/cmd.json`
- **What:**
  - Flags: `--content`, `--section "<heading>"`, `--lines a:b`, `--max-tokens N`.
  - The JSON adds `body`, `frontmatter`, `rev` (sha256 of the body, first 12 hex characters),
    `path`, `start_line`, `end_line`, `tokens`, `truncated`, and `nav` (sections with token counts,
    links out, backlinks).
  - Truncation names the budget that would fit.
  - Token counts use a shared `bin/lib/tokens.py` estimate (chars ÷ 4, documented as an estimate),
    which B1 and the briefing reuse.
- **Done when:** an agent can read any section of a long note without `cat`, and output without
  `--content` is byte-identical to today's.

### A2 `wiki new --body-file PATH | --body -` (#17)
- **What:**
  - The verb parses an optional frontmatter block in the body and merges it under verb-owned keys
    (slug, type, created). On a conflict, the verb wins and warns.
  - On success, the verb writes the journal line and calls `index_paths` (0.4).
  - Apply the same flags to `new`.
- **Tests:** the `aliases: []---` failure class from #17 cannot occur, and a body containing `---`
  lines survives intact.

### A3 Duplicate advisory on create
- **What:**
  - Probe by normalised title, and by vector when vectors are on.
  - Return `possible_duplicates: [{slug, title, score}]` in the envelope and as one human line.
  - Never refuse.
- **Done when:** creating "OMAD health system" again lists `omad-health-system`.

### A4 `wiki edit` primitives
- **What:**
  - Operations: `--append`, `--prepend`, `--replace-section "<h>"`, and `--find X --replace Y
    [--expect-count N]`.
  - All of them require `--expect-rev <rev>` from A1.
  - A stale rev exits 4 with `{current_rev}`, and a count mismatch exits 4 with the actual count.
  - With no flags, `$EDITOR` behaves as today.
  - Risk class: `safe_write`.
  - The edit is atomic: write a temp file, then rename.
  - Frontmatter is untouched unless `--set key=value` (◆ include `--set` now, or defer it?).
- **ADR (content contract):** verbs own frontmatter, edits are revision-guarded, and
  ambiguity refuses.
- **Done when:** concurrency tests (two edits from the same rev; the second refuses) are green.

### A5 Ingest race (followups item 4)
- **What:** Reproduce the loss with 16 concurrent `files ingest` runs, where 15 of 16 notes
  survive. Fix it by taking a per-vault `fcntl` lock around shadow-note creation and the index
  write, or with unique temp names and an atomic rename. Pick whichever the repro shows is the
  actual race.
- **Done when:** 16 of 16 survive across 20 runs.

### A6 Ambiguity refusals
- **What:** Every verb that resolves a note name for a **write** exits 4 with candidates when more
  than one note matches. Audit `bin/lib/resolver.py` callers.

### A7 Manual update
- Update the recipes in `SKILL.md` to the new flags.

**Stage A exit:** no stale or ambiguous write succeeds, frontmatter is never corrupted (fuzz with
`run_fuzz.py`), old envelopes stay compatible, and 4.2.0 is tagged.

---

## Stage B — retrieval (§4)

Run the bench before and after **every** step. Keep a step only if it pays for itself. Record the
numbers in the PR.

### B1 Heading-aware chunker
- **Where:** `indexlib.py:128` (`_chunks`), extracted into a new `bin/lib/chunker.py`.
- **What:**
  - Target about 900 tokens with about 15% overlap.
  - Score break points: heading level, then code fence end, then blank line.
  - Never split a code fence, a table or a list item.
  - Each chunk carries `heading_path`, `start_line`, `end_line`, `tokens`, `body_hash` and
    `chunker_version`.
- **Tests:** add table-driven unit tests covering a fence longer than the target, a giant table,
  and a document with no headings.

### B2 Index generations
- **What:**
  - The embedding identity is the tuple (source hash, model, prompt profile, dims,
    chunker_version).
  - Any change to the tuple builds a new generation alongside the old one and swaps atomically, so
    vectors from two generations never mix.
  - `status` shows the generation and its coverage.

### B3 Section vectors
- **Where:** `_embed_text` (`indexlib.py:169`) and the vector store (`bin/lib/vectorstore.py`).
- **What:**
  - Embed one vector per chunk, with the text `title › heading path | chunk`.
  - At query time, fuse the vector results at page grain and keep the best section per page.
  - Remove the 2,000-character limit.
- **Done when:** recall@5 on the deep-document subset improves, and the time for a full index of
  `~/plainkeep` is recorded.

### B4 Rerank on section text, with a position blend
- **Where:** `_candidate_texts` (`indexlib.py:87`) and `bin/lib/rerank.py`.
- **What:** Rerank the matched section's text, not the first 1,200 characters. The final score
  blends reranker and retrieval scores by retrieval rank:

  | Retrieval rank | Retrieval share | Reranker share |
  |---|---|---|
  | 1–3 | 75% | 25% |
  | 4–10 | 60% | 40% |
  | 11 and below | 40% | 60% |

### B5 Exact tier
- **What:** A slug, title or alias match ranks first regardless of fusion. Needs 0.13 so
  Hungarian titles have matchable slugs.
- **Done when:** the exact-title subset has MRR = 1.0 in every mode.

### B6 Fused graph seeding
- **Where:** `indexlib.py:369-378`.
- **What:** Move the one-hop expansion after RRF so it seeds from the fused candidates. Penalise
  hubs by degree (`1/log(2+degree)`).
- **Evidence from the 0.0 bench review (2026-10-16):** hub notes act as magnets. `webhook-relay`
  and `redis-streams` rank #1–2 even for unrelated queries. Two causes add up:
  - `_fts_query` ORs every token, stopwords included, so nearly every note is a keyword hit.
  - The graph arm then boosts the best-linked hubs.
- **Add to this step:**
  - Drop a small bilingual stopword set (EN + HU: `a`, `az`, `és`, `the`, `of`…) from the
    query's OR list, unless the query is nothing but stopwords.
  - Measure the hub penalty on the bench's `en` groups (MRR).

### B7 Result labels
- **What:** Every hit carries the following additive fields:
  - `collection`, `path`, `start_line`, `end_line`, `heading`, `tokens`
  - `status`/`plane` labels
  - `mode_used`
- **Empty results:** distinguish `no_match`, `index_stale` and `semantic_unavailable` in the
  envelope.

### B10 `search --tag` and `search --exact` (#18, part 2)
- **`--tag T`:** restrict candidates **before** every stage: the FTS `WHERE`, the vector query
  filter (LanceDB `where` on a stored `tags` column, added in B2's generation), rerank input, and
  graph expansion. Expansion must not leak notes outside the filter. Combine it with `--author`
  and `--collection`.
- **`--exact "phrase"`:** a literal contiguous phrase via an FTS5 phrase query, then verified with
  a case-insensitive substring check on the stored chunk text, because unicode61 splits `-`. No
  vector or graph additions. Each note appears once, with its matching heading. It is not a tag
  match.
- **Tests:** #18 acceptance criteria 4 and 5 in every mode (keyword, hybrid, hybrid+rerank, with
  the graph on).

### B8 `orient --project <slug> --query "<task>" --budget N` (briefing)
- **What:** Deterministic assembly of:
  - the hub summary zone, ranked above the timeline zone
  - active decisions
  - open tasks for the project
  - the top passages for the query
- **Truncation message:** "raise `--budget` to N".
- **Done when:** a briefing for a real project fits within the budget and cites `path:start-end`
  for every passage.

### B9 Windowed distill
- **Where:** `bin/files/run.py:646` (`body[:8000]`).
- **What:** Distill section by section using B1's chunker, then merge the results. Always report
  coverage (`distilled 12/12 sections`).

**Stage B exit:** recall@k improves on the deep-document and Hungarian sets, there is no
exact-title regression, lexical-only mode still works without the optional dependencies, and 4.3.0
is tagged.

---

## Stage C — one vault across machines (§7, #16)

### C1 Committed vault id
- **What:**
  - Split the marker: `.plainkeep/vault.json` holds the committed id, and machine-local registry
    state moves to `.plainkeep/local.json`, which is git-ignored.
  - Migration: on the first run of the new engine, keep the existing id and write the split. It is
    a `safe_write` and is journaled.
  - Update `bin/lib/vaultreg.py` and `vaultroot.py`.
- **ADR (vault identity):** the id travels with git, and the registry name stays local.
- **Done when:** two clones of one remote report the same `vault_id`.

### C2 Stop committing in-vault skill links (#16 §7)
- **What:** Remove the in-vault links, have `setup agents` install user-level links only, and
  ignore the old paths in `.gitignore`.

### C3 `sync` verb (#16, amended by #19)
#19 records the field failure this verb must own. An untracked local
`journal/2026/10/2026-10-14.md`, which the incoming commit also created, blocked `git pull`. The
agent then hand-moved the file and merged it back.
- **What:** a new `bin/sync/` with `cmd.json` and `run.py`. In order:
  1. **Park.** Compute the incoming tree (`git fetch`, then `git diff --name-only HEAD..@{u}`).
     Stash the tracked modifications and only those untracked files that collide with incoming
     paths, using `git stash push --include-untracked -- <paths>`. Leave unrelated untracked
     files alone.
  2. **Fast-forward.** `git merge --ff-only @{u}`. Never merge, rebase or force. On divergence,
     refuse with the exact commands to fix it.
  3. **Restore.** `git stash pop`. If the pop conflicts, stop: name the paths, give the next
     command, leave the stash recoverable, and do not commit or push. Two journals for the same
     day are the expected case. ◆ Should the journal get a line-union merge driver (append-only
     files: union is lossless), or should it always stop? Decide when building C3.
  4. **Publish.** If the tree is clean after the pop and there are local changes, stage vault
     content only, commit with a generated message (paths plus a one-line summary), and push to
     the vault's own remote.
     - Refuse `.env`, secret-looking files, and paths outside the vault.
     - Refuse when the push URL is disabled, or when it points at the plainkeep template repo.
  5. **Reindex.** Run `index --changed` (or `index_paths` from 0.4) so pulled notes are findable.
     With nothing to publish, don't make an empty commit, and say so.
- **Flags:** `--pull-only` runs steps 1–3 and 5, with no stage, commit or push. `--dry-run`
  prints the colliding paths, incoming commits, and what would be committed. It is a read.
- **Risk class (decided 2026-10-16, option a):**
  - A full `sync` that pushes is `confirm` and needs `--approved` (0.16). An agent passes it when
    the human asked to sync, so "please sync" is still one command.
  - `--pull-only` is `safe_write`, and it is the only form `jobs/registry.json` may schedule (off
    by default, every 15 min).
  - `--dry-run` is a read.
- **Tests:** #19 acceptance criteria 1–9 as fixture scenarios with two clones and one bare remote.
  The guardrail suite shows a scheduled job can only reach `--pull-only`.
- **Ordering:** two machines and a phone already share one remote (#19), so C3 is the most
  valuable step in Stage C. ◆ Consider pulling C1 and C3 forward to run right after Stage 0.

### C4 `node status [--peer host]`
- **What:** A read-only report of:
  - engine version and vault remote
  - HEAD, dirty and unpushed state
  - the default vault path
  - effective config (from 0.5)
  - models, UI version and skill-link targets
- **`--peer`:** runs the same command over `ssh host plainkeep node status --json` and diffs the
  two reports.

### C5 `node join <remote> --yes`
- **What:** A `confirm` verb that:
  - installs the peer's engine version
  - clones and registers the vault as the default, refusing a template checkout
  - writes the host config
  - runs `setup agents`
  - leaves schedules alone
- **Idempotency:** a rerun on a joined machine reports "already joined".

### C6 Doctor checks
- **What:** Fail on:
  - a default vault that is a template checkout
  - a committed link into another home directory (upgrades 0.10's warning)
  - with `--peer`, a peer that is ahead while this machine is dirty

### C7 Setup layer honesty (#16 §5, §6)
- **What:**
  - A failed model pull fails the layer, names the minimum Ollama version and exits non-zero.
  - The UI layer installs from the pinned release, not from a git remote on a sealed engine.
  - Optional layers install from the shipped `uv.lock` (`setuplib.py:219`) instead of pip ranges.

### C8 Implicit day boundaries (start/close without the ritual)
Requested by the owner on 2026-10-16.
- **Evidence for the change:**
  - On this Mac no scheduled job is installed (`plainkeep job status`), so the 07:30 start and
    18:30 close never run.
  - What they would write can be derived on demand. Carried-forward tasks are copies of
    `tasks/active|waiting`, and the close block is `git log` plus task dates.
  - A clock-driven `start` on two machines creates `journal/YYYY/MM/DD.md` on both, which is the
    exact collision in #19.
- **What:**
  1. **Lazy journal.** Today's journal file is created on the first real write (capture, task
     change, note, an agent's journal line), never by a clock. A day with no activity has no file.
  2. **The start of the day becomes a view.** `orient` (which agents already run first, 0.7)
     shows today: open and waiting tasks, the inbox count, yesterday's loose ends (active tasks
     without progress, uncommitted changes), and the sync state. It writes nothing.
  3. **The close becomes a view, plus the one step that matters.** `orient --day <date>` (and
     `week`) rebuilds a day's summary from git and task files with no write. `close` = that
     summary + `sync`, so nothing is left unpushed. Its push still needs `--approved`, and it runs
     `sync --pull-only` semantics without it.
  4. **Compatibility.**
     - `start` and `close` keep working as thin wrappers (spellings are never removed).
     - `start` = ensure today's journal + `orient`, without copying the carried-forward list.
     - `close --automated` is accepted and does nothing beyond the summary.
  5. **Optional written record.** A per-node opt-in in host config (0.5),
     `journal.daily_summary = true`, appends the facts block at night. Only the vault's writer
     node may enable it (§7). The `start` job is removed from `jobs/registry.json`, and
     `close_nudge` becomes writer-only and off by default.
- **ADR:** amend ADR-022 ("automation is the default offering"). Automation stays available but
  is no longer the default for journal writes. Maintenance jobs (index, consolidate, backup check)
  keep their defaults.
- **Tests:**
  - A day with no activity leaves no journal file.
  - `orient` output covers everything the old carried-forward list showed.
  - With two clones, both run `start` and then `sync`, and no collision occurs.
  - The old `start`/`close` invocations still exit 0.
- **Ordering:** after C3, because `close` calls `sync`. Steps 1–2 can land earlier, alone, if
  wanted.

**Stage C exit:** a fresh laptop runs `node join` and comes back green, `doctor` fails on every
failure case from #16, and 4.4.0 is tagged.

---

## Stage D — remote Reader (§9)

◆ **Decide before starting:**
1. Which ChatGPT plan? Pro means Reader only.
2. Which machine is the writer?
3. Does Claude need remote access? If yes, a public HTTPS endpoint with OAuth is required.

The answers pick D5's topology.

### D0 ADR: amend principle 6
- **What:** The remote adapter is a separate optional frontend process. It is stateless apart from
  auth, re-enters `plainkeep <verb>` for every call, and owns no index and no write path. Update
  `docs/architecture.md` principle 6 to match.

### D1 Adapter skeleton
- **Where:** `frontends/mcp-remote/`.
- **What:**
  - Write it in TypeScript to reuse `cli/src/core/mcp.ts` plumbing.
  - Tools are generated from `plainkeep.json` actions on an **allowlist** per surface: Reader is
    `search`, `fetch`, `open_section`, `briefing` and `status`.
  - Each tool has typed input schemas, with no free-form `args` and no `--yes`.
  - Dispatch re-checks the surface allowlist on every call. Annotations are hints and are never
    the guard.

### D2 OpenAI-shaped `search` / `fetch`
- **What:**
  - `search(query)` returns `{results:[{id,title,url}]}`.
  - `fetch(id)` returns `{id,title,text,url,metadata}`, backed by A1.
  - Declare an `outputSchema` and return `structuredContent` plus the same JSON as text.

### D3 Spec 2026-07-28
- **What:**
  - Support the stateless Streamable HTTP transport, `server/discover`, and the
    `Mcp-Method`/`Mcp-Name` headers.
  - Keep accepting `initialize` from older clients.
  - Bump the local stdio `PROTOCOL_VERSION` (`mcp.ts:87`) in the same PR, with a negotiation test.

### D4 Transport safety
- **What:** Bind to 127.0.0.1, validate `Origin`, and enforce a single-vault scope. Apply
  permission filters before snippets, counts, links or errors.
- **Tests:** a leak test suite (`test/run_remote_reader.py`) checks that denied content never
  appears anywhere in a response.

### D5 Topology recipes
- **Where:** `docs/remote-mcp.md`, a how-to.
- **What:** Document each topology, verified end to end with real clients:
  1. **ChatGPT through the Secure MCP Tunnel:** wrap stdio, run as a dedicated read-only user, and
     state the no-auth trade-off plainly.
  2. **ChatGPT and Claude through Cloudflare Tunnel or Caddy:** put `mcp-auth-proxy` with a
     one-account allowlist in front of the adapter.
- **Server profile:** add a launchd plist and a systemd unit for the adapter.

**Stage D exit:** real ChatGPT and Claude clients search, fetch and cite; no confirm-class action is
reachable; and 4.5.0 is tagged.

---

## Stage E — remote Capture and identity (§3, §9)

### E1 Idempotency keys and receipts
- **What:** Each key is bound to principal + action + payload hash. Durable receipts live under
  `.plainkeep/receipts/`, git-ignored and pruned after 30 days. A retried write returns the
  original receipt.

### E2 Stable IDs
- **What:** A `permalink` frontmatter field, minted lazily on the first remote reference or on
  `move`. `move` preserves it, and the resolver accepts a permalink anywhere a slug is accepted.
- **ADR (stable IDs):** record the decision.

### E3 Revision-bound citations
- **What:** Every remote hit and fetch carries `{permalink, rev}`. Resolving a stale rev returns
  the current text plus `changed_since: true`.

### E4 Capture surface
- **What:** Reader plus append-only `capture` and `bookmark`, with no URL fetch at first. Opt-in
  per adapter config.

**Stage E exit:** concurrent and retried remote writes keep every item, renames keep citations
working, and 4.6.0 is tagged.

---

## Stage F — profiles and maintenance (§3, §8, §11)

| Step | Deliver |
|---|---|
| F1 | Setup planner with Minimal, Recommended and Server profiles. It reuses C5's plan code, shows one plan (packages, download size, services, exposure, credentials still needed), then prints ready / degraded / blocked per capability |
| F2 | Systemd units generated from `jobs/registry.json` (Ubuntu LTS x86_64). The Server profile excludes the start/close journal automation |
| F3 | Optional `supersedes` / `superseded_by` / `effective` frontmatter fields. Superseded notes are demoted (×0.5), never hidden. Body-hash–based "needs review" marking on extracts when the source changes |
| F4 | `organize` duplicate detection: replace the O(n²) title pairs (`bin/organize/run.py:277`) with blocking by normalised-title n-grams, then a vector probe |
| F5 | Obsidian pack defaults: daily notes in `journal/` with format `YYYY/MM/YYYY-MM-DD`, templates, excluded files, starter Bases. *Independent of everything else; can be pulled forward at any time* |
| F6 | Health surface: core, lexical index current, semantic coverage, remote endpoint, last backup, last verified restore |
| F7 | Project Context integration: the B8 briefing composes the ledger handoff (read-only), and writes still go through the dispatcher |

**Stage F exit:** the fresh-host, reboot, update/rollback and restore scenarios pass, and 4.7.0 is
tagged.

---

## Tracking

- **GitHub tracking issues:** open one issue per stage, with the step IDs as a checklist.
- **Issues closed by this plan:**

  | Issue | Closed by |
  |---|---|
  | #8 | 0.12 |
  | #15 | 0.7 + A7 |
  | #16 | C1–C7 + 0.5 |
  | #17 | A2 + 0.4 + 0.9 |
  | #18 | 0.15 + B10 |
  | #19 | C3 + C8 |

- **When a step diverges from the design:** amend this plan in the same PR and leave the design
  doc as written. ADRs record what was actually decided.
