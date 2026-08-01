# Hybrid core proposal — a compiled TS core binary over the Python engine, code out of the vault

> ## CORRECTION (2026-08-01, after building Phase 1) — two claims below are FALSE as written
>
> The text of this proposal is left exactly as it was written on 2026-07-29, because a design
> document that quietly edits itself to match what was built teaches nobody anything. Phase 1 is
> built (branch `feat/hybrid-core-phase1`, ADR-013 in [`../../DECISIONS.md`](../../DECISIONS.md)) and
> measurement falsified two of its claims. Read the two corrections before quoting anything below.
>
> **1. "Three interpreter spawns to run one verb" → one (§1, §3) is true on a terminal and FALSE on a
> pipe, which is the path agents and scripts use.** bun marks its own stdout/stderr `O_NONBLOCK` when
> they are pipes; the flag lives on the open file description, so `stdio: "inherit"` hands it to
> CPython, which dies with `BlockingIOError` on the first write it cannot satisfy in full — the verb's
> output past the pipe buffer was silently lost. Clearing the flag needs a second process (bun exposes
> no way to do it from JS), so **the piped path pays TWO spawns, not one**, at a measured +13.4 ms.
> Medians over 25 reps on bun 1.3.14 / macOS arm64: piped **core 84.2 ms vs bash floor 76.8 ms** — on
> the piped path the core is now ~10% SLOWER than the bash it replaces, not faster. On a TTY or a file
> it is ~11% faster (69.7 vs 78.3 ms), and an MCP tool call still wins outright (59.2 vs 78.8 ms/call).
> The trade was taken deliberately — losing a verb's output is a correctness defect and 13 ms is not —
> but the durable fix is Phase 2's: today's stopgap spawns a *Python interpreter* to work around a
> *bun* limitation, inside a binary whose stated point is not needing Python. Mechanism and cost:
> `cli/src/core/dispatch.ts`, "O_NONBLOCK REMOVAL".
>
> **2. "`test/run_guardrail.py` and `test/run_resolver.py` already exercise this surface as a
> subprocess — they become the acceptance gate, run against the binary" (§3) is wrong about
> `run_guardrail.py`, and wrong about what follows from it.** `run_guardrail.py` loads
> `bin/lib/guardrail.py` **in-process via `importlib`** (`test/run_guardrail.py:9,20-22`) and never
> spawns anything, so it can never be pointed at a binary; `run_resolver.py` does both (an in-process
> import at line 63 *and* a `subprocess.run` of the dispatcher at line 114). The consequence is bigger
> than the mechanism: the Python guardrail and resolver are **permanent**, not migration scaffolding.
> The frozen plugin SDK re-exports the gate (`bin/lib/api.py:37`), `bin/doctor/run.py:15` imports the
> guardrail, and `bin/lib/manifest.py:55`, `bin/new/run.py:20` and `bin/plugin/run.py:29` import the
> resolver — so no phase deletes them, and **parity is a standing obligation rather than a one-time
> gate**. That is why the acceptance oracle is a new, permanent, Python-owned differential harness
> (`test/run_core_parity.py` over `test/cases/core-parity/`, 216 checks) instead of the existing
> suites re-pointed at the binary.
>
> Also worth knowing before quoting §5: the machine contract, the exit-code protocol and the risk
> classes did survive Phase 1 unchanged, as promised. What §5 does not mention, because nobody knew
> it yet, is that a verb killed by SIGILL/SIGFPE/SIGBUS/SIGSEGV reports SIGTRAP under the core and
> that SIGPIPE/SIGXFSZ exit 128+N — six pinned divergences out of 21 signals, macOS-measured, Linux
> unmeasured. See ADR-013's consequences.

**Status: PROPOSED (2026-07-29).** Not yet an ADR. If accepted, this becomes ADR-013 and supersedes
the *distribution model* of ADR-011 (engine files flowing into vaults via `script/update`) while
keeping ADR-011's stack split and its reasoning intact.

**Provenance.** Produced 2026-07-29 from a design conversation that priced three architectures
(all-Python core, hybrid TS-core + Python engine, full-TS rewrite) against the repo's own
principles, measurements, and the empirical pain of the 2026-07-28 rename migration (ADR-012). The
numbers below were measured on the maintainer's machine and from `test/run_search.py`'s labeled
query set.

