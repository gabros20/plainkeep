---
type: decision
title: Generate invoices with a Pandoc script
status: superseded
created: 2026-02-10
updated: 2026-09-05
tags: [decision, invoicing]
aliases: []
---
# Generate invoices with a Pandoc script

## Context
In the first months there were only two clients, and I wanted invoices as plain text in git rather than a subscription.

## Decision
Write each invoice as a markdown file with a small YAML header and render it to PDF with Pandoc and a LaTeX template, from a Makefile target.

## Consequences
Fully reproducible, but numbering and payment tracking were manual and error-prone, and the template broke on every TeX update.

Superseded by [[decision-invoice-ninja-for-invoicing]].
