# Changelog

Notable changes to the plainkeep platform, newest first. Format follows
[Keep a Changelog](https://keepachangelog.com). The *why* behind load-bearing decisions lives in the
ADR log ([`docs/DECISIONS.md`](docs/DECISIONS.md)); this file records *what changed*.

## [Unreleased]

### Added
- **A compiled `plainkeep` core binary now does the dispatching** ([`docs/DECISIONS.md`](docs/DECISIONS.md)
  ADR-013, Phase 1 — status PROPOSED). `plainkeep <verb>` used to be a bash script that started three
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
  (`test/run_core_parity.py`, 216 checks) that runs each invocation through both the binary and the
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

### Changed
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
