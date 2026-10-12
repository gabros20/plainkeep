---
type: decision
title: Host staging on a single Hetzner VPS
status: superseded
created: 2026-03-30
updated: 2026-08-02
tags: [decision, infra, staging]
aliases: []
---
# Host staging on a single Hetzner VPS

## Context
Staging needed to be cheap and always available for customer demos, and I did not want to learn a platform for it yet.

## Decision
One small Hetzner VPS running the whole stack in containers, deployed by a shell script over SSH. It cost a few euros a month and was easy to reason about.

## Consequences
Deploys were manual and the box drifted from production. The first time the disk filled up was during a customer demo.

Superseded by [[decision-fly-io-for-staging]].