---

## The verdict in one paragraph

plainkeep's seams are right — one door, risk-classed guardrail, the generated `plainkeep.json/3`
contract, multi-root plugins — but its **entry plane and distribution model** are the source of
nearly all recent operational pain. The engine is ~10.3k lines of interpreted files living *inside
every vault*, which is the single fact that necessitates `script/get`/`setup`/`update`,
`engine.txt`, `.plainkeep-engine-ref`, sha256 publishing, 3-way merges — and caused the rename
migration's stale-file and venv-breakage teeth. The fix is not a rewrite of the verbs; it is moving
the code out of the vault and putting one compiled artifact in front of it. **Proposal: a single
`plainkeep` binary (TypeScript, bun-compiled, tsgo-typechecked) that owns the dispatcher, guardrail,
resolver, help/completions, the TUI (absorbing `plainkeep-ui`), the MCP server, and the funnel
(`init`/`update`/`doctor`) — driving the existing Python engine as a versioned package provisioned
by uv.** The vault becomes 100% the owner's plaintext. Verbs, the plugin SDK, the retrieval planes,
and all test suites stay exactly as they are.

---

## 1. Why now — the evidence

- **The rename migration (ADR-012) was expensive *because* code lives in vaults.** The Mac Mini
  migration needed a two-pass `script/update`, manual deletion of stale engine files
  (`ops`, `ops.json`, `_ops`, `skills/operate-ops/`), a venv rebuild (absolute-path shebangs broke
  on `mv ~/ops ~/plainkeep`), and a ref-file rename. Every one of those steps exists only because
  the engine is files-in-the-vault.
- **The entry plane is three languages deep.** bash dispatcher → Python guardrail spawn → Python
  resolver spawn → Python verb spawn. Measured: a verb costs **~280 ms** end-to-end; a bare
  Python process is ~73 ms; the guardrail alone ~86 ms. Three interpreter spawns to run one verb.
