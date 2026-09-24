# CTK Import Scripts

Three scripts for importing Claude conversation exports into the CTK database.

---

## Quick start

```bash
# 1. Get a fresh export from Claude
#    Settings → Privacy → Export data → wait for email → click link → manifest saved to ~/Downloads

# 2. Download the two required ZIPs from the manifest URLs in Chrome:
#      conversations-000.zip  (full chat history)
#      projects-000.zip       (project names used for auto-tagging)
#    Both land in ~/Downloads automatically.

# 3. Preview what would be imported (nothing written):
python3 scripts/fetch_and_import.py --dry-run

# 4. Import for real:
python3 scripts/fetch_and_import.py
```

---

## Scripts

### `fetch_and_import.py` — main entry point

Downloads, filters, deduplicates, tags, and imports a Claude export into CTK.

**What it does, step by step:**

1. **Finds the latest manifest** in `~/Downloads` (`manifest-*.json`) — or use a specific one as a positional argument.
2. **Downloads the conversations and projects ZIPs** using the URLs in the manifest.  
   ⚠️ The URLs require your browser session — open them in Chrome to download, then re-run. Already-downloaded ZIPs are reused automatically on subsequent runs.
3. **Loads project names** from `projects-000.zip` and builds a keyword index for auto-tagging.
4. **Filters conversations** — removes:
   - Untitled conversations
   - Conversations about hardware/devices (monitors, e-readers, laptops, phones, etc.)
   - Conversations with fewer than 6 messages
5. **Deduplicates** against the CTK database — skips any conversation already imported (matched by Claude's UUID).
6. **Auto-tags each conversation** by matching its title against your Claude project names. Keyword aliases handle common cases (e.g. a conversation titled "NeoVim LSP setup" gets tagged `NeoVim`; "MCP server for CTK" gets tagged `MCP Dev`).
7. **Imports** into the CTK database via the CTK Python API, with project tags applied per conversation.

**Usage:**

```bash
# Auto-find latest manifest, download ZIPs, filter, tag, and import:
python3 scripts/fetch_and_import.py

# Use a specific manifest:
python3 scripts/fetch_and_import.py ~/Downloads/manifest-xyz.json

# Skip download — use an already-extracted conversations.json:
python3 scripts/fetch_and_import.py --json ~/Documents/GitHub/claude_export/conversations.json

# Skip download — use an already-downloaded conversations ZIP:
python3 scripts/fetch_and_import.py --zip ~/Downloads/conversations-000.zip

# Preview only — show what would be imported and which project tags assigned:
python3 scripts/fetch_and_import.py --dry-run
```

**About the manifest ZIPs:**

The Claude export manifest contains 4 ZIPs. This script uses only 2:

| ZIP | Contents | Used? |
|-----|----------|-------|
| `conversations-000.zip` | Full chat history with all messages | ✅ Yes |
| `projects-000.zip` | Project names, descriptions, system prompts | ✅ Yes — for auto-tagging |
| `light_metadata-000.zip` | Titles + timestamps only, no message bodies | ❌ No |
| `frames-000.zip` | Artifacts (canvas docs, code files created in the UI) | ❌ No |

**About auto-tagging:**

Project names are matched against conversation titles using keyword overlap scoring. The script includes manual aliases for common cases where a title's words don't literally match a project name — for example:

- `NeoVim` ← nvim, vim, zathura, telescope, obsidian
- `MCP Dev` ← mcp, claude, opencode, tui, ctk
- `AI Consciousness, Theology` ← consciousness, awareness, sentience, phenomenal, mind
- `AI Paper Discussions` ← arxiv, paper, llm, alignment, beliefs
- `Python` ← python, script, rust, tui
- `Hack and Office Stuff` ← debian, install, osx, voice, mpv, dvd

Add or edit aliases in the `PROJECT_ALIASES` dict at the top of `fetch_and_import.py`.

**Paths (hardcoded at the top of the script):**

| Variable | Default |
|----------|---------|
| `DOWNLOADS` | `~/Downloads` |
| `DB_DIR` | `~/Documents/GitHub/ctk-chats-db` |
| `MIN_MESSAGES` | `6` |

---

### `filter_conversations.py` — standalone filter

Reads a `conversations.json` export and writes a filtered version, applying the same rules as `fetch_and_import.py` (untitled, hardware topics, < 6 messages). Used internally by `reset_db.sh`.

```bash
python3 scripts/filter_conversations.py
# Reads:  ~/Documents/GitHub/claude_export/conversations.json
# Writes: ~/Documents/GitHub/claude_export/conversations_filtered.json
```

---

### `reset_db.sh` — wipe and reimport

Completely wipes the CTK database and reimports from a filtered `conversations.json`. Use this when you want to start fresh (e.g. after bulk-adjusting filters or tags).

```bash
bash scripts/reset_db.sh            # wipe, reinitialize, reimport
bash scripts/reset_db.sh --dry-run  # preview only
```

**Steps it runs:**

1. Re-runs `filter_conversations.py` to regenerate the filtered JSON.
2. Deletes `conversations.db` and reinitializes the schema with `ctk db init`.
3. Imports `conversations_filtered.json` with `ctk import`.

> **Note:** `reset_db.sh` does not apply project auto-tagging — it uses the basic `ctk import` command. For a full reset with project tags, use `fetch_and_import.py --json` after wiping the DB manually.

---

## Typical workflows

**Regular sync after a new Claude export:**
```bash
# Download conversations-000.zip and projects-000.zip from Chrome (manifest URLs)
python3 scripts/fetch_and_import.py
# Only new conversations (not already in the DB) are imported
```

**Full reset with project tags:**
```bash
# Wipe the database
rm ~/Documents/GitHub/ctk-chats-db/conversations.db
.venv/bin/ctk db init ~/Documents/GitHub/ctk-chats-db

# Reimport everything with tags
python3 scripts/fetch_and_import.py --json ~/Documents/GitHub/claude_export/conversations.json
```

**Just test the filters / tag assignments without touching the DB:**
```bash
python3 scripts/fetch_and_import.py --json ~/Documents/GitHub/claude_export/conversations.json --dry-run
```
