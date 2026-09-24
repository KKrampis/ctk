#!/usr/bin/env python3
"""
fetch_and_import.py — Download a Claude export, filter, deduplicate,
auto-tag with project names, and import into CTK.

Steps:
  1. Find the latest manifest in ~/Downloads (or pass path as arg)
  2. Download the conversations + projects ZIPs
  3. Apply content filters (untitled, hardware/device topics, <MIN_MESSAGES)
  4. Skip conversations already in the CTK database (matched by Claude uuid)
  5. Match each conversation title against Claude project names → apply as tag
  6. Import via the CTK Python API (tags applied per conversation)

Usage:
    python3 scripts/fetch_and_import.py
    python3 scripts/fetch_and_import.py ~/Downloads/manifest-xxx.json
    python3 scripts/fetch_and_import.py --json ~/path/to/conversations.json
    python3 scripts/fetch_and_import.py --dry-run

Export URLs are single-use. The script saves downloaded ZIPs to ~/Downloads
so you can re-run filtering/dedup/tagging without re-downloading.

Requires the CTK venv — the script auto-restarts with .venv/bin/python3
if run with a bare python3.
"""

# ── Ensure venv site-packages are on sys.path ──────────────────────────────
import os
import site
import sys
from pathlib import Path

_VENV_SITE = Path(__file__).parent.parent / ".venv/lib"
for _sp in _VENV_SITE.glob("python*/site-packages"):
    if str(_sp) not in sys.path:
        site.addsitedir(str(_sp))

# ── Standard imports (CTK venv available from here) ───────────────────────
import argparse
import glob
import json
import re
import sqlite3
import zipfile

try:
    import requests as _requests
    _HAS_REQUESTS = True
except ImportError:
    from urllib import request as urllib_request
    _HAS_REQUESTS = False

_BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

# ── Paths ──────────────────────────────────────────────────────────────────
DOWNLOADS   = Path.home() / "Downloads"
DB_DIR      = Path.home() / "Documents/GitHub/ctk-chats-db"
OUTPUT_JSON = Path.home() / "Documents/GitHub/claude_export/conversations_filtered.json"

# ── Filter settings ────────────────────────────────────────────────────────
MIN_MESSAGES = 4

EXCLUDE_PATTERNS = [
    r"monitor", r"e.?reader", r"ebook", r"boox", r"kindle", r"kobo",
    r"lumi", r"tab\s*x", r"thinkpad", r"laptop", r"computer", r"phone",
    r"lineage.?os", r"eizo", r"max lumi", r"max tab",
]
EXCLUDE_RE = re.compile("|".join(EXCLUDE_PATTERNS), re.IGNORECASE)

# Content block types injected by Claude's server — not real conversation
# content; strip them so they don't cause rendering noise in CTK.
_STRIP_BLOCK_TYPES = {
    "injected_prompt_block",  # date/time notes, system-reminder injections
    "token_budget",           # internal token accounting metadata
}


def _tool_use_to_text(part: dict) -> str:
    """Convert a tool_use block to a readable one-liner annotation."""
    name = part.get("name", "unknown")
    inp  = part.get("input") or {}
    if name == "web_search":
        query = inp.get("query", "")
        return f"🔍 Web search: {query!r}"
    elif name in ("web_fetch", "web_fetch_parallel"):
        url = inp.get("url") or inp.get("urls") or ""
        return f"🌐 Web fetch: {url}"
    else:
        args = json.dumps(inp)[:120] if inp else ""
        return f"🔧 Tool call: {name}({args})"


def _tool_result_to_text(part: dict) -> str:
    """Convert a tool_result block (web search results etc.) to readable text."""
    content = part.get("content", [])
    if isinstance(content, str):
        return content[:800]
    if not isinstance(content, list):
        return ""
    lines = []
    for item in content[:8]:          # cap at 8 results
        if not isinstance(item, dict):
            continue
        itype = item.get("type", "")
        if itype == "knowledge":
            title = item.get("title", "")
            url   = item.get("url", "")
            if title:
                lines.append(f"• {title}")
            if url:
                lines.append(f"  {url}")
        elif itype == "text":
            text = (item.get("text") or "").strip()
            if text:
                lines.append(text[:400])
    return "\n".join(lines)


