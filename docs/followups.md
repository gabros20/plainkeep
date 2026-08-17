# Follow-ups — known deferred work in the core binary

Every item here was found during a task review pass — first the eight that built the hybrid core
(ADR-013, Phase 1), then Phase 2's (ADR-014 → ADR-019). Each task got a spec review and a quality
review, every Critical/Important/Medium finding was fixed and re-reviewed before the task closed, and
this is the tail that was deliberately left for later. Entries closed by later fix waves have been
removed rather than left with a note.

**Every item here carries the measurement it was closed against**, or says outright that it has none.
An entry that says only "this could be a problem" is doing the thing ADR-019 is about: it looks like
coverage and it is not. If you add one, add the number you saw.

**Nothing here is a known correctness defect in shipped behaviour**, with three qualifications worth
stating plainly rather than burying. The third is the shadow-note slug race under
[The location wall](#the-location-wall-binlibwallpy): it is **measured lossy** — 15 of 16 notes
survived one 16-process run — and it is a real defect, scoped out of Phase 2 Task 1c because what it
loses is a regenerable note inside a git working tree rather than a filed original. The second is in
the same section: a vault whose path merely *contains* a sync marker is selectable but not writable,
so it is reachable, user-visible and self-contradictory — disclosed rather than fixed, because fixing
it means re-recording 59 validated guardrail verdicts. The first is a real divergence from the bash
floor and not a theoretical one:

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
4. **`bin/files/run.py::_shadow()` loses notes under concurrent ingest, measured.** A 16-process run
   filed 16 originals and left **15** shadow notes: the slug is chosen by an `exists()`-scan of the
   whole wiki and then written, so two ingests settle on the same slug and one note overwrites the
   other. The fix is the one Phase 2 Task 1c applied one tree over — create with an atomic
   create-only primitive and let `EEXIST` pick the next slug. Full entry under
   [The location wall](#the-location-wall-binlibwallpy).
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

### The location wall (`bin/lib/wall.py`)

- **`wall.py` carries TWO matchers over one marker list, and they disagree about the same path.**
  `is_walled` / `under_sync_dir` match a marker as a bare SUBSTRING (the semantics guardrail's 51
  recorded write verdicts were taken against); `vault_is_walled` / `vault_under_sync_dir` match a
  path COMPONENT (equal to a marker, or beginning with one plus `-`/` `/`.`/`_`) for the bare markers
  and a path PREFIX for the `$HOME`-anchored ones, and vault SELECTION uses those. So a vault at a
  path merely *containing* a marker — `~/notes/my.sync-notes`, `~/notes/not-iCloudy` — is now
  selectable but every write into it is still denied with exit 5 and a reason that is false of that
  path. *Deferred because converging them means re-recording 59 validated guardrail cases, which is
  its own task with its own oracle work; the split is disclosed in `wall.py`'s header, in
  `_policy_verdict`, and in a SUITE-NOTE `test/run_discovery.py` prints on every run.* The honest fix
  is probably neither matcher: ask whether the path is on a synced VOLUME rather than inferring it
  from the directory's name. That would also retire the three deliberate false positives the
  component matcher accepts (`~/notes/dropbox-export`, `~/notes/OneDrive-old`,
  `~/notes/icloud-archive` — see the CHANGELOG entry for why refusing them is the chosen side of the
  trade).
- **`vaultroot.py:require_engine` probes `bin/lib/guardrail.py` plus one verb directory, which is a
  proxy for "both dispatchers can resolve verbs here".** It is a sufficient probe today only because
  `resolver.py` (`__file__`-relative) and `resolver.ts` (data-root-relative) agree whenever the root
  carries a real engine tree. They still disagree in principle, and `bin/lib`-as-a-symlink is the
  shape where it shows. *Deferred: making `engineBin()` code-relative to match `resolver.py` is the
  real convergence and it belongs to Phase 2 Task 2 (`PLAINKEEP_ENGINE`), which relocates the engine
  out of the vault entirely and dissolves the question.*

- **`bin/files/run.py::_shadow()` picks a slug with an `exists()`-scan of the whole wiki and then
  writes it** — the exact TOCTOU shape Phase 2 Task 1c removed from `~/files/**/in/`, one tree over.
  Two concurrent `files ingest` runs can settle on the same slug and one shadow note then overwrites
  the other. *Measured lossy, not theoretical:* `test/run_originals.py`'s 16-process case reported
  **16 of 16** notes on one run and **15 of 16** on another, and prints the surviving count on every
  run rather than asserting a loss — so the number in front of you is the number that run saw. It is
  deliberately out of Task 1c's scope because the note lives inside the vault — a revertible git
  diff, not evidence — and the ORIGINALS it points at are proved lossless. The fix is the same one:
  create the note with an atomic primitive and let EEXIST pick the next slug.
- **The validated-case COUNT is written out in prose in nine places** (`bin/lib/wall.py`,
  `bin/lib/guardrail.py`, `test/lib/guardrail.py`, `test/run_guardrail.py`,
  `test/run_deterministic.py`, `test/run_discovery.py`, this file). Task 1c had to update every one
  of them by hand when the count went 51 -> 59, and nothing fails if the next person misses one. The
  parity check already prints `len(cases)`; the prose should say "the validated cases" and let the
  suite carry the number.
- **21 raw write sites in `bin/` are not behind the wall**, counted and printed as a SUITE-NOTE by
  `test/run_pathwall.py` on every run: `~/work` fleet trees, `~/.Trash`, a human-supplied `--out`,
  the guardrail's own audit log, and the vault marker/registry — the writes that *establish* where
  the wall goes. The wall as written DENIES all of them. *Whether its model should cover verb-owned
  writes outside the three roots is a policy decision, not a wiring fix, which is why it is a
  registered number rather than a bug.*
- **`create_only` is a claim, and one `.pop()` in `guard()` is what keeps it honest.** It is
  mutation-tested, and no code in `bin/` forwards `**kwargs` into a `vaultio` call (checked) — but it
  is a discipline, not a type. `lib/api.py` re-exports `classify` to plugins, so a plugin can *ask*
  with `create_only: true` and be told ALLOW; the answer is advisory and every write it then makes
  goes through `guard()` anyway.
- **`UNIQUIFY_LIMIT = 100` is a refusal that did not exist before Phase 2 Task 1c.** 101 files
  sharing one stem in one `in/` now fails with EXIT_UNEXPECTED instead of producing `brief-101.pdf`;
  the old loop was unbounded. An honest bound beats a loop a racing writer can keep alive, but it is
  a behaviour change nobody asked for. *Its cost is measured: `case_uniquify_limit` writes 199
  fixture files and runs two real `ingest` processes, ~1s, and that cost rises linearly if the bound
  is raised.*

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
- **ADR-019's own detection rule is not enforced by anything.** Nothing requires a new gate to ship
  with a call-site mutation showing it red, which is precisely the shape ADR-019 names — and it would
  be the next instance if it were claimed as enforced. *Stated rather than solved: the honest
  mechanism is a reviewer's question ("show me it red"), and this repo has no way to require one. A
  weaker but real version is available and not taken — a `run_all.py` gate that every suite added
  after this date carries a recorded RED measurement in its module docstring, which checks that
  someone wrote a number down, not that the number is true.*
- **`test/run_core_parity.py:388-393`** — nothing pins that the floor script installed into fixtures
  is verbatim-current; a matching edit to both sides would pass undetected.
- **`cli/src/core/cli.ts:57-63`** — the sanctioned `--version` floor↔core divergence pins only the
  core side; nothing pins the floor's exit 4.
- **`test/README.md:21` states a cwd invariant that nothing enforces.** "Either invocation is green
  from the repo root and from inside `test/`" is true today and is checked by hand. `ci.yml` runs
  `python3 test/run_all.py` from the repo root in all three of its invocations (`:54`, `:110`,
  `:112`) and never from `test/`, so the regression it guards against — a suite resolving something
  through `$PWD` — would land green. *Measured for Task 7's own suite only (`run_uirelease.py`: 26/26
  from both cwds); the other 57 suites are unmeasured under `cd test`. The fix is one more CI step,
  or a `run_all.py` that re-execs itself once from the other directory.*
- **`PLAINKEEP_CORE=require python3 test/run_all.py` is red without an absolute
  `PLAINKEEP_CORE_BIN`.** Exported over the whole harness, `require` reaches suites that copy a vault
  to a temp directory where the relative core path does not resolve, and they fail with
  `PLAINKEEP_CORE=require but no live core binary at '/private/var/…'`. `ci.yml:110` passes
  `PLAINKEEP_CORE_BIN="$PWD/.local/bin/plainkeep-core"` and is green; the bare gesture is not one.
  *Registered in Phase 2 Task 2 and still open — it is a harness ergonomics defect, not a product
  one, and the honest gate is the CI spelling.*
- **7 of the 17 suites that set `PLAINKEEP_ROOTS_HOME` also harden `PLAINKEEP_TEST_HOME`.** The two
  hardened during Phase 2 Task 1c were the two whose verbs newly routed through the wall; the other
  ten were exposed to the same class before that change and still are. *Unmeasured — nobody has
  checked whether any of the ten can actually reach outside its fixture.*
- **`skills/operate-plainkeep/SKILL.md` has drifted from the design doc's fenced copy, and nothing
  checks that they agree.** Measured 2026-08-02: **243 shipped lines vs 164 in
  `docs/design/PERSONAL_OS_DESIGN.md`**, 5 diff hunks, 0.76 similarity — and growing (Task 1c
  measured 216 vs 164 across 6 hunks). A rule can therefore be true in one and false in the other,
  which is how one review miss survived. Task 1c fixed the single line it owned and reconciled
  nothing else. *The file is engine-owned `NAMED_CONTENT`, so it ships to every vault; the doc is
  what a reviewer reads.*

## Toolchain, CI and the shim

- **`cli/package.json:26`** — `"@typescript/native-preview": "^7.0.0-dev"` is a caret range on a
  prerelease, which has surprising semver semantics for a non-frozen `bun install`.
- **`.github/workflows/release-ui.yml` now depends on `test/`.** Phase 2 Task 7 replaced its inline
  three-way version check with `python3 test/run_uirelease.py --tag "$GITHUB_REF_NAME"`, which is
  what stops that rule from drifting unexecuted again — but it does couple cutting a release to the
  test tree and adds a `setup-python` step to a workflow that previously needed only bun. *Accepted:
  one implementation that runs on every push beats two that agree by hand. Named here so a future
  reshuffle of `test/` knows the release depends on it.*
- **The three-way version check is anchored to `enginetree.NAMED_CONTENT`, which is a manifest of
  paths, not a schema.** `test/run_uirelease.py` finds the pin by looking for the single entry
  ending `ui/version.txt`. Two such entries, or none, fail the gate loudly (asserted) — but a rename
  to something not ending in `ui/version.txt` would too, and the message says "the manifest names
  zero or several", which is not that. *Unmeasured beyond the two asserted cases.*
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
- **The core cannot run in a deleted cwd, and that is bun's, not ours.** The bun runtime refuses to
  start before any plainkeep code runs, so `plainkeep-core` exits 1 with bun's own message whatever
  discovery would have done; `PLAINKEEP_CORE=auto` degrades to the floor for the same reason and IS
  gated (`run_discovery.py` C2), `require` exits 1 with the shim's liveness message — truthful about
  the probe and not about the cause.
- **`VaultError.saw`'s two restored lines are reconstructed, not observed.** The re-run genuinely
  cannot see `--vault` or the pre-export `PLAINKEEP_HOME`. Exporting the whole `saw` map would remove
  the reconstruction; it was judged not worth an env var carrying JSON to every child. *So a refusal
  message can name a cause the failing process did not itself witness.*
- **`engineDir()` throwing is a test-only hardening.** It closes the one repo-relative write that
  Phase 2 Task 2's review found; nothing prevents the next such write from a different helper.
  *Unmeasured: nobody has swept `test/` for other helpers that compose a path off `REPO`.*

## The engine tree (Phase 2 Task 2, ADR-017)

- **`bin/lib/enginetree.py`** — an installed engine is read-only, so CPython cannot cache bytecode
  beside it and every spawned verb re-compiles `bin/lib`: **+17.6 ms / +12.2%**, measured (ADR-017
  Consequences). `PYTHONPYCACHEPREFIX` would recover it at the cost of a third location to reason
  about; not taken, and deliberately left as a measured number rather than a fix.
- ~~**`bin/lib/enginetree.py:activate`** — rollback is unit-covered and has never been used in the
  field. Nothing prunes old versions either.~~ **Closed by Phase 2 Task 5** (ADR-021):
  `--rollback` is a recorded target executed as a runbook in
  `run_engineupdate.py::case_rollback_is_a_tested_command_sequence`, and `prune()` (default
  `--keep 2`) runs after every activation and refuses to remove the active version or the rollback
  target. It has still never been used in the FIELD; what changed is that it is exercised.
- **A tree carries TWO checksum manifests, written by different tasks for different scopes.**
  ~~No engine tree is CHECKSUMMED except one installed by `--update`.~~ **Falsified by the Task 4
  merge, and the correction is the entry**: Task 4b's `install()` records
  `<install-root>/engine/.digests/<version>.json` over `OWNED_TREES`+`OWNED_FILES` for EVERY install,
  including `script/setup`'s — so the original claim was already untrue by the time Tasks 4 and 5 sat
  in one tree. Task 5 additionally records `.pairs/<version>.json` for every pair `update()`
  activates, over the same set **plus** the compiled core, and re-verifies the staged tree against
  its source before activation. Measured on this checkout: `--digests` on a freshly installed tree
  lists `.local/bin/plainkeep-core`; the `.digests` manifest for the same tree does not. So a tree
  installed by `script/setup` IS checksummed — but its core is not, and `--print pairs` still reports
  `no manifest` for it because that column reads `.pairs/`, not `.digests/`. *Deferred: the two
  manifests overlap on ~114 files and are hashed twice per update (cheap — the cost is the 64 MB
  core, hashed once). What is not cheap is that "is this tree what was installed" now has two
  answers, and a reader can consult the one that does not cover what they care about. Unifying them
  means widening Task 4's provisioning gate to cover the core, which is a security-boundary change
  and not a merge cleanup. `case_two_digest_layers_stay_distinct` pins them apart until someone does
  it deliberately.*
- **The `remove_version()` → `os.rename` window is still open on `--install --force`.** ADR-021 D7
  carries the measurement in both directions: killed there, `--install --force` over the ACTIVE
  version leaves **no runnable engine** and a dangling `current` (recovered by a plain `--install`),
  while `--update` cannot reach the state because it refuses the running version as a target.
  `script/setup` runs `--install --force` unconditionally, so this is on the setup path.
  *Not closed here: the fix is a swap through a temporary name — unseal, `rename(dst → .retiring-*)`,
  `rename(staging → dst)`, `rmtree` — which adds two more mutations to the destructive path, and
  `script/setup` cannot simply switch to `--update` because setup must also REPAIR a broken active
  install, which `update` refuses by design.*
- **`run_engineupdate.py`'s "exactly ONE of two concurrent updates wins" cell is insensitive.**
  Call-site mutation (removing the `flock`) left it GREEN — one of the two racers still failed for an
  unrelated reason. The load-bearing cell beside it ("the loser refuses with the lock named") does go
  red, so serialization is gated; the weaker cell is not pulling its weight and should either target
  the SAME version in both racers or be dropped.
- **The XDG default path is barely exercised.** Almost every test drives `PLAINKEEP_ENGINE_HOME`, so
  `${XDG_DATA_HOME:-$HOME/.local/share}/plainkeep/` itself is covered by one manual end-to-end run
  and by `script/setup` on this machine only.
- **`bin/plugin/run.py`'s trust ceiling has not been re-examined** against a plugin that now imports
  `lib` through `$PLAINKEEP_ENGINE`. The variable is dispatcher-set and dispatcher-replaced, so the
  path is not attacker-steerable — but the ceiling was designed when a plugin bootstrapped through
  `$PLAINKEEP_HOME`, and nobody has re-read it since.
- **No engine tree is signed or checksummed.** `verify()` proves a tree is COMPLETE, not that it is
  the tree the installer wrote; anything that can write to `~/.local/share` can replace a verb in an
  activated version. That is the same trust boundary as `~/.local/bin`, and it is stated rather than
  closed.
- **`enginetree.OWNED_TREES` vs `script/engine.txt`** are two manifests for two different questions
  (what an installed engine contains vs what `script/update` refreshes into a checkout). Nothing
  checks that a runtime-owned path appears in at least one of them — which is exactly how
  `templates/verb` went stale in every existing checkout until this task.

## The plugin SDK + dependency contract (Phase 2 Task 3, ADR-018)

Registered by the r1 fix wave from the deferred MINOR/INFO list of `.orchestrate/review-task-p2-3-r1.md`.
The wave's three BLOCKING/IMPORTANT findings are fixed and are NOT here; everything below was
deliberately left. Each was reproduced by the reviewer.

- **`bin/plugin/run.py` (`cmd_sync`, the `lock["overlay"]["packs"]` write)** — `plugin sync <one-pack>`
  overwrites `overlay.packs` with only that pack, losing the record of what every *other* pack's
  declarations put in the shared overlay. No current consumer reads that key, so nothing misbehaves
  today; the audit trail is simply incomplete after a single-pack sync. The `contents` key the fix
  wave added is read back off the overlay itself and IS whole-overlay, so the on-disk truth is
  recorded either way.
- **`bin/plugin/run.py` (`cmd_sync`, `names = [a for a in argv if not a.startswith("-")]`)** — `sync
  foo bar` silently syncs `foo` and ignores `bar`. The fix wave made *unknown flags* a refusal but
  deliberately did not touch positional handling, which is a separate (and pre-existing) shape; a
  caller who names two packs gets one of them with no word said.
- **`bin/lib/pluginenv.py` (`scrub_sdk_path`)** — removes EVERY occurrence of `<engine>/bin` from
  `PYTHONPATH`, including one a caller deliberately exported before the dispatch. The scrub cannot
  currently tell its own injected entry from an identical inherited one.
- **`templates/verb/run.py`** — a newly scaffolded plugin keeps `sys.path.insert(0, …)`, so the ENGINE
  wins for it, while an unmodified old plugin gets plugin-wins under `PYTHONPATH`. The two precedence
  regimes now coexist and only the old one is warned about (ADR-018 D3 pins which way each goes, so a
  change is noticed, but the divergence itself is unresolved).
- **The overlay entry stays on `PYTHONPATH` for descendants at any depth** (disclosed in ADR-018 D2
  and pinned by a test): a python grandchild with nothing to do with plainkeep can import from a
  pack's dependency overlay.
- **`bin/lib/pluginenv.py` (`sdk_shadows`)** — scans one level (`<pack>/<verb>/lib`). A
  `$PLAINKEEP_PATH` root nested one level deeper than the resolver's "the root itself is the pack"
  shape would be missed by the shadow preflight.
- **`test/run_pluginsdk.py`** — the whole suite runs `PLAINKEEP_CORE=off`. Core coverage for the
  plugin spawn rests entirely on the parity cells in
  `test/cases/core-parity/dispatcher.json` (`plugin-spawn-environment`, and now
  `dependency-overlay-is-not-a-pack`).
- **`test/run_pluginsdk.py` (the two installed-engine cases)** — they `skip()` rather than fail if the
  installer breaks, so an installer regression surfaces here as a SKIP rather than a red check.
- **MCP can supply `--yes` for any confirm-class `plugin` subcommand.** `bin/mcp/run.py`'s `_argv_from`
  passes the free-form `args` array through verbatim and nothing strips `--yes`, so an agent can
  self-confirm `plugin add`/`trust`/`remove`/`backup`/`sync`. Pre-existing and not a Task 3
  regression — but it is what made the (now closed) `--pip-arg` hole reachable with no human present,
  and no test pins that `--yes` cannot be smuggled. **This is the one item on this list with a
  security consequence, and it is the one worth doing first.**

- **`enginetree.install()` still has an open window between `remove_version()` and `os.rename`, and
  it is open by choice.** A kill in it leaves no engine under the version name and a dangling
  `current`; a plain `--install` (not `--force`) recovers. The reason it is not closed is written out
  with its three measurements in `install()`'s own docstring, so the claim can be checked rather than
  believed: `rename(SEALED dst → .retiring-*)` gives **EACCES**; `_chmod_tree(dst, writable=True)`
  first and then the rename **succeeds**, and the old tree survives the window; a one-syscall replace
  of a non-empty unsealed `dst` gives **ENOTEMPTY (errno 66)**. So the swap-through-a-temporary-name
  IS expressible — it costs a third and fourth mutation on the destructive path to shrink a window
  that already recovers without `--force`. If it is ever implemented, the retired tree wants sweeping
  the way `.incoming-*` is.
- **The seal check samples 21 paths; it does not walk the tree.** `enginetree._SEAL_SAMPLE` is
  `VERSION`, `plainkeep`, `bin`, `bin/lib` and `frontends/raycast`, plus all 11 `NAMED_LIB_MODULES`
  and all 5 `NAMED_CONTENT` files — chosen so every module a hot patch would actually go for
  (`guardrail.py`,
  `vaultroot.py`, `resolver.py`, `wall.py`) is stat'd, at a cost of ten extra `stat` calls. It is
  still a sample: a writable file anywhere else in an activated tree — any of the **35 verb entry
  points**, for instance — is invisible to it. `verify()` does not use the list at all; it hands over
  modes it has already paid for and therefore does cover the verb entry points, so the gap is
  specifically the check run by callers that have NOT already walked the tree. *Deferred: walking is
  the honest fix and costs a full tree stat on a path that runs per invocation.*
- ~~**A fresh checkout with no `.plainkeep/vault.json` marker takes four suites red, and the failure
  does not say so.** Measured in a clean Phase 2 Task 7 worktree (`PLAINKEEP_CORE=require`, core
  binary built): `run_tui_pty` **0 passed / 13 failed**, `run_mcp` **4 / 12**, `run_mcp_protocol`
  **3 / 23**, `run_setup_layers` **100 / 1**.~~ **Closed by Phase 2 Task 5**, with the second of the
  two fixes this entry proposed: each of the four fixtures now MARKS ITS OWN copy
  (`vaultfx.mark_vault`) and excludes `.plainkeep` from the tree it copies, rather than inheriting
  whatever marker the developer's checkout carried. The independently reproduced numbers matched this
  entry exactly, and after the fix the four read **24/24, 16/16, 161/161, 101/101** in an unmarked
  worktree — and unchanged in a marked one, verified by marking this worktree and re-running.
  The suite-level fixture was chosen over a `run_all.py` precondition check for the reason the
  entry's own measurement implies: a precondition tells a contributor to go mark their checkout,
  which makes the suite pass BECAUSE of the environment rather than independently of it, and a CI
  runner or a `git archive` export has no reason to be a vault at all. *What is NOT closed: nothing
  detects the general class. A future fixture that copies the repo and forgets to mark it will be
  green on a developer's machine and red everywhere else, exactly as these four were, and the only
  thing that found them was running the suite somewhere the marker did not exist.*

## `init` and `update` (Phase 2 Task 5, ADR-021)

Registered while finishing the task after its first agent was interrupted. Everything below was
measured on this machine after merging `main` (Task 4) into the branch — several of these exist ONLY
in the merged tree and could not have been seen by either task alone.

- **A duplicate top-level `def` silently disabled a security gate, and only a suite caught it.**
  Tasks 4b and 5 both defined `enginetree.digest_problems`, ~300 lines apart, with different
  signatures. Python keeps the last, so `provision.require_delivered_intact` — the gate deciding
  whether a `uv.lock` and a `uvpin.json` may pick a binary to download and execute — raised
  `TypeError` instead of gating. Fixed (`pair_digest_problems`) and pinned by
  `case_two_digest_layers_stay_distinct`. *What is NOT closed: the AST check covers `enginetree.py`
  only. Every other module in this repo can still absorb a duplicate `def` from a merge with clean
  diffs on both sides and no runtime complaint. A repo-wide version of that check is cheap and
  nobody has written it.*
- **D6 clause 4 ("doctor never reaches the network") is enforced STRUCTURALLY, not observationally.**
  The suite reads doctor's parse tree for a call into `provision`'s downloading half and for a
  network-capable import. It cannot prove no socket is opened. It is paired with the snapshot cells,
  which prove no byte of the engine install root changes across a doctor run in both dispatcher
  modes. *An observational proof needs a suite that can fail a network — a loopback-only namespace,
  or a `urllib` shim injected into the child — and neither is offline-stdlib-cheap on macOS.*
- **`update` inherits `install()`'s replace window, and the update path CAN reach it.** Only when the
  target version already has a tree that fails `verify()` — the state an earlier kill leaves. Driven
  directly: the injection fires, the target tree is gone, and the RUNNING pair is untouched and
  answers both `vault status --json` and `capture` in both modes. Harmless because the target is
  never the running version, which is `_active_conflict`'s whole job. The window itself is still
  open on `--install --force` (see the entry above and ADR-021 D7).
- **The self-test dispatches `vault status` and nothing else.** One read-only verb through the real
  dispatcher in both modes. It catches a truncated core (measured: `--verify` rc=0, `--update` rc=5)
  and a tree that cannot start. It does not catch a pair that starts and then gets a WRITE verb
  wrong. *Deferred: a self-test that writes needs a throwaway vault it is willing to dirty, which it
  already builds — so the cost is runtime and the risk is a self-test that fails for a reason
  unrelated to the pair.*
- **`--keep` cannot go below 2 and nothing warns when a prune declines to run.** `prune()` protects
  the active version and the rollback target unconditionally, so a `--keep 1` is silently raised.
  Correct, and invisible: an operator managing disk gets no signal that their bound was ignored.
- **The pair manifest is not a security boundary**, and the ADR says so. It proves the tree is what
  was copied from the source. Anyone who can write inside `engine/` can generally write the `.pairs/`
  directory beside it. Closing that needs a signature and a key, which this phase has not decided.

### Deferred by the r1 review of this task

Registered from `.orchestrate/review-task-p2-5-r1.md` (PASS_WITH_FOLLOWUPS, 0 blocking, 2 IMPORTANT
both fixed in the r1 fix wave). Each line below was **measured by the reviewer** by driving the
product, not read off the code; the evidence is kept with the item so nobody has to re-derive it.

- **`activate()` (`enginetree.py:1056`) and `install()` (`:945`) do not take `_UpdateLock`, so
  `_active_conflict`'s answer can go stale mid-update.** A concurrent manual `--activate <target>`
  can point `current` at the very tree an in-flight update is about to replace — the one arrangement
  in which the "everything destructive acts on a tree nothing is running" invariant does not hold.
  `--update` is serialized against `--update` (proven: rc 0/3, no half-installed tree, no stale lock
  after a SIGKILL); it is the *other two entry points* that stand outside the lock.
- **The new path-wall exemptions are non-distinctive text matched by `startswith`.** The three
  exempted spellings (`f.write_text(text, encoding="utf-8")`, `d.mkdir(parents=True,
  exist_ok=True)`, `p.parent.mkdir(...)`) are common enough that a future unrelated write in the same
  file is licensed silently. `test/run_pathwall.py:218` flags this against itself; the fix is to
  anchor the exemption to a line number or an enclosing function rather than to a prefix.
- **`run_provision.py:1055` matches doctor's provisioning rows by three literal marks**, with a
  non-vacuity floor of `>= 2` found. A *fourth* provisioning row added later would be uncovered and
  the floor would still be met, so coverage silently stops tracking the surface it describes.
- **The "exactly ONE of two concurrent updates wins" cell is insensitive** (already registered above;
  the reviewer supplied the discriminating form). Two racers targeting the **same** version give rc
  `0` / `3` with the loser naming the lock, the winner's tree `--verify` OK and `capture` rc 0 in both
  modes, and `--print pairs` coherent (`state_agrees_with_current: true`). That is the one-line fix:
  make both racers ask for the same version.
- **`--install` records `.digests` but no `.pairs`** (already registered above as the non-uniform
  checksum property; the reviewer measured the operator-visible consequence). `--print pairs` reports
  `no manifest` for a `script/setup`-installed tree, and that tree's 64 MB compiled core therefore
  carries no checksum at all.
- **`_generate_manifest` failure leaves `init` at exit 0 with `manifest: false`.** The vault then
  needs one dispatch to self-heal. Rendered to the operator, so this is a stated choice rather than a
  slip — but a fresh vault whose surface file is missing is a vault no agent can enumerate until
  something happens to run in it.
- **An engine installed before Task 4 widened `OWNED_FILES` is refused by `--activate`/`--verify`.**
  Measured against the developer's real `4.0.0-dev`: "missing engine file: pyproject.toml, uv.lock,
  bin/lib/pluginenv.py, tools/". Dispatch still works, because `require_intact` probes a subset —
  so the machine looks healthy right up until the first `--update`. **Task 6 should expect
  `script/setup` to be required before the first `--update` on any such machine**, and that is the
  form this is most likely to be discovered in.
- **Pre-existing, not this task: a global `PLAINKEEP_CORE=require` leaks into fixtures that build a
  core-less engine.** `PLAINKEEP_CORE=require python3 run_all.py` is exit 1 on `main` too:
  `run_get.py` (15/5, "PLAINKEEP_CORE=require but no live core binary at …" inside its own throwaway
  engine — and on `main` it is worse, raising `FileNotFoundError`) and one `run_terminal.py` cell
  (62/1, identical on `main`). **Exporting the variable across the whole batch is not a supported
  spelling**; "both dispatcher modes" has to be a per-suite property, which is how
  `run_engineupdate.py` does it.
- **`case_two_digest_layers_stay_distinct`'s behaviour half is placement-sensitive.** A duplicate
  `def` appended AFTER the `if __name__ == "__main__"` block is invisible to the CLI — `main()` runs
  before the second `def` binds — so only the AST cell catches that shape. Measured both ways: a
  duplicate inserted mid-file reddens both halves, one appended at the end reddens only the AST cell.
  The AST cell is the load-bearing one, and it is the one whose scope is `enginetree.py` alone.

## Provisioning: the uv bootstrap and the delivered lock (Phase 2 Task 4, ADR-020)

Registered by the r1 fix wave from the deferred MINOR/INFO list of
`.orchestrate/review-task-p2-4-r1.md`. The wave's BLOCKING finding (the uv pin sat outside the
checksum gate, so a hot-patched `bin/lib/uvpin.json` installed and executed an attacker-supplied
`uv`) and its three IMPORTANT ones are fixed and are NOT here. Every item below was re-measured
during the wave rather than copied across; the line numbers are post-fix.

