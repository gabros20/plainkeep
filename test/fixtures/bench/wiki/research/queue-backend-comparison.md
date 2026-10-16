---
type: research
title: Queue backend comparison for the webhook relay
status: active
created: 2026-06-02
updated: 2026-07-11
tags: [research, queue, redis]
aliases: [queue research]
---
# Queue backend comparison for the webhook relay

Notes from evaluating where the relay's delivery queue should live. The decision itself is in [[decision-redis-streams-for-webhook-queue]]; this note keeps the evidence, including the parts that did not make it into the decision.

## Question
The relay accepts events from senders, acknowledges them quickly, and delivers them to customer endpoints with retries. The queue between those two halves must survive a process restart, support delayed redelivery with backoff, and be cheap to operate for one person. Peak load is modest: a few hundred events per second in bursts, a few million per day.

## Candidates
Four candidates were tried: a managed message queue (SQS), Redis Streams with consumer groups, a Postgres table polled with `SKIP LOCKED`, and NATS JetStream. RabbitMQ was dropped early because running a cluster by hand is exactly the operational burden this project is trying to avoid.

## Method
Each candidate got the same synthetic workload: 200 events per second for ten minutes, bursts of 2,000 events, consumers killed at random, and 5 percent of deliveries failing so the retry path stays busy. I measured end-to-end latency at the 50th and 99th percentile, duplicate deliveries after a consumer crash, and the lines of glue code needed to get delayed retries working.

## Results
All four handled the throughput comfortably. SQS had the highest tail latency but the least to operate. The Postgres table was the simplest and fell over first under burst load. JetStream was pleasant to use but one more thing to learn. Redis Streams had the lowest latency and the smallest glue code, with delayed retries implemented as a sorted set of due times.

## Cost and effort
Monthly cost at the expected volume was close to zero for SQS and for Postgres, a few euros for a small Redis instance with a replica, and a little more for a managed NATS cluster. Effort matters more than cost here: the Postgres variant needed a cleanup job for finished rows, the SQS variant needed a separate delay mechanism for retries beyond fifteen minutes, and the JetStream variant needed me to learn its retention and acknowledgement policies from scratch. Redis needed the least new knowledge, since I already run it for caching on other projects, and its failure modes are well documented by other people.
## Risks considered
Redis keeps its working set in memory, so the stream must be trimmed or it will eventually fill the instance. Persistence is configured with an append-only file synced every second, which accepts a loss of up to one second of queued references on a crash; the object-store journal makes that recoverable. A managed Redis would remove most of this, but its price at this volume is out of proportion to the rest of the stack.
## Operational findings
The surprise came from the failure drills. When the Redis replica was promoted while consumers were mid-flight, unacknowledged entries stayed in the pending-entries list of the old consumer names, and nothing redelivered them. A periodic `XAUTOCLAIM` with a minimum idle time of 60000 milliseconds recovered every stuck entry in the drills, and it is now part of the consumer loop. Memory use stayed flat as long as the stream is trimmed by minimum id after each acknowledged batch.
