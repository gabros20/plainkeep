@AGENTS.md

Before any plainkeep action, read and follow the **operate-plainkeep** skill — installed into
`~/.claude/skills/` by `plainkeep setup agents --yes`, so it loads as an ordinary skill. It is
engine-owned and lives outside this vault; there is no `skills/` directory here to read it from.

Operate only through the `plainkeep <verb>` surface it defines. Never invent a verb, and never
grep or script your way around it — `plainkeep search` is how you find things.
