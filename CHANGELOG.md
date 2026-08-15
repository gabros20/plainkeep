# Changelog

Notable changes to the plainkeep platform, newest first. Format follows
[Keep a Changelog](https://keepachangelog.com). The *why* behind load-bearing decisions lives in the
ADR log ([`docs/DECISIONS.md`](docs/DECISIONS.md)); this file records *what changed*.

## [Unreleased]

### Added
- **Schedule times are a product surface: `plainkeep job set`, and the setup wizard asks.** The
  hours automation runs at were two literals in `jobs/registry.json` that only a hand-edit could
  change, so a day that runs 08:00–22:00 got a system that opened it at 07:30 and closed it at
  18:30. `plainkeep job set <name> --daily HH:MM | --weekly "Day HH:MM" | --monthly "D HH:MM" |
  --every <minutes>` rewrites **only** that job's schedule (every other field and the file's order
  are preserved; `--dry-run` shows the entry and writes nothing), and a name the engine knows but
  the registry lacks is **seeded from the engine defaults** — which is how a vault created before a
  job existed adopts it, since `jobs/registry.json` is yours and an engine update never delivers it.
  The setup wizard's automation step now asks *day starts at?* / *day closes at?* and writes the
  answers through the same path, before the schedule is rendered and loaded, so the first activation
  already carries them. `set` **never touches launchd** — it is a vault write, activation stays with
  the confirm-class `plainkeep job enable` — and when the edited job is currently loaded it says the
  loaded schedule is stale and prints the one command that fixes it. `plainkeep setup automation
  --yes` and `--all --yes` stay non-interactive and leave existing times alone.

### Changed
- **One validated schedule parser, and the registry-name rule moved to the read.** Schedule shapes
  were parsed inline while rendering a plist, so an unknown weekday silently became Sunday, `"7am"`
  surfaced as an unpacking `ValueError`, and a missing `schedule` as a `KeyError` — each contained
  per job, none of them diagnosed. A malformed schedule is now a §15 legality warning like any
  other: `plainkeep job list` flags it with the correction (`'7am' is not HH:MM — write 07:00`) and
  `apply`/`enable` refuse whole-command before rendering anything. Separately, a registry **key**
  that is not a plain identifier is refused when the registry is read rather than on the paths that
  install — so `plainkeep job disable`, which is deliberately permissive about risk class, can no
  longer be handed a key that resolves outside `~/Library/LaunchAgents`.

### Added
- **Automation is now the default offering, and activation is a product verb** ([`docs/DECISIONS.md`](docs/DECISIONS.md)
  ADR-022 — accepted 2026-08-14). The §15 registry gains a `start` job (daily 07:30,
  `plainkeep start --automated`), and `plainkeep job enable | disable | status` replaces the printed
  `launchctl` recipe: `enable` re-renders from the registry, installs a copy into
  `~/Library/LaunchAgents` and `launchctl bootstrap`s it (confirm-class — `--yes` or exit 3;
  `--dry-run` previews); `status` reports rendered / installed / loaded plus drift. The setup
  wizard now defaults automation ON (still one skippable prompt), and the `automation` layer is
  `ready` only when the schedule is actually loaded. Doctor warns (never fails) on drift or a
  rendered-but-unloaded schedule. Plists are built with `plistlib` — registry content can no longer
  express plist structure — and `enable`/`apply` refuse any job `job list` flags as illegal.
  **Upgrade note:** plists rendered by an earlier version differ byte-wise from a fresh render
  (indentation/key order), so the first `job status`/doctor after upgrading reports drift once —
  `plainkeep job apply` re-renders and clears it; nothing about the schedule changed.

