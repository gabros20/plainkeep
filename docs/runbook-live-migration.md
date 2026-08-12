# Runbook — live migration of the `gabros20/ops` vault (Mac Mini)

**Audience:** a coding agent (Claude Code or similar) operating on Tamás's Mac Mini, where the real
vault lives. You have no other context; this document is self-contained. The human has approved the
live migration following this runbook — but every refusal the product prints is CORRECT behavior:
read it, follow its hint, never work around it. **There is no `--force` anywhere, by design.**

**What this does:** moves the vault off its vault-local engine copy (the committed `bin/`, `script/`,
`skills/operate-plainkeep`, … — ~109 tracked paths) onto the installed, versioned, read-only engine
at `~/.local/share/plainkeep/engine/current/`, with a receipt and a tested rollback. This exact
sequence was rehearsed end-to-end on a clone of this vault (2× — see
`.orchestrate/canary-ops-r2-report.md` in the plainkeep repo, verdict CANARY GREEN).

**Engine version:** plainkeep repo @ `b236558` or later (`github.com/gabros20/plainkeep`, main).

---

## Phase 0 — safety net (do not skip)

1. `plainkeep backup` (or `git -C <vault> status` + `git log origin/main..main`) — commit and **push
   everything**: tracked changes, unpushed commits. The migration refuses staged changes and dirty
   engine paths; a pushed vault is also your disaster recovery.
2. Snapshot machine-local untracked state (cheap, optional but recommended):
   `cp -a <vault>/.logs /tmp/vault-logs-backup 2>/dev/null; true` (audit log). `.venv` and `.index`
   are regenerable (`plainkeep setup`, `plainkeep job run index`) — no need to back them up.
3. Note current launcher: `readlink ~/.local/bin/plainkeep` (expect it points INTO the vault —
   the pre-migration shape).

## Phase 1 — pull the new engine into the vault checkout

The vault's own engine copy predates Phase 2 (it has no `bin/lib/migrate.py`). `script/update` pulls
engine files from upstream into the checkout (STAGED, reviewable); `script/setup` installs them.

4. `git -C <vault> remote -v` — confirm `upstream` → `github.com/gabros20/plainkeep`. If absent:
   `git -C <vault> remote add upstream https://github.com/gabros20/plainkeep.git`
5. `<vault>/script/update` — fetches upstream, stages engine files, updates `.plainkeep-engine-ref`.
   Review `git diff --staged` briefly (engine paths only), then commit:
   `git -C <vault> commit -m "engine: sync to plainkeep main (Phase 2)"`.
   If merge-conflict markers are surfaced (a local engine edit), STOP and report to the human.
6. `<vault>/script/setup --yes` — installs the engine as a versioned tree outside the vault,
   repoints `~/.local/bin/plainkeep` at `…/engine/current/plainkeep`. Verify:
   `test -f ~/.local/share/plainkeep/engine/current/bin/lib/migrate.py && echo ok`
7. `plainkeep doctor` — resolve anything it FAILs on before continuing (warns are fine).

## Phase 2 — the adapter decision (already made by the human: REPOINT)

The vault commits two symlinks `.claude/skills -> ../skills` and `.codex/skills -> ../skills` that
let coding agents discover the `operate-plainkeep` skill. Migration removes `skills/`, so preflight
will refuse while they point there. The chosen end state: point them at the installed engine.

8. ```
   ln -sfn ~/.local/share/plainkeep/engine/current/skills <vault>/.claude/skills
   ln -sfn ~/.local/share/plainkeep/engine/current/skills <vault>/.codex/skills
   git -C <vault> add .claude/skills .codex/skills
   git -C <vault> commit -m "adapters: repoint skills at the installed engine (pre-migration)"
   ```
   (`ln -sfn` on a symlink-to-dir can nest on some setups — verify with `ls -la` that the links
   themselves changed, `readlink <vault>/.claude/skills` prints the engine path.)

## Phase 3 — preflight and migrate

