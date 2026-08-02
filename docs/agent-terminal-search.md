# Running plainkeep from an agent terminal (venv, PATH, and semantic search)

This guide gets stage-2/3 semantic search (`PLAINKEEP_VECTORS=1`, `PLAINKEEP_RERANK=1`) working when `plainkeep` is
driven by an **agent terminal** instead of your interactive shell. Read it if keyword search works
but vectors don't, or if `plainkeep index` warns that `lancedb` is missing even though you installed it.

An "agent terminal" is any non-interactive driver: a Telegram/Hermes-style agent, a dispatched
Claude Code session, or a cron job.

> [!NOTE]
> **Stage 1 (keyword + wikilink graph) needs nothing but Python 3.10+ stdlib** and always works.
> This page is only about the *optional* vector/rerank planes. Everything here is setup, not code.

## The one install story

One command provisions everything, and the dispatcher finds it automatically:

```bash
cd "$PLAINKEEP_HOME"          # e.g. ~/plainkeep
plainkeep setup search --yes  # .venv + lancedb/fastembed + embed model + index — one command
```

`plainkeep setup search --yes` does four things:

1. Creates `$PLAINKEEP_HOME/.venv`.
2. Installs *only* the search deps (`lancedb` + `fastembed`) into it — read from the engine's `pyproject.toml` `[search]` extra, which is also what `uv.lock` is resolved against (ADR-019).
3. Pulls the embedding model.
4. Builds the index.

Preview it first with `plainkeep setup search --dry-run`. That writes nothing and needs no `--yes`.

### How the dispatcher picks its interpreter

The `plainkeep` dispatcher **prefers `$PLAINKEEP_HOME/.venv/bin/python3` whenever it exists and starts**
— for the guardrail, the resolver, and every verb. So `plainkeep index` and `plainkeep search` import
the vector plane with no manual `PATH` surgery.

If there is no venv, or the venv python is broken (a stale symlink after a system-python upgrade),
the dispatcher falls back to bare `python3`. That is the stdlib keyword floor. It never fails every
verb just because the vector plane is missing.

```
plainkeep  ──►  $PLAINKEEP_HOME/.venv/bin/python3   (if it exists — created by `plainkeep setup search`)
           └─ else bare python3 on PATH  ──►  import lancedb ?
                                                ├─ yes → stage-2 vectors
                                                └─ no  → keyword-only (a warning, never a crash)
```

### The contract (ADR-009)

| Interpreter | Holds | When the dispatcher uses it |
|---|---|---|
| bare `python3` | stdlib floor (keyword + graph) | no venv, or venv python won't start |
| `$PLAINKEEP_HOME/.venv/bin/python3` | all optional deps (search + models) | it exists and passes a start-probe |

Agent terminals inherit this for free. They run the same `plainkeep` script, so they get the same
interpreter with no per-agent PATH ordering.

> [!NOTE]
> The `.venv` is the single home for **all** optional deps, not just search. `plainkeep setup models
> --yes` installs the file-processing deps (Pillow, trafilatura, mlx-vlm) into the same venv. So
> `plainkeep files`, `plainkeep enrich`, and `plainkeep doctor` see them under the same
> dispatcher-preferred interpreter. This page focuses on search, but the interpreter contract covers
> both.

> [!NOTE]
> On **macOS Intel (x86_64)**, `pip` resolves `lancedb` to 0.25.x. That is expected and works.
> The `[search]` extra carries platform-aware markers, so it installs cleanly on every host. `requirements-search.txt` is a mirror of it for by-hand installs.

## What the agent terminal still needs: `PLAINKEEP_HOME` and the `PLAINKEEP_*` flags

The interpreter is handled for you. Two things still must reach the agent's shell:

- **`PLAINKEEP_HOME`** — so `plainkeep` and its venv resolve to *your* vault, not a default.
- **`PLAINKEEP_VECTORS=1` / `PLAINKEEP_RERANK=1`** — the opt-in flags that turn the vector/rerank arms *on* at
  query time. The venv makes them importable; these flags make search *use* them.

