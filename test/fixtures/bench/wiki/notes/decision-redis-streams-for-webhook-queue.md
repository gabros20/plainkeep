---
type: decision
title: Use Redis Streams for the webhook queue
status: active
created: 2026-07-15
updated: 2026-07-15
tags: [decision, queue, redis]
aliases: []
---
# Use Redis Streams for the webhook queue

## Context
The comparison in [[queue-backend-comparison]] showed that the relay's retry path wants delays longer than the previous queue handles gracefully.

## Decision
Move the delivery queue to Redis Streams with consumer groups, replacing SQS. Delayed retries are a sorted set of due times. Streams gave the lowest latency and the smallest amount of glue code.

## Consequences
One more stateful service to run, with a replica and a periodic autoclaim of stuck entries. In return, retries behave exactly as the retry policy describes.
