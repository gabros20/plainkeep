---
type: runbook
title: PostgreSQL failover runbook
status: active
created: 2026-05-14
updated: 2026-09-21
tags: [runbook, postgres, infra]
aliases: [db failover]
---
# PostgreSQL failover runbook

How to move the production database from a failed primary to the streaming replica without losing acknowledged writes. Written for a one-person on-call rotation, so every step is something you can paste, and every step says what "done" looks like. Related: [[webhook-relay]], [[engineering-practice]].

## Scope and roles
There is one primary and one asynchronous streaming replica, on separate hosts in the same region. The replica lags the primary by well under a second in normal operation, so a hard failover can lose the last few hundred milliseconds of writes. That is accepted for the relay's event tables, because every inbound event is also appended to the object-store journal before it is acknowledged to the sender. The billing tables are the exception and are covered by the manual reconciliation step at the end of this runbook.

## Detecting a primary failure
Three independent signals must agree before anyone promotes anything: the uptime probe fails for two consecutive minutes, `pg_isready` run from the replica host fails, and the cloud console shows the instance as unreachable rather than merely busy. A busy primary in the middle of a long checkpoint looks identical to a dead one from the application side, and promoting the replica while the old primary still accepts writes creates a split brain that costs far more than five extra minutes of waiting.

## Promoting the replica
Pause the application workers with the deploy tool's maintenance switch first, so nothing writes during the change. Then run `pg_ctl promote` on the replica and confirm with `SELECT pg_is_in_recovery();` returning false. Point the DNS alias `db-primary` at the promoted host; its TTL is 30 seconds. Do not re-attach the old primary as a replica until it has been rebuilt from a fresh base backup: its timeline has diverged and a plain restart will refuse to follow the new one.

## Application cutover
Resume the workers one at a time, starting with the webhook receiver, then the delivery workers, then the scheduled jobs. Watch the queue depth dashboard; a short spike is normal while the backlog drains. If the error rate stays above 2 percent for ten minutes, stop and read the next section before assuming there is a data problem, because stale connections are the usual cause.

## Reconciliation of billing data
The billing tables are the only data that may be behind after a hard failover. After the cutover, export the last hour of invoices and payments from the payment provider's dashboard and compare the totals with the promoted database. Any payment present at the provider but missing locally is re-imported with the reconciliation script; any local row without a provider record is flagged for manual review rather than deleted. Record the outcome in the incident note, even when the answer is that nothing was missing.
## Connection pool reset after promotion
PgBouncer sits between the workers and the database. After a promotion it keeps its idle server-side connections to the old primary until `server_idle_timeout` expires, and the shipped default of 600 seconds meant that workers kept failing for ten minutes after a clean promotion during the March drill. We now set the timeout to 45 seconds and also issue `RECONNECT` on the pgbouncer admin console right after the DNS change, which drops the stale server connections at once instead of waiting for them to age out.
