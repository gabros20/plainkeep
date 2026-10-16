---
type: note
title: Backoff and retry notes
status: active
created: 2026-06-11
updated: 2026-06-11
tags: [reliability]
aliases: []
---
# Backoff and retry notes

Retry loops need a budget. A retry budget stops retries from multiplying load: budget the retries per minute, budget the retries per customer, and budget the total time. Retry with exponential backoff, retry with jitter, and retry only what is safe to repeat. Whatever the budget says, log every retry.