def clean_messages(convs: list) -> list:
    """Clean message content for CTK display:
    - Drop server-injected blocks (injected_prompt_block, token_budget).
    - Drop bare document references (no exported content).
    - Convert tool_use / tool_result blocks to readable text so CTK
      can display them in the message bubble instead of showing nothing.
    """
    for conv in convs:
        for msg in conv.get("chat_messages", []):
            content = msg.get("content")
            if not isinstance(content, list):
                continue

            text_parts:  list[dict] = []
            extra_lines: list[str]  = []
            keep_parts:  list[dict] = []   # image / thinking / etc.

            for part in content:
                if not isinstance(part, dict):
                    keep_parts.append(part)
                    continue
                btype = part.get("type", "")

                if btype in _STRIP_BLOCK_TYPES:
                    continue                         # silently drop

                elif btype == "document":
                    if part.get("file_content") or part.get("content"):
                        keep_parts.append(part)      # has real content
                    else:
                        title = part.get("title", "file")
                        extra_lines.append(f"📎 Attached: {title}")

                elif btype == "tool_use":
                    extra_lines.append(_tool_use_to_text(part))

                elif btype == "tool_result":
                    result_text = _tool_result_to_text(part)
                    if result_text:
                        extra_lines.append(result_text)

                elif btype == "text":
                    text_parts.append(part)

                else:
                    keep_parts.append(part)          # image, thinking, etc.

            # Merge extra_lines into the text content
            if extra_lines:
                note = "\n".join(extra_lines)
                if text_parts:
                    # Append tool annotations after the existing text
                    text_parts[-1] = dict(
                        text_parts[-1],
                        text=(text_parts[-1].get("text") or "") + "\n\n" + note,
                    )
                else:
                    text_parts = [{"type": "text", "text": note}]

            msg["content"] = text_parts + keep_parts
    return convs


# Words too short or generic to be useful for project matching
_STOP = {
    "and", "the", "for", "with", "from", "that", "this", "are", "was",
    "has", "not", "all", "but", "our", "its", "can", "new", "use",
    "via", "how", "why", "get", "set", "run",
}

# Manual keyword aliases — extra terms that should map to a project
# (in addition to the words extracted from the project name itself)
PROJECT_ALIASES: dict[str, list[str]] = {
    "NeoVim":                          ["nvim", "neovim", "vim", "zotero", "zathura",
                                        "telescope", "obsidian"],
    "Writing pipeline - ref management": ["bibtex", "zotero", "refero", "pdf",
                                          "annotation", "reference"],
    "Personas":                        ["persona", "personas", "chen", "individuation"],
    "Persona Vectors and LLM Individuation": ["persona", "vector", "individuation",
                                              "linear", "representation"],
    "AI Consciousness, Theology":      ["consciousness", "awareness", "sentience",
                                        "phenomenal", "global", "workspace",
                                        "introspection", "mind"],
    "AI Paper Discussions":            ["arxiv", "paper", "llm", "alignment",
                                        "misalignment", "beliefs", "belief",
                                        "standards", "psychology"],
    "Beliefs Research Writing":        ["belief", "beliefs", "standards", "llm"],
    "Algoverse":                       ["algoverse", "algo"],
    "MCP Dev":                         ["mcp", "claude", "opencode", "tui", "ctk"],
    "Chrome AddOns":                   ["chrome", "addon", "extension", "gmail",
                                        "browser"],
    "AT finance":                      ["tax", "contractor", "finance", "payment",
                                        "international"],
    "Fellowships - Anthropic":         ["fellowship", "welfare", "nyu", "conference",
                                        "neurips", "workshop", "submission"],
    "ARENA":                           ["arena"],
    "Hack and Office Stuff":           ["debian", "install", "osx", "text", "voice",
                                        "mpv", "dvd", "converting"],
    "BlogBook Papers":                 ["blog", "book", "writing", "pipeline"],
    "Python":                          ["python", "script", "rust", "tui"],
    "Introspection  - Digital  Minds": ["introspection", "digital", "mind"],
}


# ── Project loading ────────────────────────────────────────────────────────

def load_projects_from_zip(zip_path: Path) -> list[dict]:
    """Read all project JSON files from the projects ZIP."""
    projects = []
    with zipfile.ZipFile(zip_path) as z:
        for name in z.namelist():
            if name.endswith(".json"):
                with z.open(name) as f:
                    projects.append(json.load(f))
    return projects


