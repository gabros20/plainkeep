---
type: decision
title: Run staging on Fly.io
status: active
created: 2026-08-02
updated: 2026-08-02
tags: [decision, infra, staging]
aliases: []
---
# Run staging on Fly.io

## Context
The single VPS for staging drifted from production and was deployed by hand.

## Decision
Run staging on Fly.io with the same container images as production, deployed by the same pipeline, and scaled to zero overnight to save money.

## Consequences
Staging now fails the same way production does, which is the point. Cold starts make the first demo request slow, so demos get a warm-up request first.