### Added
- **A compiled `plainkeep` core binary now does the dispatching** ([`docs/DECISIONS.md`](docs/DECISIONS.md)
  ADR-013, Phase 1 — accepted 2026-08-01). `plainkeep <verb>` used to be a bash script that started three
  Python interpreters (guardrail, resolver, verb); the gate and the resolver are now compiled into one
  binary that does all three jobs in one process. **Nothing about the surface changed** — same verbs,
  same flags, same `--json` envelope, same exit codes, same `.logs/` lines — and the old bash
  dispatcher is kept verbatim as the zero-install floor, reachable any time with
  `PLAINKEEP_CORE=off plainkeep …`. What you feel (medians of 25 runs through the shim, macOS arm64):
  **TAB completion is about half again as fast** — 55 ms against the floor's 87 ms, a ratio of
  1.54–1.62x across four runs. A verb whose output goes to **a file** is ~7% faster (88 vs 96 ms), and
  on a **terminal** the helper described below never runs at all, so it is faster there too.
  **Bare `plainkeep` in a terminal now opens the TUI**
  (piped or redirected it still prints help, so scripts and agents are unaffected), and `plainkeep ui`
  and `plainkeep mcp` are answered inside the binary — no separate `plainkeep-ui` download needed on
  that path. Every claim here is gated by a permanent differential test suite
  (`test/run_core_parity.py`, 217 checks) that runs each invocation through both the binary and the
  bash floor and compares exit status, stdout, stderr and the audit line.
  **Three things to know before you rely on it.** (1) **Piping a verb's output is ~7 ms (~8%) slower
  than the bash floor** — 103 vs 96 ms. Clearing a bun quirk that would otherwise truncate output past
  the pipe buffer needs one extra helper process, and that helper costs ~14 ms on the piped path — the
  core's ~6 ms head start absorbs half of it, which is how a 14 ms helper nets out to ~7 ms against the
  floor. A terminal, a file and MCP tool calls are all still faster. A durable fix belongs to Phase 2.
  (2) **A verb killed by a fault signal reports the wrong one**: SIGILL/SIGFPE/SIGBUS/SIGSEGV surface
  as SIGTRAP and print a bun crash report to stderr, and SIGPIPE/SIGXFSZ exit `128+N`. Fifteen of the
  twenty-one terminating signals pass through exactly (measured on bun 1.3.14 / macOS arm64; Linux not
  yet measured). (3) `plainkeep ui` still cannot be Ctrl-C'd once an action has run — a pre-existing
  `@clack/prompts` limitation, unchanged by this work and shared with the standalone UI.
- **Building from source now needs [Bun](https://bun.sh) >= 1.2.21** (CI and the released binaries use
  1.3.14, pinned in `.bun-version`). Older bun silently drops empty-string arguments when spawning a
  child — which would make the dispatcher eat an empty verb argument — so `cd cli && bun run build`
  refuses to run below that version rather than producing a subtly wrong binary. This affects
  contributors only: a vault installs a binary, never a toolchain.
- **`ui/` — the ops terminal UI lives in this repo now, and `ops setup ui` installs it**
  ([`docs/DECISIONS.md`](docs/DECISIONS.md) ADR-011). The TypeScript TUI (formerly the standalone
  `ops-ui` repo; history preserved via subtree) is template-only source under `ui/` — NOT in
  `script/engine.txt`, so no TS/Node ever flows into a vault. Vaults install a **self-contained
  compiled binary** instead: a new optional, confirm-gated `ui` setup layer (wizard default ON)
  downloads the platform asset from the template repo's GitHub release via the authenticated `gh`
  CLI, verifies its sha256 against `checksums.txt`, and places it at `$OPS_HOME/.local/bin/ops-ui`
  — where the `bin/ui/` shim now resolves first (`$OPS_UI_BIN` → `.local/bin` → PATH). Release
  binaries (darwin-arm64/x64, linux-x64/arm64) are cross-compiled with `bun build --compile` by
  `.github/workflows/release-ui.yml` on `ui-v*` tags; CI gains a `ui` job (typecheck + compile
  smoke + the non-TTY exit-2 contract).