9. ```
   python3 ~/.local/share/plainkeep/engine/current/bin/lib/migrate.py --preflight <vault> \
     --engine-source <vault>
   ```
   Expected: `rc 0`, `state pristine`, `ready to migrate`, ~109 paths listed for removal. The line
   `N engine path(s) that ref never synced, NOT compared` is normal reporting, not an error.
   - If it refuses on DIVERGED naming a path: a local engine edit exists; it wrote a recovery patch
     OUTSIDE the vault and named it. STOP, save the patch path, report to the human.
   - If it refuses on the symlinks: Phase 2 didn't take; re-check step 8.
10. ```
    python3 ~/.local/share/plainkeep/engine/current/bin/lib/migrate.py --migrate <vault> --yes \
      --engine-source <vault>
    ```
    Expected `rc 0` in well under a minute. **Expected in the summary:**
    `com.plainkeep.backup_check.plist rc 1` under "exited NON-ZERO — recorded, not gated" — that is
    the backup verb reporting a dirty tree (the migration's own canary notes), NOT a failure.
    Any `rc 5` refusal: read it, it tells the truth about the state and the recovery; report to the
    human rather than improvising.

## Phase 4 — verify (acceptance item 13)

11. Receipt: `… migrate.py --print receipt <vault>` → `status: complete`, `removed` ≈ 109,
    all schedule entries `routed: true`.
12. Protected content: the migration already verified byte-identity across the removal internally.
    Cross-check the working tree: `git -C <vault> status` should show ONLY the canary writes —
    new `inbox/cap-*.md` notes and today's `journal/YYYY/MM/*.md` appended (the journal line reads
    `captured: plainkeep migration canary …`; it is a real, intended canary write that the receipt's
    `canary_writes` does not list — known gap NEW-10). Anything else modified: STOP and report.
13. `plainkeep doctor` → **rc 0, zero FAIL lines** (adapters now report
    `provides operate-plainkeep`). `plainkeep status`, `plainkeep vault status` answer normally.
14. Note: the first `plainkeep --help` after migrating regenerates `plainkeep.json` (larger — new
    engine has more verbs). Expected once; commit it with the sync commit in step 16.
15. `plainkeep job apply` then reload the schedules (the operator's out-of-root step):
    `launchctl unload ~/Library/LaunchAgents/com.plainkeep.*.plist 2>/dev/null; launchctl load ~/Library/LaunchAgents/com.plainkeep.*.plist`
    (adjust to however the plists were loaded on this machine; `plainkeep job list` shows them).
16. Commit the canary notes + regenerated files and **push**:
    `git -C <vault> add -A && git -C <vault> commit -m "vault: migrated off the vault-local engine copy" && git -C <vault> push`
    (The migration commit itself is already on the branch; this pushes everything.)

## Rollback (any time before you delete the receipt yourself — the receipt IS the rollback)

`python3 ~/.local/share/plainkeep/engine/current/bin/lib/migrate.py --rollback <vault> --yes`
restores all removed paths, HEAD, and the launcher, and deletes the receipt. Re-running the migration
afterwards converges. If rollback refuses, its message states why truthfully — report it verbatim.

## If you prefer fresh-clone instead (fallback only)

If Phase 1–3 refuse in a way the human can't resolve: push everything, clone fresh
(`git clone git@github.com:gabros20/ops.git`), `git fetch upstream` in it, register it
(`plainkeep vault register <path> --yes`), and run Phases 1–4 there — this is byte-for-byte the
rehearsed canary shape. Machine-local `.venv`/`.index` regenerate; copy `.logs/` over if you want
continuous audit history.

## Hard rules for the agent

- Never edit `bin/lib/migrate.py` or any engine file to get past a refusal.
- Never use `git push --force`, never rewrite vault history.
- Vault content is the human's private notes: operate on paths, don't read/quote note content.
- Anything unexpected → stop, capture the exact output, report. The system fails safe; you cannot
  make it worse by stopping, only by improvising.
