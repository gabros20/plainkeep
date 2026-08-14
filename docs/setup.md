# Layered setup

This guide shows how to finish setting up your vault after bootstrap. It is for anyone running `plainkeep setup` on a fresh clone, plus agents that need to advance layers safely.

## Before you start

The bootstrap scripts handle L0. Run `script/get` and `script/setup` first; they:

- **Install the engine** into `${XDG_DATA_HOME:-~/.local/share}/plainkeep/engine/<version>/` and
  point a `current` symlink at it.
- Put that installed launcher on PATH (`~/.local/bin/plainkeep` → `…/engine/current/plainkeep`).
- Install shell completion.
- Create `~/work` and `~/files`.
- Track the template as a fetch-only `upstream` remote.
- Mark + register the checkout as a **vault**.
- Run `plainkeep doctor --init`.

GitHub remote and push work is left to you.

### The engine and the vault are two trees (ADR-017)

Since Phase 2 Task 2, plainkeep's code does not live in the vault it edits. The engine is a
versioned, **read-only** tree; the vault is your data. They must not overlap — an invocation where
the data root IS the engine root, or either contains the other, is refused with exit 5.

What that changes in practice:

| You want to… | Do this |
| --- | --- |
| Run plainkeep | `plainkeep <verb>` (the installed launcher on PATH). Your checkout's own `./plainkeep` against that same checkout is the refusal above. |
| See which engine is live | `plainkeep vault status` — it reports the vault, the engine, and whether they agree. |
| Pull an engine fix | `script/update` (refreshes the SOURCE checkout, staged) then `python3 <engine>/bin/lib/enginetree.py --update <checkout>` (stages, checksums, self-tests, then switches one pointer). Two steps on purpose: an update you have not installed cannot break a running vault. |
| Start a NEW vault | `plainkeep vault init <path> --yes` — content dirs, configuration, `plugins/`, a generated `plainkeep.json`, the marker and a registry entry. **No engine code**: a vault is data. |
| Adopt an EXISTING directory as a vault | `plainkeep vault register <path> --yes`. `init` creates; `register` adopts — including this checkout, which is legitimately both an engine source and a vault. |
| Roll back | `python3 <engine>/bin/lib/enginetree.py --rollback` — see the runbook below. |
| See what a rollback would do | `python3 <engine>/bin/lib/enginetree.py --print pairs`, or the `engine:` rows in `plainkeep doctor`. |
| Add a capability | `plainkeep new verb <name>` — it scaffolds into `<vault>/plugins/local/`, which is yours and survives every engine upgrade. Editing the engine is not an option; it is read-only. |
| Install engines somewhere else | export `PLAINKEEP_ENGINE_HOME`. It relocates the install ROOT only; it never steers where a running dispatch loads code from. |

The cost, stated: a read-only tree cannot cache compiled Python beside its source, so each spawned
verb re-compiles the shared library it imports — **+17.6 ms, +12.2%** measured on macOS arm64 /
CPython 3.12 (ADR-017 Consequences).

### Updating the engine, and rolling back (ADR-021)

An update stages a checksum-verified **core+engine pair** into a new version directory, runs a real
verb through that pair's own dispatcher before anything is activated, and then switches exactly one
symlink. **The pair you were running is kept** — it is never the target of an update, so nothing the
update does can remove it.

```sh
ENG="$(python3 -c 'import os;print(os.path.expanduser("~/.local/share/plainkeep/engine/current"))')"

script/update                                        # 1. pull engine files into the checkout (staged)
python3 "$ENG/bin/lib/enginetree.py" --update .      # 2. stage → checksum → self-test → activate
plainkeep doctor                                     # 3. confirm
```

Step 1 re-runs itself once if the update changed `script/engine.txt` (it prints
`script/engine.txt changed — re-running against the new manifest`). That matters because the
manifest is an engine path too: a pass reads the list it started with, so a newly ADDED entry could
otherwise pull the name of a file without the file, and step 2 would then refuse for a missing
source path that the manifest had only just started requiring.

If step 2 refuses, nothing was activated and you are still on the pair you were running. If step 3
is unhappy, the rollback is three lines and it is a tested sequence, not advice:

```sh
python3 "$ENG/bin/lib/enginetree.py" --print pairs   # what would a rollback do?
python3 "$ENG/bin/lib/enginetree.py" --rollback      # switch back
plainkeep doctor                                     # confirm the pair that landed works
```

**If an update is interrupted** — a `^C`, a `SIGKILL`, a laptop that slept — re-run the same
`--update` command. It converges: it finishes whatever was left, and a second run says
`already active` and does nothing. `plainkeep doctor` warns when an update was interrupted between
recording its intent and moving the pointer.