One item from that list is **closed rather than deferred**: `repoint_pin` shipping as an unasserted
exploit primitive is now `case_pin_is_gated`, which drives the tamper on both implementations and
both verbs and asserts exit 5 with an empty `tools/` and an unexecuted payload.

- **`bin/lib/provision.py:243` — the Python offline refusal nests its multi-line hint in
  parentheses.** Measured side by side on one engine: the module prints `cannot download uv 0.12.1
  (offline) (plainkeep needs uv 0.12.1 and cannot reach the network…` — the whole seven-line manual
  recipe, closing paren included — where `plainkeep-core --core-provision ensure-uv --offline` prints
  the same recipe clean. The "byte-identical refusal" claim holds for the hint TEXT and not for what
  the operator sees, and the difference is `output.fail`'s `f" ({hint})"` meeting a hint that was
  written to stand alone.
- **`bin/lib/provision.py:244` — an offline refusal exits `EXIT_UNEXPECTED` (1), not `EXIT_DENY`
  (5).** Measured `rc=1` on **both** implementations, so this is a shared spelling rather than a
  divergence (the review recorded it against the Python side only). Refusing to download because the
  operator asked for offline is a policy decision the caller made, not an unexpected failure, and the
  exit code is the only part of the refusal a script can branch on.
- **`cli/src/core/provision.ts:201` — `spawnSync("tar", …)` resolves `tar` from `PATH`** inside the
  binary whose reason for existing is to need nothing from the host. It runs *after* the sha256 check,
  so it is not a gate bypass; it is a host dependency in the one code path that claims not to have
  any.
