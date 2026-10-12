---
type: meeting
title: Infra meeting log, 2026 Q3
status: active
created: 2026-07-06
updated: 2026-09-28
tags: [meeting, infra, log]
aliases: [infra log]
---
# Infra meeting log, 2026 Q3

A running log of the monthly infra review I hold with myself and, when they are around, the two contractors who help with deploys. One section per meeting, newest last.

## 2026-07-06
Agenda: alert noise, staging cost, the state of the database replica. We agreed to cut the alert list to the five that have ever woken anyone. Staging runs around the clock for no reason; schedule it down overnight. The replica lag graph looks healthy, no action.

## 2026-08-03
Agenda: queue migration status, certificate expiry, laptop replacement. The queue migration is two weeks from done. Certificates are all on automatic renewal except the wildcard one for staging, which is tracked as a task. The laptop gets replaced after the next invoice cycle, not before.

## 2026-08-31
Agenda: postmortem follow-ups, dependency updates, on-call handover. The follow-ups from the August outage are all done except the status page automation. Dependency updates go in weekly batches, Fridays, never on a customer's month-end. On-call stays with me; the contractors only get the runbooks.

## 2026-08-17
Agenda: staging bill, access review, documentation gaps. The staging bill is higher than production's, which is absurd; scale it down at night. Access review: two contractor accounts that are no longer needed are removed, and the shared password vault gets a fresh audit log export. Documentation gaps: the deploy procedure exists only in my head and a shell history file, so the next two Fridays go to writing it down properly.

## 2026-09-01
Agenda: runbook review, customer feedback, the next quarter's hiring question. Every alert now has a runbook entry or is deleted; three alerts were deleted. Customer feedback is positive but asks for a replay button on single deliveries. On hiring: not this quarter, because the work is lumpy and a contractor on call covers the gaps. Revisit after the second customer is live and the workload is known.
## 2026-09-15
Agenda: log retention, the second customer, tooling. Logs stay thirty days in the hot store and are not archived; the journal is the system of record, not the logs. For the second customer, the receivers need per-tenant rate limits before anything else. On tooling, no new tools this quarter unless something breaks: the list of things I operate is already longer than I would like, and every addition has to replace something.
## 2026-09-28
Agenda: backups, cost, the Q4 plan. Decision on backup retention: keep nightly database dumps for 35 days, monthly dumps for 13 months, encrypted with age and stored in a bucket under a separate account. A restore drill is scheduled for the first Friday of November, and a dump that cannot be restored counts as a missing dump. Cost is flat. Q4 plan: second customer onboarding in October, a quiet November apart from the restore drill, and a December freeze from the twentieth. The open question for next time is whether the status page automation is worth finishing.