def load_projects(manifest_dir: Path) -> list[dict]:
    """Try to load projects from a ZIP in the same directory as the manifest."""
    zip_path = manifest_dir / "projects-000.zip"
    # Also scan Downloads for any projects ZIP
    if not zip_path.exists():
        candidates = sorted(
            DOWNLOADS.glob("projects-*.zip"), key=os.path.getmtime, reverse=True
        )
        if candidates:
            zip_path = candidates[0]
    if zip_path.exists():
        projects = load_projects_from_zip(zip_path)
        print(f"  Loaded {len(projects)} projects from {zip_path.name}")
        return projects
    print("  No projects ZIP found — project tagging skipped")
    return []


# ── Project → keyword matcher ──────────────────────────────────────────────

def _keywords(text: str) -> set[str]:
    return {w for w in re.findall(r"\b\w{3,}\b", text.lower()) if w not in _STOP}


def build_project_keywords(projects: list[dict]) -> dict[str, set[str]]:
    """Return {project_name: keyword_set} for named projects."""
    result: dict[str, set[str]] = {}
    for p in projects:
        name = (p.get("name") or "").strip()
        if not name:
            continue
        kws = _keywords(name)
        # Add manual aliases
        for alias_list in [v for k, v in PROJECT_ALIASES.items() if k == name]:
            kws |= set(alias_list)
        result[name] = kws
    return result


def match_projects(title: str, project_kws: dict[str, set[str]]) -> list[str]:
    """Return project names whose keywords overlap with *title*, best first."""
    title_words = _keywords(title)
    if not title_words:
        return []
    scores: dict[str, int] = {}
    for proj, kws in project_kws.items():
        overlap = title_words & kws
        if overlap:
            scores[proj] = sum(len(w) for w in overlap)
    if not scores:
        return []
    best = max(scores.values())
    threshold = max(4, int(best * 0.5))
    return [p for p, s in sorted(scores.items(), key=lambda x: -x[1]) if s >= threshold]


# ── Download helpers ───────────────────────────────────────────────────────

def find_latest_manifest() -> Path:
    pattern = str(DOWNLOADS / "manifest-*.json")
    matches = sorted(glob.glob(pattern), key=os.path.getmtime, reverse=True)
    if not matches:
        sys.exit(f"No manifest-*.json found in {DOWNLOADS}")
    return Path(matches[0])


def _download_category(manifest_data: dict, category: str, dest_dir: Path) -> Path | None:
    """Download one category ZIP. Returns path or None if not in manifest."""
    entry = next(
        (d for d in manifest_data["data_files"] if d["category"] == category), None
    )
    if not entry:
        return None
    zip_path = dest_dir / entry["filename"]
    if zip_path.exists():
        print(f"  Already downloaded: {zip_path.name}")
        return zip_path
    print(f"  Downloading {entry['filename']} …")
    try:
        if _HAS_REQUESTS:
            resp = _requests.get(
                entry["export_url"],
                headers={"User-Agent": _BROWSER_UA},
                stream=True,
                timeout=120,
            )
            resp.raise_for_status()
            with open(zip_path, "wb") as fh:
                for chunk in resp.iter_content(chunk_size=1 << 20):
                    fh.write(chunk)
        else:
            from urllib.request import Request, urlopen
            req = Request(entry["export_url"], headers={"User-Agent": _BROWSER_UA})
            with urlopen(req, timeout=120) as r, open(zip_path, "wb") as fh:
                while chunk := r.read(1 << 20):
                    fh.write(chunk)
        # Verify it's actually a ZIP (the server may return an HTML login page)
        with open(zip_path, "rb") as fh:
            magic = fh.read(4)
        if magic[:2] == b"PK":
            print(f"  Saved → {zip_path}")
        else:
            zip_path.unlink(missing_ok=True)
            raise RuntimeError(
                "Server returned an HTML page instead of a ZIP.\n"
                "The download URL requires your browser session.\n"
                f"Please open this URL in Chrome/Safari and save the file to {dest_dir}:\n\n"
                f"  {entry['export_url']}\n\n"
                f"Then re-run the script — it will detect the saved file automatically."
            )
    except Exception as exc:
        # URL already consumed — try to find an existing file
        fallback = sorted(dest_dir.glob(f"{category}-*.zip"),
                          key=os.path.getmtime, reverse=True)
        if fallback:
            print(f"  URL expired ({exc}) — using {fallback[0].name}")
            return fallback[0]
        print(f"  URL expired and no local {category} ZIP found: {exc}")
        return None
    return zip_path


