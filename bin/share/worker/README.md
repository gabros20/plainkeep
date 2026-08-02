# plainkeep share worker

A tiny Cloudflare Worker + KV store implementing capability-URL, expiring shares for `plainkeep share`
(`docs/design/proposals/2026-07-10-capability-url-share.md`, ADR-008). It stores the **plaintext**
OPSX bundle (raw wiki markdown + rendered HTML) under a 24-char unguessable token (~124 bits) — the
token IS the secret; there is no encryption and no key. Modeled on SharzyL/pastebin-worker.

## API

| Method | Path | Headers | Body | Returns |
|--------|------|---------|------|---------|
| `PUT` | `/` | `X-Publish-Token` (required iff `PUBLISH_TOKEN` secret is set), `X-Expire-Seconds` | OPSX blob | `{ id, admin_token }` |
| `GET` | `/<id>` | `Accept: text/html` (browser) | — | rendered HTML half (CSP + `Link: rel="alternate"` to the `.md` form) |
| `GET` | `/<id>` | any other `Accept` (curl, agent fetch tools) | — | markdown half, `text/markdown` (content negotiation) |
| `GET` | `/<id>.md` | — | — | markdown half, `text/markdown` — the canonical agent form |
| `GET` | `/<id>?raw=1` | — | — | the whole stored OPSX blob, `application/octet-stream` (`plainkeep share pull`, debugging) |
| `DELETE` | `/<id>` | `X-Admin-Token` | — | `204` (admin only) |
| any GET on a legacy (non-OPSX / encrypted) blob | — | — | — | `410` `{error: "legacy encrypted share — re-publish with current plainkeep share"}` |

The id regex accepts `[a-z0-9]{10,32}` — 24-char tokens are current; 10-char legacy ids still route
(to the 410 above) until their TTL expires.

**One link, two forms.** Publish returns a single token. `https://<worker>/<token>` opens as a
self-contained HTML page in a browser (inline CSS, data-URI images, no JS — nothing to execute, so
`Content-Security-Policy: default-src 'none'; img-src data:; style-src 'unsafe-inline'` is safe to
send unconditionally). Append `.md`, or let content negotiation do it on the bare URL, and you get
the exact wiki markdown instead — the same thing `plainkeep share pull` fetches locally. See
`docs/share-agent-markdown.md`.

Expiry is native KV `expirationTtl` (1:1 from `--expires`, so there is no cleanup code). Revoke is a
`DELETE` with the `admin_token` returned at publish time and recorded in `.share/ledger.json`.

## Deploy (once)

`wrangler.toml` is **per-vault** (gitignored) and lives in the VAULT, at `<vault>/.share/wrangler.toml`
— not in this directory. This directory is engine-owned and an installed engine is sealed read-only,
so a per-vault file written beside `worker.js` could not be written at all. The engine ships
`wrangler.toml.example` only; `script/update` never overwrites your KV namespace id.

`plainkeep share init` prints these with your own paths filled in.

```sh
cp <engine>/bin/share/worker/wrangler.toml.example ~/plainkeep/.share/wrangler.toml
wrangler kv namespace create PLAINKEEP_SHARE --config ~/plainkeep/.share/wrangler.toml   # paste the id in
cd <engine>/bin/share/worker
wrangler deploy --config ~/plainkeep/.share/wrangler.toml
wrangler secret put PUBLISH_TOKEN --config ~/plainkeep/.share/wrangler.toml   # optional but recommended
plainkeep share init --endpoint https://plainkeep-share.<subdomain>.workers.dev
```

`PUBLISH_TOKEN` is optional: if the secret is unset, `PUT /` stays open (graceful for a fresh deploy
before you've run the secret step). Once set, every publish must send a matching `X-Publish-Token`
header or the worker returns `401` — this is what stops a discovered endpoint from being used as a
free anonymous file host. `plainkeep share init --yes` generates the token, runs `wrangler secret put
PUBLISH_TOKEN` for you, and writes it to `.share/config.json` (vault-private, same handling as the
`admin_token`s already in `.share/ledger.json`).

Free tier: 100k requests/day, 1k KV writes/day — a collection coalesces into ONE KV entry.

## Security notes

- **Capability-URL trust model.** The worker holds plaintext, not ciphertext — secrecy is the
  24-char unguessable token plus TLS plus the KV TTL, the same trust model as a secret gist or an
  "anyone with the link" doc. Cloudflare can technically read anything published through it; that is
  an accepted tradeoff, not an oversight — see `docs/design/proposals/2026-07-10-capability-url-share.md`
  and ADR-008 (`docs/DECISIONS.md`) for why no zero-knowledge variant also satisfies "one link an
  agent can fetch."
- `plainkeep share` publishes only; it is confirm-class (`--yes`) because a PUT is a transmission. The
  human sends the resulting link.
- The worker is vendored inside the engine boundary (`bin/share/worker/`) so fixes distribute via
  `script/update`.
- Cloudflare bot-management **403s the default `Python-urllib/x.y` User-Agent** (a `PUT` that works
  from `curl` fails from stdlib `urllib` for this reason alone). `plainkeep share` sends
  `User-Agent: plainkeep-share/1.0` so the edge lets it through; keep that header if you customize the
  client. When forwarding a link (e.g. over Telegram), send it **whole** — the token is the entire
  secret, and a truncated link does not resolve.
