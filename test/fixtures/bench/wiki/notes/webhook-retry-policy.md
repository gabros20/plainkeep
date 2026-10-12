---
type: note
title: Webhook retry policy
status: active
created: 2026-07-02
updated: 2026-07-02
tags: [webhook, reliability]
aliases: []
---
# Webhook retry policy

Retry policy: retry five times, backoff doubling, jitter on. The retry count is the retry budget for a delivery: a budget of five retries. Retry again later, retry with jitter, never retry a 4xx except 408 and 429. Budget review monthly; budget exhausted means the delivery is parked. See [[retry-budget]].
