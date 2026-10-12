---
type: decision
title: Use Invoice Ninja for invoicing
status: active
created: 2026-09-05
updated: 2026-09-05
tags: [decision, invoicing]
aliases: []
---
# Use Invoice Ninja for invoicing

## Context
With four clients, the markdown-and-script setup was costing more time each month than it saved, and numbering mistakes had started to appear.

## Decision
Self-host Invoice Ninja for invoice numbering, PDF generation and payment tracking. The monthly procedure is in [[havi-szamlazasi-folyamat]].

## Consequences
One more service to back up. Invoice history was imported once; older invoices stay in git.
