#!/bin/bash
# Raycast Script Command — search (proposal Part 3.3).
#
# @raycast.schemaVersion 1
# @raycast.title Plainkeep Search
# @raycast.mode fullOutput
# @raycast.packageName plainkeep
# @raycast.icon 🔎
# @raycast.argument1 { "type": "text", "placeholder": "query" }
# @raycast.description Ranked file#heading hits from the plainkeep index.
# @raycast.author plainkeep
#
# Uses `plainkeep search --json` (the stable machine contract) and extracts the top hit paths with
# bash-only tools — no python, no lib import. Open a hit in the terminal with `plainkeep open <slug>`.
set -euo pipefail
PLAINKEEP="$(command -v plainkeep || true)"
# The engine ships its own launcher, and since Phase 2 Task 2 the vault does not carry one
# (`$PLAINKEEP_HOME/plainkeep` is a path a data vault does not have). Fall back to the
# activated engine tree, which is where an install puts it.
[ -n "$PLAINKEEP" ] || PLAINKEEP="${PLAINKEEP_ENGINE:-${XDG_DATA_HOME:-$HOME/.local/share}/plainkeep/engine/current}/plainkeep"
"$PLAINKEEP" search "$1" --json 2>/dev/null \
  | grep -o '"path":"[^"]*"' \
  | sed 's/^"path":"//; s/"$//' \
  || echo "no hits (try: plainkeep index)"