- **The checksum gate is LAYOUT-DERIVED, and outside that layout there is silently no gate.** Both
  `enginetree._looks_installed` and `deliveredDigestProblems` decide whether a tree is "installed" by
  asking whether its parent directory is named `engine`. Measured: a tree at `<…>/engine/4.0.0-dev/`
  is gated, while the identical tree at `<…>/engines/4.0.0-dev/` or `<…>/opt/4.0.0-dev/` returns
  `_looks_installed=False` and `digest_problems=[]` — no digests, no gate, no word said. Nothing
  produces those layouts today, and this is now the shape of the whole provisioning security
  boundary rather than of a completeness check.
- **`bin/lib/enginetree.py:_seal_installed` raises when `tools/` is absent.** Measured on a tree with
  no `tools/`: `FileNotFoundError: … /engine/9.9.9/tools`. Reachable by re-sealing a tree from a
  pre-Task-4 build through the repair branch of `install()`. Untested. The r1 wave made the sealing
  walk skip `PROVISION_DIR` outright, so the `chmod` on the next line is now a repair for
  already-0555 trees rather than the load-bearing half — but it still assumes the directory is there.
- **Nothing provisions the engine in the field, so the ADR-013 interpreter repoint is inert.** No
  `plainkeep` verb reaches `provision.sync()` or `ensure_uv` — the r1 wave fixed doctor's row to say
  so, but did not add the verb. The consequence rides one layer down: `enginePython()`
  (`cli/src/core/provision.ts`) returns `null` on any engine nobody hand-provisioned, so
  `blockingRestoreInterpreter` (`dispatch.ts`) falls straight back to the old bare `python3`. The
  O_NONBLOCK parity case is green, and it is green THROUGH THAT FALLBACK rather than through the
  repoint it is meant to exercise. Correct code, unreachable until a `plainkeep` verb provisions.