- **ui layer update detection (offline).** The engine ships its expected ui version in
  `bin/ui/version.txt` (engine-owned → propagated by `script/update`); `ops-ui --version` reports
  the installed one. On mismatch the layer reports `partial` — "update available: installed X → Y"
  — and the ordinary `ops setup ui --yes` re-downloads, pinned to the expected release tag
  (`ui-v<version>`). The release workflow fails on any drift between the tag, `ui/package.json`,
  `bin/ui/version.txt`, and `ui/src/version.ts`.
  **Superseded twice — read this before pushing a `ui-v*` tag.** The hybrid-core work (ADR-013) moved
  the TUI's source into `cli/` and deleted `ui/`, and the release workflow was left pointing at the
  old paths, so for a while it failed on its first step every time rather than "on drift". **That is
  fixed: the pipeline works again**, and the drift it fails on is between three things, not four —
  the tag, the engine-owned pin `bin/ui/version.txt`, and `cli/src/tui/version.ts`.
  `cli/package.json`'s `"version"` is the core workspace's own (`0.0.0`) and is deliberately not in
  the comparison. The check itself now lives in `test/run_uirelease.py` and runs on every push, not
  only when a tag is cut — see the Changed entry below.

### Changed
- **The `plainkeep-ui` release can be cut again, and the two checks that guard it now actually run**
  ([`docs/DECISIONS.md`](docs/DECISIONS.md) ADR-019, Phase 2 Task 7). Two things guard a `ui-v*`
  release: the three versions must agree (the tag, the engine-owned pin the engine downloads by, and
  the version compiled into the binary), and the binary must be built by a bun new enough not to eat
  empty arguments. Both were written down. Neither ran — the version check lived inside the
  tag-triggered workflow, so nothing but cutting a release could execute it, and the bun floor gated
  `bun run build` but not `bun run build:ui`, which is the script that actually produces the
  downloadable binary. **Both are now checked on every push** by `test/run_uirelease.py`, which also
  proves on each run that the version check goes red on every way the three can disagree; the release
  workflow calls the same code with the real tag. For contributors this means one visible change:
  `cd cli && bun run build:ui` refuses on bun older than 1.2.21, the way `bun run build` already did.
