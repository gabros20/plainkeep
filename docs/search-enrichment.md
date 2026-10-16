# Search enrichment — generated meta for every source

This guide sets up the local model behind `plainkeep enrich` and the `plainkeep models` surface that manages it.
It is for anyone who wants notes to be more findable, and it covers what still works with no model
installed at all.

Full design: [`docs/design/proposals/2026-07-07-search-enrichment-pipeline.md`](design/proposals/2026-07-07-search-enrichment-pipeline.md).

## What it does

A source is only as findable as the text that describes it.

`plainkeep enrich` reads a note's derived text and writes a short `description` + `keywords` into that
note's frontmatter, using a small local LLM. The derived text is the `.extract.md` buffer for a
file, or the note's own body for a bookmark or wiki note.

Frontmatter is the top FTS chunk and leads the file inside the embed window. So this meta makes a
source both keyword- and semantically-searchable, with no new index code.

It runs on its own too. `plainkeep files extract` and `plainkeep bookmark` call enrichment automatically after
writing their note, unless `PLAINKEEP_ENRICH=off`. The call is best-effort and non-fatal, so a model
hiccup never fails the extract or save.

## Prerequisites & install

Pull the default model:

```bash
ollama pull gemma4:e4b              # default — EN + Hungarian (140+ languages), ~5 GB
```

> [!IMPORTANT]
> Install the model into **the same Ollama that `plainkeep` actually talks to** — not some other host or
> profile. This is the same "same interpreter/daemon `plainkeep` runs" trap as vector search. If
> enrichment works in your shell but not from an agent terminal, see
> [`docs/agent-terminal-search.md`](agent-terminal-search.md).

Pick a different model with `PLAINKEEP_ENRICH_MODEL` (or `models.enrich` in `~/.config/plainkeep/config.toml`; the environment variable wins):

| Model | When |
|---|---|
| `gemma4:e4b` (default) | general EN + Hungarian use |
| `OpenEuroLLM-Hungarian` | Hungarian-max quality override |
| `gemma4:e2b` | low-RAM hosts |

No model pulled, or no Ollama running at all? Enrichment still runs. See [Degradation](#degradation).

## Env knobs

| Knob | Values | Default | Effect |
|---|---|---|---|
| `PLAINKEEP_ENRICH` | `auto`, `off` | `auto` | `off` disables enrichment entirely — no model call, no floor |
| `PLAINKEEP_ENRICH_MODEL` | an Ollama model tag | `gemma4:e4b` | Model used to generate `description` / `keywords` |
| `PLAINKEEP_ENRICH_KEEP_ALIVE` | Ollama `keep_alive` value | `0` | `0` unloads the model after each call. `plainkeep enrich --all` raises this to `5m` for the batch so a multi-GB model isn't reloaded per note |
| `PLAINKEEP_STT_MODEL` | a model id for whichever ASR backend runs | backend's hardcoded default | Overrides the transcription model in `plainkeep files extract`'s audio tier |
| `PLAINKEEP_STT_RUNTIME` | `parakeet`, `mlx-whisper`, `faster-whisper`, `whisper-cli`, `auto` | `auto` | Pins one ASR backend instead of cascading through all installed ones |

## Usage

```bash
plainkeep enrich <slug>              # enrich one note (idempotent — no-op if the text hasn't changed)
plainkeep enrich <slug> --reenrich   # force past the idempotency key
plainkeep enrich --all               # sweep every note lacking a current key, sequentially, one warm model load
```

`plainkeep files extract` and `plainkeep bookmark` call the same path automatically once they've written their
note. There is nothing to opt into beyond having a model reachable, or accepting the floor.

## `plainkeep models` — see and swap what each stage uses

`plainkeep models` manages every model-backed stage, not just enrichment. The stages are `stt`, `ocr`,
`vlm`, `enrich`, `embed`, and `rerank`.

```bash
plainkeep models list                        # per stage: configured model, runtime, pulled/available?
plainkeep models status                      # ollama ps — resident (loaded) models
plainkeep models stop [--all] [<model>]      # unload a resident model (reloads on next use, no data loss)
plainkeep models pull --stage enrich --approved   # ollama pull the configured model for one stage (or --all)
plainkeep models test enrich --approved           # run the stage's configured model on a sample, print output + timing
```

`pull` and `test` both self-gate behind `--approved`, because both can pull gigabytes.

Use `test` to A/B a candidate before adopting it:

1. Run `plainkeep models test enrich --model <candidate> --approved`.
2. Compare the output.
3. Set the stage's env var (e.g. `PLAINKEEP_ENRICH_MODEL`) to make it permanent.

## Verify

```bash
plainkeep doctor
```

`plainkeep doctor` probes the configured enrich model against the local Ollama daemon, unless
`PLAINKEEP_ENRICH=off`. It prints one of:

- `enrich model reachable (gemma4:e4b via Ollama)`
- `enrich model/Ollama not reachable — files extract/bookmark auto-enrich will fall back to the
  stdlib keyword floor (no description, no LLM keywords). Fix: run ollama and pull the configured
  PLAINKEEP_ENRICH_MODEL; or set PLAINKEEP_ENRICH=off.`

## Degradation

Enrichment never crashes an extract or save, and never hallucinates on garbage input.

| Condition | Result |
|---|---|
| No model reachable | `description` falls back to the leading sentence(s) of the source text. `keywords` fall back to a deterministic stdlib frequency+stopword extractor (`keyword_floor`) — no pip deps, works with nothing installed |
| Text shorter than ~40 chars, empty, or the `_(no text extracted)_` sentinel | Skipped entirely — no model call, no floor description (empty `description`, floor `keywords` if there's any text at all) |
| `PLAINKEEP_ENRICH=off` | No enrichment at all — `files extract` / `bookmark` skip the call |
| Model reachable | Full `{description, keywords}` from `PLAINKEEP_ENRICH_MODEL`, deterministic (temperature 0, fixed seed) so a re-run on unchanged text is a true no-op |

The floor keeps search improving even with zero models installed. A reachable model just makes the
`description` prose and the `keywords` sharper.
