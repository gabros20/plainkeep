# Agent access — read shared wiki notes

How a shared link serves raw wiki markdown to any agent with a fetch tool. For anyone pointing an
agent (or a plain `curl`) at a `plainkeep share` link.

There is one link. Append `.md` and the agent reads the exact wiki source.

## The link

`plainkeep share <slug> --approved` prints:

```text
shared note <slug>
  https://ops-share.example.workers.dev/abc123def456ghi789jkl012
  agents / LLMs: https://ops-share.example.workers.dev/abc123def456ghi789jkl012.md   (raw markdown — paste into any chat/coding agent)
  revoke: plainkeep share revoke abc123def456ghi789jkl012 --approved
```

- **A browser** opening the bare URL gets the rendered HTML page.
- **An agent** (Claude Code, Codex, `curl`, `web_fetch`, any fetch tool) appends `.md`, or pastes
  the printed `agents / LLMs:` line as-is.

The `.md` URL returns `text/markdown; charset=utf-8`: the wiki source, YAML frontmatter and body,
byte-identical to what was published.

No headers. No keys. No query strings. No URL math beyond "add `.md`".

`agent_url` in `--json` output and `plainkeep share list --json` is always `url + ".md"`.

## Content negotiation (the lazy-paste bonus)

The bare URL (no `.md`) also serves markdown to anything that isn't a browser.

The worker checks the `Accept` header. It returns HTML only when `Accept` contains `text/html`.
`curl`'s default `*/*` and most agent fetch tools get markdown straight off the bare link.

`.md` stays the documented, deterministic contract. Negotiation just means pasting the plain link
into an agent tends to work too.

```sh
curl https://<worker>/<token>.md      # markdown, guaranteed
curl https://<worker>/<token>         # markdown too, via negotiation (non-browser Accept)
```

## With `plainkeep` on the machine

```bash
plainkeep share pull 'https://<worker>/<token>'   # fetch + unpack locally, same markdown, no browser
```

`plainkeep share pull <url> [--out file.md]` works on the bare link or the `.md` link interchangeably. It
normalizes either into the same fetch.

## Legacy links

Anything published under the previous encrypted model (`#fragment`, `?k=`, `X-Ops-Share-Key`) is
dead.

Every route for a legacy blob returns **HTTP 410** with a `re-publish with current plainkeep share` hint.
There is no key to recover it with. Re-run `plainkeep share <slug> --approved` to get a current link.

## Tests

- `bin/lib/sharelib.py`: `parse_share_link`, `markdown_from_blob`, `pull_markdown_from_url`
- `test/run_backup_share.py`
