#!/bin/bash
# Raycast Script Command — task-list (proposal Part 3.3).
#
# @raycast.schemaVersion 1
# @raycast.title Plainkeep Task List
# @raycast.mode fullOutput
# @raycast.packageName plainkeep
# @raycast.icon 📋
# @raycast.description Active and waiting plainkeep tasks.
# @raycast.author plainkeep
set -euo pipefail
PLAINKEEP="$(command -v plainkeep || true)"
# The engine ships its own launcher, and since Phase 2 Task 2 the vault does not carry one
# (`$PLAINKEEP_HOME/plainkeep` is a path a data vault does not have). Fall back to the
# activated engine tree, which is where an install puts it.
[ -n "$PLAINKEEP" ] || PLAINKEEP="${PLAINKEEP_ENGINE:-${XDG_DATA_HOME:-$HOME/.local/share}/plainkeep/engine/current}/plainkeep"
exec "$PLAINKEEP" task list
