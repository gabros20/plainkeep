---
type: area
title: Engineering practice
status: active
created: 2026-01-10
updated: 2026-09-28
tags: [area, engineering]
aliases: [practice]
---
# Engineering practice

How I run the technical side of the practice: one production system at a time, boring infrastructure, runbooks for anything that can wake me up. The live project is [[webhook-relay]]; the procedures are [[postgres-failover-runbook]] and [[code-review-checklist]]; the open design questions live in the decision notes, for example [[decision-redis-streams-for-webhook-queue]].

## Principles
- operate fewer things, and know each one
- write the runbook before the alert
- staging should fail like production

## Timeline
- 2026-04-24 — first queue decision ([[decision-sqs-for-webhook-queue]])
- 2026-08-19 — the August outage ([[webhook-outage-postmortem-2026-08]])
- 2026-09-28 — backup retention agreed ([[infra-meeting-log-2026-q3]])
