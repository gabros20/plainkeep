# Decisions log — Personal OS design

Append-only ADR log for the *design itself* (the §8.2 pattern, applied to this design repo).
Each entry: context → decision → why → status. Newest at the bottom. Appendix A / A.1 in
`PERSONAL_OS_DESIGN.md` holds the per-merge table; this log holds the load-bearing "why".

---

## ADR-001 — Adopt six gbrain ideas into v3.7 (2026-06-17)
**Context.** Studied `garrytan/gbrain` (Postgres-native "compiled intelligence" runtime) for ideas
to sharpen this plaintext/git/shell design.
**Decision.** Transpose six ideas that fit the first principles: compiled-truth/timeline two-zone
notes (§7.2, §10.1), brain-first lookup (§1, §12.3), the Iron Law — model picks WHAT, system
guarantees WHERE/HOW (§1, §5, §12.3), zero-LLM auto-backlinks (§10), routing-eval fixtures (§11),
and a `consolidate` dream-lite job (§15). Reject gbrain's runtime weight (Postgres, embeddings
stack, minions/autopilot, skillopt, schema-packs).
**Why.** The ideas are substrate-independent discipline; the runtime is unearned complexity that
violates reversibility (principle 6). gbrain's own 147k-token agent file is the cautionary tale.
**Status.** Done (v3.7). Validated by the `test/` harness.

