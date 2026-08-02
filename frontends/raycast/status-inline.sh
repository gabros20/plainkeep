#!/bin/bash
# Raycast Script Command — status-inline (proposal Part 3.3). A menu-bar/inline one-liner.
#
# @raycast.schemaVersion 1
# @raycast.title Plainkeep Status
# @raycast.mode inline
# @raycast.refreshTime 30s
# @raycast.packageName plainkeep
# @raycast.icon 🧭
# @raycast.description Plainkeep orientation in one line (tasks / inbox / index / git).
# @raycast.author plainkeep
#
# `plainkeep orient --line` is a ≤60-char cached string built for exactly this kind of prompt hook.
set -euo pipefail
PLAINKEEP="$(command -v plainkeep || true)"
# The engine ships its own launcher, and since Phase 2 Task 2 the vault does not carry one
# (`$PLAINKEEP_HOME/plainkeep` is a path a data vault does not have). Fall back to the
# activated engine tree, which is where an install puts it.
[ -n "$PLAINKEEP" ] || PLAINKEEP="${PLAINKEEP_ENGINE:-${XDG_DATA_HOME:-$HOME/.local/share}/plainkeep/engine/current}/plainkeep"
exec "$PLAINKEEP" orient --line
