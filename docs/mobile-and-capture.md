# Mobile & capture — the documented (not-an-app) surface

This guide shows how to capture and read your vault away from your desk: a global hotkey on macOS, a Shortcut on iOS, and reading/editing on a phone. It is for anyone running plainkeep who wants mobile access without a native app.

## The one rule

plainkeep has no server, no daemon, no account. The vault is plaintext plus git. Mobile-lite is documentation, not code (proposal Part 3.4).

The only sync transport is `git push` / `git pull` over your own private remote. Everything here rides on that one fact.

**URIs and share-sheets OPEN and CAPTURE. On the desktop, all writes still go through a verb.**

- Capture (hotkey, Shortcut, share sheet) drops a file into `inbox/` and nothing else.
- You run `plainkeep triage` later on a real machine.
- Capture never edits the wiki or tasks. Reading and editing notes in Obsidian mobile or GitJournal
  (section 3) is a separate, deliberate act: those edits are plain markdown commits made by you, and the
  desktop picks them up with `git pull` and `plainkeep index --changed`.
- Nothing here bypasses the `plainkeep` dispatcher or the guardrail on the machine that runs plainkeep.

---

## 1. Desktop capture — a global hotkey on macOS

The fastest capture path on macOS is an [Apple Shortcut](https://support.apple.com/guide/shortcuts-mac/welcome/mac) bound to a global hotkey or the share sheet. It shells straight to `plainkeep capture`.

**Build it:**

1. Shortcuts.app → **＋** → name it *Plainkeep Capture*.
2. Add **Receive Text** as input. Tick *Show in Share Sheet* so it accepts selected text and URLs from any app.
3. Add **Run Shell Script** with these settings:
   - Shell: `/bin/zsh` (or bash)
   - Pass input: **as arguments**
   - Script:
     ```bash
     export PATH="$HOME/.local/bin:/opt/homebrew/bin:$PATH"
     PLAINKEEP="$(command -v plainkeep || echo "$HOME/plainkeep/plainkeep")"
     "$PLAINKEEP" capture "$*"
     ```
4. Assign a keyboard shortcut in *Shortcuts → Settings → Keyboard*, or trigger it from Raycast or the share sheet.

Because it shells to `plainkeep capture`, the guardrail and `.logs/` apply. The Shortcut is just another unprivileged caller.

Pair it with the Raycast **Plainkeep Capture** command ([`frontends/raycast`](../frontends/raycast/README.md)) for a Spotlight-style capture box.

---

## 2. iOS capture — a Shortcut that writes into `inbox/`

iPhone and iPad have no shell. So the Shortcut writes a file into the vault, and git carries it home.

Two workable shapes follow. Working Copy is the lower-friction path for most people.

### 2a. Working Copy (recommended — real git on iOS)

[Working Copy](https://workingcopy.app) clones your vault's private remote on-device and exposes *Write File* and *Append* Shortcut actions.

1. Clone the plainkeep remote into Working Copy once.
2. Build a Shortcut *Plainkeep Capture*:
   - **Receive Text/URL from Share Sheet**
   - **Text** (timestamp + body)
   - Working Copy **Write File** into `inbox/cap-<timestamp>.md` (append or new file)
3. End the Shortcut with Working Copy **Commit** + **Push**.
4. Next time you're at a machine: `git pull`, then `plainkeep triage`.

Keep the file shape trivial. A plain markdown file with your text is enough — `plainkeep triage` classifies and routes it.

### 2b. a-Shell / iSH (a POSIX shell on iOS)

If you run [a-Shell](https://holzschu.github.io/a-Shell_iOS/), you can `git clone` the remote and run a cut-down capture: write to `inbox/`, `git commit`, `git push`.

This is heavier to set up than Working Copy.

> **Never** point iCloud, Dropbox, or Syncthing at the `.git` directory to "sync" the vault. See anti-roadmap #9. `plainkeep doctor` fails if the vault resolves under a sync-wall. git push/pull is the transport; a file-sync tool corrupts the repo.

---

## 3. Read & edit on mobile — Obsidian mobile or GitJournal

The vault is Obsidian-compatible (see [`docs/obsidian-compat.md`](obsidian-compat.md)). On mobile you rent an existing app rather than build one.

| App | What you get | Sync |
| --- | --- | --- |
| **Obsidian mobile** | Native wikilinks, backlinks, graph, and Properties — the same notes you edit on desktop | [Obsidian Git](https://github.com/Vinzent03/obsidian-git) plugin, or Working Copy's repository as the vault folder on iOS via the Files provider |
| **[GitJournal](https://gitjournal.io)** | A git-native notes app that speaks markdown frontmatter and `[[wikilinks]]` directly. Lighter than Obsidian for pure capture/edit | Clones your private remote with its own git |

Either way, sync is git over your private remote (via Working Copy or the app's own git).

Edits you make on mobile land as commits. You pull them on desktop.

Re-index after pulling: `plainkeep index --changed` picks up externally-edited notes. It never rewrites your files.

---

## 4. The one sync transport

```
 phone (Working Copy / Obsidian Git / GitJournal)
        │  git push / pull
        ▼
 private remote (your own git host — GitHub private repo, a self-hosted bare repo, …)
        ▲
        │  git push / pull
 desktop (the vault, the plainkeep verbs, the index)
```

No provider ever sees plaintext beyond your chosen git host. No piece here transmits on its own — you push, you pull.

Capture is additive (`inbox/`). Triage is deliberate. Edits from an editor app are yours and arrive as commits. The guardrail sits on every write plainkeep itself makes on the desktop side.