## ADR-002 — Retrieval staging: Karpathy wiki foundation; local file-based vectors as stage-2 (2026-06-18)
**Context.** Open question: does this system need vector embeddings? Surveyed the 2026 X landscape
(Karpathy LLM Wiki, Farzapedia, gbrain, FalkorDB, LightRAG, Kwipu, RDF) and measured retrieval on a
fixture corpus with a real local embedder (ollama `mxbai-embed-large`).
**Decision.** Keep the compiled-markdown + index-files + wikilink-graph + agent-navigation
foundation (the dominant validated pattern — this design already is it). Stage retrieval:
rg → FTS5 + wikilink-graph → (when earned) `sqlite-vec` + local Ollama embeddings + RRF hybrid.
Reject the server/graph-DB tier (Postgres+pgvector, FalkorDB, LightRAG, Kùzu-as-server, RDF).
**Why.** The **retrieval add-on test** (§10.2.1): file-based + locally-computed + rebuilt-from-
plaintext = allowed (it's "one SQLite file over a server"); a server or an LLM-extracted second
graph that goes stale = rejected (principle 6). Measurement: on queries sharing no vocabulary with
the target note, keyword+graph = 0.00 recall@5, local vectors = 1.00 — vectors recover exactly what
keyword structurally cannot. The graph need is already met for free by `[[wikilinks]]` + backlinks.
**Status.** Decided; magnitude now MEASURED. A fair, ops-shaped vault was built from a real
58-note LLM/agents KB (13 area hubs, 435 wikilinks, frontmatter; `vault/`, built by a 14-agent
workflow) and queried with 25 realistic queries (`test/vault_queries.txt`: 11 exact-term, 14
natural-language) via `run_search_live.py` with real local embeddings (ollama mxbai-embed-large):
- All 11 exact-term queries: keyword+graph == vector (keyword suffices).
- Of 14 natural-language queries: **~8 clear vector wins** (keyword put the wrong note #1),
  and on **5 of them keyword+graph missed the right note entirely even at rank 3**; vectors got
  them at #1. Net: ~32% clear vector wins, 40% divergence — **above the ~25% threshold**.
- Verdict: **stage-2 vectors are EARNED for natural-language / conceptual retrieval.**
- Caveat: this corpus is conceptual (curriculum); an entity/proper-noun-heavy vault (clients,
  people, dates) skews more lexical. `ops search` query-logging (`.logs/queries.jsonl`) is live,
  so the production log confirms the per-domain mix over time. Engine when built: `sqlite-vec` +
  local Ollama, per ADR-003 — **revised by ADR-006 for scale: vectors in LanceDB, not sqlite-vec.**

## ADR-003 — Vector engine: SQLite + `sqlite-vec`, not libSQL (2026-06-18)
**Context.** Evaluated libSQL (Turso's MIT SQLite fork): file-format-compatible, embedded single-file,
*native* vector search (`F32_BLOB`, `vector_distance_cos`, DiskANN `vector_top_k`).
**Decision.** Use plain SQLite + the `sqlite-vec` extension. Keep libSQL as a documented, zero-
migration drop-in fallback (same file format); its sync/cloud features stay off-limits.
**Why.** Reversibility (principle 6): plain SQLite is already present (Python `sqlite3` stdlib, the
CLI, public domain, outlives any company); libSQL is a one-company fork whose reason-for-being —
embedded replicas / sync / cloud — is the server gravity this design walls off. The index is a
disposable rebuilt-from-markdown cache, so native-vs-extension vectors is mere ergonomics, and
DiskANN's ANN only matters past ~100k vectors (brute-force cosine is instant at this scale).
**Status.** Decided for the *keyword/metadata/graph* store (SQLite FTS5 stays). **Revised in part by
ADR-006:** at the target scale (100k–500k+ notes → ~1M+ chunks) sqlite-vec's brute-force does NOT
hold, so VECTORS live in LanceDB (embedded, file-based ANN), not sqlite-vec. SQLite remains the
keyword/metadata/graph engine.

## ADR-004 — Validate the design by simulation, not just review (2026-06-16 → ongoing)
**Context.** The system has no implementation yet — only a spec. A spec can't be unit-tested, but it
can be modeled and attacked.
**Decision.** Build `test/`: a deterministic guardrail/jobs/sweep/wiki/state model exercised by
adversarial cases, plus an LLM-operator simulation (the design's own AGENTS.md + operate-ops fed to
a real model) judged for drift/bypass/misfiling, with a two-model agnosticism diff.
**Why.** It catches what review misses by eye. It found ~17 real spec gaps/holes — `ops repo adopt`,
the sweep write-zone carve-out, backup risk class, symlink/case-insensitivity/transmit-by-any-tool
guardrail escapes, global slug uniqueness, worktree sanctioning, the swept-rescue rule — each fixed
in the spec then re-validated. Prompt-injection (6 attacks) and the agnosticism diff both pass clean.
**Status.** Ongoing. Hard gates are structural; free-text checks match meaning to avoid LLM-phrasing
flakiness (see `test/README.md`).

## ADR-005 — Stage-2 embedding model: EmbeddingGemma-300m (default, configurable) (2026-06-19)
**Context.** ADR-002 earned stage-2 vectors; ADR-003 fixed the store (sqlite-vec + local Ollama).
Open: which local, multilingual (EN + HU + DE + common), efficient, mid-sized, frontier embedder?
Researched MTEB multilingual standings (HF Hub + web) and A/B'd on the vault with real local models.
**Decision.** Default **`embeddinggemma` (google/embeddinggemma-300m)**: 303M, 768-dim Matryoshka
(→256/128), Gemma license, 100+ langs incl. Hungarian/German, #1 under 500M on MTEB multi/en/code,
Ollama-native. Keep the model a one-line config (`OPS_EMBED_MODEL`) with **per-model prompt
profiles**, so `bge-m3` (MIT, proven low-resource/Hungarian) and `qwen3-embedding:0.6b` (Apache-2.0,
modern, ~2×) are drop-in alternatives. Engine per ADR-003 (local file, no server).
**Why (measured `test/run_search_live.py`, vault):**
- **Prompt prefixes are MANDATORY, not optional.** Run *without* them, EmbeddingGemma collapsed
  (80% divergence, degenerate repeated top-1, Hungarian unusable). *With* the doc/query prompts
  (`title: none | text:` / `task: search result | query:`) it behaved correctly (48% divergence on
  English, comparable to mxbai's 40%). The implementation MUST encode per-model prompts.
- **No English regression:** with prompts, EmbeddingGemma ≈ mxbai-embed-large on the English vault,
  so switching from English-only mxbai to multilingual Gemma costs nothing on English and adds
  multilingual headroom.
- **Multilingual is vectors-only:** on Hungarian queries vs the English vault, keyword+graph scored
  ~0 (no cross-language token overlap) while EmbeddingGemma bridged several correctly (e.g.
  "token költség"→token-optimization, "több ágens koordinálása"→coordination-strategies). Cross-
  lingual is a stress case; mono-lingual HU→HU will be stronger. For any non-English content,
  keyword cannot work and a multilingual embedder is the only path.
**Caveat.** A/B corpus is English; Hungarian tested cross-lingually (no HU notes yet). Confirm with
real HU content + the production query log before final lock. `bge-m3` is the fallback if Hungarian
quality disappoints (longest low-resource track record). **Status.** Default chosen; implement on go-ahead.

## ADR-007 — Accept and implement the v4 platform roadmap (2026-07-02)
**Context.** The 15-agent research synthesis produced `docs/design/proposals/2026-07-01-v4-platform-roadmap.md`
(machine contract → plugins → frontends → pipeline → durability). The owner accepted the whole
roadmap for implementation (5.3 `ops publish` and 3.5 TUI stay deferred, per the proposal itself).
**Decision.** Implement Parts 0–5 as specified, on branch `v4-platform`, one revertible commit per
package: 0.1–0.5 trust fixes (exit-code protocol 0/2/3/4/5, `script/update` 3-way merge keyed on
`.ops-engine-ref`, doctor sync-wall/second-remote/churn checks, system-wide `--dry-run`); 1.1–1.2
the machine contract (`--json` envelope via `bin/lib/output.py` on every verb, ops.json v2 with
capabilities/source/hints/output blocks); 2.1–2.4 the platform (multi-root resolver `bin/` →
`plugins/` → `$OPS_PATH`, frozen `lib/api.py` SDK v1.0, `ops plugin` with guardrail trust ceiling,
`ops mcp` stateless stdio server re-entering the dispatcher); 3.1–3.4 surfaces (Obsidian Frontend
Zero pack + Bases + JSON Canvas, Raycast script commands, `ops open`/`ops orient`, search snippets);
4.1–4.4 the pipeline (`files extract` tiers, checked provenance planes, `files distill`,
`ops organize` propose→review→apply with closed op catalog); 5.1–5.4 durability (restic backup
family, zero-knowledge `ops share` with vendored worker, `script/get` install funnel).
**Why.** Ten sequential implementation agents + one QA agent + operator validation: full offline
suite green (36 sections, incl. 12 new v4 suites), all 12 anti-roadmap constraints verified with
zero violations, no verb spelling removed (22 kept, 6 added: mcp/open/orient/organize/plugin/share),
stdlib-only zero-install path intact, guardrail remains the single enforcement path.
**Status.** Done on `v4-platform` (a2ac7f3…285f5e5). QA's one wrinkle (`organize apply` ignored
`--dry-run`) was fixed post-QA: `apply --dry-run` now previews the exact replay — budget and
protected-path accounting included — writing no files, commits, ledger lines, or journal entries.

## ADR-006 — Scale to 100k–500k+ notes from day one, single-machine, no server (2026-06-19)
**Context.** Owner's ambition is gbrain-scale (hundreds of thousands of notes), not a small personal
KB. Prior ADRs sized some choices for small scale. Re-architect for volume *without* abandoning the
first principles (plaintext truth, reversibility, no server). Grounded in research: sqlite-vec is
brute-force (fails >~1M vectors); git degrades badly past ~10k files (gbrain abandoned git-wiki at
~5k); LanceDB is embedded/file-based with disk IVF-PQ ANN, <20ms @1M, larger-than-RAM, billions-scale
single-node; SQLite FTS5 scales to millions of rows.
**Decision — the scale-ready stack (all embedded, no server, rebuildable from markdown):**
1. **System of record stays plaintext markdown in ONE git repo** (principle 1–2 intact, unbent).
   Correction to an earlier overstatement: a single repo IS enough at 100k–500k *text* notes.
   Git slows from three independent causes — (a) working-tree file count, (b) repo size / binary
   blobs, (c) a single flat directory — and only the ones this design already neutralizes bite:
   - **(a) file count** is solved by git's large-repo features (`feature.manyFiles`, `core.fsmonitor`,
     `core.untrackedCache`, `commit-graph`) — near-constant `git status` regardless of count
     (Microsoft runs the 3.5M-file Windows repo on git; 100k–500k is far below any wall);
   - **(b) size/binaries** is the cause that actually choked gbrain (its "7,471-file/2.3GB wiki" was
     ~300KB/file = embedded media, NOT 7k text files). This design **structurally excludes it**:
     the sorting rule is plaintext→git, binary→`~/files`. 500k notes × ~5KB ≈ 2.5GB of well-delta-
     compressed text — fine;
   - **(c) flat directory** is solved by **subdirectory fanout** (`notes/<aa>/<slug>.md`) *within the
     one repo* — a filesystem hygiene step, not a reason to split repos.
   So **"git is the spine" holds unbent** — one repo, fanned out + tuned, binaries already elsewhere.
   **Multiple repos are an OPTIONAL, much-later lever**, not a day-one requirement — justified only by
   millions of files, or a desire to separate cadence/backup (e.g. a noisy auto-ingest feed) or do
   per-machine selective sync. Truth never moves into a DB.
2. **Keyword + metadata + graph:** SQLite **FTS5** (+ a `links`/typed-`edges` table, recursive-CTE
   multi-hop). Scales to millions of rows; stays one file.
3. **Vectors:** **LanceDB** (embedded, file-based, IVF-PQ disk ANN) — NOT sqlite-vec. Flat index at
   small N, auto/again to IVF-PQ as the table grows, so there is **no migration** between small and
   large — we build on LanceDB from day one. Embeddings via local EmbeddingGemma (ADR-005).
4. **Two-stage retrieval:** ANN candidate-gen (LanceDB) + BM25 (FTS5) → RRF → **local cross-encoder
   rerank** (e.g. `bge-reranker-v2-m3` via Ollama/llama.cpp) for precision at scale. The reranker
   is gbrain's zerank role, run locally.
5. **Indexing at volume:** batched + parallel + **resumable/checkpointed** embedding (initial 100k
   backfill is a multi-hour job — `op_checkpoint` pattern, incremental by file-hash already in
   `indexlib`); a file-watcher for live incremental; QAT-quantized embedder (~200MB) for throughput.
6. **Background work:** a resumable indexing/consolidate worker under launchd (single-node);
   escalate to a job queue only if multi-source/multi-machine (still out of scope).
**Why this keeps the philosophy at scale.** LanceDB + SQLite are BOTH embedded and file-based — so
100k–1M notes are served **single-machine with no server**, preserving principle 6. Markdown stays
truth and every index rebuilds from it (`rm -rf .index && ops index`), preserving principles 1–2 and
reversibility. We reach gbrain *capability* without gbrain's Postgres server — the embedded-ANN era
(LanceDB) is what makes that newly possible. And the storage spine does NOT bend: one git repo of
plaintext, fanned out and tuned, carries the whole scale (point 1) — gbrain's git pain was binary
*size*, which the plaintext→git / binary→`~/files` rule already prevents.
**What this explicitly reverses.** No more "at your scale you don't need it." ANN, two-stage
rerank, subdirectory fanout, and resumable bulk indexing are CORE from day one, brought up small on the
same architecture so there is never a re-platforming. **Status.** Architecture set; supersedes the
small-scale framing in ADR-002/003 and §10.2. **Stages 1–3 IMPLEMENTED** (`bin/lib/indexlib.py`,
`embed.py`, `vectorstore.py`, `rerank.py`; `ops index`/`search`): FTS5+graph → LanceDB ANN +
EmbeddingGemma (OPS_VECTORS=1) → fastembed cross-encoder rerank (OPS_RERANK=1), all embedded /
local / no-server, opt-in, rebuildable. Remaining: scale-out *plumbing* (repo sharding + resumable
batched backfill + file-watcher), which earns its keep only as the vault approaches 100k — the
architecture itself does not change.

## ADR-008 — Drop zero-knowledge sharing for capability URLs (2026-07-10)
**Context.** The operator's actual job with `ops share` is "paste one link into any chat/coding
agent and it reads the note" — no headers, no second URL, no local tooling. The shipped
zero-knowledge design (AES-256-GCM, key in the URL `#fragment`, PrivateBin pattern) cannot clear that
bar, because HTTP never sends `#fragment` to a server: a server-side agent-fetchable route
structurally cannot receive a fragment-only key. Every variant tried to bridge the gap failed the
same way or worse: `?k=<key>` query param (key lands in worker access logs and browser history),
`X-Ops-Share-Key` header (unusable by generic fetch tools that can't set custom headers), a second
`agent_url` printed alongside the human link (two-link contract — the exact ergonomics failure being
fixed), `ops share pull` local decrypt (requires `ops` installed — fails for "any chat/coding agent"),
and mechanical URL-math instructions for agents to derive the fetch URL themselves (fragile, another
thing to get wrong). See `docs/design/proposals/2026-07-10-ops-share-agent-markdown-url.md` §11.5 and
`docs/design/proposals/2026-07-10-capability-url-share.md` for the full exhausted-alternatives table.
**Decision.** Remove zero-knowledge encryption entirely (not kept as a flag). `ops share <slug>
--yes` now PUTs the plaintext OPSX bundle to the worker under a single 24-char unguessable token
(~124 bits, up from the old 10-char id). ONE link: the bare URL renders HTML in a browser; append
`.md` (or let content negotiation handle a bare non-browser fetch) and the same link returns raw wiki
markdown. `PUT /` additionally requires an `X-Publish-Token` when the `PUBLISH_TOKEN` wrangler secret
is set, so a discovered endpoint can't be abused as an anonymous file host. Links published under the
old encrypted model return `410` on every route — re-publish.
**Why.** Zero-knowledge and "one link an agent can fetch" are logically incompatible, not just
hard to engineer: a fragment is by definition never transmitted, so any route a server-side fetch
tool can use must carry the key somewhere the server (and therefore anyone with log access) can see
it — at which point ZK has already been given up in substance, and keeping the fragment charade only
costs a second URL. The content being shared is, by definition, content the operator chose to
publish (never `~/files`, never anything outside the explicit `ops share` invocation) — so "the
provider can technically read it" is a narrower exposure than it sounds, not a new category of risk.
An unguessable token over TLS with a TTL is exactly the trust model already accepted for the
`--gist` fallback (a GitHub secret gist); this makes the primary path match the trust model of the
fallback instead of promising a stronger property it can't structurally deliver.
**Status.** Implemented (this refactor: `bin/share/worker/worker.js`, `bin/lib/sharelib.py`,
`bin/share/run.py`). Requires a worker redeploy (`wrangler deploy` + `wrangler secret put
PUBLISH_TOKEN`) before publish/fetch routes reflect this ADR in production; `ops share init` prints
the exact steps.
## ADR-009 — The interpreter + search-dependency contract: stdlib floor, optional `.venv`, dispatcher-preferred (2026-07-22)
**Context.** The optional vector/rerank planes (lancedb + fastembed) are heavy pip deps, while the
stage-1 keyword floor is stdlib-only (principle 6, zero-install). The install story had drifted into
*two* incompatible tellings: `docs/agent-terminal-search.md` told the user to hand-make a `.venv` and
carefully **prepend** `$OPS_HOME/.venv/bin` to `PATH` so the bare-`python3` dispatcher would pick it
up — a silent-failure trap ("works in my shell, not for the agent") — while `ops setup search`
installed the *entire* `requirements.txt` (dragging in the file-processing deps Pillow/trafilatura/
mlx-vlm that belong to the `models` layer) into whatever interpreter happened to be running. A
repo-local `.venv` was, by design, invisible to `ops` — the load-bearing gap.
**Decision.** One contract, four rules:
1. **Bare `python3` is the stdlib floor.** Every core verb works on it with no venv, no optional
   deps (the zero-install path is never broken — a regression test asserts it).
2. **The optional `.venv` is the SINGLE home for ALL optional deps (search + models).** `ops setup
   search` creates `$OPS_HOME/.venv` and installs the *search-only* set (`requirements-search.txt` =
   lancedb + fastembed, a strict subset of `requirements.txt`) into it; `ops setup models` installs
   the file-processing set (Pillow, trafilatura, +mlx-vlm on Apple Silicon) into the **same** venv.
   Neither installs the whole `requirements.txt`. Each layer still owns its own dep subset — the venv
   is just the shared, dispatcher-visible environment they land in. The venv lives inside `$OPS_HOME`
   (path-wall-allowed) and is `.gitignore`d + rebuildable (`rm -rf .venv && ops setup search --yes
   && ops setup models --yes`) — a disposable cache, never truth (principle 1).
3. **The dispatcher prefers `.venv/bin/python3` when it exists AND starts**, else bare `python3` — for
   the guardrail, resolver, and every verb. This is the fix that makes a repo-local venv actually
   load-bearing: `ops index`/`ops search` import the vector plane, and `ops files`/`enrich`/`doctor`
   see the file-processing deps, with no `PATH` surgery — and any agent terminal inherits it for free
   because it runs the same `ops` script. The dispatcher (and setup's create-if-missing logic) probe
   that the venv python actually *starts*, not merely that the symlink is executable: a `.venv` that
   survived a system-python upgrade or ABI break falls back to bare `python3` instead of returning
   126/127 on every verb, and a half-built `.venv` is repaired rather than trusted as complete.
4. **Search readiness is OPERATIONAL, not "installed":** `deps-importable` (probed through the
   dispatcher's interpreter) + `model-pulled` (ollama reachable vs present, distinguished) +
   `index-built`; `OPS_VECTORS`/`OPS_RERANK` are advisory (surfaced as a handoff when unset).
**Why.** The venv was the right mechanism and the wrong plumbing: isolating heavy deps from the
stdlib floor is correct, but a venv the dispatcher can't see forces per-terminal PATH rituals that
fail silently. Teaching the dispatcher to prefer it collapses two install stories into one, deletes
the PATH-ordering footgun, and keeps the stdlib floor intact (no venv ⇒ bare `python3`, unchanged).
Making the venv the single home for BOTH dep sets closes a silent capability regression: installing
the models deps into bare `sys.executable` while the dispatcher had already switched every verb to the
venv left image/OCR/enrich/extract paths quietly downgraded (the deps were "installed" but invisible).
One environment, dispatcher-preferred, keeps every optional dep consistently reachable. **Status.**
Done. `ops` prefers `.venv/bin/python3` (with a start-probe, not just `-x`); `ops setup search`
provisions the venv + `requirements-search.txt` + model + index, and `ops setup models` installs its
file-processing deps into the SAME venv; a broken/half-built venv is repaired rather than trusted;
docs collapsed to one story; `/.venv/` gitignored before any venv-creation code. Verified: full
offline suite green in a `main==HEAD` clone.

## ADR-010 — First-run funnel: dashboard is the non-interactive form, `--wizard` the interactive one (2026-07-23)
**Context.** Roadmap Part 5.4 asks for a first-run experience that is a "wizard with ≤5 skippable
prompts, vectors/jobs OFF" — a guided path that never surprises a new user with heavy installs. The
layered `ops setup` engine (ADR added with the layered flow) already exposes every layer's state and a
one-door `advance()`; what was missing was the *interactive* front for someone typing at a terminal.
**Decision.** Two forms over ONE engine — no second advance path:
1. **`ops setup` (dashboard) + `ops setup --all --yes` are the NON-INTERACTIVE form.** A read-only
   status table plus a best-effort batch advance for agents, CI, and piped installs. This is what the
   `curl … | sh` funnel and `--json` consumers use.
2. **`ops setup --wizard` is the INTERACTIVE form.** A pure-stdlib `input()` loop, one skippable
   prompt per layer in `LAYERS` order, with SAFE DEFAULTS pre-selected: skeleton ON (required,
   safe-write), search/models/automation OFF (vectors OFF, no model pulls, jobs OFF), backups never a
   yes/no (a printed `ops backup init` handoff, never auto-run — it is `gate="blocked"`, needs human
   secrets). Already-ready layers are noted and skipped; blocked/not_applicable layers show their
   reason + `next` and are not prompted. Each accepted layer advances through the SAME
   `setuplib.advance(id, yes=True, fake=…)` the dashboard/`--all` use.
   **tty-guard:** interactive-only. No tty, or `--json`/`--dry-run` paired with `--wizard`, exits `2`
   (EXIT_USAGE) printing the exact non-interactive alternatives (`ops setup --all --yes`,
   `ops setup --json`, `ops setup --all --dry-run`) — "refusals teach". `--dry-run` is refused rather
   than previewed because the wizard's advance is real (fake only under `OPS_SETUP_FAKE`); a preview
   would need a second code path, and the dashboard already owns the non-interactive preview.
**Why.** One engine, two faces keeps the machine contract (`--json`, exit codes, `advance()`) intact
while giving a human a guided, safe-by-default door. The prompt/answer loop is factored around an
injected input-callable and output-callable (`_run_wizard(rows, ask, say)`), so the accepted-layers
behaviour is unit-testable with scripted answers and no real tty. **Status.** Done. `--wizard`
implemented in `bin/setup/run.py` (`_run_wizard`, `_ask_yes_no`, `_wizard` guard); `cmd.json`/`ops.json`
mention it; `script/setup`'s closing banner points at it as the guided next step; the `curl … | sh`
funnel (`script/get`) installs LEAN + non-interactive by default with a `--full` contributor escape
hatch. Together the dashboard + wizard fulfil roadmap Part 5.4.

## ADR-011 — One repo (`opskit`), two stacks, binary-distributed TUI (2026-07-25)
**Context.** The system had sprawled across three repos: `personal-operating-system` (the template:
Python engine + funnel), the private vault (`gabros20/ops`, a derived copy), and a standalone
`ops-ui` repo (the TypeScript terminal UI). Three repos for one system was unmaintainable, and the
name no longer fit a repo that is a *kit* — engine + TUI + funnel — rather than anyone's data. A
full single-stack rewrite was considered and rejected: Go/Rust is blocked outright (the search layer
runs on `lancedb`/`fastembed`, the models layer on `mlx-vlm` — Python libraries), and a TS engine
rewrite would re-verify ~32 green verbs to gain nothing the `ops.json/3` contract seam doesn't
already provide, while losing the bare-`python3` zero-install floor macOS ships with.
**Decision.**
1. **Rename the template to `opskit`** (GitHub redirects the old URLs; `script/get`/`script/setup`
   now point at it). The private vault keeps its name and derives from the template exactly as
   before — the template/instance split is unchanged.
2. **Merge the TUI into the template as `ui/`** (git-subtree, history preserved; the standalone repo
   retires). `ui/` is **template-only source**: it is NOT in `script/engine.txt`, so `script/update`
   never propagates TS source, `node_modules`, or a toolchain requirement into any vault.
3. **Vaults receive a compiled binary, not source.** `.github/workflows/release-ui.yml` (tag
   `ui-v*`) cross-compiles `ui/` with `bun build --compile` into self-contained binaries
   (darwin-arm64/x64, linux-x64/arm64) + `checksums.txt` on a GitHub release. A sixth setup layer —
   `ops setup ui` (optional, confirm-gated, wizard default ON) — downloads the matching asset with
   the **authenticated `gh` CLI** (the template may be private; anonymous curl cannot reach its
   assets), verifies the sha256, and installs to `$OPS_HOME/.local/bin/ops-ui`, which the stdlib
   `bin/ui/` shim now resolves first ($OPS_UI_BIN → `.local/bin` → PATH). Contributor checkouts
   (with `ui/` + bun) build from source instead. The release host is derived from the vault's
   `upstream` remote (else `origin`) — never hardcoded.
**Why.** One repo to maintain, zero new floor requirements: the engine stays stdlib Python (tested,
data-adjacent, agent-patchable without a toolchain), the TUI stays TypeScript (the maintainer's
stack), and the machine contract remains the seam that makes the stacks irrelevant to each other.
Binary distribution resolves the only real objection to co-location — a Node payload flowing into
every derived vault via `script/update`. **Status.** Done. Layer + probes + seams
(`_gh_present`/`_ui_*`) in `setuplib`, tests in `run_setup_layers` (host-independent), CI `ui` job
(typecheck + compile smoke + non-TTY exit-2 contract), release workflow, docs updated.

## ADR-012 — rename opskit -> plainkeep with full-consistency naming (2026-07-28)
**Status.** Accepted.
**Context.** `opskit` collided with 40+ same-named GitHub repos, the `opskit` name was already
squatted on both npm and PyPI, and "ops" reads as DevOps tooling to anyone skimming a repo list —
a semantic miscategorization for a personal knowledge/task system that has nothing to do with
infrastructure operations. A name that cannot be searched for and is misread on sight was judged
not to be a cosmetic problem but a load-bearing one, worth a full-consistency rename rather than a
patch.
**Decision.** Rename the brand, the CLI, and every internal name for one-name consistency rather
than layering a new label over the old internals: brand `opskit` → `plainkeep`; CLI dispatcher
`ops` → `plainkeep`; vault folder `~/ops` → `~/plainkeep`; every `OPS_*` environment variable and
same-named Python constant → `PLAINKEEP_*`; the machine contract file `ops.json` → `plainkeep.json`
(contract phrase `ops.json/3` → `plainkeep.json/3`); the terminal UI binary `ops-ui` →
`plainkeep-ui`. One name, everywhere, rather than a marketing rename over unchanged internals.
**Alternatives.** (a) Keep the `opskit` name and rely on GitHub topics/description for
discoverability — rejected: the name collision with 40+ repos is permanent and topics don't fix
what a human reads first. (b) Rename the CLI to the shorter `keep` — rejected: standing alone,
"keep" is an ambiguous brand fragment (a note-taking app, a password vault, a to-do list all fit
that word equally), where `plainkeep` reads unambiguously as this system's own name.
**Consequences.** Existing vaults migrate manually: `mv ~/ops ~/plainkeep`, re-symlink the
dispatcher onto PATH, rename `.ops-engine-ref` → `.plainkeep-engine-ref`, and export the renamed
`PLAINKEEP_*` environment variables in place of the old `OPS_*` ones. The terminal UI is released
as `ui-v0.2.0` with `plainkeep-ui` release assets (`plainkeep-ui-darwin-arm64`, etc.), superseding
the `ops-ui`-named assets from ADR-011's release workflow.

## ADR-013 — Hybrid core: a compiled TS binary in front of the Python engine (Phase 1) (2026-08-01)
**Status.** Accepted (2026-08-01). Phase 1 is built and gated on branch `feat/hybrid-core-phase1`. It
supersedes the **distribution model** of ADR-011 only — the *TUI* stops being a separately downloaded
`plainkeep-ui` binary and becomes part of the core binary. ADR-011's stack split (stdlib-Python
engine, TypeScript for the interactive chrome, the machine contract as the seam between them) stands
unchanged and is the reason this was cheap.
**Context.** The entry plane was three interpreter spawns deep — bash dispatcher → `guardrail.py` →
`resolver.py` → the verb — and the engine lives *inside every vault*, which is what necessitates
`script/get`/`setup`/`update`, `engine.txt`, `.plainkeep-engine-ref` and the 3-way merges that made
the ADR-012 rename migration expensive. The full argument, the measurements behind it, and the three
architectures priced against each other are in
[`design/proposals/2026-07-29-hybrid-core-binary.md`](design/proposals/2026-07-29-hybrid-core-binary.md);
that proposal has a correction block at the top recording what Phase 1 falsified in it.
**Decision.** Adopt the proposal's Option B — a single compiled `plainkeep` core binary (TypeScript
under `cli/`, bun-compiled, tsgo-typechecked) that owns the dispatch, in front of the untouched
Python engine — and ship it in three phases. **Phase 1, what this branch actually contains:**
1. `cli/` absorbs `ui/`: one TS workspace producing two artifacts from one source tree
   (`plainkeep-core`, and `plainkeep-ui` for the floor).
2. The root `plainkeep` is a **shim**. `PLAINKEEP_CORE=auto|require|off` (with `PLAINKEEP_CORE_BIN`
   as an absolute override) chooses the core binary or the **bash floor** — the pre-core dispatcher
   preserved verbatim in the same file, still the zero-install path.
3. Guardrail and resolver **semantics** are re-implemented in TS (`cli/src/core/guardrail.ts`,
   `resolver.ts`) so gate + resolve + exec happen in one process. `bin/lib/guardrail.py` and
   `bin/lib/resolver.py` are **untouched** and still authoritative for everything Python.
4. In-core interceptions, in the order they landed: `__complete` (TAB completion), `ui` (the clack
   TUI, also reached by bare `plainkeep` on a TTY), and `mcp` (the stdio server). `help` is NOT
   intercepted — it still regenerates `plainkeep.json` through the Python manifest plane.
5. A permanent, Python-owned differential oracle: `test/run_core_parity.py` over language-neutral
   catalogs in `test/cases/core-parity/`, comparing the binary against the bash floor on exit status,
   stdout, stderr and the audit line — **217 checks**, of which 209 run locally on macOS and 8 are
   the opt-in fault-signal cells (below); one of the 217 is an accounting invariant asserting that the
   catalogs still declare every invocation they are pinned to declare, so coverage cannot shrink
   quietly. Plus 91 bun unit tests and a PTY gate for the TUI.
**Alternatives.** Priced in the proposal §2 and not re-argued here: **A** all-Python (uv-distributed,
Textual TUI) — rejected because the interactive chrome stays at interpreter speed and the existing
clack TUI is discarded; **C** full TS rewrite — rejected because the compute plane (LanceDB, MLX,
extract/STT/VLM tiers) and the frozen Python plugin SDK drag it back to the hybrid after months of
parity risk. The one thing measurement has since changed about that comparison is the latency claim —
see the first consequence.
**Consequences — what this costs, measured.** Every figure below was measured on **bun 1.3.14 /
macOS arm64**; none is an estimate. Each is also recorded next to the code it constrains
(`cli/src/core/dispatch.ts`'s header, `test/cases/core-parity/dispatcher.json`'s rationales), because
the run logs cited by name live under `.orchestrate/`, which is deliberately untracked and will not
outlive this branch's review.
- **The piped path pays TWO spawns, and is now slower than the floor it replaces.** The headline
  "three spawns become one" holds on a terminal and for a file, and is FALSE for a pipe. bun marks
  its own stdout/stderr `O_NONBLOCK` when they are pipes, that flag lives on the open file
  description, and `stdio: "inherit"` hands it to CPython, which then dies with `BlockingIOError` on
  the first write it cannot satisfy in full — a verb's output past the pipe buffer was silently lost
  (measured: 81,920 bytes delivered and exit 1, where the floor delivered 500,062 and exit 0). The
  fix spawns one `python3` helper to clear the flag, and it runs only when fd 1 or 2 is a pipe.
  **The cost is not merely "one extra spawn" — on that path the port is now a latency REGRESSION
  against the bash it replaces.** Medians over 25 reps on a trivial verb
  (`.orchestrate/review-task7-quality-r1.md` §R1): piped **core 84.2 ms vs floor 76.8 ms**, ~10%
  slower with non-overlapping spreads, where on a TTY or a file the core is ~11% faster (69.7 vs
  78.3 ms) and the helper itself accounts for +13.4 ms. Measured again independently while writing
  this entry, twice, on the real `status --json` verb through the shim
  (`.orchestrate/raw/task8-timing.log`, 25 reps per cell): piped **103.0 vs 95.5** and **103.0 vs
  94.5 ms** — the core **7.5–8.5 ms (~8–9%) slower** — against ~7% faster to a file (86.7–88.5 vs
  93.3–95.5). Different baseline, same finding, so the number to carry is the direction: **piped is
  slower, everything else is faster.** The two figures in this bullet are different quantities and a
  reader who subtracts them will not get the third: **~13–15 ms is the helper's own cost**, and
  **~7 ms is the net against the floor** — the core's ~6 ms head start on the non-piped path absorbs
  half the helper, which is why a 14 ms helper shows up as a 7 ms regression. (The floor's own
  pipe-vs-file delta is +0.6 ms, so essentially all of the 14 is the helper.) Interactive use pays
  none of it;
  agents driving MCP still win (59.2 ms/call vs the floor's 78.8 over 200 calls); the caller who pays
  is the shell script running piped verbs in a loop. **The durable fix belongs to Phase 2** — the
  helper spawns a Python interpreter to work around a bun limitation, inside a binary whose point is
  not needing Python, which is the dependency direction backwards. It is a stopgap carried
  deliberately: correctness first, and ~13–15 ms is the price of not truncating a verb's output.
  **That fix is upstream and identified rather than speculative:** `oven-sh/bun#33560` ("stdio: fix
  O_NONBLOCK leak from process.stdout and make console writer EAGAIN-safe") is this exact defect,
  child-inheritance case included, with #33827/#35953/#36066 adjacent. Checked 2026-08-01: all open
  and unmerged, and the newest bun release is still 1.3.14 — so the trigger for deleting the helper is
  a bun that carries #33560, and `dispatch.ts` names the lines to delete and the parity case that must
  stay green without them.
- **The Python guardrail and resolver are PERMANENT, so parity is a standing obligation rather than a
  migration step.** They cannot be deleted in any phase: the frozen plugin SDK re-exports the gate
  (`bin/lib/api.py:37`, `classify = guardrail.classify`), `bin/doctor/run.py:15` imports the
  guardrail, and `bin/lib/manifest.py:55` (which `help` and `mcp` render from) plus
  `bin/new/run.py:20` and `bin/plugin/run.py:29` import the resolver. Two implementations of one
  contract will drift unless something watches them, which is why `run_core_parity.py` joined the
  permanent suite instead of being a one-time gate.
- **Six pinned signal divergences, macOS-measured, Linux UNMEASURED.** Of the 21 default-terminating
  signals, **15 reproduce the floor exactly and 6 diverge** on bun 1.3.14 / macOS arm64
  (`.orchestrate/raw/task4-fix2-signal-matrix.log`): SIGILL, SIGFPE, SIGBUS and SIGSEGV die as
  SIGTRAP because bun's crash handler intercepts the re-raise (and prints a crash report to stderr
  where the floor is silent), and SIGPIPE and SIGXFSZ exit 128+N because bun ignores them
  process-wide. So for exactly the failures where the signal *was* the diagnosis, it stops being one.
  Unreachable from `bin/` today — nothing there sets a fault disposition — but reachable by a
  crashing native extension, which is the optional search/model plane Phase 2 packages. Every signal
  **but SIGEMT** — 20 of the 21, the exception being macOS-only, so a cross-platform catalog cannot
  guard it (the case rationale says so, and the log records it AGREEING on macOS) — is a named case in
  `test/cases/core-parity/dispatcher.json`, so a bun upgrade that changes delivery in either direction
  reddens a specific cell. **Linux delivery has never been measured**; CI's first
  run on ubuntu-latest is the measurement, and its expectations must not be pre-adjusted to match a
  guess.
- **Signal tests that make macOS write a crash report are opt-in on darwin — a deliberate coverage
  trade, in TWO suites.** Every death by a "create core image" signal makes macOS write a report and
  pop a dialog blaming plainkeep, per run. Both places that do it are gated behind the same pair of
  variables — `PLAINKEEP_PARITY_FAULT_SIGNALS=1` or `PLAINKEEP_REQUIRE_CORE=1` (the CI/release path):
  the parity catalog's 8 cells (`test/cases/core-parity/dispatcher.json`) and the bun-side signal
  sweep (`cli/src/core/dispatch.test.ts`), which is split so the quiet signals still run by default.
  Each prints a visible SKIP — the harness per cell, the bun side as a notice plus bun's own skip
  count — and neither ever reads as a pass. *Corrected 2026-08-01: the gate originally covered only
  the parity catalog, which moved the noise instead of removing it, since `bun test` is the most-run
  command in the repo and produced 5 reports per run on its own. The category is "anything in this
  repo that kills a child with a report-generating signal", not "the parity catalog's cells".* The
  cost is real and macOS-specific: four of the six divergence pins are only enforced on an opt-in run
  or a release check, because CI runs Linux, where the same cells pin a *different* platform's
  delivery. Partial coverage in this exact file is what hid two defect classes already.
- **Toolchain: bun >= 1.2.21 to build.** Older bun DROPS empty-string entries when spawning a child,
  so the dispatcher would silently eat an empty verb argument (verified broken on 1.1.45 and 1.2.0,
  fixed on 1.2.21; `cli/package.json`'s `check:bun` refuses to build below it). `.bun-version` pins
  **1.3.14**, the revision every measurement in this entry was taken on, and **`ci.yml`** installs
  from that file rather than `latest` — pinning behaviour that is measured, not assumed. Named as
  that one workflow rather than as "CI", because it is not true of the other: `release-ui.yml` still
  says `bun-version: latest`, so the artifact a floor user installs would be built on an unpinned
  toolchain. Moot only because that workflow is dead (see below) — reviving it means pinning it.
- **MCP is byte-identical to the Python server with one irreducible exception.** Whole sessions are
  byte-compared across both modes. The exception is key ORDER inside a non-string cmd.json value:
  `JSON.parse` hoists integer-like keys before any serializer sees them, so no serializer can recover
  the order Python preserves. It is pinned by `case_parser_key_order` and enumerated, with the other
  unreachable-from-this-repo fidelity limits, in `cli/src/core/mcp.ts`'s header and
  `.orchestrate/task-7-report.md` §7. "Byte-compatible" is true only with that list attached.
- **Unchanged, deliberately:** the `plainkeep.json/3` envelope, the exit-code protocol (0/2/3/4/5),
  the risk classes, the multi-root plugin model and the trust ceiling. The seam did its job — this
  refactor changes who dispatches, not what a caller sees. The bash floor remains a complete
  dispatcher, so `PLAINKEEP_CORE=off` is a real escape hatch and not a legacy branch.
- **Known and deferred, not lost:** `plainkeep ui` cannot be terminated except by SIGKILL once an
  action has run, on floor and core alike — `@clack/prompts` 0.7.0 never removes the SIGINT/SIGTERM
  listeners each `spinner()` adds, which drops bun's default disposition. It is a pre-existing
  disclosure pinned by `test/run_tui_pty.py`, and the ~15-line `withSpinner()` fix is deliberately NOT
  in Phase 1 because it changes TUI behaviour. The Minor/LOW/INFO findings this run's fourteen
  reviews batched rather than fixed — 57 after curation, none blocking — are tracked in
  [`followups.md`](followups.md), grouped by area with the file:line and the reason each was deferred.
  The three worth naming here: **`pyJsonDumps` emits invalid JSON for a `Map` with non-string keys**
  (verified unreachable today) and **is a line-for-line clone of `pythonRepr` that only it got the
  key-order fix for** (`cli/src/core/mcp.ts` vs `guardrail.ts` — the two walkers have begun to drift),
  and **`check:bun` does not gate `build:ui`**, so a bun older than 1.2.21 can still build the floor's
  UI binary.
**One operational consequence that is live right now, not deferred:** the `ui-v*` release pipeline
(`.github/workflows/release-ui.yml`) is **already non-functional**. Phase 1 moved the TUI's source
from `ui/` into `cli/` and deleted `ui/`; that workflow still reads `ui/package.json` and
`ui/src/version.ts` and still sets `working-directory: ui`, so pushing a `ui-v*` tag fails on its
first step. It was left byte-identical deliberately — it is tag-triggered, so it could not fire during
Phase 1, and whether the floor's separately-downloaded `plainkeep-ui` survives at all is a Phase 2/3
question. **Until it is repointed or deleted, the floor's `plainkeep-ui` cannot be re-released**:
`plainkeep setup ui --yes` still installs the last published asset and a contributor checkout still
builds from source (`cd cli && bun run build:ui`), but no new release can be cut. The workflow now
says so at the top of its own file, which is where a maintainer stands when it goes red.

**Phases 2–3, unchanged from the proposal** and NOT decided by accepting this entry: Phase 2 packages
`bin/**` as a uv-provisioned `plainkeep-engine` and takes the code out of the vault (and owns the
durable fix for the O_NONBLOCK helper); Phase 3 deletes `script/`, `engine.txt`,
`.plainkeep-engine-ref` and the `ui-v*` pipeline — which, per the paragraph above, is dead already
rather than working until then. **Amended in part by ADR-014 (proposed):** the word "packages" in
that sentence is wrong about the mechanism — the engine ships as a versioned immutable FILE TREE
exec'd by installed path, not as an importable Python package or a wheel. The phase boundary
(deletion is Phase 3) is unchanged and reaffirmed there.

## ADR-014 — plainkeep is an installed tool with registered data roots, not a directory named `~/plainkeep` (2026-08-01)
**Status.** **Accepted** (2026-08-02) — proposed 2026-08-01, accepted by the maintainer the next day.
It is the gate on Phase 2, and it is now open: Phase 2 implementation may start. It
**amends ADR-013's Phase 2 description**
in exactly one respect (the engine ships as a versioned immutable *file tree* exec'd by installed
path, not as an importable Python package), noted in place at the end of that entry; ADR-013's
Phase 1 record and its Phase 2/3 deletion boundary stand unchanged. Basis:
`.orchestrate/panel-synthesis-phase2.md` over two blind panel answers
(`.orchestrate/panel-fable-phase2.md`, `.orchestrate/raw/panel-codex-phase2.log`) and
`.orchestrate/scout-phase2.md`. Every file:line below was re-read while writing this entry.
> **Amended by ADR-015 (2026-08-02).** The snippet quoted just below is `guardrail.py` as it stood
> when this entry was written. ADR-015 anchored the wall to the active data root and converged the
> sibling-roots variable, so those four lines have moved. Nothing in this entry's reasoning changes:
> the wall still cannot police a *misresolved* root, and validating the root is still Task 1.
>
> **Implementation notes from Phase 2 Task 1b (2026-08-02)** — where the shipped code deviates from
> the wording below, recorded here rather than left for a reader to discover as a discrepancy. None
> of them changes a decision; each is a decision this entry did not make.
>
> 1. **`PLAINKEEP_HOME` requires a MARKER, not registration.** D3 says a validated root is
>    "structurally a vault (marker present), registered"; D4's steps 1 and 3 both go *through* the
>    registry, so registration is inherent there. For step 2 it is not, and requiring it would make
>    the canary this entry calls mandatory evidence — a full clone of the real vault at a scratch
>    path, deliberately unregistered — impossible to run against the real wall. The marker is what
>    keeps step 2 honest: `.plainkeep/` is gitignored, so pointing `PLAINKEEP_HOME` at a checkout of
>    the template still refuses.
> 2. **The engine-root disjointness check is NOT enforced yet.** D3 requires the data root to be
>    outside the engine root and vice versa. In Phase 1 the engine still lives *inside* the vault
>    (`$PLAINKEEP_HOME/bin`), which is what Phase 2's later tasks move — enforcing it now would refuse
>    every existing vault including this repo. It arrives with the engine's relocation.
> 3. **The core does not re-implement discovery; it runs the same Python module the bash floor runs**
>    (`python3 <engine>/bin/lib/vaultroot.py --select`). Discovery refuses in roughly fifteen distinct
>    ways, each with its own message, and two dispatchers whose refusal text must stay byte-equal is
>    the drift this repo has already paid for once — the same reasoning that kept `classify()` out of
>    `guardrail.ts`. It costs the core one process per invocation — measured A/B against a build
>    with the call stubbed out, 70.2 ms -> 99.0 ms on `vault list --json` (+28.8 ms median, +41%,
>    25 interleaved runs, bun 1.3.14 / macOS arm64 / CPython 3.12) — which dents ADR-013's
>    one-spawn headline and is stated there rather than hidden.
> 4. **A verb invoked DIRECTLY (`python3 bin/<verb>/run.py`) still trusts the `PLAINKEEP_HOME` it is
>    handed.** Validation belongs to the dispatcher and runs once per invocation; every product
>    surface (the shim, the core, `job run`, MCP) goes through one. `active_root()` reads the
>    variable and refuses when it is absent, but it does not re-validate — doing so would make every
>    module import pay a registry read. The escape hatch is deliberate and ungated.
> 5. **`resolver.py`'s `_ops_home()` was a FIFTH engine-relative fallback** (`ENGINE_BIN.parent`),
>    beyond the four `parents[2]` sites this entry enumerates. It is deleted with them: a plugin scan
>    is one of the things that must not happen before a root is validated.

**Context — the thing nobody had written down.** Phase 2 has been discussed as a packaging exercise.
The line that decides its real nature is `bin/lib/guardrail.py:42-49`:

```python
HOME = os.environ.get("PLAINKEEP_TEST_HOME", os.environ.get("HOME", "/Users/tamas"))
PLAINKEEP_HOME = Path(os.environ.get("PLAINKEEP_HOME", Path(__file__).resolve().parents[2]))
BIN = Path(__file__).resolve().parents[1]

VAULT = f"{HOME}/plainkeep"
```

The path-wall's vault segment is the literal string `$HOME/plainkeep`. **`PLAINKEEP_HOME` — the
variable every other subsystem uses — does not move it.** `_write_verdict` is a chain
(`guardrail.py:119-138`) whose ALLOW arms test `_under(path, VAULT)`, `_under(path, FILES)`,
`_under(path, WORK)` and `_under(path, DOTFILES)`; anything else falls through to
`return Decision(DENY, f"path escapes the three roots: {path}", "deny")` (`:138`). So a vault at any
path other than `~/plainkeep` has **every guarded write DENIED today**. "Moved vault" and "multiple
vaults" are not migration edge cases to be handled — they are capabilities the guardrail has never
had, and taking the engine out of the vault forces them into existence. plainkeep, as shipped, is not
"a tool that operates on vaults"; it is *one directory with a name*, and the whole safety story is
anchored to that name. That is a product-identity and trust-model change, and it is the prerequisite
for Phase 2 rather than a detail inside it.

Three further facts make the change non-optional rather than aspirational:

1. **`PLAINKEEP_HOME` is two variables wearing one name.** `bin/lib/paths.py:8` —
   `PLAINKEEP_HOME = Path(os.environ.get("PLAINKEEP_HOME", Path(__file__).resolve().parents[2]))` —
   copied verbatim into `guardrail.py:43`, `indexlib.py:23` and `vectorstore.py:12`. That
   `parents[2]` fallback *is* the "engine lives in the vault" assumption, in code, four times. Two
   modules already draw the right distinction and need no change: `resolver.py:24`
   (`ENGINE_BIN = Path(__file__).resolve().parents[1]  # bin/ — ships with the CODE, reserved`) and
   `manifest.py:18-19` (`BIN = …parents[1]   # the verbs live with the CODE (bin/), not under
   PLAINKEEP_HOME` / `MANIFEST = paths.PLAINKEEP_HOME / "plainkeep.json"  # …written to the data
   root`).
2. **The wall is not on every write path, so a misresolved root is not necessarily a loud refusal.**
   `bin/capture/run.py:21` computes `f = paths.INBOX / …` and `:27-30` does
   `paths.INBOX.mkdir(parents=True, exist_ok=True)` / `f.write_text(…)` / `paths.append_journal(…)`
   without ever calling `guardrail.classify`; the dispatcher gate admits the verb on *declared risk*
   alone (`guardrail.py:261-277`). A wrong data root therefore creates `inbox/`, `journal/` and logs
   wherever it points and returns success. The 217-check parity oracle stays green through this: it
   proves two dispatchers agree, not that the shared child wrote to the right root.
3. **Installed-binary self-location is already wrong for an installed binary.** `resolveHome()`
   (`cli/src/core/dispatch.ts:52-56`) falls back to `path.resolve(path.dirname(process.execPath),
   "..", "..")` — for `~/.local/bin/plainkeep-core` that is `~`. And the launchd renderer bakes the
   vault-local shim into every scheduled job: `bin/job/run.py:60` builds
   `[str(paths.PLAINKEEP_HOME / "plainkeep"), *toks[1:]]`, `:86` writes `PLAINKEEP_HOME` into the
   plist's `EnvironmentVariables`. Removing that shim without regenerating the jobs is `ENOENT` at
   2am.

A smaller divergence, found while writing this entry and not previously recorded: the sibling-roots
anchor is relocatable by **two different environment variables** depending on which module you ask —
`paths.py:17` reads `PLAINKEEP_ROOTS_HOME`, `guardrail.py:42` reads `PLAINKEEP_TEST_HOME`. The
decomposition below has to name one owner for that anchor too, or the wall and the paths module can
be pointed at different homes.

**Decision.**
1. **plainkeep becomes an installed tool that operates on registered data roots.** The vault is data
   the tool is *pointed at*, identified by a marker and a registry entry, at any path. It is no
   longer a directory whose name is part of the safety model. This is the product statement the rest
   of Phase 2 implements.
2. **`PLAINKEEP_HOME` splits, by name, ownership and precedence:**

   | Name | Meaning | Owner | Failure |
   |---|---|---|---|
   | `PLAINKEEP_HOME` | the selected vault's **data root, only** — kept because it is public SDK/API surface (`docs/plugins.md`, the scaffold, the test harness, `resolver.py:16-17` reads it per call) | invocation selector; the core **validates it and overwrites it in the child environment** | mandatory, **no fallback**: unset/invalid/unregistered → refuse before any I/O |
   | `PLAINKEEP_ENGINE` | the activated immutable engine tree | the core/updater; **caller input must not control it** — the core replaces any inherited value | absent/unverified → refuse; never derived from a user-writable fallback |
   | `PLAINKEEP_ROOTS_HOME` | the `~/work`/`~/files`/`~/dotfiles` anchor (`paths.py:17`), independent of which vault is selected | user/machine configuration | `guardrail.py:42`'s `PLAINKEEP_TEST_HOME` converges on this variable, or the divergence is recorded as deliberate |
   | vault registry | names/ids and canonical paths of known vaults | the installed core, stored **outside every vault** under user configuration | stale entry → refuse loudly, never rescan or substitute |

   The four `parents[2]` fallbacks (`paths.py:8`, `guardrail.py:43`, `indexlib.py:23`,
   `vectorstore.py:12`) and `resolveHome()`'s executable-relative fallback
   (`dispatch.ts:55`) are **deleted**, not narrowed.
3. **The data root is validated and mandatory, and validation happens before anything else.** Before
   the guardrail, the audit log, the resolver, the plugin scan or any verb spawn: absolute, existing,
   canonicalized for enforcement, structurally a vault (marker present), registered, not inside the
   engine root and the engine root not inside it, not inside a walled-off or cloud-sync tree
   (`guardrail.py:51-60`). Missing or stale selection exits **2** (`EXIT_USAGE`, `guardrail.py:29`
   with the isolation fallback at `:31`) naming the path and the mechanism that failed; a
   policy-denied location exits **5** (`EXIT_DENY`). Neither may create a log, an index or a
   directory on the way out. Because of Context 2, "it refuses" is proven by a **wrong-root
   side-effect test** — a `capture` against a bad root must create zero files — not by a guardrail
   unit test.
4. **The discovery contract for an installed binary with no vault-local shim**, checked in this
   order, each step validated before acceptance, no silent fall-through from an explicitly supplied
   value:
   1. explicit `--vault <registered-name|absolute-path>`;
   2. `PLAINKEEP_HOME` from the environment;
   3. **marker walk-up from `$PWD`** (git-style, nearest ancestor wins) — and the marker alone does
      not establish trust: walk-up may select only a marker whose id/path is **already registered**,
      so an arbitrary checkout of the public template cannot spoof a vault;
   4. the configured default vault in user configuration;
   5. otherwise **refuse**, listing the mechanisms. Never derive the vault from the installed
      executable.

   This resolves the four cases that have no answer today. **Multiple vaults:** only the selected
   vault is authorized for the invocation — registering several must not widen the wall to all of
   them. **A moved vault:** invocation from inside it identifies it by marker id, but rebinding the
   canonical path is an explicit act; invocation from elsewhere against a stale registry fails loudly
   rather than scanning the filesystem or silently choosing another vault. **cron/launchd:** never
   depend on discovery at all — jobs are regenerated to invoke the stable installed launcher with the
   validated root baked in absolutely (`job/run.py:60,86` is the code that must change), and a
   sanitized-environment launch is a gate, not documentation. **An agent shelling in from an
   arbitrary cwd:** step 3 serves it inside a vault; outside any vault it gets step 4 or a refusal —
   never a guess.
5. **The path-wall follows the validated data root.** `VAULT = f"{HOME}/plainkeep"`
   (`guardrail.py:46`) becomes the validated selected root. Without this, moved and multiple vaults
   stay blanket-DENY and the migration's own canary — a full clone of the real vault at some other
   path — cannot be exercised under the real wall.
6. **Amendment to ADR-013's Phase 2 description: the engine ships as a versioned immutable FILE
   TREE, exec'd by installed path — not as a Python package.** ADR-013's closing paragraph says
   Phase 2 "packages `bin/**` as a uv-provisioned `plainkeep-engine`", inheriting the proposal's
   wording (`docs/design/proposals/2026-07-29-hybrid-core-binary.md:209-210`). Both panelists reached
   the same correction independently, and the controller verified the linchpin: **no verb is ever
   imported.** Verbs are spawned by path (`cli/src/core/dispatch.ts:525`,
   `return spawnVerb(pickPython(home), script, args, home);`; the bash floor at `plainkeep:45`), and
   the only `importlib` use in the engine is capability probing that deliberately never imports —
   `bin/lib/imagelib.py:49` (`"""Optional dep present? importlib probe only — never imports…"""`) and
   `bin/lib/manifest.py:79`. There are no `__init__.py` files under `bin/` (verified: zero), and each
   verb's `sys.path.insert(0, str(Path(__file__).resolve().parents[1]))` is *correct for any location
   of an intact `bin/` tree* — it is only wrong if the tree is shredded into site-packages.
   Consequently: no `pyproject.toml`-shaped conversion of the verbs, no `__init__.py` campaign, no 34
   console entrypoints, no wheel. **uv's role shrinks to what uv is good at — pinning a CPython and
   locking/installing dependencies** (a deps-only project; no build backend). Phase 2's largest and
   riskiest task, as previously scoped, does not exist.
**Alternatives.**
(a) **Keep `PLAINKEEP_HOME` dual-meaning and let the engine derive its own root only.** Rejected: the
`parents[2]` fallback resolves *silently* into wherever the engine is installed, and per Context 2
the write path does not consult the wall, so the failure is a successful write to the wrong root
rather than a refusal. Nothing red ever appears.
(b) **Leave the wall anchored at `$HOME/plainkeep` and require every vault to live there.** Rejected:
it forecloses moved and multiple vaults permanently, and it makes the migration's mandatory canary —
a full clone of `gabros20/ops` at a scratch path — unrunnable against the real wall, which is the
one gate that stands between the design and irreplaceable notes.
(c) **Convert the engine into an importable package with 34 console entrypoints.** Rejected per
Decision 6: it buys nothing the runtime asks for and expands the parity surface.
(d) **A vault-local forwarding stub so old plugins keep resolving `lib.api`.** Rejected by both
panelists: it creates an engine-owned file in the vault that must still be installed, updated,
merged and eventually migrated — precisely the machinery Phase 2 exists to remove. The mechanism
Phase 2 adopts instead (the dispatcher exporting the engine's own `bin/` on `PYTHONPATH`) is a plan
detail, recorded in `.orchestrate/plan-phase2.md`, not an ADR-level commitment.
(e) **Marker-only discovery with no registry.** Rejected: any checkout carrying a marker could then
present itself as a vault and position the wall.
**Consequences — what this buys, what it costs, what it forecloses.**
- **It buys** the capabilities the wall has silently denied since it was written — a vault at any
  path, more than one of them, and a canary migration on a clone — plus the closure of the
  silent-wrong-root write class, which is the only known path to *data loss with a zero exit code*
  in a system whose entire guarantee is that the wall holds.
- **It costs a change to the trust model, and this is the honest headline.** Today the wall's vault
  segment is a constant: `$HOME/plainkeep`. Afterwards it is **configuration** — whoever supplies a
  root that passes validation positions one segment of the wall. Judgement, not measurement: this is
  not a new class of exposure, because `HOME` itself is environment-controlled at `guardrail.py:42`
  (and `PLAINKEEP_TEST_HOME` overrides it outright), so the wall was always env-movable; what changes
  is that it becomes *intentionally* movable, and validation — marker plus registration plus the
  engine-root disjointness check — is the only thing keeping it a wall rather than a suggestion. An
  unvalidated environment value moving the wall is the one genuinely new risk this entry creates, and
  it is why Decision 3 puts validation ahead of every other subsystem.
- **Anyone relying on today's behaviour loses two things.** "Writes outside `~/plainkeep` are denied"
  stops being an invariant of the binary and becomes an invariant *of the selected root*. And an
  unset `PLAINKEEP_HOME` stops being survivable: every caller, script, scheduled job or agent that
  leaned on the `parents[2]` fallback now exits 2 with a remediation. That is the intent — a loud
  break replacing a silent misresolution — but it is a break, and migration owns regenerating the
  jobs and symlinks that depend on it.
- **Registration becomes a real object with a lifecycle** — register, rebind, deregister, "which is
  the default" — that must be designed rather than implied. So does the contributor case: a checkout
  that is simultaneously an engine source tree and a vault now needs an explicit answer, because the
  fallback that used to answer it is gone.
- **The parity oracle's scope is now understood, not assumed.** 217 green checks prove the two
  dispatchers agree; they do not prove the child wrote to the right root. Phase 2 adds the
  side-effect test the oracle structurally cannot contain.
- **No deletion is authorized by this entry.** ADR-013's Phase 3 boundary — `script/`, `engine.txt`,
  `.plainkeep-engine-ref`, the `ui-v*` pipeline — stands; Phase 2 may stop *using* the vault's engine
  copy but does not remove it.
- **Carried open, deliberately unmeasured**: no `uv lock` has ever been run here (verified: no
  `pyproject.toml`, no `setup.py`, no `uv.lock` exist), so universal resolution and installed-artifact
  behaviour are unproven; whether `PYTHONPATH` set for a verb leaks into unrelated grandchildren is
  to be proven per-spawn, not assumed; and the contents, plugins and schedules of `gabros20/ops` have
  not been inspected by anyone in this design round, which is why the canary is mandatory evidence
  rather than a formality.

## ADR-015 — the path-wall is enforced on the write path, not just modelled (2026-08-02)
**Status.** **Accepted** (2026-08-02). Independent of ADR-014's status: it changes no discovery
contract and introduces no new variable. It is the prerequisite that makes ADR-014's Task 1 gate
observable at all, and it **supersedes the snippet quoted in ADR-014's Context** — that entry quotes
`bin/lib/guardrail.py:42-49` as it stood before this change; the reasoning it draws from the snippet
is unaffected, but the four lines themselves have moved.

**Context — the wall existed and nothing called it.** `bin/lib/guardrail.py`'s own docstring says
`classify()` is the reusable seam, "a write-verb calls this on the path IT computes (Iron Law — the
verb owns placement), so the wall holds where the path is actually known." No verb ever did. The
only callers in the tree were the test harness and `lib/api.py`'s re-export for plugins; the single
`classify(` hit inside a verb (`bin/triage/run.py`) is an unrelated local text classifier. What the
dispatcher enforced was `gate()` — the verb's **declared risk class** — after which
`bin/capture/run.py` computed `paths.INBOX / …` and went straight to `mkdir` / `write_text` /
`append_journal` with nothing between it and the disk.

This was found while designing Phase 2, by a panelist that refused to inherit the brief's claim, and
it had already survived being asserted in both directions by other readers. A guardrail unit test
could not have caught it: the failing region is not inside `classify()`, it is the absence of a call
to it. The scored-verdict lesson is recorded as a standing rule — **a gate that never exercises the
failing region is a green test of nothing**.

**Decision.**
1. **`bin/lib/vaultio.py`** is the enforced seam: `guard()` classifies
   `{"kind": "write", "path": …, "realpath": …}` and refuses `DENY` on the shared exit-code protocol
   (5 = `EXIT_DENY`). `mkdir` / `write_text` / `write_bytes` / `append_text` / `open_append` /
   `move` / `copy2` / `copytree` / `replace` wrap it. The verb still owns placement — the Iron Law is
   unchanged; the seam only asks whether the placement is allowed, at the one moment it is knowable.
2. **Every vault-data write in `bin/` routes through it** — 80 call sites across 28 files — including
   `paths.ensure_journal`/`append_journal`, so plugins reaching them through the frozen SDK
   (`lib/api.py`) inherit the wall with no plugin edits and no API change.
3. **The wall follows the active data root.** `VAULT = f"{HOME}/plainkeep"` alone is the
   *conventional* location and does not move with `PLAINKEEP_HOME`; anchored there, a vault anywhere
   else had every guarded write denied. `VAULT_ROOTS` now carries the conventional root **and** the
   active `PLAINKEEP_HOME`. Each root also carries its `realpath`, because `classify()` re-runs the
   verdict on the resolved path and takes the stricter one — on macOS a root under `/tmp` or
   `/var/folders` resolves through `/private/…` and would otherwise read as an escape.
4. **The sibling-roots anchor is converged.** `bin/lib/paths.py` read `PLAINKEEP_ROOTS_HOME`;
   `guardrail.py` read only `PLAINKEEP_TEST_HOME`. Two variables were relocating the same conceptual
   thing, which was invisible while nothing consulted the wall and would have denied every write to
   a relocated `~/files` once something did. `guardrail.HOME` now falls back
   `PLAINKEEP_TEST_HOME → PLAINKEEP_ROOTS_HOME → $HOME`, test-first so the validated spec model
   (`test/lib/guardrail.py`) and its 51 parity cases resolve exactly as before.
5. **`test/run_pathwall.py`** is the gate, and every assertion in it is a **filesystem walk**, not an
   exit code. During development the walled-root case exited 5 *while having already written the
   note* — the journal append refused after the inbox write succeeded — which is the whole argument
   for the assertion shape, observed live. It also ratchets: any new raw write in `bin/` fails the
   suite unless it joins the exemption list, keyed by source text with a stated reason.

**What this does NOT do, stated so a green suite is not over-read.**
- **It cannot police a misresolved data root**, because the wall is anchored to the value it would
  have to doubt. That is ADR-014 / Phase 2 Task 1 (marker, registry, no silent fallback), and this
  entry does not pre-empt it. What changes is that Task 1's gate is now *provable*: "a `capture`
  against a bad root creates zero files" is a real assertion instead of an unreachable one.
- **15 write sites stay outside the wall**, each listed with a reason: `~/work` fleet trees
  (`new project`, `repo clone/adopt`, `archive`) which the wall denies without task context, `~/.Trash`
  (the recoverable end of the decay machine, outside the three roots by design), a human-supplied
  `--out` on `share`, and the guardrail's own audit log (it records the refusal, so it cannot be
  subject to it).
- **One of those is a contradiction, not an omission, and it is the sharpest thing this work
  surfaced.** `_in_originals` denies every write under `~/files/**/in/` — "originals are read-only
  evidence", a *validated* case (`originals-in-readonly`) — while `files ingest --client` and
  `new client` exist precisely to put an original into `in/`. Creating a new original is not
  modifying evidence, but the rule as validated does not draw that line and the fix would flip a
  validated case, so it is recorded rather than quietly redrawn. Today only the verb's own
  uniquifying loop guarantees ingest never overwrites an original. **Resolved by ADR-016**, which
  redraws the rule as append-only and measures what that loop was actually worth: 217 of 320
  originals silently destroyed under 16-way concurrency.

**Alternatives rejected.** Adding `classify()` calls verb-by-verb with no shared helper (nothing
stops verb #35 from skipping it — the exact failure being fixed). Widening the wall so the exempt
destinations pass (it would delete the rule for `~/work` and `in/` to avoid writing down a policy
question). Refining `_in_originals` to "deny only an existing path" (correct-looking, but it flips a
validated case, and a wiring commit is the wrong place to move the spec).

**Consequences.** One `guardrail.classify()` call per guarded write — a pure in-process path
decision, no I/O beyond one `realpath`. A verb whose computed path escapes now exits 5 with the
wall's reason instead of writing; that is the point, and the ratchet is what keeps it true. Test
fixtures that symlink the repo into a temp `PLAINKEEP_HOME` now refuse writes that resolve back into
the real checkout — `test/run_setup_layers.py` was doing exactly that to `plainkeep.json` and now
copies it, which the wall was right to catch.


## ADR-016 — `~/files/**/in/` is append-only, and the create-only guarantee is the filesystem's (2026-08-02)
**Status.** **Accepted** (2026-08-02). Resolves the contradiction ADR-015 recorded rather than fixed.
It MOVES a validated rule — `test/cases/guardrail_cases.json`'s `originals-in-readonly` — so the spec
model, the enforced guardrail, the case file and the parity check all change in one commit.

**Context — the rule and the verb contradicted each other, and the verb won.** `_in_originals`
denied every write under `~/files/**/in/` ("originals are read-only evidence"). But `plainkeep files
ingest --client` exists to PUT an original there. Both statements cannot hold, and the way the tree
resolved it was the worst of the three options: ingest went on `test/run_pathwall.py`'s exemption
list, outside the wall entirely, with its own `while dest.exists(): dest = …-2…` loop as the only
thing standing between an arrival and an overwrite. So the rule that existed to protect originals was
the reason the only verb that writes one was unpoliced.

That loop is not a guarantee. Measured at BASE 5436ec6, with the loop as written: 16 processes
ingesting one filename into one directory destroyed **217 of 320 originals**, silently, in **20 of 20
rounds**. `shutil.move` onto an existing path simply replaces it, and the `exists()` test happened
earlier.

**Decision.** `~/files/**/in/` is **APPEND-ONLY**. An original may ARRIVE by ATOMIC CREATION;
overwrite, replace, mutate and delete of an existing leaf never happen — `kind: "delete"` under `in/`
is now DENY outright, on the path and on its realpath.

The wall admits exactly one write SHAPE there: an action carrying `create_only`, which is a claim
about a syscall that fails EEXIST — `os.link`, `open(O_CREAT|O_EXCL)`, `mkdir(2)` — and **never**
about a prior `exists()` test. `bin/lib/vaultio.py` gains `move_create_only()` and `mkdir(exist_ok=
False)` carries the same claim. The public `guard()` STRIPS `create_only`: only the primitives that
make the syscall may assert it. `files._arrive()` therefore contains no `exists()` call at all — it
ATTEMPTS each candidate name and lets `FileExistsError` advance to the next.

**The arrived leaf must be the vault's OWN inode, and the seam is not destination-only.** Two
corrections from the quality review, both reproduced end to end before they were fixed, and both
about the half of the rule that says an existing original is never mutated or removed:

  * `link(2)` grants a second NAME for an inode and on macOS it FOLLOWS a symlink, so linking a
    symlinked or already-hard-linked source filed a file the vault did not own: the other name
    stayed outside `in/`, stayed writable by anything, and one `printf` afterwards edited the
    "original" with no verb, no wall and no trace, leaving the shadow note's `sha256` a lie. Such a
    source is now COPIED into a private inode rather than linked — refusing it would be sound but
    would break the ordinary drop-folder case with no remedy the user could apply — and the copy is
    still create-only (`O_EXCL`), verified byte for byte before the source is unlinked. The link
    branch does not pre-stat the source (that would be a check-then-act on a path outside the
    vault); it verifies AFTERWARDS that the destination names the very file the source named and
    that the two are its only names, and backs out if not.
  * the wall classified only the DESTINATION, so `kind: "delete"`'s DENY under `in/` — a validated
    case since this ADR — had no caller on any verb's path, and `move_create_only`'s unconditional
    `os.unlink(src)` renamed an existing original to `-2` (exit 0, "filed") or moved it out of the
    hub entirely when the ingested path was already under `in/`. `vaultio._guard_delete()` is that
    seam: the source is classified before anything is created, and again at the syscall.
    `run_pathwall.py` pins every raw removal and rename in `bin/`, because `_is_raw_write` had no
    pattern for either and that is why nothing caught it.

**Why not "deny an existing path, allow a new one".** It is the obvious fix and it is TOCTOU-prone:
the wall would answer "this path is free" and another arrival would take it before the write. The
advisor gate rejected it, and the numbers above are what it looks like in practice. The rule as
shipped consults **no mutable state at all**, so the wall has no window of its own; the guarantee
lives one layer down, in the syscall.

**Consequences.** Three exemptions LEAVE `run_pathwall.py`'s `EXEMPT` map (18 sites -> 15) rather than
being reworded, and its stale-exemption ratchet is what proves the sites are really fixed. The
validated case is SPLIT, not deleted: `originals-in-mutate-denied` keeps the old action and verdict,
`originals-in-create-allowed` is the new half, and seven further cases follow it — three of them
pinning that `create_only` launders nothing (not iCloud through a symlink, not a path outside the
three roots, not another task's `~/work` repo), the rest covering the `in/` container, a case-folded
`IN/`, and delete under `in/` by path and by realpath. 51 cases -> 59. `paths.ROOTS_HOME` now mirrors
`wall.HOME`'s precedence, because from this change the two anchor the same tree from opposite sides
and disagreeing produces a DENY on a correct path. `test/run_originals.py` (107 checks) keeps the BASE
loop alive inside the test and ASSERTS THAT IT STILL LOSES FILES, so the concurrency gate cannot
quietly stop racing.

The **delete** half of the rule is enforced at the same seam and ratcheted structurally.
`vaultio._guard_delete` runs on the source of every primitive that takes a name away —
`move_create_only`, `_unlink_arrived_source`, and (from the r3 fix wave) `move` and `replace`, which
had been left behind on the same seam. `run_pathwall.py` (17 checks) reads those call sites out of
the **AST** rather than grepping the file for a string: the first version of that check was
`'_guard_delete' in seam`, and since that substring lives in the helper's own `def` and docstring it
stayed GREEN with every call site deleted — a green check of nothing, inside the ratchet that exists
to stop exactly that. The delete scan also matches `os.replace` / `Path.replace` now; `rename(2)`
destroys a name as surely as `unlink` does, and one live unpinned site (`vaultreg.py`'s registry
write) had been sitting under that blind spot. `_guard_delete` is scoped to the append-only question
alone — routing sources through `classify` wholesale imported its `.env` secret rule, so an ordinary
evidence file under a `.env/` directory became a hard refusal citing "reading .env / secret values is
denied", which is the wrong rule and the wrong message for a file that is being moved.

**What this does NOT do.** The shadow note `files._shadow()` writes beside each original picks its
slug with an `exists()`-scan of the wiki and then writes — the same shape, one tree over, and it is
NOT race-free. It lives inside the vault (a revertible git diff, not evidence), so it is out of scope
here and stated by `run_originals.py` as a SUITE-NOTE on every run. The cross-device branch is
exercised by forcing `link(2)` to report EXDEV; no second volume is mounted, so what is proved is
that the fallback's guarantee is `O_EXCL`'s.

It also does not make the copy crash-safe on a filesystem with **no hard links at all** (exFAT, some
FUSE mounts). Everywhere else the copy is STAGED — filled under a `.pk-arriving-*` name, verified,
then `link`ed onto the destination — so the destination name never exists in a partial state. Where
no second name can be minted, `O_EXCL` is applied straight to the destination and there is a window
between the create and the last byte in which the leaf is SHORT. An exception unwinds and removes it;
a `SIGKILL` or a power loss does not, and append-only means that truncated leaf can never afterwards
be replaced. There is no atomic create-only rename to fix this with; the residue is stated in
`_direct_create_only_copy`'s docstring, which is where a reader meets it.

**Where the staging leaf lives, and why it is not under `in/`.** It used to be filled *beside* the
destination — inside the append-only tree — which made the same `SIGKILL` leave a second, subtler
residue: a `.pk-arriving-*` orphan under `in/` that **nothing could ever remove**, because
`classify({"kind": "delete", …})` denies it and `vaultio._guard_delete` enforces that. The feature
created permanent litter it forbade itself to clean up. `vaultio._staging_dir` now walks up from the
destination to the nearest directory that is *not* inside an originals tree, staging there instead —
so a crash orphan sits outside the rule and stays removable. The append-only rule itself is unchanged;
the marker simply stopped being subject to it, which is the fix that does not cost a hole. The walk
stops if it would leave the **filesystem** (an `in/` that is its own mount), because the staging leaf
is `link`ed onto the destination and that link must not fail EXDEV; in that case staging falls back
beside the destination and the old residue returns, which is the pre-existing behaviour and never
worse than it.

## ADR-017 — the engine is a versioned, immutable tree outside every vault (2026-08-02)
**Status.** **Accepted** (2026-08-02). It implements ADR-014 D2 and D6 rather than deciding anything
new about them: D6 already said the engine ships as a versioned immutable *file tree* exec'd by
installed path, and D2 already named `PLAINKEEP_ENGINE` and its ownership. What this entry records is
the part D2/D6 left to implementation — **where** the tree lives, **how** the rule that caller input
must not control it is made true, and what turning ADR-014 D3's disjointness check on actually costs.
Basis: Phase 2 Task 2, implemented against a fully green suite; every claim below was measured or
mutation-tested rather than argued.

**Decision.**

1. **The install location is `${XDG_DATA_HOME:-$HOME/.local/share}/plainkeep/engine/<version>/`, with
   a `current` symlink beside the versioned directories.** XDG-correct, and next to
   `$XDG_CONFIG_HOME/plainkeep/` where ADR-014 already put the vault registry. `PLAINKEEP_ENGINE_HOME`
   relocates the install ROOT; it is read by the installer surface only (`--install`, `--activate`,
   `--print`) and **never by a dispatch**, which is what lets the hermetic test suite install engines
   into temp directories without opening a second way to steer where code is loaded from.

2. **`PLAINKEEP_ENGINE` is an OUTPUT, never an input, and that is how D2's rule is satisfied.** The
   engine root is derived from where the code is — `Path(__file__).resolve().parents[2]` in
   `bin/lib/enginetree.py`, `realpath(execPath)/../..` in the core, a `$0` symlink chain ending in
   `cd -P` in the bash floor — and **nothing that decides where to load code from ever reads the
   variable**. Both dispatchers OVERWRITE it at their entry point, before any flag branch and before
   discovery. The alternative — read it and validate it — was rejected: a validated variable is still
   a variable, and the failure it admits (a caller naming a well-formed engine tree of their choosing)
   is exactly the one D2 forbids.

   Its consumers are the processes that genuinely cannot self-locate: a PLUGIN verb under
   `<vault>/plugins/<pack>/<verb>/`, a frontend script, a scheduled job. `templates/verb/run.py` — the
   scaffold every plugin starts from — is the load-bearing one; it bootstrapped `lib` through
   `$PLAINKEEP_HOME/bin`, so without this variable every scaffolded plugin would have died on its
   import the moment the engine moved.

3. **The ownership manifest is executable, not descriptive.** `enginetree.OWNED_TREES` /
   `OWNED_FILES` is simultaneously what `--install` copies and what `verify()` checks; an install
   that fails verification is never renamed into place and never activated. Measured against the
   tree at implementation time: **35 verb directories** (each with `run.py` AND `cmd.json`) and
   **24 `bin/lib` modules** — the plan section's "×34 / ×19" was stale before this task began, and
   the counts are pinned in the suite so a silent shrink reddens.

4. **An installed engine is read-only (dirs 0555, files 0444/0555).** That is the property that makes
   "immutable" mean something: the manual this task rewrote used to describe editing engine files in
   place, and now there is nothing to edit. The measured cost is stated in Consequences.

5. **ADR-014 D3's disjointness check turns ON here** — the data root may not be inside the engine
   root and the engine root may not be inside the data root, refused with **exit 5** (`EXIT_DENY`),
   the same code as the walled-off/cloud-sync location verdict it sits beside and for the same
   reason: it is a refusal about WHERE, not a missing selection (which is 2). Task 1b defined the
   rule and deliberately did not enforce it, because while the engine was `<vault>/bin` the rule was
   unsatisfiable and would have refused every existing vault. A sequencing split, not a legacy
   exception.

6. **Task 1b's `require_engine(sel)` is INVERTED, not deleted.** It asked whether the selected VAULT
   carried a copy of the engine — a question that existed only because Phase 1 ran the engine out of
   the vault it acted on. The seam is kept (it is the one function both dispatchers run, which is
   what makes their refusals byte-identical rather than two spellings that drift) and its subject
   moves to the ENGINE tree. Deleting it outright would have been acceptable; keeping the old
   question would not, because it refuses every data-only vault — the shape Task 5's `init` exists to
   produce.

7. **`script/setup` installs the engine and puts the INSTALLED launcher on PATH.** This is the
   explicit answer to the contributor case ADR-014 raised and left open. The checkout stays both
   things — the engine SOURCE and a registered data vault — and the two roles stop being the same
   directory. `script/update` refreshes the source; installing it is a second, separate step.

**Alternatives.**
(a) **Read and validate `PLAINKEEP_ENGINE` instead of overwriting it.** Rejected per Decision 2.
(b) **Put the engine under `~/.plainkeep/engine/`.** Rejected: it invents a dotfile home beside the
XDG dirs ADR-014 already committed to for the registry, for no gain.
(c) **Enforce disjointness in Task 1b with a legacy exception for `<vault>/bin`.** Rejected in the
plan and re-affirmed here: a silent exception for the one shape that violates the rule defeats the
rule, where a sequencing split does not.
(d) **A `plainkeep engine` VERB rather than a module CLI.** Deferred, not rejected. A verb changes the
verb surface, `plainkeep.json`, the completion catalogs and the help output, none of which this task
was scoped to move; `bin/lib/enginetree.py --install|--activate|--verify|--print` is what
`script/setup` and the harness call today.

**Consequences.**
- **It buys** the thing Phase 2 exists for: a vault is data. A vault holding only notes now
  dispatches (all 271 checks in `test/run_discovery.py` run against data-only vaults), an engine
  upgrade is not a merge into somebody's notes, and a rollback is re-activating a previous version.
  It also closes a hole nobody had named: `resolver.ts` derived engine `bin/` as `<home>/bin`, so a
  vault that happened to carry `bin/capture/` had it resolved as an `engine` verb — the one source a
  plugin is forbidden to shadow.
- **It costs a break for anyone dispatching a vault's own launcher against that vault**, which is
  what `script/setup` produced before this task and therefore what every existing install looks
  like. It is exit 5 with a remediation naming the installer, not a silent misresolution — but it is
  a break, and re-running `script/setup` is the migration.
- **CPython cannot write `__pycache__` into a read-only tree**, so an installed engine re-compiles
  the `bin/lib` modules it imports on every invocation. **Measured** — macOS arm64 / CPython 3.12,
  the same tree installed twice (once sealed, once `--writable`), both warmed three times, then 25
  runs of `plainkeep vault list --json` through the bash floor INTERLEAVED with alternating order:
  **161.6 ms median read-only against 144.0 ms writable, +17.6 ms / +12.2%** (writable min/max
  141.1/151.3, read-only 158.5/172.5; the writable tree had 7 `.pyc` files after warm-up, the sealed
  one 0). `.orchestrate/raw/task-p2-2-pycache-bench.log`. It is paid by every spawned Python verb,
  and on the compiled-core path it is paid by the discovery spawn and the verb rather than by the
  core's own work. The fix, if it is ever worth taking, is `PYTHONPYCACHEPREFIX` pointing at a cache
  directory outside the tree; it is NOT taken here because it adds a third location to reason about
  for a cost smaller than the discovery spawn ADR-014 already accepted (+28.8 ms). Recorded so the
  next person measures rather than rediscovers.
- **`PLAINKEEP_ENGINE` becomes public API for plugins.** A plugin bootstraps `lib` through it and has
  no fallback; a plugin invoked outside a dispatch refuses with exit 2 rather than guessing a path.
  That is deliberate — a plugin reached outside a dispatch has not been gated either.
- **Carried open, deliberately unmeasured**: no engine has been installed on a machine other than
  this one, so the XDG default path is exercised only through `PLAINKEEP_ENGINE_HOME` overrides in
  the suite plus one manual end-to-end run; multi-version rollback (`--activate <older>`) has unit
  coverage but no field use; and `plainkeep plugin`'s trust ceiling has not been re-examined against
  a plugin that now imports `lib` through an env var the dispatcher sets.