An interactive login shell sources `~/.zshrc`, so these are already present.

An agent terminal frequently is not. It builds its own environment per session and may source only
bash profiles (`~/.profile`, `~/.bash_profile`), which on a zsh-only Mac often don't exist.

The fix is generic: **point the agent's terminal at a bash-safe init file that exports
`PLAINKEEP_HOME` and the `PLAINKEEP_*` flags.** Use `export` statements only, no zsh-only syntax, because agent terminals
typically source init files with `bash`. You do not need to prepend `.venv/bin` to `PATH` — the
dispatcher does that job.

### Worked example — Hermes (`~/.hermes/config.yaml`)

*Accurate as of Hermes mid-2026. Hermes is a third-party tool, so verify against its current docs.*

Point Hermes at your init file:

```yaml
terminal:
  backend: local
  cwd: ~/plainkeep
  shell_init_files:
    - /Users/<you>/.zshrc          # or a dedicated bash shim, see below
```

> [!IMPORTANT]
> **`shell_init_files` must be a YAML *list*, not a scalar string.** Hermes discards a non-list value
> silently (its loader does `if not isinstance(files, list): files = []`) and falls back to bash
> profiles with **no error**. `hermes config set terminal.shell_init_files /path/to/.zshrc` can write
> a bare string, so confirm the file actually contains a `- ` list item. This silent no-op is the
> single most common cause of "it works in my shell but not for the agent".

If your `~/.zshrc` has zsh-only constructs, don't source it with bash. Use a dedicated shim instead:

```bash
# ~/.hermes/plainkeep-shell-init.sh   (bash-safe: export only)
export PLAINKEEP_HOME="$HOME/plainkeep"
export PLAINKEEP_VECTORS=1     # the dispatcher already prefers $PLAINKEEP_HOME/.venv/bin/python3 — no PATH surgery
export PLAINKEEP_RERANK=1
```

```yaml
terminal:
  shell_init_files:
    - /Users/<you>/.hermes/plainkeep-shell-init.sh
```

**Session/reload behavior (Hermes):** a config change may be picked up by file mtime, but **existing
sessions keep their old environment snapshot**. Start a **new** session (`/new`) to get the new env.
The gateway can't restart itself from inside a running session; restart it from a separate terminal
if a full reload is needed.

## Verify — from inside the agent's terminal

Run these *in the agent's terminal*, not your own. The whole point is that its environment can differ
from yours.

```bash
plainkeep setup search           # expect: search "ready" (deps-importable ✓ · model-pulled ✓ · index-built ✓)
plainkeep doctor                 # expect: "optional: lancedb present (stage-2 vectors)"
plainkeep index                  # embeds notes → vectors.lance (no fallback warning)
plainkeep search "some idea"     # semantic hits, not just keyword
```

If a check fails:

| Symptom | Fix |
|---|---|
| `plainkeep setup search` reports `blocked` | Run the exact command in its `next` field (e.g. install ollama). |
| `plainkeep doctor` prints `PLAINKEEP_VECTORS=1 but lancedb is NOT importable by this python3` | The venv wasn't created or is missing deps. Re-run `plainkeep setup search --yes` (or preview with `--dry-run`). |

`plainkeep index` still succeeds keyword-only. It never crashes on a missing vector plane. That graceful
fallback is by design — see the CHANGELOG / issue #3.

## What this deliberately does not require

- No manual venv.
- No `PATH` ordering.
- No vault-side daemon.

`plainkeep setup search` creates the one venv and the dispatcher prefers it. The stdlib keyword floor
still runs on bare `python3` when there is no venv. An agent terminal only needs `PLAINKEEP_HOME` and
the `PLAINKEEP_*` flags in its environment — it runs the same `plainkeep` script, so it inherits the
same interpreter. One install story, everywhere.