- **INFO — `run_provision.py`'s uv-driven cells depend on a gitignored `<repo>/tools/`.** They skip,
  loudly, on a checkout nobody has provisioned: a bare `git archive` export of this commit reports
  `SKIPPED — uv sync --frozen cells (this checkout has not provisioned uv — run
  python3 bin/lib/provision.py --ensure-uv once)`, where the registered checkout runs them. Coverage
  varies with the machine; the skip says so rather than passing quietly.
- **INFO — five of the six pinned uv targets are never exercised**, and `platform_target()`'s musl
  detection is a heuristic on both implementations: `platform.libc_ver()` returns `('', '')` on musl,
  which is the cheapest stdlib signal available, and a glibc interpreter on a musl host would be
  misread (`bin/lib/provision.py:164`). Only this machine's `aarch64-apple-darwin` is ever downloaded
  in anger.

## Automation lifecycle (`plainkeep job enable/disable/status`, ADR-022)

Found by the two review waves of the automation-default task (r1 FAIL → fix wave → r2
PASS_WITH_FOLLOWUPS, 2026-08-14; `.orchestrate/review-task-automation-r1.md` / `-r2.md` in the
working repo). Blockers and importants were fixed in-branch; this is the deferred tail.

- **`bin/backup/run.py` still has its own plist renderer and prints the deprecated
  `cp` + `launchctl load` handoff** (r1/I3). `com.plainkeep.backup.cloud` is invisible to
  `job status` and doctor's rows, and the activation story it teaches contradicts ADR-022's.
  Measured: `grep` puts the second template at `bin/backup/run.py:303-352`; ADR-022 scopes itself
  to §15 in a Consequences bullet rather than claiming it. Fix: render through `launchdlib`,
  activate through `job enable`, or register it as a §15 job.
