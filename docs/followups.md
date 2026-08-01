# Follow-ups — known deferred work in the core binary

Every item here was found during the eight-task review pass that built the hybrid core (ADR-013,
Phase 1): each task got a spec review and a quality review, every Critical/Important/Medium finding
was fixed and re-reviewed before the task closed, and this is the tail that was deliberately left for
later. Entries closed by later fix waves have been removed rather than left with a note.

**Nothing here is a known correctness defect in shipped behaviour**, with one qualification worth
stating plainly rather than burying, because it is a real divergence from the bash floor and not a
theoretical one:

> **`resolver.ts:60` sorts directory names in UTF-16 code-unit order where Python's `sorted()` uses
> code points.** For any verb or pack directory name outside ASCII — astral characters, some
> private-use ones — the core can order verbs differently from the floor, which is visible in
> completion output and in multi-root resolution precedence. Every verb and pack name in this repo
> and in every pack the author has seen is ASCII, so the divergence is unreachable in practice, but it
> is reachable *in principle* by anyone who names a plugin directory in Chinese or with an emoji.

Two other classes are divergences by construction and are covered by XFAIL rows rather than being
bugs to fix silently: `pythonRepr`/`pythonNumStr`/`pyStrRepr` do not reproduce CPython's `repr()` for
values no `cmd.json` in this repo produces (below), and the MCP key-order limit is irreducible and
already documented in [`DECISIONS.md`](DECISIONS.md) ADR-013 and `cli/src/core/mcp.ts`'s header.

The branch history carries which review raised each item and why; this file keeps only what someone
fixing it needs.

---

## Start here — the five worth doing first

1. **`cli/src/core/mcp.ts:175` (`pyJsonDumps`) and `guardrail.ts:209` (`pythonRepr`) have drifted.** `pyJsonDumps` is a
   line-for-line clone of `pythonRepr`, and only `pyJsonDumps` got the key-order fix. Two walkers
   encoding the same claim about CPython, already diverging, is how the next MCP divergence gets
   shipped.
2. **`cli/src/core/mcp.ts:191`** — `pyJsonDumps` emits invalid JSON for a `Map` with non-string
   keys. Verified unreachable today (the only serialised `Map`, `mcp.ts:298`, routes every key through
   `dictKey()`), so it is latent rather than live — but it is the only entry on this page that could
   put malformed bytes on the wire if a future caller builds a `Map` differently.
3. **`test/run_core_parity.py:1005`** — the shim-block case filter tests reversed containment
   (`only in "shim"` instead of `"shim" in case_id`), so any single character of `s`/`h`/`i`/`m` pulls
   in all 13 shim checks while the documented spelling silently drops them. A filter that covers less
   than it says is the defect class this whole suite exists to catch.
4. **`cli/package.json:17`** — `build:ui` has no `check:bun` prefix where `build` and `test` do, so
   the artifact a floor user installs can be built by a bun older than 1.2.21 — the version that
   silently eats empty-string arguments, which is why the pin exists.
5. **`plainkeep ui` cannot be interrupted once an action has run.** `@clack/prompts` 0.7.0 adds
   SIGINT/SIGTERM listeners in every `spinner()` and never removes them, which drops bun's default
   disposition; `kill -9` is the only way out. Floor and core alike, pinned by `test/run_tui_pty.py`.
   The ~15-line fix (snapshot listeners before the spinner, remove the new ones after `stop()`) also
   removes clack's `unhandledRejection` handler, which currently suppresses bun's fatal-on-rejection
   behaviour — so it changes more than it looks like it does.

---

## Core binary (`cli/src/`)

### Python-semantics reproduction (`guardrail.ts`)

- **`guardrail.ts:132-137`** — `pythonRepr` reorders integer-like object keys where Python preserves
  insertion order. *Deferred: the verdict is unaffected, it is XFAIL-covered, and the root cause is
  `JSON.parse` discarding order before any serializer runs — the same irreducible limit ADR-013
  documents for MCP.*
- **`guardrail.ts:105-107`** — `pythonNumStr` is `String(v)`, which is not Python's number `repr`:
  diverges on `1e-5`, `1e16`, and bigint precision past 2^53. *Deferred: needs a float-repr
  implementation; no `cmd.json` in this repo carries such a value, and the fuzz suite XFAILs the row.*
