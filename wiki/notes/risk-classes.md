---
type: note
title: Guardrail risk classes
status: active
created: 2026-06-29
updated: 2026-06-29
tags: [meta, safety, starter]
aliases: [risk-classes, guardrail]
---
# Guardrail risk classes

Before any verb runs, the guardrail classifies it. The same wall applies to you and to any agent —
which is what makes automation safe rather than scary.

| Class | Meaning |
|---|---|
| `read` | pure read — runs freely, even unattended |
| `safe_write` | writes inside the roots — every change is a revertible git diff |
| `draft_only` | produces a draft (e.g. `plainkeep invoice`) — a human sends; the system never transmits |
| `confirm` | needs an explicit `--approved`; the default for any new/undeclared verb |
| `deny` | always refused: force-push, `rm -rf`, reading secrets, writing iCloud/family paths |

Because most of the daily verbs in [[the-plainkeep-loop]] are `read` or `safe_write`, and it's all git
underneath, even a mistake is one `git revert` away. See [[welcome]] to start using it.