- **`setup automation` without `--yes` prints the generic confirm line** — "installs downloads and
  local dependencies" — which is false for this layer and omits the launchd facts that made it
  confirm-class (r2/M7). The wizard prompt says the right thing; `_confirm_message()` needs the
  same special case `models` already has.
- **`hermetic.seal()` makes `launchctl_available()` true everywhere**, so the off-Darwin refusal in
  `job enable` and doctor's availability guard are untestable on the one platform where they are
  real (r2/M9). No suite asserts them today — measured zero hits. Fix: gate availability on
  `is_darwin() or override`, or let a suite opt out of the seam.
- **No suite exercises a *failing* `bootstrap`** — the fake always succeeds; the error path is
  truncated launchctl stderr, never measured (implementer OQ3, r2 concurs). And **`job status` has
  no last-run/last-exit column** (OQ5): a job that bootstraps cleanly and dies at 07:30 is visible
  only in `.logs/jobs/<name>.log`; `launchctl print` carries the data.
- **Drift names two remedies in two surfaces** (OQ6): the setup layer says `plainkeep setup
  automation`, doctor says `plainkeep job apply`. Both converge, measured; it reads as noise.

## Configurable schedule times (`plainkeep job set`, the wizard's day bookends)

Found by the review of the job-set task (r1 PASS_WITH_FOLLOWUPS, 2026-08-15;
`.orchestrate/review-task-jobset-r1.md` in the working repo). The importants and minors were fixed
in-branch; this is what was deliberately deferred.