- **`guardrail.ts:109-124`** — `pyStrRepr` escapes only `<0x20`/`0x7f`, so U+00A0 renders raw where
  Python writes `'\xa0'`. *Deferred, but note this one is **fully fixable** unlike the two above — it
  is a character-class table, not a float algorithm.*
- **`guardrail.ts:213`** — on invalid-UTF-8 argv the TS side logs a mangled line while Python's
  `UnicodeEncodeError` hits a bare `except` and writes **no** audit line at all. *Deferred: the
  divergence favours the TS side (a record exists), and matching Python would mean deliberately losing
  an audit record.*
- **`guardrail.ts:63`** — `MAX_JSON_DEPTH` bounds nesting but not width; a shallow-but-enormous risk
  value is still fully materialised into the reason, stderr and audit line. *Deferred: parity with
  CPython's own `repr` behaviour, not a port defect.*
- **`guardrail.ts:71-84,95`** — `gate()` calls `cmdField` twice, re-reading and re-parsing the same
  file each time. *Deferred: a caching question, and the file is small.*
- **`guardrail.ts:46-47`** — the comment claims `PLAINKEEP_HOME` is read per call "like Python";
  `guardrail.py` computes it once at import. The comment is wrong, the behaviour is right.
- **`guardrail.ts:48-52`** vs **`resolver.ts:63-69`** — `plainkeepHome()` duplicates the unexported
  `opsHome()`. *Deferred: drift here would point the lock read and audit trail at a different vault
  than verbs resolve from, so it is worth de-duplicating before either grows a rule.*
- **`guardrail.ts:96,347,353-354`, `index.ts:29`** — dead `return Boolean(v)` branch in
  `pythonTruthy`; `getCloseMatches` omits CPython's `n<=0`/cutoff validation despite being exported
  through the barrel; `ratio()` computed twice per candidate (faithful to CPython, but wasteful); the
  barrel re-exports an `EXIT_USAGE` the gate never produces.

### Resolution and completion

- **`resolver.ts:60`** — `sortedChildNames` uses `names.sort()` (UTF-16 code units) where Python uses
  code points. **See the qualification at the top of this file** — this is the one entry that can
  change observable behaviour, for non-ASCII directory names.
- **`resolver.ts:238-243`** — `expanduser` does not expand `~user` forms via the passwd database as
  Python's `os.path.expanduser` does. *Deferred: out of the parity catalog's scope; worth a line in
  the docs if anyone ever uses that form.*
- **`resolver.ts:285`** — `String.trim()` strips a wider (Unicode) whitespace set than Python's
  `str.strip()`.
- **`resolver.ts:24-30,53-61`** — `resolve()` is exception-free and therefore degrades silently to
  "no verbs" on an unreadable directory where Python raises `PermissionError`. *Deferred: never
  consciously ratified as the dispatcher's contract — decide it rather than inherit it.*
- **`resolver.ts:198`, `:392`** — `iterCmds`/`pluginPacks` destructure `[pack, dir]` where the rest of
  the file uses `[name, pack]`; the inverted names read as a bug that isn't one.
- **`complete.ts:223-230,160-173,129-158`** — `loadCmds()` eagerly validates every sidecar's grammar
  on every invocation, so **one** malformed `cmd.json` anywhere makes **every** TAB fall through to
  Python. *Deferred: correct but slow-and-global; a per-sidecar failure would be better.*
- **`complete.ts:323`** — the `as ArgSpec` cast is the one place a broken invariant becomes an escaped
  `TypeError` instead of a graceful fall-through.
- **`complete.ts:72-77`** — `FallThrough.why` is write-only; `super(why)` already stores it as
  `Error.message`.

### MCP server

