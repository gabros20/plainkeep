# Contributing

This repo is the **template/engine** for the `~/plainkeep` system. A user's own vault is a copy of
it; the framework files (`bin/`, `skills/`, `plainkeep`, the adapters, `docs/`, `script/`) travel and
update, while their content (`wiki/ tasks/ journal/ jobs/registry.json`) is theirs. The boundary is
[`script/engine.txt`](script/engine.txt).

## Run the tests

```sh
python3 test/run_all.py          # every offline suite (stdlib only, no network, no LLM)
python3 test/run_<suite>.py      # one suite
```

All suites must stay green. CI (`.github/workflows/ci.yml`) runs `run_all.py` on every push/PR.

## Run the engine you just edited

Since ADR-017 the engine is a versioned tree installed OUTSIDE any vault, so "run my edit" is no
longer automatically "`./plainkeep <verb>`". Exactly one gesture broke, and it is worth knowing
which, because the fast loop still exists.

**What broke:** `./plainkeep <verb>` **with this checkout as the selected vault** — the default,
since the marker walk-up finds the checkout's own marker. A vault is data and an engine is code, and
one directory cannot be both, so that is **exit 5** with a remediation naming the installer. Nothing
about PATH is involved; it is refused whether or not `plainkeep` is on PATH at all.

**The fast loop — live, no install step.** Point `PLAINKEEP_HOME` at any *other* vault and the
checkout's own launcher runs your edit immediately:

```sh
export PLAINKEEP_HOME=/tmp/pk-dev-vault     # any marked vault that is not this checkout
./plainkeep <verb>                          # runs bin/<verb>/run.py as it is on disk, right now
```

This is the loop to use while iterating on a verb. Your edit is live because the checkout IS the
engine here — nothing was snapshotted.

**The install loop — when you need the shipped shape.** Use this to test what users actually run:
the installed tree, `plainkeep` on PATH, or the checkout acting as its own vault. An installed engine
is a read-only snapshot, so **an edit is invisible until you re-install**:

```sh
python3 bin/lib/enginetree.py --install . --force   # ~0.2 s; re-points `current` atomically
plainkeep <verb>                                    # the INSTALLED launcher, put on PATH by script/setup
```

`--force` is required because re-installing the same `VERSION` must REPLACE the tree rather than
refuse it — which is exactly the contributor case. If `plainkeep` is not on PATH, invoke the
launcher by path: `"$(python3 bin/lib/enginetree.py --print current)"/plainkeep <verb>`.

To install without touching your real engine, point the install ROOT somewhere disposable — the
variable is read by the installer surface only, never by a dispatch:

```sh
export PLAINKEEP_ENGINE_HOME=/tmp/pk-dev
python3 bin/lib/enginetree.py --install . --force
"$(python3 bin/lib/enginetree.py --print current)"/plainkeep <verb>
```

The checkout can also still be ACTED ON as a vault — `plainkeep status` from an *installed* engine
against this directory works fine. What is refused is only the checkout's own launcher against
itself.

## Add a verb — one folder

> [!IMPORTANT]
> **Adding a verb for your own vault?** Don't touch `bin/` — that's the engine, and `script/update`
> owns it. `plainkeep new verb <name>` scaffolds into `plugins/local/<name>/` (update-safe, same
> shape, same guardrail); see [`docs/plugins.md`](docs/plugins.md). The steps below are for
> contributing a **core engine verb** to this template.

A new engine verb costs exactly one directory under `bin/`. Nothing else to wire — `plainkeep help`,
the manifest, the guardrail, and every agent learn it from the same place. Scaffold with
`plainkeep new verb <name>` and move the folder from `plugins/local/` into `bin/`, or create it by
hand:

1. **`bin/<verb>/run.py`** — the implementation. Import shared helpers from `lib`:
   ```python
   import sys; from pathlib import Path
   sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
   from lib import paths, filing   # paths/filing/guardrail/agent/manifest as needed
   ```
   The verb owns *where/how* (compute paths from `paths.*`, create tasks/notes via `filing.*`). Borrow
   model judgment, if any, through `agent.run_agent(prompt, scope)` — always with a deterministic
   fallback (§6).
2. **`bin/<verb>/cmd.json`** — the manifest sidecar. Set `verb`, `summary`, `usage`, and the **`risk`**
   class (`read` / `safe_write` / `draft_only` / `confirm` / `deny`); new/undeclared verbs default to
   `confirm`. Also declare the machine contract: an **`output`** block, a **`hints`** string, and
   **`dry_run`** for mutating verbs — shapes in [`docs/machine-contract.md`](docs/machine-contract.md).
   Emit through `lib.output` (`emit` / `emit_rows` / `fail`), never hand-rolled JSON.
3. **Register the group** in `bin/lib/manifest.py` `GROUPS` (optional but tidy).
4. **`plainkeep help`** — regenerates `plainkeep.json`; confirm the verb appears. Run it through
   either loop above (`PLAINKEEP_HOME=<other vault> ./plainkeep help`, or re-install and use the
   installed launcher) — **not** `./plainkeep help` against this checkout, which is exit 5.
5. **`test/run_<verb>.py`** — cover it against a temp `PLAINKEEP_HOME` (and `PLAINKEEP_ROOTS_HOME`
   for the sibling roots). Add it to `test/run_all.py`.
6. **`plainkeep doctor`** — should stay all-green (same two ways of invoking it).

### Conventions

- **Flat verbs, shallow subactions.** `plainkeep task add …` is fine; never nest deeper.
- **Plaintext is truth.** No binaries in `wiki/`; indexes are disposable caches.
- **The guardrail is the system's, not the verb's.** Don't re-implement safety per verb — declare the
  right `risk` and (for verbs that handle caller-supplied paths) call `guardrail.classify()`.
- **The roots are walls.** Write only inside `~/plainkeep`, `~/files`, and the task's `~/work` repo.
  Never iCloud/family paths; never transmit (drafts only).

The full rationale lives in [`docs/design/PERSONAL_OS_DESIGN.md`](docs/design/PERSONAL_OS_DESIGN.md)
and the decision log [`docs/DECISIONS.md`](docs/DECISIONS.md).
