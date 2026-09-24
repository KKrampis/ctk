#!/usr/bin/env bash
# reset_db.sh — wipe and reinitialize the CTK chat database, then re-import
# filtered conversations.
#
# Usage:
#   bash scripts/reset_db.sh            # uses default paths below
#   bash scripts/reset_db.sh --dry-run  # show what would happen, change nothing

set -euo pipefail

DB_DIR="$HOME/Documents/GitHub/ctk-chats-db"
FILTERED="$HOME/Documents/GitHub/claude_export/conversations_filtered.json"
CTK_BIN="$(dirname "$0")/../.venv/bin/ctk"
SCRIPT_DIR="$(dirname "$0")"

DRY_RUN=false
[[ "${1:-}" == "--dry-run" ]] && DRY_RUN=true

echo "=== CTK database reset ==="
echo "  DB:       $DB_DIR"
echo "  Import:   $FILTERED"
echo "  Dry-run:  $DRY_RUN"
echo ""

# ── 1. Re-filter the export ────────────────────────────────────────────────
echo "Step 1: filtering conversations export …"
if $DRY_RUN; then
    python3 "$SCRIPT_DIR/filter_conversations.py" 2>&1 | head -5
    echo "  (dry-run: file not overwritten)"
else
    python3 "$SCRIPT_DIR/filter_conversations.py"
fi
echo ""

# ── 2. Wipe the database ──────────────────────────────────────────────────
DB_FILE="$DB_DIR/conversations.db"
echo "Step 2: wiping database …"
if $DRY_RUN; then
    echo "  (dry-run) would delete $DB_FILE and run: ctk db init $DB_DIR"
else
    if [[ -f "$DB_FILE" ]]; then
        rm "$DB_FILE"
        echo "  Deleted $DB_FILE"
    fi
    "$CTK_BIN" db init "$DB_DIR"
    echo "  Reinitialized schema at $DB_DIR"
fi
echo ""

# ── 3. Import filtered conversations ─────────────────────────────────────
echo "Step 3: importing filtered conversations …"
if $DRY_RUN; then
    echo "  (dry-run) would run: ctk import $FILTERED --db $DB_DIR"
else
    "$CTK_BIN" import "$FILTERED" --db "$DB_DIR"
fi
echo ""

echo "Done."
