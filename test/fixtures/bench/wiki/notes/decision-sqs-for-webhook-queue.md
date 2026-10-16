---
type: decision
title: Use SQS for the webhook queue
status: superseded
created: 2026-04-24
updated: 2026-07-15
tags: [decision, queue]
aliases: []
---
# Use SQS for the webhook queue

## Context
The first prototype of [[webhook-relay]] needed a durable queue between receiving and delivering events, and I wanted nothing to operate.

## Decision
Use SQS with a dead-letter queue. Retries use per-message visibility timeouts. Nobody has to patch or monitor a broker, and the free tier covers the prototype.

## Consequences
Delayed retries beyond fifteen minutes needed a workaround, and tail latency was noisy. Good enough for a prototype.

Superseded by [[decision-redis-streams-for-webhook-queue]].
