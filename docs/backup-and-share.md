# Backup & share — the durability floor and the sharing surface

How to use plainkeep's two durability verbs: `plainkeep backup` (Part 5.1) makes the system
trustworthy, and `plainkeep share` (Part 5.2) makes it reachable. For anyone running or maintaining
a plainkeep vault.

Backup comes first. `~/files` is the one root where loss is physically irreversible.

## `plainkeep backup` — the restic family

Bare `plainkeep backup` is a read-only nag. It verifies `~/plainkeep` is committed and pushed, and
exits 1 if anything is at risk. It never transmits.

The subcommands add the real durability floor. restic is auto-detected (`shutil.which`); every
restic path degrades with an install hint when restic is absent.

| Subcommand | Class | What it does |
|---|---|---|
| `backup init` | confirm (`--approved`) | Human-run once. Writes `.backup/config.json` with the local external-SSD repo and B2 bucket as **`op://` references** (never resolved), renders `com.plainkeep.backup.cloud.plist`, and prints the `cp` / `launchctl load` lines. It never installs a launch agent for you. |
| `backup status` | read | Snapshot age per target. Exits 1 if any target is stale over 48h, unconfigured, or unverifiable. Works without restic. |
| `backup run [--target local\|cloud]` | safe_write; **cloud is confirm** | `restic backup` of `~/files`, `~/dotfiles`, and the working trees of `~/plainkeep` and `~/work`, then `restic check`. |
| `backup drill` | confirm (`--approved`) | Restore the latest snapshot to a temp dir and diff. A backup that has never been restored is a hypothesis. |
| `backup bundle` | safe_write | Rotated `git bundle --all` of `~/plainkeep` and every `~/work` repo into `~/files/backups/bundles/` (keep last N, `PLAINKEEP_BUNDLE_KEEP`). Closes the "remote-less repo has one copy in the world" hole; captured by restic. |

### The append-only cloud push runs outside the verb surface

The scheduled cloud backup is **launchd invoking restic directly**. It uses a dedicated append-only
(no-delete, no-prune) B2 key, resolved at runtime from `op://` via `op read`.

This resolves the never-transmit tension honestly. The human consents once at `init --approved`.
Thereafter it is machine infrastructure, like Time Machine.

Even a fully compromised laptop or agent can only ADD to history, never erase it. Pruning is manual
only, with a separate privileged key.

The rendered plist carries exactly this command. `plainkeep` itself never performs the cloud push.

`.backup/config.json` and the rendered plist are user-owned (per-vault). They are not in
`script/engine.txt`.

## `plainkeep share` — capability URL, confirm-gated

`plainkeep share <slug>` and `share collection <tag>` render the note(s) to a self-contained
**OPSX** bundle (raw wiki markdown plus rendered HTML) locally:

- inline CSS
- wikilinks resolve only within the shared set; others degrade to plain text
- images under a size cap inline as data URIs

The bundle is PUT **as plaintext** to a vendored Cloudflare Worker plus KV (`bin/share/worker/`),
under a 24-char unguessable token (`[a-z0-9]{24}`, ~124 bits).

That token IS the secret. There is no encryption and no key to lose.

**One link, two ways to read it:**

| You open | You get |
|---|---|
| `https://<worker>/<token>` in a browser | the rendered HTML page |
| `https://<worker>/<token>.md` | raw wiki markdown, `text/markdown` — paste into any chat or coding agent with a fetch tool |
| `https://<worker>/<token>` from `curl` / `web_fetch` (non-browser `Accept`) | raw markdown too — content negotiation serves the same thing the `.md` suffix does |

There is no second "agent URL" to construct and no header to set. Append `.md`, or hand the printed
`agents / LLMs:` line straight to the agent, and it reads the exact wiki source. See
[`share-agent-markdown.md`](share-agent-markdown.md).

### Rules and safeguards

- Publishing is a transmission, so every transmitting subaction self-gates `--approved`
  (`EXIT_CONFIRM=3`). `--dry-run` renders locally and stops before the PUT. The verb's output is a
  **draft link** — the human sends it.
- `PUT /` requires a matching `X-Publish-Token` header when the `PUBLISH_TOKEN` wrangler secret is
  set (`plainkeep share init` provisions it). A discovered endpoint can't be abused as a free file host.
- Expiry is native KV `expirationTtl`, 1:1 from `--expires` (e.g. `7d`).
- `share revoke <id>` issues a `DELETE` with the admin token recorded in `.share/ledger.json`.
- `share list` shows every share.
- Links published under the previous encrypted model return **410** on every route. Re-publish.
- `--gist` is a throwaway fallback (`gh gist create`, confirm-gated). Same trust model as this
  capability-URL scheme, just hosted by GitHub instead of the vendored worker.
- `plainkeep sweep` warns on expired shares and on notes edited since they were shared.

### Threat model — what changed, and why

The share model is **capability URLs** (ADR-008), not encryption.

The worker (Cloudflare, an infrastructure provider we already trust for DNS and edge) can
technically read anything published through it. That is the tradeoff, made explicit and accepted.

The property given up is zero-knowledge. Previously the decryption key rode the URL `#fragment`.
HTTP never sends a fragment to a server, so the provider held only ciphertext.

But that design cannot satisfy the operator's actual requirement: one URL that a browser renders and
a headless agent can fetch. A `#fragment`-only key is, by construction, never on the wire for a
server-side fetch to use.

Every variant that tried to bridge the two produced either a two-link contract or an "install
something first" step. Both were rejected on ergonomics:

- `?k=` query key
- an `X-Ops-Share-Key` header
- a second `agent_url`
- client-side `plainkeep share pull` math

Once the key has to be visible to the server for any agent-fetchable route to work, zero-knowledge
buys nothing over a plain unguessable token. So we drop it and get the one-link contract for free.

The remaining exposure is: secrecy = link + TTL, and holder-of-URL can read. That is exactly the
trust model already accepted for `--gist`: a secret gist.

Full writeup, alternatives tried, and the URL-pattern decision:
[`design/proposals/2026-07-10-capability-url-share.md`](design/proposals/2026-07-10-capability-url-share.md).
ADR: [`DECISIONS.md`](DECISIONS.md) (ADR-008).

See [`bin/share/worker/README.md`](../bin/share/worker/README.md) for the worker API and deploy
steps.
