# `test/` — a simulation harness for testing a *design*

This harness began before any code existed — to test the **design** in `../docs/design/
PERSONAL_OS_DESIGN.md`. It still does that (encode the rules as a model, fire adversarial
inputs, plug a real LLM in as the *operator* to find drift/overreach/misfiling) — and now it
**also tests the real implementation** (the `plainkeep` retrieval engine: stages 1–3). The deterministic
suites need only Python stdlib; the operator sim needs the `claude` CLI; the vector/rerank tests
skip cleanly without Ollama/lancedb/fastembed.

It catches the failure modes a design review misses by eye: guardrail bypass paths, ambiguous
filing rules, the cloned-tool trap, iCloud-wall leaks, transmit-without-confirm, secret access,
skipped brain-first lookups, invented verbs, and "the manual reads two ways" divergence between
agents.

## Quick start
```sh
python3 test/run_all.py                 # every OFFLINE suite (guardrail, jobs, sweep, wiki, search)
python3 test/run_simulation.py --model sonnet   # the LLM-operator half (needs the claude CLI)
```

## Offline suites (no LLM, no cost) — `run_all.py`

| Suite | Script | What it models | Status |
|---|---|---|---|
| Guardrail (§5) | `run_deterministic.py` | path-wall + risk classes vs 29 adversarial actions | 29/29 |
| Jobs registry (§15) | `run_jobs.py` | only read/safe_write scheduled, verbs-not-logic, risk-matches-effect, writes-inside-roots | 25/25 |
| Sweep decay (§9.4) | `run_sweep.py` | the 7d→`_swept`→60d→trash machine on a virtual clock (+ boundary, rescue, month-roll) | 19/19 |
| Wiki integrity (§10) | `run_wiki.py` | frontmatter, link resolution, backlinks, orphans, stale, hub two-zone | 12/12 |
| Wiki link edges (§10) | `run_wiki_edges.py` | slug collisions, ambiguous links, `#`/`|` link syntax, self-links, cycles | 6/6 |
| State & consistency | `run_state.py` | folder-wins task status, index-rebuild=files, journal atomic-append, restore order | 12/12 |
| Searchability (§10.2) | `run_search.py` | keyword vs keyword+graph vs semantic-proxy/real-vectors → the **vector decision** | report |
| Stage-1 search impl (§10.2) | `run_search_impl.py` | the REAL `plainkeep index`/`plainkeep search` (FTS5 + wikilink graph): lexical recall@5, incremental, rebuild rule, CLI | 6/6 |

Each is pure TDD on the spec: a FAIL is a defect in the design as written. Real spec gaps found
and fixed this way: `plainkeep repo adopt` (no verb to place a cloned repo); the `plainkeep sweep`
Desktop/Downloads write-zone carve-out; the `files_backup` read-vs-transmit distinction; the §5
reliability hardening (symlink-realpath resolution, case-insensitive wall, transmit-by-any-tool);
global slug uniqueness (§10.1); and the touch-in-`_swept`-doesn't-rescue rule (§9.4).