- **Your existing plugins keep working after the engine moved — with no edits**
  ([`docs/DECISIONS.md`](docs/DECISIONS.md) ADR-018, Phase 2 Task 3). Every plugin ever scaffolded
  loads the SDK with `sys.path.insert(0, str(Path(os.environ["PLAINKEEP_HOME"]) / "bin"))`, and after
  the engine moved out of the vault that is a directory your vault does not have. `plainkeep` now puts
  the engine's own `bin/` on `PYTHONPATH` when it spawns a plugin verb, so the stale line becomes a
  harmless no-op and `from lib import api` resolves from the installed engine. `PLAINKEEP_API_VERSION`
  is still `"1.0"` and nothing in it changed. **One case to know about**: if your verb ships a
  top-level `lib.py` or `lib/` next to its `run.py`, it now shadows the SDK for that verb (Python
  looks in a script's own directory first). `plainkeep doctor` and `plainkeep plugin add` both tell
  you; rename it and the SDK comes back. Only plugin verbs get this — core verbs find their code
  through their own location, as before.
- **New — plugins can declare their dependencies, and they survive an engine update**
  ([`docs/DECISIONS.md`](docs/DECISIONS.md) ADR-018, Phase 2 Task 3). A pack's `plugin.json` may now
  carry `"dependencies": ["httpx>=0.27"]`, and `plainkeep plugin sync <name> --yes` installs them into
  `<vault>/.plugin-deps/` — **in your vault, not in the engine**, so the next engine update (which
  replaces the engine wholesale) leaves them exactly where they were. Declared only: nothing is ever
  guessed from your imports, and a declaration that could steer pip (a flag, a URL, a local path) is
  refused. A missing module now says which pack wanted it and whether it was declared, instead of
  printing a traceback. Anything you previously `pip install`ed into `<vault>/.venv` still works
  unchanged; declaring it is what makes it travel with the vault. Adding a dependency in an update
  asks you to re-trust the pack, like any other growth in what it can do. The overlay sits at the
  vault ROOT rather than under `plugins/`, because `plugins/` is the directory verbs are discovered in
  — an installed package must never be able to become a runnable `plainkeep <verb>`. `sync` takes only
  `--no-index` and `--find-links=<local dir>` beyond `--yes` (for an offline wheelhouse) and refuses
  any other pip argument, so nothing but what a pack declared can be installed; what actually landed
  is recorded in `plugins.lock.json`. If you already have a `plugins/.deps/` from an earlier build,
  nothing reads it — delete it and re-run `plainkeep plugin sync --yes`.
- **BREAKING — plainkeep is now INSTALLED, and your vault is just your notes**
  ([`docs/DECISIONS.md`](docs/DECISIONS.md) ADR-017, Phase 2 Task 2). plainkeep's code used to live
  inside the vault it edited: `~/plainkeep/bin/` was the engine, `~/plainkeep/plainkeep` was the
  launcher, and updating plainkeep meant merging code into the same git repository that held your
  notes. The engine now lives in its own versioned, **read-only** tree at
  `~/.local/share/plainkeep/engine/<version>/`, with a `current` symlink saying which version is
  live, and a vault contains nothing but your data.

  **What you do once:** re-run `script/setup`. It installs the engine, points `plainkeep` on your
  PATH at the installed launcher, and registers your checkout as a vault. Until you do, running your
  checkout's own `./plainkeep` against that same checkout refuses with exit 5 and tells you this —
  a vault and an engine may no longer be the same directory, because a tool that can edit itself
  while acting on your notes is one bad write away from both being wrong.

  **What you get.** A vault that holds only notes now works — so a second vault is just a folder
  with a marker, and `plainkeep --vault work capture "…"` acts on it. Upgrading plainkeep is no
  longer a merge into your notes: `script/update` refreshes the source, `script/setup` installs it,
  and a bad version is rolled back by re-activating the previous one instead of by reverting commits
  in the repository your notes live in. Scheduled jobs, the MCP server, `search`/`open`/`wiki`'s fzf
  previews and the Raycast scripts all now invoke the installed launcher, so none of them can be
  answered by an executable that happened to be sitting in a vault.

  **What it costs.** A read-only engine cannot cache compiled Python beside its source, so each
  spawned verb re-compiles the shared library it imports: **+17.6 ms per invocation, +12.2%**,
  measured 25 interleaved runs on macOS arm64 / CPython 3.12. Writing a plugin is unaffected —
  `plainkeep new verb <name>` still scaffolds into `<vault>/plugins/local/`, which is yours, survives
  every engine upgrade, and is where a new capability belongs now that the engine is not editable.

- **BREAKING — plainkeep now operates on REGISTERED vaults, and no longer guesses one**
  ([`docs/DECISIONS.md`](docs/DECISIONS.md) ADR-014, Phase 2 Task 1b). A vault used to be "whatever
  directory `PLAINKEEP_HOME` names, or failing that, wherever the engine happens to be installed."
  The second half of that sentence was the problem: for an installed `~/.local/bin/plainkeep-core`
  it resolved to `~`, and because not every write consults the path-wall, a wrong root looked like
  success — a note filed into the wrong tree with exit 0. That fallback is deleted, in all nine
  places it lived, and a root is now VALIDATED before the gate runs, before the resolver scans
  plugins, and before any verb is spawned.
  **What you get.** A vault can live anywhere, you can have more than one, and you pick between them
  explicitly: `plainkeep --vault <name|id|path> <verb> …` (global, and only *before* the verb —
  `plainkeep capture --vault x` is still capture's own argument). With no selector, plainkeep looks
  at `PLAINKEEP_HOME`, then walks up from the current directory for a vault marker, then falls back
  to your default vault — and refuses, naming all four mechanisms and what each one saw, rather than
  picking something. `plainkeep vault status` prints that whole chain, including when it refuses,
  which is when you actually want it.
  **What you must do once, per existing vault** — an unregistered vault now exits 2 on every verb:

      PLAINKEEP_HOME=/path/to/vault python3 /path/to/vault/bin/vault/run.py register /path/to/vault --yes

  (It is `bin/vault/run.py` and not `plainkeep vault register` on purpose: every `plainkeep <verb>`
  validates a root first, and the vault you are registering does not have one yet. A fresh install
  needs nothing — `script/setup` does this for you.)
  **Also changed:** the path-wall's vault segment is now the ONE selected root instead of the
  conventional `~/plainkeep` plus the active one, so selecting vault A no longer authorizes writes
  into vault B; the dispatchers export the vault's *canonical* path (a vault reached through a
  symlink is one vault, not two) plus a new `PLAINKEEP_VAULT_ID`; and a vault inside an iCloud or
  Dropbox tree is refused outright (exit 5). Gated by `test/run_discovery.py` (270 checks), whose
  centre is a two-vault test that walks the filesystem and proves the note landed in the vault you
  selected and nothing moved in the other one.
  **Three further refusals came out of the review waves, and each one refuses something that used to
  work.** (1) Selecting a vault that carries a marker but no engine — an ordinary notes vault, which
  is what most second vaults are — now exits 2 saying so, where it used to fail at the far end of the
  dispatch with either a raw CPython "can't open file" or a false `unknown verb 'capture'`. A vault
  carrying `bin/lib` but no verb directory is refused the same way; it used to capture notes on the
  bash floor and answer `unknown verb` in the compiled core, for one and the same command. (2) A
  registry holding **two entries that spell one canonical path** (say one through a symlink) is now
  rejected as a duplicate instead of loading. `vaults.json` is hand-edited, so this can turn a
  registry that worked yesterday into an exit 2 — remove the redundant entry. (3) **Which paths count
  as "inside a sync tree" changed shape**, and the direction is deliberate. Matching is no longer a
  bare substring: a path component must equal a sync marker or begin with one plus a separator, and
  `~/Library/CloudStorage` is matched as an anchored prefix. That is what it takes to catch the
  spellings the sync clients really use — `~/Library/CloudStorage/OneDrive-Personal`,
  `~/Library/CloudStorage/GoogleDrive-<account>`, `~/Dropbox (Team Name)`, `~/Dropbox Personal`,
  `~/Dropbox.nosync` — every one of which was briefly *accepted* mid-review. The cost, stated
  plainly: **`~/notes/dropbox-export`, `~/notes/OneDrive-old` and `~/notes/icloud-archive` are
  refused too**, because nothing in their spelling distinguishes them from a real mount point. Where
  the two cannot be told apart, plainkeep refuses — the refusal is visible and tells you to run
  `plainkeep vault rebind <name> <new-path> --yes`, where the miss would silently leave a `.git`
  inside a live sync client. If one of those names is your vault, rebind it to a path that does not
  begin with a provider's name. (`~/notes/my.sync-notes`, `~/notes/not-iCloudy` and
  `~/Pictures-notes` are unaffected.) A related wart is disclosed rather than fixed: the *write* wall
  still matches substrings, so a path merely containing "icloud" can be selectable yet unwritable —
  see [`docs/followups.md`](docs/followups.md).
