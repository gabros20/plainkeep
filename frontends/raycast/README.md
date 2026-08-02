# Raycast Script Commands — the first built frontend (Part 3.3)

Zero-build [Raycast Script Commands](https://github.com/raycast/script-commands): plain bash, no
extension to compile. Every command shells to `plainkeep` on `PATH`, falling back to the activated
engine's launcher (`${XDG_DATA_HOME:-~/.local/share}/plainkeep/engine/current/plainkeep`), so the
guardrail and `.logs/` apply exactly as on the terminal — the frontend has **zero privileged
access** and re-enters through the dispatcher, never importing `bin/lib`.

The fallback used to be `$PLAINKEEP_HOME/plainkeep`. Since Phase 2 Task 2 a vault has no launcher of
its own — it is data — so that path names nothing, and the engine's own launcher is what a frontend
must reach for.

## Install

1. Run `script/setup`, which installs the engine and puts `plainkeep` on your `PATH`.
2. Raycast → *Extensions* → *Script Commands* → *Add Directories* → point it at this folder inside
   the ACTIVE engine (`$(plainkeep vault status --json | ...)` — or simply
   `~/.local/share/plainkeep/engine/current/frontends/raycast`).
3. The commands appear in Raycast root search: **Plainkeep Capture**, **Plainkeep Search**, **Plainkeep Task Add**,
   **Plainkeep Task List**, **Plainkeep Status**.

Because this folder is engine-owned (`enginetree.OWNED_TREES`, `script/engine.txt`), improvements
arrive with the next installed engine version — and the `current` symlink means the Raycast
directory you pointed at follows the upgrade with no reconfiguration. Your own `*.sh` do NOT belong
here any more: an installed engine is read-only and is replaced wholesale on upgrade. Keep personal
scripts in a directory of your own and add it to Raycast as a second source.

## Commands

| Script | Runs | Mode |
|---|---|---|
| `quick-capture.sh` | `plainkeep capture <text>` | compact |
| `search.sh` | `plainkeep search <q> --json` → top hit paths | fullOutput |
| `task-add.sh` | `plainkeep task add <title>` | compact |
| `task-list.sh` | `plainkeep task list` | fullOutput |
| `status-inline.sh` | `plainkeep orient --line` | inline (30s refresh) |

Graduate to a full React extension only after this tier proves the `--json` surface — see the
roadmap. For global-hotkey and mobile capture, see [`docs/mobile-and-capture.md`](../../docs/mobile-and-capture.md).