`--keep N` bounds how many versions survive (default 2 — the active pair and the one you would roll
back to). It cannot go below 2: retaining the previous pair is the contract, not a preference.

### Migrating an existing `~/ops` vault

If you have an existing vault from before the opskit → plainkeep rename (ADR-012), move it by hand:

```sh
mv ~/ops ~/plainkeep                       # 1. move the vault itself
mv ~/plainkeep/.ops-engine-ref ~/plainkeep/.plainkeep-engine-ref  # 2. rename the engine-ref file
~/plainkeep/script/setup --yes             # 3. install the engine and re-point PATH at it
```

Step 3 replaced a manual `ln -sf ~/plainkeep/plainkeep …` onto PATH. Since ADR-017 the thing on PATH
is the INSTALLED engine's launcher, not the vault's — a vault has no launcher of its own.

Then, in your shell profile, rename every exported `OPS_*` environment variable to its `PLAINKEEP_*`
counterpart (e.g. `OPS_HOME` → `PLAINKEEP_HOME`, `OPS_VECTORS` → `PLAINKEEP_VECTORS`) and re-source
it. Run `plainkeep doctor` afterward to confirm the vault is healthy under its new name.

`plainkeep setup` handles the remaining local layers. It is safe to rerun. Each layer checks its current state first and reports one status:

| Status | Meaning |
| --- | --- |
| `ready` | Layer is fully set up. |
| `partial` | Some pieces present, some missing. |
| `absent` | Not set up. |
| `blocked` | Needs a prerequisite or human input first. |
| `not_applicable` | This host can't run it (e.g. launchd off macOS). Advisory, never a failure. |