- **Renamed: `opskit` → `plainkeep`, full consistency** (ADR-012). `opskit` collided with 40+
  same-named GitHub repos, was squatted on npm and PyPI, and read as DevOps tooling on sight — not a
  cosmetic problem, so the rename goes all the way through rather than stopping at the brand: the
  `ops <verb>` CLI dispatcher is now `plainkeep <verb>`; the vault folder is `~/plainkeep`; every
  `OPS_*` environment variable and same-named constant is now `PLAINKEEP_*` (`OPS_HOME` →
  `PLAINKEEP_HOME`, `OPS_VECTORS` → `PLAINKEEP_VECTORS`, and so on, no exceptions); the machine
  contract file is `plainkeep.json` (contract phrase `ops.json/3` → `plainkeep.json/3`); and the
  terminal UI binary is `plainkeep-ui`, released as `ui-v0.2.0` with `plainkeep-ui-*` assets.
  Existing vaults migrate by hand — see "Migrating an existing `~/ops` vault" in
  [`docs/setup.md`](docs/setup.md). GitHub redirects the old repo URLs.
- **Repo renamed: `personal-operating-system` → `opskit`** (ADR-011). The template is the *kit* —
  engine + TUI + funnel — not anyone's data; GitHub redirects the old URLs, and
  `script/get`/`script/setup` now point at `gabros20/opskit` (`script/get.sh.sha256` regenerated).
  Derived vaults are untouched (update the `upstream` remote URL at leisure; redirects cover it).