def extract_conversations(zip_path: Path) -> list:
    with zipfile.ZipFile(zip_path) as zf:
        conv_name = next((n for n in zf.namelist()
                          if n.endswith("conversations.json")), None)
        if not conv_name:
            sys.exit(f"conversations.json not found in {zip_path}")
        with zf.open(conv_name) as f:
            data = json.load(f)
    convs = data if isinstance(data, list) else data.get("conversations", [])
    print(f"  Found {len(convs)} conversations")
    return convs


# ── DB helpers ─────────────────────────────────────────────────────────────

def load_existing_ids(db_dir: Path) -> set[str]:
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


# ── Content filter ─────────────────────────────────────────────────────────

def should_keep(conv: dict) -> tuple[bool, str]:
    title = (conv.get("name") or conv.get("title") or "").strip()
    msgs  = len(conv.get("chat_messages", []))
    if not title or re.fullmatch(r"\(untitled\)", title, re.IGNORECASE):
        return False, "untitled"
    if EXCLUDE_RE.search(title):
        return False, "hardware/device topic"
    if msgs < MIN_MESSAGES:
        return False, f"too short ({msgs} msgs)"
    return True, ""


def filter_by_project_terms(
    convs: list,
    project_kws: dict[str, set[str]],
    terms: list[str],
) -> tuple[list, list]:
    """Keep only conversations whose matched project tags contain any of *terms*
    (case-insensitive partial match against project names).

    Returns (kept, dropped_with_reason).
    """
    if not terms:
        return convs, []

    # Resolve which project names satisfy any term
    matching_projects: set[str] = set()
    for proj_name in project_kws:
        for t in terms:
            if t.lower() in proj_name.lower():
                matching_projects.add(proj_name)

    print(f"  Project filter terms  : {terms}")
    print(f"  Matching project names: {sorted(matching_projects)}")
    if not matching_projects:
        print("  WARNING: no projects matched — importing nothing")
        return [], [(c, "no project match") for c in convs]

    kept, dropped = [], []
    for c in convs:
        title = c.get("name") or c.get("title") or ""
        tags  = match_projects(title, project_kws)
        if any(t in matching_projects for t in tags):
            kept.append(c)
        else:
            dropped.append((c, "not in selected projects"))
    return kept, dropped


def filter_and_dedup(
    convs: list, existing_ids: set[str]
) -> tuple[list, list]:
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


# ── Import via CTK Python API ──────────────────────────────────────────────

def do_import(
    convs: list,
    project_kws: dict[str, set[str]],
    db_dir: Path,
) -> int:
    """Import *convs* into the CTK DB, tagging each with matched projects."""
    from ctk.core.database import ConversationDB
    from ctk.importers.anthropic import AnthropicImporter

    importer = AnthropicImporter()
    db = ConversationDB(str(db_dir))
    saved = 0
    with db:
        for conv_data in convs:
            trees = importer.import_data([conv_data])
            for tree in trees:
                # Apply matched project tags
                project_tags = match_projects(tree.title or "", project_kws)
                if project_tags:
                    tree.metadata.tags.extend(project_tags)
                db.save_conversation(tree)
                tag_str = ", ".join(project_tags) if project_tags else "(no project match)"
                print(f"  ✓ {tree.title[:60]!r:62s}  → {tag_str}")
                saved += 1
    return saved


