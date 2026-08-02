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

## Run the engine you just edited — the edit→install→run loop

**Since ADR-017 there is no `./plainkeep <verb>` loop.** This checkout is the engine SOURCE and (if
you registered it) a data vault, and one directory cannot be both: dispatching through the
checkout's own launcher against the checkout is refused with **exit 5**, naming the installer as the
remediation. What runs your edits is an INSTALLED engine, and an installed engine is a read-only
snapshot — so an edit is not live until you re-install it.

```sh
python3 bin/lib/enginetree.py --install . --force   # ~0.2 s; re-points `current` atomically
plainkeep <verb>                                    # the INSTALLED launcher, put on PATH by script/setup
```

`--force` is required because re-installing the same `VERSION` must REPLACE the tree rather than
refuse it — which is exactly the contributor case. If `plainkeep` is not on PATH, invoke the
launcher by path: `"$(python3 bin/lib/enginetree.py --print current)"/plainkeep <verb>`.

To iterate without touching your real install, point the install ROOT somewhere disposable — the
variable is read by the installer surface only, never by a dispatch:

```sh
export PLAINKEEP_ENGINE_HOME=/tmp/pk-dev
python3 bin/lib/enginetree.py --install . --force
"$(python3 bin/lib/enginetree.py --print current)"/plainkeep <verb>
```

The checkout can still be ACTED ON as a vault — `plainkeep status` from an installed engine against
this directory works fine. What is refused is dispatching *through this checkout's own launcher*.

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
4. **Re-install, then `plainkeep help`** — `python3 bin/lib/enginetree.py --install . --force` first
   (see the loop above; the installed tree is a snapshot, so an un-installed verb does not exist to
   the dispatcher), then `plainkeep help` regenerates `plainkeep.json`. Confirm the verb appears.
5. **`test/run_<verb>.py`** — cover it against a temp `PLAINKEEP_HOME` (and `PLAINKEEP_ROOTS_HOME`
   for the sibling roots). Add it to `test/run_all.py`.
6. **`plainkeep doctor`** — should stay all-green (again: through the installed launcher).

### Conventions

- **Flat verbs, shallow subactions.** `plainkeep task add …` is fine; never nest deeper.
- **Plaintext is truth.** No binaries in `wiki/`; indexes are disposable caches.
- **The guardrail is the system's, not the verb's.** Don't re-implement safety per verb — declare the
  right `risk` and (for verbs that handle caller-supplied paths) call `guardrail.classify()`.
- **The roots are walls.** Write only inside `~/plainkeep`, `~/files`, and the task's `~/work` repo.
  Never iCloud/family paths; never transmit (drafts only).

The full rationale lives in [`docs/design/PERSONAL_OS_DESIGN.md`](docs/design/PERSONAL_OS_DESIGN.md)
and the decision log [`docs/DECISIONS.md`](docs/DECISIONS.md).
