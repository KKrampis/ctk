#!/usr/bin/env python3
"""
Filter Claude export conversations before importing into CTK.

Removes:
  - Untitled conversations
  - Conversations about e-readers, monitors, computers/hardware
  - Conversations with fewer than MIN_MESSAGES messages

Usage:
    python3 scripts/filter_conversations.py
"""

import json
import re
from pathlib import Path

INPUT  = Path.home() / "Documents/GitHub/claude_export/conversations.json"
OUTPUT = Path.home() / "Documents/GitHub/claude_export/conversations_filtered.json"

MIN_MESSAGES = 4

# Title patterns to exclude (case-insensitive, partial match)
EXCLUDE_PATTERNS = [
    # Hardware / devices
    r"monitor",
    r"e.?reader",
    r"ebook",
    r"boox",
    r"kindle",
    r"kobo",
    r"lumi",
    r"tab\s*x",
    r"thinkpad",
    r"laptop",
    r"computer",
    r"phone",
    r"lineage.?os",
    r"eizo",
    r"max lumi",
    r"max tab",
    # Generic junk
    r"^\(untitled\)$",
    r"^untitled$",
]

EXCLUDE_RE = re.compile("|".join(EXCLUDE_PATTERNS), re.IGNORECASE)


def load(path: Path):
    with open(path) as f:
        data = json.load(f)
    return data if isinstance(data, list) else data.get("conversations", [])


def should_keep(conv: dict) -> bool:
    title = (conv.get("name") or conv.get("title") or "").strip()
    if not title or re.fullmatch(r"\(untitled\)", title, re.IGNORECASE):
        return False
    if EXCLUDE_RE.search(title):
        return False
    if len(conv.get("chat_messages", [])) < MIN_MESSAGES:
        return False
    return True


def main():
    convs = load(INPUT)
    kept, dropped = [], []
    for c in convs:
        (kept if should_keep(c) else dropped).append(c)

    print(f"Total:   {len(convs)}")
    print(f"Kept:    {len(kept)}")
    print(f"Dropped: {len(dropped)}")
    print("\nDropped titles:")
    for c in dropped:
        title = c.get("name") or c.get("title") or "(untitled)"
        msgs  = len(c.get("chat_messages", []))
        print(f"  [{msgs:3d} msgs] {title}")

    with open(OUTPUT, "w") as f:
        json.dump(kept, f, indent=2)
    print(f"\nWritten → {OUTPUT}")


if __name__ == "__main__":
    main()
