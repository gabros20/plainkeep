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
**Status.** PROPOSED. Phase 1 is built and gated on branch `feat/hybrid-core-phase1` (nothing
pushed); this entry is written for the promotion decision, not as a record of one. It
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
   stdout, stderr and the audit line — **216 checks**, of which 208 run locally on macOS and 8 are
   the opt-in fault-signal cells (below). Plus 91 bun unit tests and a PTY gate for the TUI.
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
- **The fault-signal cells are opt-in on macOS — a deliberate coverage trade.** Each of those deaths
  makes macOS write a crash report and pop a notification blaming plainkeep, per run. The 8 cells
  whose signal has the "create core image" default action are therefore skipped locally on darwin
  unless `PLAINKEEP_PARITY_FAULT_SIGNALS=1` or `PLAINKEEP_REQUIRE_CORE=1` (the CI/release path) is
  set; a skipped cell prints a visible SKIP and is counted apart from the passes, never as one. The
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
  in Phase 1 because it changes TUI behaviour. The 101 Minor/LOW/INFO findings from this run's
  fourteen reviews — batched rather than fixed, none blocking — are collected in
  `.orchestrate/followups.md` (a run artifact, untracked: read it before the branch's review notes are
  cleared, and promote the entries worth keeping into issues). Three are named here so that promoting
  this ADR does not depend on that file surviving: **`pyJsonDumps` emits invalid JSON for a `Map` with
  non-string keys** and **is a line-for-line clone of `pythonRepr` that only it got the key-order fix
  for** (`cli/src/core/mcp.ts` vs `guardrail.ts` — the two walkers have begun to drift), and
  **`check:bun` does not gate `build:ui`**, so a bun older than 1.2.21 can still build the floor's UI
  binary.
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

**Phases 2–3, unchanged from the proposal** and NOT decided by promoting this entry: Phase 2 packages
`bin/**` as a uv-provisioned `plainkeep-engine` and takes the code out of the vault (and owns the
durable fix for the O_NONBLOCK helper); Phase 3 deletes `script/`, `engine.txt`,
`.plainkeep-engine-ref` and the `ui-v*` pipeline — which, per the paragraph above, is dead already
rather than working until then.