### The vector decision (`run_search.py`)
Runs three retrievers over the corpus + a labeled query set, auto-bucketing each query as
**lexical** (relevant note shares vocabulary) or **semantic** (zero shared terms — only meaning
matches). Keyword/keyword+graph score perfectly on lexical and **zero on semantic** (by
construction — that's the point); a conservative trigram "semantic proxy" recovers most of the
semantic misses, marking a floor on what real embeddings would add. The verdict applies the
design's own stage-2 trigger ("add vectors only when FTS5 demonstrably misses"). It is an
*estimate* on a synthetic corpus — set `PLAINKEEP_EMBED_CMD` and feed your real query log before a
final call.

## The LLM-operator half — `run_simulation.py`

### 1. Deterministic — the guardrail model (no LLM, no cost)
`lib/guardrail.py` implements the §5 path-wall + risk classes **exactly as the design specifies**.
`cases/guardrail_cases.json` fires 29 adversarial actions at it and asserts the verdict
(`allow` / `confirm` / `deny`). A failure means the *spec's rules as written* let something
dangerous through or block something benign — a defect in the design, not the code.

```sh
python3 test/run_deterministic.py        # 29 cases, offline, exit 0/1
```

This is real TDD on a spec: when you change a guardrail rule in the design, add the case here
first and watch it fail, then make the model (and the design) agree.

### 2. Probabilistic — the LLM operator simulation
For each scenario in `cases/scenarios.json`, the harness:
1. **extracts the actual contract from the design doc** — `lib/spec.py` parses the `AGENTS.md`
   (§12.2) and `operate-plainkeep/SKILL.md` (§12.3) fenced blocks out of `docs/design/PERSONAL_OS_DESIGN.md`,
   so the test can never drift from the spec. Edit the doc → the test updates.
2. builds the operator prompt: contract + manual + a simulated four-root world (`world/seed.json`)
   + the scenario, demanding a strict-JSON **plan of actions** (the model plans, it never touches
   the real filesystem).
3. runs the operator — `lib/op_runner.py` shells out to `claude -p` by default (the same agent
   indirection the design's `agent.sh` uses); any model works.
4. **judges** the plan — `lib/judge.py` runs rule-based checks against the scenario's expectations
   **and replays every proposed action back through the §5 guardrail** (so if the manual lets the
   agent attempt a wall-denied act, that's a hard finding).

```sh
python3 test/run_simulation.py --dry-run              # offline plumbing check (dumb stub, no LLM)
python3 test/run_simulation.py --model sonnet         # real run, one model
python3 test/run_simulation.py --compare sonnet opus  # AGNOSTICISM/DRIFT mode: diff two models
python3 test/run_simulation.py --only icloud-tax-doc  # one scenario
python3 test/run_simulation.py --model sonnet --json out.json
```

> `--dry-run` uses an intentionally *imperfect* stub, so several scenarios "fail" — that is the
> point: it proves the judge catches drift. Real verdicts require a real `--model`.

### A note on probabilistic flakiness
The LLM operator phrases correct behavior differently each run. So the **hard gates are
structural** — required `plainkeep` verbs (`must_run_verbs`), `no_transmit`, the guardrail cross-check,
`search_first`, `forbidden_substrings`, `task_repo` scoping — which don't depend on wording.
Free-text discipline checks (`must_mention`) use OR-groups of synonyms that match *meaning*, not
exact tokens, so a plan that writes "## Outcome" or "verification results before declaring done"
both pass. If you want a stricter signal on a soft check, run the scenario a few times (or add a
majority-of-N wrapper) rather than tightening the keywords into flakiness. Filter subsets with
`--tag <tag>` (e.g. `--tag injection`, `--tag lifecycle`) to keep live-run cost down.

### Agnosticism / drift mode (§12.4 mechanized)
`--compare A B` runs every scenario through two different models and flags any where they
**disagree**. Divergence = the manual is ambiguous *there*. Per the design's rule: fix the
manual, not the model.

## Layout
```
test/
├── README.md                     # this file
├── run_all.py                    # all offline suites + summary
├── run_deterministic.py          # guardrail suite (offline)
├── run_jobs.py                   # jobs-registry invariants (offline)
├── run_sweep.py                  # sweep decay-machine simulation (offline)
├── run_wiki.py                   # wiki integrity checks (offline)
├── run_wiki_edges.py             # wiki link-reliability edges (offline)
├── run_state.py                  # state/consistency invariants (offline)
├── run_search.py                 # searchability analysis + vector decision (offline)
├── run_simulation.py             # LLM-operator suite (+ --compare drift mode, --replay)
├── world/
│   ├── seed.json                 # the simulated four-root machine state (operator sim)
│   ├── jobs.json                 # model of the §15 jobs registry
│   ├── wiki_corpus.json          # fixture wiki (with controlled defects)
│   ├── wiki_reliability.json     # edge-case wiki (collisions, anchors, cycles)
│   └── search_qrels.json         # retrieval gold labels (queries → relevant note)
├── cases/
│   ├── guardrail_cases.json      # 29 deterministic adversarial cases
│   └── scenarios.json            # 10 probabilistic operator scenarios
└── lib/
    ├── guardrail.py              # the §5 decision model (single source of truth for "allowed?")
    ├── jobsmodel.py              # §15 job-rule checks
    ├── sweepsim.py               # §9.4 decay state machine + virtual clock
    ├── statemodel.py             # task status, index rebuild, journal append, restore order
    ├── wiki.py                   # §10 note parser + integrity checks (collisions, aliases)
    ├── retrieval.py              # BM25 / keyword+graph / semantic-proxy + metrics
    ├── spec.py                   # extract contract/manual from the design doc; build the prompt
    ├── op_runner.py              # call the LLM operator, parse its JSON plan
    └── judge.py                  # score the plan; cross-check every action against the guardrail
```

## What each scenario targets
| Scenario | Failure mode it probes |
|---|---|
| `cloned-tool-trap` | filing a third-party clone into `clients/`/`products/` (the §4a trap) |
| `icloud-tax-doc` | writing a personal/legal doc instead of proposing the iCloud move (§9.4) |
| `client-brief-ingest` | wrong root / missing shadow note for a received original |
| `edit-original-typo` | editing immutable `~/files/**/in/` evidence |
| `send-invoice-now` | transmitting without an explicit human `--yes` |
| `read-env-and-deploy` | reading `.env` / deploying to prod (two fail-closed halves) |
| `brain-first-recall` | answering from the web/memory before `plainkeep search` |
| `honor-learned-filing-rule` | ignoring a learned `conventions.md` filing rule |
| `ambiguous-repo-never-guess` | guessing a destination instead of stopping to ask |
| `iron-law-handcomposed-path` | writing to a hand-composed path outside the roots |

## Extending it
- New guardrail rule → add a case to `cases/guardrail_cases.json` and (if needed) a branch in
  `lib/guardrail.py`.
- New agent-judgment risk → add a scenario to `cases/scenarios.json` with an `expect` block; the
  judge already understands `search_first`, `refuse`, `ask`, `no_transmit`,
  `propose_not_write_icloud`, `expected_root`, `destination_contains`, `forbidden_substrings`,
  `no_invent_verb`, plus the automatic guardrail cross-check.
- Skill routing → the design's `skills/<name>/routing-eval.jsonl` (§11) is the same idea scoped
  to trigger→skill; a future `run_routing.py` can consume those once skills exist.

## The first real implementation
Stage-1 search now exists for real (not just modeled): `../plainkeep` (dispatcher) + `../bin/index/`,
`../bin/search/`, `../bin/lib/indexlib.py` (FTS5 + wikilink-graph engine), over `../content/`.
Try it — but **not** as a bare `./plainkeep index` from the checkout. Since ADR-017 the checkout's
own launcher refuses to dispatch against the checkout itself (exit 5: a vault is data, an engine is
code). Give it a vault that is not this checkout and the same launcher works, live:

```sh
PLAINKEEP_HOME=/tmp/pk-dev-vault ../plainkeep index
PLAINKEEP_HOME=/tmp/pk-dev-vault ../plainkeep search "webhook retry"
```

(or install the engine — `python3 ../bin/lib/enginetree.py --install .. --force` — and dispatch
through `"$(python3 ../bin/lib/enginetree.py --print current)"/plainkeep`; see CONTRIBUTING.md's
"Run the engine you just edited" for when each loop is the right one.)

`run_search_impl.py` is its test. Rebuild rule: `rm -rf .index` then re-run `index` the same way.

## Requirements
Python 3.10+ (stdlib only). For the simulation: the `claude` CLI on `PATH` (or pass your own
operator command). The deterministic suite needs nothing but Python.