The full contract is in [the machine contract §7](machine-contract.md#7-plainkeep-setup-layer-status-enum).

The six layers are: `skeleton`, `search`, `backups`, `models`, `automation`, `ui`.

## Guided first-run

The easiest way to finish setup:

```sh
plainkeep setup --wizard
```

The wizard walks the layers in order with skippable prompts and safe defaults pre-selected:

- `skeleton` **on** — required and safe.
- `automation` **on** — your day starts and closes on a schedule (see [Automation](#automation-the-default) below). It installs launch agents in `~/Library/LaunchAgents`; reversible with one command.
- `ui` **on** — a small sha256-verified binary download into the vault's own `.local/bin`.
- `search`, `models` **off** — no vectors, no model pulls.

Press Enter to accept a default. Type `y`/`n` to override.

The automation prompt names the actual jobs and times it is about to schedule, read from *your*
`jobs/registry.json` rather than from a sentence in this document:

```
schedule these to run unattended — start (daily 07:30), index (every 60m),
consolidate (daily 02:30), organize_scan (weekly Sun 03:00),
close_nudge (daily 18:30), backup_check (weekly Fri 17:00)? [Y/n]
```

Already-`ready` layers are noted and skipped. `blocked` and `not_applicable` layers show their reason and next step, and are never prompted to install.

Backups are never a yes/no here. The wizard prints the `plainkeep backup init` handoff (it needs human secrets) and never runs it.

Each accepted layer runs through the same engine as every other path below. There is no second code path.

The wizard is interactive-only. With no tty (e.g. a piped `curl … | sh`), or combined with `--json` or `--dry-run`, it exits `2` and points you at the non-interactive forms:

- `plainkeep setup --all --yes` to apply.
- `plainkeep setup --json` to inspect.

Those forms and the per-layer controls are below.

## Run the dashboard

```sh
plainkeep setup
```

Read the checklist top to bottom.

Required structure failures are fixed by the `skeleton` layer. Optional layers degrade gracefully: missing search, model, backup, or automation pieces become warnings with a one-line next step, not a broken vault.

The `automation` row reads `ready` only when the jobs are **loaded into launchd**, not merely rendered — the two are different facts and `plainkeep job status` shows them separately.

## Advance layers

Run one layer at a time for a controlled setup:

```sh
plainkeep setup skeleton --yes
plainkeep setup search --yes
plainkeep setup models --yes
plainkeep setup automation --yes   # renders the job plists AND loads them into launchd
plainkeep setup ui --yes
```

Use `--yes` when the layer is allowed to install packages, pull models, or write generated local files.

To advance every non-ready layer setup can safely handle:

```sh
plainkeep setup --all --yes
```

`--all` is best-effort:

- It advances every attemptable layer.
- A failure in one independent layer does not abort the rest.
- Layers that are already `ready`, `blocked` on a missing prerequisite, or `not_applicable` are skipped and never fail the run.
- Exit is `1` only if a layer it actually *attempted* failed.

### Preview first with `--dry-run`

Any advance can be previewed. `--dry-run` prints exactly what *would* run and installs or writes nothing.

A dry-run is a read, so it never needs `--yes` — even for the confirm-class `search`/`models` layers:

```sh
plainkeep setup search --dry-run     # what the search layer would create/install/pull
plainkeep setup --all --dry-run      # the whole plan, nothing touched
```

## The `.venv`: one home for all optional deps

Bare `python3` is the stdlib floor. Every core verb works on it with zero optional deps.

The optional `$PLAINKEEP_HOME/.venv` holds **all** optional deps. The `plainkeep` dispatcher prefers it whenever it exists and actually starts, falling back to bare `python3` otherwise. So `plainkeep index`, `plainkeep search`, `plainkeep files`, and `plainkeep doctor` all see those deps with no manual `PATH` surgery — including from an agent terminal.

Each layer installs its own dep subset into the same venv:

- `plainkeep setup search --yes` creates `$PLAINKEEP_HOME/.venv` (if missing), installs the search deps (`lancedb` + `fastembed`), pulls the embedding model, and builds the index.
- `plainkeep setup models --yes` does **two things**, and it says so before you confirm: it pulls the local Ollama model **weights** (gigabytes, over the network — not a pip install), and it installs the file-processing packages (Pillow, trafilatura, and — on Apple Silicon — mlx-vlm) into the **same** venv.

**Where those package lists come from (ADR-020).** Both are read from the engine's own
`pyproject.toml` — the `[search]` and `[models]` extras — which is the same file the engine's
`uv.lock` is resolved against. `requirements.txt` / `requirements-search.txt` are still there as the
by-hand `pip install -r` story, but they are **mirrors**: when the two disagree the pyproject wins,
and the suite reddens rather than letting them drift.

The venv is just the shared, dispatcher-visible environment they land in. So `plainkeep doctor`'s optional probes (Pillow / mlx_vlm / lancedb) run under the same interpreter and report consistently.

Re-running the relevant `plainkeep setup <layer>` provisions or migrates that layer's deps.

The venv is disposable and rebuildable:

```sh
rm -rf .venv && plainkeep setup search --yes && plainkeep setup models --yes
```

A broken or half-built venv is repaired automatically on the next `plainkeep setup search`/`models`.

This is the single install story. See [ADR-009](DECISIONS.md) and [the agent-terminal guide](agent-terminal-search.md).

## Provisioning the engine itself (ADR-020)

The layers above provision a **vault's** `.venv`. The **engine** has one too, and it is what makes
plainkeep work on a machine with no system Python at all:

```sh
python3 bin/lib/provision.py --ensure-uv     # download + verify the pinned uv
python3 bin/lib/provision.py --sync           # uv sync --frozen against the delivered lock
```

…or, on a machine that has no `python3` to run that with, the same two steps from the compiled core:

```sh
plainkeep-core --core-provision ensure-uv
plainkeep-core --core-provision sync
```

Four things about it are worth knowing before you run it:

- **uv is downloaded, not vendored, and pinned by exact version + sha256** (`bin/lib/uvpin.json`).
  The download is verified before it is made executable; a mismatch deletes it and refuses.
- **A uv already on your machine is ignored.** Not preferred, not fallen back to. The pin is the
  point: your uv is some version, with some config, and the engine's lock was resolved by neither.
- **It lands inside the engine version** (`<engine>/tools/`), so it is replaced with the engine and
  rolls back with it. That directory is the one writable path in an otherwise read-only tree.
- **Offline, it refuses and prints the exact manual command** — URL, expected sha256, destination —
  and leaves nothing half-installed. Air-gapped installs are a documented two-step, not a dead end.

`python3 bin/lib/enginetree.py --verify --digests` answers the question the seal cannot: is this
still the code that was installed? (The checksums live *outside* the tree, beside the versioned
directories, so an edit inside it cannot rewrite them.)

## The terminal UI layer

```sh
plainkeep setup ui --yes
```

This installs the compiled **`plainkeep ui`** binary (ADR-011) — the guided human terminal UI — into `$PLAINKEEP_HOME/.local/bin/plainkeep-ui`, where the `bin/ui/` shim looks first.

Key facts:

- It downloads the matching platform asset from the template repo's GitHub release with the authenticated **`gh` CLI**. If the layer reports `blocked`, install gh with `brew install gh`.
- It verifies the asset's sha256 against the release's `checksums.txt` before installing.
- The binary is self-contained — no Node or Bun needed.
- **Updates ride `script/update`.** The engine ships the expected version in `bin/ui/version.txt`; the installed binary self-reports via `plainkeep-ui --version`. When they disagree, the layer turns `partial` with "update available" and re-running `plainkeep setup ui --yes` re-downloads the pinned release.

Full install, use, and update details are in the [terminal UI guide](terminal-ui.md).

## Automation: the default

Scheduling is not an add-on here. The premise of the whole system is that your day opens and closes
without you asking, so the `automation` layer is **on by default** in the wizard and in
`plainkeep setup --all --yes`. What it schedules (macOS/launchd; see the note at the end):

| Job | When | What it does |
| --- | --- | --- |
| `start` | daily 07:30 | opens today's journal, carries forward open tasks |
| `close_nudge` | daily 18:30 | writes the day's facts digest, flags loose ends |
| `index` | every 60m | keeps search current |
| `consolidate` | daily 02:30 | the nightly digest |
| `organize_scan` | weekly Sun 03:00 | proposes filing moves (proposes — never applies) |
| `backup_check` | weekly Fri 17:00 | nags if the backup is stale |

Only `read` and `safe_write` jobs may be scheduled (§15). A `confirm`-class verb — anything that
transmits, or that moves your files without asking — can never run unattended, and both
`plainkeep job list` and `plainkeep doctor` flag a registry that tries.

### The three states, and why they are three

```sh
plainkeep job status
```

```
6 job(s) — rendered (vault) / installed (LaunchAgents) / loaded (launchd):

  start          yes  yes  yes  loaded
  index          yes  yes  -    installed, not loaded
  consolidate    yes  -    -    rendered only
```

- **rendered** — `jobs/launchd/com.plainkeep.<job>.plist` exists in your vault *and still matches a
  fresh render of `jobs/registry.json`*. A file that no longer matches is **drift**, not a render:
  it describes a schedule nobody currently wants.
- **installed** — a copy of that plist sits in `~/Library/LaunchAgents/`.
- **loaded** — launchd actually answers for the label (`launchctl print`).

They are reported separately because they used to be conflated, and the middle two were where a
schedule quietly failed to exist. Until ADR-022, `plainkeep job apply` rendered the files and
*printed* the `ln -sf` + `launchctl load` lines for you to paste; every machine where nobody pasted
them had a green setup row and no automation.

### Turning it on, off, and back on

```sh
plainkeep job enable --all --yes      # render fresh, install, load
plainkeep job enable start --yes      # just one job
plainkeep job disable --all --yes     # unload + remove the installed copies
plainkeep job enable --all --dry-run  # exactly what would happen; nothing written, launchctl not called
```

`enable` and `disable` need `--yes` (they exit `3` without it), and so does `plainkeep setup
automation` — the layer is confirm-class for the same reason the verb is. That is not ceremony: they
write outside your vault, into `~/Library/LaunchAgents`, and they change the state of a running
system daemon. `--dry-run` is a read and never needs `--yes`.

A job whose registry entry is illegal under §15 — inline shell logic, a verb that does not exist, an
external command that is not on the allowlist, a name that is not a plain identifier — is **refused**
by `apply` and `enable`, not skipped. `plainkeep job list` shows you which and why. What the product
refuses to run once by hand it will not schedule to run unattended.

`enable` always **re-renders from the registry** before installing, so editing `jobs/registry.json`
and re-running is the whole edit loop — it never installs a stale file. It unloads before it loads,
so re-enabling an already-running job is safe and idempotent.

`disable` removes the installed copies and unloads the jobs. It leaves the rendered plists under
`jobs/launchd/` alone: those are your vault's record of the schedule, owned by `plainkeep job apply`.

Nothing is lost by declining. Every job is one verb you can run yourself:

```sh
plainkeep job run start
```

### If something looks wrong

`plainkeep doctor` reports two advisory warnings (never a failure — running plainkeep by hand is a
legitimate way to run it):

- *rendered but not loaded* → `plainkeep job enable --all --yes`
- *rendered plists no longer match the registry* → `plainkeep job apply`

Job output goes to `.logs/jobs/<name>.log`.

**Off macOS** the layer reports `not_applicable`: launchd is macOS-only. The registry is
scheduler-neutral by design, so the jobs remain runnable by hand (or from any scheduler you point at
`plainkeep job run <name>`), and nothing about this ever fails a health check on Linux.

## Blocked: backups

Blocked layers stay blocked and print the exact remediation in `next`.

Backups are blocked because encrypted off-machine backup setup needs human choices and secrets (and `restic` installed):

```sh
plainkeep backup init
```

## Agent path

Agents should inspect state before acting:

```sh
plainkeep setup --json
```

Use the returned rows to choose the smallest non-ready layer, then advance it explicitly:

```sh
plainkeep setup <layer> --yes
```

Rules:

- Do not invent install commands from status text.
- If a row is `blocked`, report its handoff command to the human instead of working around it.

## Check health

`plainkeep doctor` is the checker:

```sh
plainkeep doctor
```

It reports the same setup layers and:

- Fails required non-ready layers.
- Treats optional `partial`, `absent`, or `blocked` layers as advisory warnings.
- Treats `not_applicable` layers as informational — never a failure, even were the layer required.