- **The dispatcher carries a venv-liveness probe hack** (probe `.venv/bin/python3 -c ''` so a
  half-dead venv can't 126/127 every verb) — a symptom of interpreter management being nobody's
  explicit job.
- **The funnel lives outside the one door.** `script/get`/`setup`/`update` are bash scripts a user
  runs directly — the only mutations of the system that don't go through `plainkeep <verb>`,
  guardrail, and `.logs/`.

## 2. The decision space — three options, priced

### Option A — all-Python core (uv-distributed package, Textual TUI)

Fold the funnel into Python verbs, package the engine, distribute via uv. **Buys:** zero port risk
(guardrail/resolver stay the tested code), one artifact/one version, tests carry over ~100%,
smallest effort. **Costs:** the interactive chrome (completions on every TAB, TUI navigation, help,
refusal round-trips) stays at interpreter speed ~80–100 ms vs ~10 ms compiled; the existing 663-LOC
clack TUI is discarded and rebuilt in Textual — an ecosystem that is not the maintainer's stack
(ADR-011 chose TS for the TUI explicitly); no single-binary story (PyInstaller-class tools choke on
native wheels like LanceDB).

### Option B (recommended) — hybrid: compiled TS core + Python engine via uv

Detailed in §3. **Buys:** compiled chrome (~10 ms), the clack TUI kept and absorbed, funnel inside
the one door, code out of the vault, engine and tests untouched. **Costs:** a byte-faithful guardrail
port (gated by existing golden tests), and a permanent two-artifact version pairing
(binary ↔ engine package).

### Option C — full TS rewrite (~10.3k LOC, verbs included)

**Buys:** one language, one true single binary, ~10 ms verbs, compile-time-typed contract
generation. **Why it fails on inspection:** honestly pursued, "all TS" converges back on the hybrid —

- Embeddings and enrichment already speak Ollama HTTP from stdlib (`bin/lib/embed.py`,
  `bin/lib/enrichlib.py`) — portable for free, but that's not where the work is.
- LanceDB-TS and an ONNX reranker are napi native addons; `bun build --compile` with embedded
  native addons across four cross-compiled release targets is the flakiest corner of the toolchain —
  the release pipeline gets harder, not simpler.
- The extract tiers (trafilatura, mlx-whisper / faster-whisper / parakeet) and mlx-vlm image
  reading are Python/MLX-native. TS "equivalents" (readability+turndown, whisper.cpp,
  vision-via-Ollama) are *different engines*: every swap re-opens a measured result
  (ADR-002/005/006 quality — e.g. semantic-bucket recall@5 going 0.00 → 1.00 with the current
  stack). Apple's local-ML ecosystem is Python-first and keeps regenerating this gravity.
- The plugin SDK (`lib/api.py`, frozen `PLAINKEEP_API_VERSION`, plugins are Python `run.py` trees)
  dies unless a Python runtime ships anyway — at which point the result *is* the hybrid, reached
  via months of parity risk. Frontmatter/filing/date/Hungarian-text parity bugs from a port land as
  **writes in users' vaults**.
- The beneficiaries can't feel it: agents don't notice 100 ms and never open the TUI.

| | A: all-Python | **B: hybrid** | C: full TS |
|---|---|---|---|
| TUI | Textual (rewrite) | **clack, compiled, kept** | clack, compiled, kept |
| Chrome latency | ~80 ms | **~10 ms** | ~10 ms |
| Verb latency (today ~280 ms) | ~100 ms | ~100 ms | ~10 ms |
| Port/parity risk | none | small (guardrail, golden-tested) | large (10k LOC + ML re-benchmarks) |
| Release artifacts | one package | binary + engine pair | one binary, napi cross-compile pain |
| Plugin SDK | intact | intact | broken, or hybrid-anyway |
| ML plane (measured quality) | native | native | regressed or sidecarred |
| Effort | smallest | small–medium | months |

**Option B is not a fork in the road; it is the trunk.** If TS ever earns more territory, verbs port
one at a time behind the unchanged contract, gated by the same golden tests, when feature work
touches them anyway. Option A closes the clack door; Option C is Option B reached the expensive way.

## 3. The hybrid architecture — three planes, two artifacts

```
┌─ Plane 1: plainkeep core (ONE compiled binary; TS source in cli/, bun-compiled, tsgo-typechecked)
│    dispatcher · guardrail · resolver · help/completions · TUI (absorbs ui/) · MCP server
│    funnel verbs: init (wizard | --yes) · update (self-update + engine sync) · doctor (env plane)
│    contract-driven: renders menus/help/tools from plainkeep.json — knows no verb internals
│
├─ Plane 2: plainkeep engine (versioned Python package; today's bin/** unchanged)
│    provisioned by uv: pinned CPython + locked deps; extras map to setup layers
│    (plainkeep-engine[search] → lancedb, [models] → mlx/whisper tiers)
│    verbs · lib/ · plugin SDK (lib/api.py, frozen) · plugins via multi-root resolution
│
└─ Plane 3: Ollama (existing seam, untouched)
     embed (embeddinggemma, prompt profiles) · enrich (gemma4:e4b) · vision as it lands
```

- **One door, now including the funnel.** `script/get`/`setup`/`update` are deleted; `init`,
  `update`, `doctor` are verbs behind the same guardrail and logs. `curl … | sh` installs the
  binary; everything after is `plainkeep <verb>`.
- **Per-verb cost drops ~280 ms → ~100 ms**: guardrail + resolve run in-process; one Python spawn
  remains (the verb itself).
- **The contract is the inter-plane interface.** `plainkeep.json/3` was designed so stacks are
  irrelevant to each other (ADR-007/ADR-011); this proposal is that seam doing its intended job.
  The envelope, exit-code protocol (0/2/3/4/5), and risk classes do not change.
- **The vault becomes pure data**: wiki/ tasks/ journal/ inbox/ templates/ jobs/registry.json +
  `.plainkeep/` config. No `bin/`, no `script/`, no `engine.txt`, no `.plainkeep-engine-ref`, no
  venv probe. `script/update`'s 3-way merge machinery is deleted, not ported.
- **Update is atomic**: `plainkeep update` swaps the binary (checksum-verified) and `uv sync`s the
  engine to the version the binary pins. No merges, no ref files; survives folder moves.

### The uv provisioning contract

- The core vendors/bootstraps uv (MIT, static binary — vendorable if the single-vendor risk ever
  bites) and uses it to provision a pinned CPython and the locked engine env under
  `$PLAINKEEP_HOME/.plainkeep/` (or a shared cache) — the bare-`python3` floor and the dispatcher
  venv probe are retired together.
- The binary **pins the engine version** it drives (compatibility pair released together). `doctor`
  verifies the pair and repairs with `uv sync` — the "broken venv" class of failure becomes
  self-healing.
- Setup layers map to extras: stage-1 search is stdlib (no extra), `[search]` adds LanceDB,
  `[models]` adds the extract/STT/VLM tiers. Same layers, same wizard, new mechanism.

### Guardrail parity — the one port that must be exact

The TS core reimplements `guardrail.py` + `resolver.py` semantics: risk classes, `--yes`/dry-run
gating, path-wall, did-you-mean, logging, exit codes 0/2/3/4/5. `test/run_guardrail.py` and
`test/run_resolver.py` already exercise this surface as a subprocess — they become the acceptance
gate, run against the binary. No release until byte-faithful on the full matrix.

## 4. Sequencing — each phase independently shippable

1. **Phase 1 — the core binary.** New `cli/` workspace (absorbing `ui/`). Binary replaces the bash
   dispatcher + `plainkeep-ui`; shells to `bin/<verb>/run.py` in-place (engine still in the repo).
   Bare `plainkeep` in a TTY opens the TUI; `--flags`/non-TTY stays machine mode. Gate: full suite
   green against the binary; guardrail parity matrix green. Users get the premium surface with zero
   verb changes.
2. **Phase 2 — engine out of the vault.** Package `bin/**` as `plainkeep-engine`; core provisions it
   via uv; `init` creates data-only vaults; `update` becomes binary+engine sync. Existing vaults
   migrate with one command (`plainkeep update` detects the legacy layout, removes engine files from
   the vault after a clean sync).
3. **Phase 3 — delete the scaffolding.** `script/`, `engine.txt`, `.plainkeep-engine-ref`, the
   `ui-v*` release pipeline, the gh-authenticated binary download. Natural moment for a deliberate
   `plainkeep.json/4` bump if the frozen `ops_*` wire keys are ever to be renamed — a contract
   decision, separate from this proposal.

**Standing rules** (the strangler discipline): never port the compute plane (extract/STT/VLM/
rerank/LanceDB) — that's where the measured quality lives; port a pure-stdlib verb to TS only when
feature work touches it *and* the golden tests gate it. No scheduled "big rewrite," ever.

## 5. What this does NOT change

- Plaintext truth, git spine, no daemon, no server — the binary is invoked-per-call, never resident.
- The retrieval stack: FTS5 + LanceDB + EmbeddingGemma + rerank, per ADR-002/005/006 measurements.
  (An earlier sqlite-vec suggestion is explicitly rejected here for the record — brute-force, dies
  past ~1M vectors; the 100k–500k-note target requires disk-ANN. "Leaner" is not the criterion;
  *measured better under the embedded-no-server rule* is.)
- The machine contract envelope and exit-code protocol (`plainkeep.json/3` — no second schema).
- The plugin model: Python `run.py` trees, frozen SDK, trust ceiling, multi-root resolution.
- The private-vault derivation disappears as *machinery*, but the template/instance concept survives
  as "binary + engine package vs. your data repo" — a cleaner cut of the same idea.

## 6. Open questions

1. **Version-pairing policy** — lockstep releases (binary pins exact engine version) vs. a
   compatibility range. Lockstep is simpler and recommended until proven painful.
2. **uv bootstrap** — vendor the uv binary inside the installer vs. download-on-install. Vendoring
   is more plainkeep-shaped (no third-party fetch at install time) at the cost of installer size.
3. **Name grabs** — `plainkeep` on npm and PyPI were free at rename time (2026-07-28); Phase 2
   publishes to PyPI and the installer wants npm/brew channels. Register before this matters.
4. **Agent patchability trade-off** — today an agent can patch engine files inside its own vault;
   after Phase 2 it patches via PRs to the template instead. Accepted here as healthier (vault
   diffs become pure knowledge diffs), but it is a real behavior change for `skills/operate-plainkeep`.
5. **Windows** — bun compiles to windows-x64 and uv provisions CPython there; nothing in this
   proposal blocks it, but the path-wall and launchd-shaped automation layer remain macOS/Linux
   assumptions. Out of scope, noted for the record.
