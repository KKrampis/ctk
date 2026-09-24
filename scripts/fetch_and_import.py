#!/usr/bin/env python3
"""
fetch_and_import.py — Download a Claude export, filter, deduplicate, and import.

Steps:
  1. Read the manifest JSON from ~/Downloads (or pass path as arg)
  2. Download the conversations ZIP from its export_url
  3. Extract conversations.json from the ZIP
  4. Apply content filters (untitled, hardware/device topics, <MIN_MESSAGES)
  5. Skip conversations already in the CTK database (matched by Claude uuid)
  6. Run: ctk import <filtered_file> --db <DB_DIR>

Usage:
    python3 scripts/fetch_and_import.py
    python3 scripts/fetch_and_import.py ~/Downloads/manifest-xxx.json
    python3 scripts/fetch_and_import.py --dry-run

Export URLs are single-use — the script saves the downloaded ZIP to
~/Downloads so you can re-run filtering/dedup without re-downloading.
"""

import argparse
import glob
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path
from urllib import request as urllib_request

# ── Paths ──────────────────────────────────────────────────────────────────
DOWNLOADS    = Path.home() / "Downloads"
DB_DIR       = Path.home() / "Documents/GitHub/ctk-chats-db"
OUTPUT_JSON  = Path.home() / "Documents/GitHub/claude_export/conversations_filtered.json"
CTK_BIN      = Path(__file__).parent.parent / ".venv/bin/ctk"

# ── Filter settings ────────────────────────────────────────────────────────
MIN_MESSAGES = 6

EXCLUDE_PATTERNS = [
    r"monitor", r"e.?reader", r"ebook", r"boox", r"kindle", r"kobo",
    r"lumi", r"tab\s*x", r"thinkpad", r"laptop", r"computer", r"phone",
    r"lineage.?os", r"eizo", r"max lumi", r"max tab",
]
EXCLUDE_RE = re.compile("|".join(EXCLUDE_PATTERNS), re.IGNORECASE)


# ── Helpers ────────────────────────────────────────────────────────────────

def find_latest_manifest() -> Path:
    """Return the most recently modified manifest-*.json in ~/Downloads."""
    pattern = str(DOWNLOADS / "manifest-*.json")
    matches = sorted(glob.glob(pattern), key=os.path.getmtime, reverse=True)
    if not matches:
        sys.exit(f"No manifest-*.json found in {DOWNLOADS}")
    return Path(matches[0])


def download_conversations_zip(manifest: Path) -> Path:
    """Download the conversations ZIP from the manifest export_url.

    Saves to ~/Downloads next to the manifest. Returns the local ZIP path.
    If the ZIP already exists (re-run), skips the download.
    Export URLs are single-use — if the URL is expired (403) we fall back
    to any existing ZIP matching the filename.
    """
    with open(manifest) as f:
        data = json.load(f)

    conv_file = next(
        (d for d in data["data_files"] if d["category"] == "conversations"), None
    )
    if not conv_file:
        sys.exit("Manifest has no 'conversations' entry.")

    zip_name = conv_file["filename"]          # e.g. conversations-000.zip
    export_url = conv_file["export_url"]
    zip_path = DOWNLOADS / zip_name

    if zip_path.exists():
        print(f"  ZIP already downloaded: {zip_path}")
        return zip_path

    print(f"  Downloading {zip_name} …")
    try:
        urllib_request.urlretrieve(export_url, zip_path)
        print(f"  Saved → {zip_path}")
    except Exception as exc:
        # Single-use URL already consumed — look for any matching ZIP in ~/Downloads
        fallbacks = sorted(DOWNLOADS.glob("conversations-*.zip"), key=os.path.getmtime, reverse=True)
        if fallbacks:
            zip_path = fallbacks[0]
            print(f"  Download failed ({exc})")
            print(f"  Using existing ZIP: {zip_path}")
        else:
            sys.exit(
                f"Download failed: {exc}\n"
                "Export URLs are single-use. Request a new export at https://claude.ai/settings\n"
                "Or pass an existing conversations.json directly: --json <path>"
            )
    return zip_path


def extract_conversations(zip_path: Path) -> list:
    """Extract conversations.json from the ZIP and return the list."""
    with zipfile.ZipFile(zip_path) as zf:
        names = zf.namelist()
        # Find the conversations.json (may be nested inside a folder)
        conv_name = next((n for n in names if n.endswith("conversations.json")), None)
        if not conv_name:
            sys.exit(f"conversations.json not found in {zip_path}. Contents: {names}")
        with zf.open(conv_name) as f:
            data = json.load(f)

    convs = data if isinstance(data, list) else data.get("conversations", [])
    print(f"  Found {len(convs)} conversations in ZIP")
    return convs