- **JSON config writes are not atomic** (r1/I1, part 3). `vaultio.write_text` is
  `p.write_text(text)` — truncate-then-write, no temp file, no rename — so an interrupted write
  leaves a half file. Measured with `ulimit -f 1` on a 963-byte `jobs/registry.json`: the write dies
  mid-file and the registry comes back 512 bytes, after which every `job` action refuses it as
  invalid JSON, `plainkeep job set` (the surface that repairs a schedule) included. The failure is
  now CONTAINED and names `git checkout` as the way back, which is the in-branch half; the write
  itself is still not atomic. This is house-wide, not job-specific — `share/run.py` (config +
  ledger), `backup/run.py` (config), `plugin/run.py` (lock) and `lib/manifest.py` write JSON the
  same way — and the repo already has the correct pattern twice, at `enginetree.py:446` and
  `migrate.py:968` (write `tmp`, then `os.replace`). Fix: a `vaultio.write_text_atomic` those five
  callers use. `jobs/registry.json` is the one now written *programmatically*, from two callers, one
  of which runs on a fresh machine — so it is the one that raised the question.
- **`parse_schedule`'s registry-borne refusals are pinned only where a CLI flag can reach them**
  (r1 test-gap 6). A non-dict `schedule`, two cadences in one entry and a boolean
  `interval_minutes` can all arrive from the FILE, where no flag parser has normalised them first;
  the suite exercises those shapes through `job set`'s flags, which can only produce a string or an
  int. Measured by hand during the review (`schedule names 2 cadences (daily, interval_minutes)`)
  and correct — just not pinned. Fix: a fixture registry per shape in `run_jobverb.py`, asserted on
  `job list` (warn text) and `job enable` (whole-command refusal).
