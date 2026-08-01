# Docs

Start with the project [README](../README.md) — what plainkeep is and how to install it. The docs
here follow [Diátaxis](https://diataxis.fr): each file serves one purpose. Pick your path:

**I want to set it up**

| Doc | Covers |
|---|---|
| [`setup.md`](setup.md) | the layered `plainkeep setup` installer — wizard, dashboard, every layer |
| [`terminal-ui.md`](terminal-ui.md) | install, use, and update the guided `plainkeep ui` terminal UI |
| [`backup-and-share.md`](backup-and-share.md) | restic backups and capability-URL sharing |
| [`mobile-and-capture.md`](mobile-and-capture.md) | capture from a phone; read the vault on mobile |

**I want to understand it**

| Doc | Covers |
|---|---|
| [`how-it-works.html`](how-it-works.html) | the 2-minute interactive tour (open in a browser) |
| [`architecture.md`](architecture.md) | why the system is shaped this way — principles and trade-offs |
| [`DECISIONS.md`](DECISIONS.md) | the ADR log: every load-bearing decision, with the why |
| [`design/`](design/) | the original design spec and accepted proposals (ADRs supersede) |

**I want the exact rules** (reference)

| Doc | Covers |
|---|---|
| [`machine-contract.md`](machine-contract.md) | the `--json` envelope, exit codes, `plainkeep.json`, `cmd.json`, completion |
| [`obsidian-compat.md`](obsidian-compat.md) | the normative definition of "Obsidian-compatible" |
| [`../CHANGELOG.md`](../CHANGELOG.md) | what changed and when |

**I want to go deeper** (optional capabilities)

| Doc | Covers |
|---|---|
| [`plugins.md`](plugins.md) | write your own verbs; install and trust packs |
| [`agent-terminal-search.md`](agent-terminal-search.md) | semantic search from agent terminals — venv and PATH story |
| [`image-reading.md`](image-reading.md) | OCR and VLM description for images (`plainkeep files extract`) |
| [`search-enrichment.md`](search-enrichment.md) | generated description/keywords metadata (`plainkeep enrich`, `plainkeep models`) |
| [`share-agent-markdown.md`](share-agent-markdown.md) | how shared links serve raw markdown to agents |

**Operating the system** (humans and agents alike):
[`skills/operate-plainkeep/SKILL.md`](../skills/operate-plainkeep/SKILL.md), paired with the agent
contract [`AGENTS.md`](../AGENTS.md). Contributing to the engine: [`CONTRIBUTING.md`](../CONTRIBUTING.md),
with the known deferred work in [`followups.md`](followups.md).