# ── Main ───────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", nargs="?", type=Path,
        help="Manifest JSON (default: latest manifest-*.json in ~/Downloads)")
    parser.add_argument("--json", type=Path, dest="json_path",
        help="Skip download — use this conversations.json directly")
    parser.add_argument("--zip", type=Path, dest="zip_path",
        help="Skip download — use this conversations ZIP directly")
    parser.add_argument("--projects", type=str, default="",
        help="Comma-separated project name terms (partial match). "
             "Only conversations tagged with a matching project are imported. "
             "Example: --projects 'Persona,Minds'")
    parser.add_argument("--dry-run", action="store_true",
        help="Preview only, import nothing")
    args = parser.parse_args()

    project_terms = [t.strip() for t in args.projects.split(",") if t.strip()]

    print("\n=== Claude export import ===")
    print(f"DB       : {DB_DIR}")
    print(f"Projects : {project_terms or '(all)'}")
    print(f"Dry-run  : {args.dry_run}\n")

    # ── 1. Load conversations ──────────────────────────────────────────────
    projects_zip_dir: Path = DOWNLOADS
    if args.zip_path:
        print(f"Step 1: extracting from {args.zip_path} …")
        convs = extract_conversations(args.zip_path)
        projects_zip_dir = args.zip_path.parent
    elif args.json_path:
        print(f"Step 1: loading from {args.json_path} …")
        with open(args.json_path) as f:
            data = json.load(f)
        convs = data if isinstance(data, list) else data.get("conversations", [])
        print(f"  Found {len(convs)} conversations")
        # look for projects ZIP alongside the JSON
        projects_zip_dir = args.json_path.parent
    else:
        manifest = args.manifest or find_latest_manifest()
        print(f"Manifest: {manifest}\n")
        with open(manifest) as f:
            manifest_data = json.load(f)
        print("Step 1: downloading ZIPs …")
        conv_zip = _download_category(manifest_data, "conversations", DOWNLOADS)
        if not conv_zip:
            sys.exit("Could not obtain conversations ZIP.")
        print("Step 2: extracting conversations …")
        convs = extract_conversations(conv_zip)
        projects_zip_dir = DOWNLOADS
    print()

    # ── 2. Load projects ───────────────────────────────────────────────────
    print("Step 2: loading projects for tagging …")
    projects = load_projects(projects_zip_dir)
    project_kws = build_project_keywords(projects)
    print(f"  {len(project_kws)} named projects available for tagging")
    print()

    # ── 2b. Clean message content ─────────────────────────────────────────
    convs = clean_messages(convs)

    # ── 3. Load existing IDs ───────────────────────────────────────────────
    print("Step 3: checking existing DB …")
    existing_ids = load_existing_ids(DB_DIR)
    print()

    # ── 4. Filter + dedup ─────────────────────────────────────────────────
    print("Step 4: filtering …")
    kept, dropped = filter_and_dedup(convs, existing_ids)
    print(f"  Total   : {len(convs)}")
    print(f"  Kept    : {len(kept)}")
    print(f"  Dropped : {len(dropped)}")
    print()

    if dropped:
        print("Dropped:")
        for c, reason in sorted(dropped, key=lambda x: x[1]):
            title = c.get("name") or c.get("title") or "(untitled)"
            msgs  = len(c.get("chat_messages", []))
            print(f"  [{msgs:3d}] ({reason}) {title[:65]}")
        print()

    if not kept:
        print("Nothing new to import.")
        return

    # ── 4b. Project filter (--projects) ────────────────────────────────────
    if project_terms:
        print("Step 4b: applying project filter …")
        kept, proj_dropped = filter_by_project_terms(kept, project_kws, project_terms)
        print(f"  After project filter: {len(kept)} kept, {len(proj_dropped)} dropped")
        print()
        if not kept:
            print("Nothing matched the project filter.")
            return

    # ── 5. Preview project tagging ─────────────────────────────────────────
    print("To be imported (with matched project tags):")
    for c in sorted(kept, key=lambda x: -len(x.get("chat_messages", []))):
        title  = c.get("name") or c.get("title") or "(untitled)"
        msgs   = len(c.get("chat_messages", []))
        tags   = match_projects(title, project_kws)
        tag_str = ", ".join(tags) if tags else "—"
        print(f"  [{msgs:3d}] {title[:55]:<55s}  → {tag_str}")
    print()

    if args.dry_run:
        print("(dry-run) Nothing written.")
        return

    # ── 6. Import ──────────────────────────────────────────────────────────
    print("Step 5: importing into CTK database …")
    saved = do_import(kept, project_kws, DB_DIR)
    print(f"\nDone — {saved} conversation(s) saved.")


if __name__ == "__main__":
    main()