- ~~**An existing vault's agent adapters are never refreshed**~~ — **DONE.** Generated adapters now
  carry a stamp (contract version + a hash of the body); `plainkeep vault sync-adapters` rewrites
  only `stale`/`missing` ones — the files it can prove it wrote and nobody has edited — and leaves
  an `edited` or `unmanaged` adapter alone with a `.plainkeep-new` sibling to merge, because an
  adapter is a file its owner is invited to edit. `plainkeep doctor` warns when an adapter still
  names a vault-relative `skills/…` path that does not exist, matched by a regex rather than a
  substring so the ABSOLUTE engine path in a healthy adapter is not mistaken for a dangling one.
  The original entry, kept for the record:

- **An existing vault's agent adapters are never refreshed** (found while fixing the dangling
  manual pointer). `AGENTS.md`/`CLAUDE.md` are VAULT-owned: `plainkeep vault init` writes them once
  and, as `migrate.py`'s own note says, "an installed engine does not provide them" — `script/update`
  refreshes them only in a SOURCE CHECKOUT. So a vault created before a contract change keeps the old
  text forever. Measured on a real machine: a vault migrated from the pre-ADR-017 layout still told
  every agent to read `skills/operate-plainkeep/SKILL.md` *relative to the vault*, where no `skills/`
  has existed since the engine moved out — the agent got ENOENT and improvised. `plainkeep setup
  agents` (this release) makes that mostly moot by delivering the manual through each agent's own
  skills directory rather than through a path in prose, so a stale pointer no longer costs the agent
  its manual. The gap that remains is the text itself. Fix: a contract-version marker in the adapters
  plus `plainkeep vault sync-adapters --yes` (or a `doctor --init` repair) that rewrites them when the
  engine ships a newer contract, and a `doctor` check that the manual reference RESOLVES — the
  ADR-019 "unwired rule" detector pointed at the agent contract, which would have caught this on the
  day ADR-017 landed.