- **BREAKING: `ops share` moved from zero-knowledge encryption to capability URLs**
  ([`docs/design/proposals/2026-07-10-capability-url-share.md`](docs/design/proposals/2026-07-10-capability-url-share.md),
  [`docs/DECISIONS.md`](docs/DECISIONS.md) ADR-008). HTTP never sends the URL `#fragment` to a
  server, so zero-knowledge encryption and "one link an agent can fetch" turned out to be mutually
  exclusive — every bridge attempted (`?k=` query key, `X-Ops-Share-Key` header, a second `agent_url`
  line, `--plain`) either leaked the key onto the wire or produced a two-link contract. Dropped
  entirely: AES-256-GCM encryption, the `#fragment` key, the in-browser JS decrypt viewer, `?k=`,
  `X-Ops-Share-Key`, and the `--plain` flag (plaintext is now the only mode, so the flag is
  meaningless). `ops share <slug> --yes` now PUTs the plaintext OPSX bundle to the worker under a
  single 24-char unguessable token (~124 bits): the bare URL renders HTML in a browser, `<url>.md`
  (or content negotiation on the bare URL) returns raw wiki markdown for any chat/coding agent's
  fetch tool — no headers, no second link. `PUT /` now requires a matching `X-Publish-Token` header
  when the `PUBLISH_TOKEN` wrangler secret is set, so a discovered endpoint can't be abused as a free
  file host; `ops share init --yes` provisions it alongside the existing `wrangler deploy` step.
  **Links published under the old encrypted model return HTTP 410 on every route** — they must be
  re-published. `ops share pull <url>` keeps its surface (fetch + local unpack, now key-less).
  **Action required:** redeploy the worker (`wrangler deploy`) and run `ops share init --yes` to set
  the publish token before publishing again.

### Added
- **Search-enrichment pipeline**
  ([`docs/design/proposals/2026-07-07-search-enrichment-pipeline.md`](docs/design/proposals/2026-07-07-search-enrichment-pipeline.md),
  [`docs/search-enrichment.md`](docs/search-enrichment.md)).
  A modality-agnostic **"enrich" stage** that generates a `description` + `keywords` for every
  ingested source (image/voice/video/pdf/link) via a small local LLM (default `gemma4:e4b` for EN+HU;
  `OpenEuroLLM-Hungarian` as the HU-max override), feeding both keyword and semantic search for free
  (frontmatter is already the top FTS chunk and leads the embed window — no new index code). The
  `.extract.md` note is the file-based working memory between models; models load per-note and unload
  (`OPS_ENRICH_KEEP_ALIVE`, default `0`); a deterministic **stdlib** keyword floor keeps search
  improving with no model pulled, and enrichment is idempotent (a content-hash `enrich_key`, plain
  re-runs are no-ops, `--reenrich` forces).
  New **`ops enrich <slug> [--reenrich] | --all`** verb, auto-wired (best-effort, non-fatal) into
  `ops files extract` and `ops bookmark` unless `OPS_ENRICH=off`. New **`ops models`**
  verb (`list|status|stop|pull|test`) — a management surface for the model behind every stage
  (stt/ocr/vlm/enrich/embed/rerank): see what's configured/pulled/resident, offload a resident model,
  or A/B-test a candidate on a sample before adopting it via its env var (`pull`/`test` are
  confirm-gated `--yes`). Also ships **`OPS_STT_MODEL`/`OPS_STT_RUNTIME`**, retrofitting the one
  previously-hardcoded model choice (the audio transcription tier) onto the same env-config pattern.
  `ops doctor` gained a soft probe for enrich-model reachability. Also flags the adjacent gap that no
  verb currently reaches the video/URL extraction tier for a bare URL (unaddressed).
  **Implemented** (commits `d3e617c`/`65cab0e`, CI-green): `bin/lib/enrichlib.py`, `bin/enrich/`,
  `bin/models/`, the `files extract`/`bookmark` wiring, and the `doctor` probe all ship; the live
  Ollama call is `# pragma: no cover` pending on-host validation, same discipline as image reading.