def load_existing_ids(db_dir: Path) -> set:
    """Return the set of conversation IDs already in the CTK database."""
    db_file = db_dir / "conversations.db"
    if not db_file.exists() or db_file.stat().st_size == 0:
        print("  Database is empty — no duplicates to skip")
        return set()
    try:
        conn = sqlite3.connect(db_file)
        rows = conn.execute("SELECT id FROM conversations").fetchall()
        conn.close()
        ids = {r[0] for r in rows}
        print(f"  {len(ids)} conversations already in DB")
        return ids
    except sqlite3.OperationalError:
        print("  Database has no conversations table yet")
        return set()


def should_keep(conv: dict) -> tuple[bool, str]:
    """Return (keep, reason_if_dropped)."""
    title = (conv.get("name") or conv.get("title") or "").strip()
    msgs  = len(conv.get("chat_messages", []))

    if not title or re.fullmatch(r"\(untitled\)", title, re.IGNORECASE):
        return False, "untitled"
    if EXCLUDE_RE.search(title):
        return False, "hardware/device topic"
    if msgs < MIN_MESSAGES:
        return False, f"too short ({msgs} messages)"
    return True, ""


def filter_and_dedup(convs: list, existing_ids: set) -> tuple[list, list]:
    """Split convs into (kept, dropped). dropped items carry a reason."""
    kept, dropped = [], []
    for c in convs:
        uid = c.get("uuid") or c.get("id", "")
        if uid in existing_ids:
            dropped.append((c, "already in DB"))
            continue
        keep, reason = should_keep(c)
        if keep:
            kept.append(c)
        else:
            dropped.append((c, reason))
    return kept, dropped


# ── Main ───────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "manifest", nargs="?", type=Path,
        help="Path to manifest JSON (default: latest manifest-*.json in ~/Downloads)",
    )
    parser.add_argument(
        "--json", type=Path, dest="json_path",
        help="Skip download and use this conversations.json directly",
    )
    parser.add_argument("--dry-run", action="store_true", help="Preview only, import nothing")
    args = parser.parse_args()

    print(f"\n=== Claude export import ===")
    print(f"DB       : {DB_DIR}")
    print(f"Dry-run  : {args.dry_run}\n")

    # 1. Load conversations — from --json, ZIP, or manifest download
    if args.json_path:
        print(f"Step 1: loading from {args.json_path} …")
        with open(args.json_path) as f:
            data = json.load(f)
        convs = data if isinstance(data, list) else data.get("conversations", [])
        print(f"  Found {len(convs)} conversations")
    else:
        manifest = args.manifest or find_latest_manifest()
        print(f"Manifest : {manifest}\n")
        print("Step 1: downloading conversations ZIP …")
        zip_path = download_conversations_zip(manifest)
        print()
        print("Step 2: extracting conversations …")
        convs = extract_conversations(zip_path)
    print()

    # 2/3. Load existing IDs
    print("Step 3: loading existing DB IDs …")
    existing_ids = load_existing_ids(DB_DIR)
    print()

    # 4. Filter + dedup
    print("Step 4: filtering …")
    kept, dropped = filter_and_dedup(convs, existing_ids)

    print(f"  Total    : {len(convs)}")
    print(f"  Kept     : {len(kept)}")
    print(f"  Dropped  : {len(dropped)}")
    print()
    print("Dropped breakdown:")
    for c, reason in sorted(dropped, key=lambda x: x[1]):
        title = c.get("name") or c.get("title") or "(untitled)"
        msgs  = len(c.get("chat_messages", []))
        print(f"  [{msgs:3d} msgs] ({reason}) {title[:70]}")
    print()

    if not kept:
        print("Nothing new to import.")
        return

    print("To be imported:")
    for c in sorted(kept, key=lambda x: -len(x.get("chat_messages", []))):
        title = c.get("name") or c.get("title") or "(untitled)"
        msgs  = len(c.get("chat_messages", []))
        print(f"  [{msgs:3d} msgs] {title[:70]}")
    print()

    if args.dry_run:
        print("(dry-run) Stopping here — nothing written.")
        return

    # 5. Write filtered JSON
    OUTPUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_JSON, "w") as f:
        json.dump(kept, f, indent=2)
    print(f"Wrote {len(kept)} conversations → {OUTPUT_JSON}")

    # 6. Import
    print(f"\nStep 5: importing into CTK database …")
    result = subprocess.run(
        [str(CTK_BIN), "import", str(OUTPUT_JSON), "--db", str(DB_DIR)],
        capture_output=False,
    )
    if result.returncode != 0:
        sys.exit(f"ctk import failed (exit {result.returncode})")
    print("\nDone.")


if __name__ == "__main__":
    main()
