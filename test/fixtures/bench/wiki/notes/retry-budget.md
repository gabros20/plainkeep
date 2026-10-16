---
type: note
title: Retry Budget
status: active
created: 2026-07-20
updated: 2026-07-20
tags: [reliability, webhook]
aliases: [retry cap]
---
# Retry Budget

A cap on how much extra load retries are allowed to create. Instead of every delivery retrying up to a fixed count, each customer endpoint gets a ratio of retries to first attempts, measured over a sliding window. When the ratio is spent, further failures wait for the window to move on. This keeps one broken endpoint from turning into a retry storm that slows down everybody else. Used by [[webhook-relay]]; the surrounding policy is in [[webhook-retry-policy]].