- **Cross-architecture image reading proposal**
  ([#1](https://github.com/gabros20/personal-operating-system/issues/1),
  [`docs/design/proposals/2026-07-06-image-reading.md`](docs/design/proposals/2026-07-06-image-reading.md)).
  Design for three escalating layers on top of `ops files extract`'s image tier — Layer 1 metadata
  (format/dimensions/EXIF via Pillow, GPS dropped for privacy), Layer 2 OCR (GLM-OCR/DeepSeek-OCR via
  `mlx-vlm` on Apple Silicon or Ollama on Intel/any, falling back to `ocrmac`/`tesseract`), and Layer 3
  VLM understanding (Qwen3-VL 4B → moondream → skip, `--describe`) — with on-demand model load/unload
  (`keep_alive:0`, no daemon), sequential-run peak-memory discipline, and a `OPS_IMAGE_FAKE` test seam.
  New env knobs: `OPS_OCR`, `OPS_VLM`, `OPS_VLM_FALLBACK`, `OPS_MLX`, `OPS_VLM_KEEP_ALIVE`,
  `OPS_IMAGE_FAKE`. `requirements.txt` documents the optional deps (`Pillow`, `mlx-vlm` on Apple
  Silicon) and that model weights are pulled via `ollama pull`, not pip. `ops doctor` gained soft probes
  for `PIL`/`mlx_vlm`/`ollama`/`ocrmac`/`tesseract` and a pointed warning when `OPS_VLM` is set but
  neither runtime is available. **Implemented** (commits `a298c47`/`15cc5cc`, CI-green): the runtime
  (`bin/lib/imagelib.py`), the lazy `_tier_image` OCR cascade, image metadata on ingest, and the
  `--describe` VLM wiring all ship, exercised offline via the `OPS_IMAGE_FAKE` seam; the live model
  backends are `# pragma: no cover` pending on-host validation.

### Fixed
- **`ops share` preview: GFM pipe tables rendered as raw `|` paragraphs**
  ([#6](https://github.com/gabros20/personal-operating-system/issues/6)). `sharelib.render_note_html`
  now recognizes header + `|---|` separator + body rows and emits `<table>` inside a horizontally
  scrollable `.table-wrap` (same mobile discipline as `pre` blocks). Inline markdown in cells still
  runs through the existing inline pass.

- **`ops index` crashed with `OPS_VECTORS=1` when `lancedb` was missing**
  ([#3](https://github.com/gabros20/personal-operating-system/issues/3)). The vector-plane probe
  (`indexlib._vec_modules`) only checked that `vectorstore` imports — but `vectorstore` imports
  `lancedb` lazily, so the probe passed and indexing then died in pass 2 with `ModuleNotFoundError`
  instead of falling back to keyword-only. Now:
  - `vectorstore.available()` deep-probes the actual `lancedb` import, and `_vec_modules()` gates on
    it, so the existing keyword-only fallback actually fires.
  - Pass 2 (embedding) is wrapped so any mid-run vector failure (embedder unreachable, disk, a
    connect that fails after the probe) still leaves a complete keyword/graph index — `ops index`
    never loses the durable pass-1 work.
  - The fallback message and `ops doctor` are now **actionable**: when `OPS_VECTORS=1` but `lancedb`
    isn't importable, both say exactly how to fix it (install `requirements.txt` into *the `python3`
    that runs `ops`*, verify with `python3 -c 'import lancedb'`), and `requirements.txt` documents
    that `ops` dispatches via bare `python3` — optional deps must live on that interpreter's PATH.
  - **Platform-aware `lancedb` pin.** The old `lancedb>=0.33` was uninstallable on macOS-Intel
    (x86_64), where upstream ships no wheels past 0.25.x — so `pip install -r requirements.txt`
    failed outright on Intel Macs. Replaced with mutually-exclusive PEP 508 markers: Intel Macs pin
    `>=0.25,<0.26`, everything else (Apple Silicon / Linux / Windows) `>=0.33`. The engine's vector
    code works on both.

### Changed
- **`ops share` end-to-end ([#2](https://github.com/gabros20/personal-operating-system/issues/2)).**
  Dogfooding the share surface on live Cloudflare Workers surfaced three defects, all fixed:
  - **Publish 403.** Stdlib `urllib`'s default `Python-urllib/x.y` User-Agent is blocked by
    Cloudflare bot-management (a `PUT` that works from `curl` failed from the client). The client now
    sends `User-Agent: ops-share/1.0` on every `PUT`/`DELETE` (`bin/share/run.py`).
  - **Encrypted link downloaded ciphertext instead of rendering.** The worker always returned
    `application/octet-stream`, and no in-browser viewer existed. The worker now serves a
    self-contained **decrypt viewer** on browser navigation (`Accept: text/html`): it fetches the raw
    blob (`?raw=1`) and decrypts in-page with Web Crypto using the key from the URL `#fragment`,
    then renders into a **scriptless sandboxed iframe** (the note can never read the key) and strips
    the `#fragment` from the address bar (`bin/share/worker/worker.js`). `--plain` shares render
    directly; `curl`/programmatic GETs still receive the raw blob.
  - **Silent failure on truncated links.** A forwarded link that lost its `#…` tail now shows a clear
    "this encrypted link is missing its key" message in the viewer, and `ops share` reminds you to
    send the full link (the `#…` is the decryption key).

### Changed
- **Shared-note reading experience redesigned** (`bin/lib/sharelib.py`). The recipient view was
  cramped and, worse, wide code lines blew out the page width — breaking the whole mobile layout so
  body text scrolled off-screen. The new self-contained stylesheet is a Flexoki-palette,
  kepano-minimal reading layout: system type scale, generous spacing, safe-area padding, automatic
  light/dark via `prefers-color-scheme` (pure CSS — the note renders in a scriptless sandboxed
  iframe), and **code blocks that scroll horizontally inside a bordered panel** instead of forcing
  the page wide. The Markdown renderer now also emits real **lists** (`-`/`*`/`+`, `1.`) and
  **blockquotes** (`>`) rather than dumping them as literal-prefixed paragraphs.
- The viewer no longer strips the `#key` fragment from the URL after decrypting. Doing so left the
  in-app browser (e.g. Telegram) on a keyless URL, so "Open in Safari" / reload / copy-link failed
  with "missing key". The key now stays in the fragment (the zero-knowledge model — fragments are
  never sent to the server), keeping the link reloadable and portable across browsers.
- **Mobile-browser hardening** for the share pages (`bin/share/worker/worker.js` viewer +
  `sharelib.py` bundle): `viewport-fit=cover`, per-scheme `theme-color`, `color-scheme`, and the
  iOS 26 "Liquid Glass" edge-strip + matching-background pattern so the iPhone status/URL bar blends
  with the note instead of showing a white/dark band; the viewer's iframe is pinned to the visual
  viewport (`position:fixed; inset:0`) to avoid the `100vh` address-bar jump; and note padding now
  respects `env(safe-area-inset-*)` so text clears the notch/home indicator.
- Documented the viewer, the `?raw=1` route, the content-negotiated `GET`, and the Cloudflare
  User-Agent gotcha in `bin/share/worker/README.md`.

### Added
- **`docs/agent-terminal-search.md`** — how to get semantic search (`OPS_VECTORS`/`OPS_RERANK`)
  working when an **agent terminal** (Hermes, a dispatched session, cron) drives `ops` rather than
  your interactive shell ([#4](https://github.com/gabros20/personal-operating-system/issues/4)). The
  engine runs whichever `python3` is first on `PATH`, and agent terminals often don't source
  `~/.zshrc`, so they silently run a different interpreter without the venv's optional deps. The doc
  covers the venv, PATH order, and the agent-shell-init requirement (with Hermes `shell_init_files`
  as a dated worked example, including the silent YAML-scalar-vs-list pitfall). Linked from the docs
  index and the `operate-ops` skill; `ops doctor`'s vector-misconfig warning now names
  `which python3` and points at the doc.
- Committed known-answer crypto fixture (`test/fixtures/share_kat.json`) plus a **Node Web Crypto
  cross-check** in the share suite that pins byte-compatibility between `sharelib.py`'s AES-256-GCM
  output (`nonce ‖ ct ‖ tag`, base64url) and the browser viewer. Runs in CI — it needs only `node`,
  not the optional `cryptography` package.
- A real-transport **User-Agent assertion** in the share suite (a one-shot local HTTP server captures
  the actual `PUT` and checks the header), so the 403 fix can't silently regress.

---

History before this changelog is in `git log` and the ADR record ([`docs/DECISIONS.md`](docs/DECISIONS.md),
ADR-001 … ADR-007, which covers the v4 platform).
