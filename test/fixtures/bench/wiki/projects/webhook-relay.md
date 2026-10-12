---
type: project
title: Webhook relay
status: active
created: 2026-04-20
updated: 2026-09-18
tags: [project, webhook, infra]
aliases: [relay]
---
# Webhook relay

A small service that receives webhooks from third-party senders on behalf of a customer, acknowledges them immediately, and delivers them to the customer's own endpoints with retries. Built first for [[kovacs-es-tarsa-kft]], now shaped so it can serve a second customer without code changes.

## Architecture
Two stateless receiver hosts sit behind a load balancer. They verify the sender's signature, write the raw event to an object-store journal, and push a reference onto the delivery queue ([[decision-redis-streams-for-webhook-queue]]). A pool of delivery workers pops entries, posts them to the customer endpoint, and reschedules failures with exponential backoff and a [[retry-budget]]. The database holds only metadata: endpoints, secrets, delivery attempts.

## Operations
Deploys are rolling, one receiver at a time. The database failover procedure is in [[postgres-failover-runbook]] and the last incident is written up in [[webhook-outage-postmortem-2026-08]]. Staging lives where [[decision-fly-io-for-staging]] says it does. Alerts go to a single channel; anything that wakes me up needs a runbook entry or it gets deleted.

## Customer-facing behavior
Customers see a dashboard with recent deliveries and a manual replay button. Each delivery carries an idempotency key so a customer endpoint can safely ignore duplicates. Delivery attempts are kept for thirty days, the raw journal for ninety. Endpoints that fail continuously for seventy-two hours are paused and the customer is emailed.

## Roadmap
Next up is a second customer. That means per-customer secrets rotation, a limit on dashboard history so one noisy account does not slow the page, and a small admin screen for pausing an endpoint by hand. After that comes a bulk replay feature, which needs thought about ordering and about not overwhelming an endpoint that has just recovered. Nothing here is committed to a date; the order follows whatever the first customer actually asks for, and anything nobody asks for within a quarter gets removed from this list.
## Security
Each customer endpoint has its own signing secret, stored encrypted and never shown again after creation. Outbound deliveries are signed with a timestamp so the customer can reject replays. The receivers accept only the sender's published IP ranges where the sender publishes them, and otherwise rely on signature verification alone. Secrets rotate on request, with a short overlap during which both the old and the new secret are valid, so that a customer can deploy the change without dropping events.
## Known limitations
Request bodies larger than 256 KB are not queued directly. The receiver stores the full body in the object-store journal and queues a pointer, so the delivery worker has to fetch it back before posting; bodies above 5 MB are rejected outright with a 413. Ordering is per endpoint, not global, and a replay always goes to the back of that endpoint's queue.

## Timeline
- 2026-04-20 — project started with the first receiver prototype
- 2026-06-30 — first customer integration live
- 2026-08-19 — outage and postmortem
- 2026-09-18 — second milestone rescheduled ([[egyeztetes-2026-09-kovacs-atutemezes]])