- **`mcp.ts:175`** vs **`guardrail.ts:209`** — the two walkers have drifted (see triage #1).
- **`mcp.ts:191`** — invalid JSON for a `Map` with non-string keys (see triage #2).
- **`mcp.ts`** — the only production module in `cli/src/core` with no `.test.ts` sibling, and the
  largest; `pyJsonDumps` is proved only indirectly, through the Python protocol differential.
- **`mcp.ts:898-900`, `:839`** — two comments describe a stdin-pausing mechanism that this same file
  measured and **rejected**, and one names a function `pumpOneLine` that does not exist. *Two-line
  edit; left because it is comment-only, but these comments have been used as evidence in review.*
- **`mcp.ts:707-714,909-918`** — `waiters` is drained only by `wake()`, which post-fix has no
  data/end caller in a signal-free session, so the array grows monotonically in structure. *Measured
  not a live leak: a 2M-frame stress run showed RSS plateauing.*
- **`mcp.ts:721-731`** — signal handlers uninstall before the bounded end-of-life drain, leaving a
  window where a signal kills by signal instead of exiting on protocol. *Measured too small to be a
  real hazard; belongs on the disclosed-limits list if it ever widens.*

### TUI and interception

- **`ui.ts:107`** — `plainkeep ui --version` diverges across modes when the standalone `plainkeep-ui`
  is absent (core prints its own version; floor prints an install hint). *Deliberate — the core
  reports the version of the code that will actually run — and now disclosed in
  [`terminal-ui.md`](terminal-ui.md).*
- **`ui.ts:129-134`, `:164-179`** — the one hand-written exit-code decision and `drainStdout` have
  zero test coverage (reachable only through a dynamic import a bun test cannot substitute), and
  `drainStdout` is a near-verbatim, independently-timed copy of `main.ts`'s `drain` that covers only
  stdout while the crash path also writes stderr.
- **`ui.ts:162`** — `EXIT_USAGE` re-declared locally instead of imported from `guardrail.ts`'s
  canonical export, in the file whose comments lecture about the frozen protocol.
- **`interception.ts:127-167`** — the exit guard covers `process.exit` only; `process.reallyExit`
  (patched by `signal-exit`, live during every TUI action) escapes it. *Not reachable today; any
  future build-time assertion must cover `.reallyExit(` and `process.abort(` too.*
- **`interception.ts:162,75-87`** — `runOwningStdio` drains stdout only, and `reportUndrainedBytes` is
  dead code by the module's own measurement.
- **`bin/ui/cmd.json` + `cli/src/tui/app.ts:85-90`** — the TUI's menu offers `ui` itself with
  `tty:true`, so it can launch itself recursively without bound. *(The related risk — a stdio server
  appearing in the menu — is now guarded: `run_mcp_protocol.py:1085` asserts `bin/mcp/cmd.json` still
  declares `hidden:true`.)*
- **`cli/src/tui/` — the deferred `withSpinner()` helper** (see triage #5): snapshot
  `process.listeners()` before a spinner and remove the new ones after `stop()`. Both artifacts
  compile from `cli/src/tui/`, so floor and core get it together.

### Dispatcher and entry points

- **`main.ts:27`** — the presence-vs-truthiness contract is pinned only by a parity case that needs a
  built binary and Python; `complete.test.ts` would still pass if it were reverted.
- **`main.ts`** (drain comment) — asserts `process.exit()` truncates queued output and that no
  buffered result exceeds 64 KiB. Both are demonstrably false on bun 1.3.14 (82,400-byte `__complete`
  output measured, no truncation reproducible at 500,001 bytes).
- **`dispatch.ts`** (`interceptionFor` comment) — the null-prototype argument is measurably false:
  `Object.create(null)` does survive property assignment with its prototype intact. The conclusion is
  right, the stated reason is not.
- **`cli.ts:82-104`** — the flag-membership arrays can silently disagree with the branch bodies that
  dispatch on them; a fourth flag with no matching branch would become `--core-gate`.
- **`index.ts`** — no test exercises the barrel's re-exports, so a broken one is caught only by tsgo,
  and only once something consumes the barrel.

## Oracle and tests (`test/`)

- **`run_core_parity.py:1005`** — reversed containment in the shim-block filter (see triage #3).
- **`run_core_parity.py:156`** — `subprocess.run(text=True)` applies universal-newline translation to
  both streams: the one place a real `\r`-vs-`\n` divergence would be invisible to a byte-exact
  comparator.
- **`run_core_parity.py:270`** — the fixture vault never includes the real `output.py`, so
  `guardrail.py` always runs on its fallback constants; parity is proven against the fallback, not the
  real import.
- **`run_core_parity.py:582-591,628,637`** — two shim checks call `_shim_env(None)` without overriding
  `HOME`, leaking the developer's real `$HOME` into an otherwise hermetic oracle.
- **`test/cases/core-parity/dispatcher.json:56,59,61,62`** — the four fault-signal cells all pin the
  same bun-crash-handler mechanism, so one representative would cut the **opt-in** run's crash-report
  noise roughly fourfold. *Deferred deliberately: the crash-noise gate removed that noise from routine
  runs, and reduction would delete cells for every platform including Linux CI, where they cost
  nothing.*
- **`test/cases/core-parity/dispatcher.json:29-42`** — only the self-SIGTERM shape is a permanent
  case; group-delivered SIGINT, the more failure-prone shape, exists only in a one-off probe log.
- **`test/cases/core-parity/dispatcher.json:46`** — the "what to do when a signal cell goes red"
  instruction is buried in a 2,700-character JSON rationale that CI failure output never surfaces.
- **`test/cases/core-parity/resolver.json`** — no case exercises 2+ emitted `cmd.json` siblings inside
  one directory (intra-directory ordering).
- **`cli/src/core/dispatch.test.ts`** — nothing pins that a legitimate **own** registration named
  `toString` still resolves; the eight-prototype-names test is also satisfied by a hardcoded blacklist.
- **`run_tui_pty.py:231-254`** — `check_renders`'s shared `try` mislabels a late `Timeout` and
  silently drops two of its three checks.
- **`run_tui_pty.py`** — gaps in an otherwise deterministic suite: no on-protocol exit-code assertion,
  nothing checks the terminal is left non-raw, the interrupted-action-during-spinner path is never
  driven, and a real SIGINT mid-action (as opposed to the `0x03` byte) is unpinned.
- **`run_mcp_protocol.py`** — nine hostile-peer protocol edges are uncovered though the server was
  measured correct on all of them: split/multi frames, CRLF and lone-CR, NUL byte, object-shaped id,
  duplicate ids, id-less unknown method, invalid UTF-8, 4 MB frame. The hand-rolled `takeLines`
  splitter is never exercised with `\r` at all.
- **`run_completion.py:26,145-146`** — "green in both modes" is a near-mode-invariant signal: 41 of 42
  checks invoke the Python script directly and only one routes through the shim.
- **`test/fuzz/`** — the fuzz harnesses have no runner and no recorded invocation, so they run only
  when someone remembers they exist.
- **`test/run_core_parity.py:388-393`** — nothing pins that the floor script installed into fixtures
  is verbatim-current; a matching edit to both sides would pass undetected.
- **`cli/src/core/cli.ts:57-63`** — the sanctioned `--version` floor↔core divergence pins only the
  core side; nothing pins the floor's exit 4.

## Toolchain, CI and the shim

- **`cli/package.json:17`** — `build:ui` bypasses the bun version gate (see triage #4).
- **`cli/package.json:26`** — `"@typescript/native-preview": "^7.0.0-dev"` is a caret range on a
  prerelease, which has surprising semver semantics for a non-frozen `bun install`.
- **`.github/workflows/release-ui.yml`** — non-functional (it still points at the deleted `ui/`), and
  separately it installs `bun-version: latest` rather than `.bun-version`, so the artifact a floor
  user installs would be built on an unpinned toolchain. *The file says the first part at the top of
  itself; reviving the workflow means fixing both.*
- **`plainkeep:67-68`** — an explicitly **empty** `PLAINKEEP_CORE`/`PLAINKEEP_CORE_BIN` is treated as
  unset and silently falls back, rather than tripping the unrecognised-mode exit 2 the file's own
  principle calls for.
- **`plainkeep:70,86`** — the shim's liveness probe inherits stdin with no redirect and no timeout (a
  candidate that blocks on stdin could hang `plainkeep`, or steal the verb's bytes), and it checks
  exit 0 only while discarding the identity string, so any executable that exits 0 on an unknown flag
  qualifies as "a live core". *Together these are the one place a wrong artifact can be adopted
  silently.*
- **`plainkeep:31-34,42-43`** vs **`cli/src/core/dispatch.ts:236-243`** — the core gates before the
  venv probe where the floor probes first, so a refused verb costs the floor a probe and the core
  none. Undisclosed, and the matrix cannot detect a probe regression on refused verbs.
