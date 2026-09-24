"""Sidebar widget: tabbed conversation browser.

Tabs replace the line-mode VFS virtual directories from the legacy
``ctk chat`` (``/starred/``, ``/pinned/``, ``/tags/``, ``/sources/``,
``/recent/``). Selecting a tab refilters the list in place; the
underlying DataTable still drives selection.

Pagination: most tabs fetch one page (``DEFAULT_PAGE_SIZE`` rows) at
a time using cursor-based keyset pagination. When more pages remain
the header reads ``conversations · N loaded · more (Ctrl+L)``; the
app binds Ctrl+L to ``load_more()``. The "Recent" tab is the one
exception — a fixed 20-row snapshot that never paginates.

Adding a new filter mode means: append a ``(label, mode_key)`` tuple to
``_TAB_DEFS`` and handle the new key in ``_fetch_page``.

Tags / Projects tabs use a two-level drill-down:
  Level 1 — tag list: each row is a tag name + conversation count.
  Level 2 — conversation list: after selecting a tag, shows conversations
             tagged with it. A "← back" row at the top returns to level 1.
  Projects tab is identical to Tags but filters out system tags
  (``claude``, ``anthropic``) so only project-derived tags appear.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple, cast

from textual.containers import Vertical
from textual.widgets import DataTable, Static, Tab, Tabs

from ctk.core.database import ConversationDB
from ctk.core.models import PaginatedResult

# Tags that are added by the importer automatically — not project names.
_SYSTEM_TAGS = {"claude", "anthropic"}

# Sentinel row keys used in the tag-browser level-1 table.
_KEY_BACK = "__back__"


def _flags(conv) -> str:
    parts = []
    if getattr(conv, "starred_at", None) or getattr(conv, "starred", False):
        parts.append("⭐")
    if getattr(conv, "pinned_at", None) or getattr(conv, "pinned", False):
        parts.append("📌")
    if getattr(conv, "archived_at", None) or getattr(conv, "archived", False):
        parts.append("📁")
    return "".join(parts) or " "


def _title(conv) -> str:
    title = getattr(conv, "title", None) or "(untitled)"
    branch_count = getattr(conv, "branch_count", 0)
    suffix = f" ⑃{branch_count}" if branch_count else ""
    max_title = 32 - len(suffix)
    short = title if len(title) <= max_title else title[:max_title - 2] + "…"
    return short + suffix


# Tab id -> (label, filter_mode). Order is the strip order.
_TAB_DEFS: List[Tuple[str, str]] = [
    ("all",      "All"),
    ("starred",  "⭐ Starred"),
    ("pinned",   "📌 Pinned"),
    ("recent",   "Recent"),
    ("archived", "📁 Archived"),
    ("tags",     "🏷 Tags"),
    ("projects", "🗂 Projects"),
]


class ConversationList(Vertical):
    """Sidebar tab strip + scrollable conversation table.

    Selection events on the underlying ``DataTable`` bubble up as
    standard Textual messages; the parent app handles them via
    ``on_data_table_row_*`` hooks.

    In Tags / Projects mode the table operates in two levels:
      - ``_selected_tag is None``  → level 1: tag list
      - ``_selected_tag == <name>`` → level 2: conversations for that tag
    """

    DEFAULT_PAGE_SIZE = 200

    def __init__(self, db: ConversationDB) -> None:
        super().__init__(id="sidebar")
        self._db = db
        self._conversations: List = []
        self._table: DataTable = DataTable(cursor_type="row", zebra_stripes=True)
        self._tabs = Tabs(*[Tab(label, id=tab_id) for tab_id, label in _TAB_DEFS])
        self._title_label = Static("conversations", id="sidebar-title")
        self._search: Optional[str] = None
        self._mode: str = "all"
        # Cursor pagination state.
        self._next_cursor: Optional[str] = ""
        self._has_more: bool = False
        # Tag-browser drill-down state (None = showing tag list).
        self._selected_tag: Optional[str] = None

    def compose(self):
        yield self._title_label
        yield self._tabs
        yield self._table

    def on_mount(self) -> None:
        self._table.add_columns("", "title", "updated")
        self.refresh_list()

    # ------------------------------------------------------------------
    # Public API used by the app
    # ------------------------------------------------------------------

    def refresh_list(self, search: Optional[str] = None) -> None:
        """Reload from the DB, starting at page 1."""
        if search is not None:
            self._search = search or None
        self._reset_and_fetch()

    def set_mode(self, mode: str) -> None:
        """Change the filter mode. Resets pagination and tag drill-down."""
        self._mode = mode
        self._selected_tag = None
        self._reset_and_fetch()

    def load_more(self) -> int:
        """Fetch the next page (if any) and append to the table."""
        if not self._has_more or self._next_cursor is None:
            return 0
        page = self._fetch_page(cursor=self._next_cursor)
        added = self._merge_page(page)
        self._update_title()
        return added

    def has_more(self) -> bool:
        return self._has_more

    def loaded_count(self) -> int:
        return len(self._conversations)

    def selected_conversation_id(self) -> Optional[str]:
        """Return the currently highlighted conversation id.

        Returns ``None`` when the sidebar is in tag-list mode (level 1)
        so the app does not try to open a tag name as a conversation.
        """
        if self._is_tag_browser() and self._selected_tag is None:
            return None  # tag-list level — no conversation to open
        if not self._conversations:
            return None
        try:
            row_key = self._table.coordinate_to_cell_key(
                self._table.cursor_coordinate
            ).row_key
            key = row_key.value if row_key else None
            # Ignore the "← back" sentinel row
            return key if key and key != _KEY_BACK else None
        except Exception:
            return None

    def focus_table(self) -> None:
        self._table.focus()

    # ------------------------------------------------------------------
    # Tag-browser row interception
    # ------------------------------------------------------------------

    def on_data_table_row_highlighted(
        self, event: DataTable.RowHighlighted
    ) -> None:
        """Stop highlight events from reaching the app while in tag-list mode."""
        if self._is_tag_browser() and self._selected_tag is None:
            event.stop()

    def on_data_table_row_selected(
        self, event: DataTable.RowSelected
    ) -> None:
        """Handle tag-row selection (drill-down or back) without bubbling."""
        if not self._is_tag_browser():
            return  # let normal conversation tabs handle it

        key = (event.row_key.value or "") if event.row_key else ""

        if key == _KEY_BACK:
            # Return to tag list
            self._selected_tag = None
            self._reset_and_fetch()
            event.stop()
            return

        if self._selected_tag is None:
            # Level-1 tag list — drill into the selected tag
            self._drill_into_tag(key)
            event.stop()
        # Level-2 (conversation list inside a tag) — let app open it normally.

    # ------------------------------------------------------------------
    # Internal: tag-browser helpers
    # ------------------------------------------------------------------

    def _is_tag_browser(self) -> bool:
        return self._mode in ("tags", "projects")

    def _get_tag_rows(self) -> List[Tuple[str, int]]:
        """Return [(tag_name, count), ...] for the current tag-browser mode."""
        try:
            all_tags = self._db.get_all_tags(with_counts=True)
        except Exception:
            return []
        rows = []
        for t in all_tags:
            name = t.get("name", "")
            count = t.get("usage_count", 0)
            if not name or count == 0:
                continue
            if self._mode == "projects" and name in _SYSTEM_TAGS:
                continue
            rows.append((name, count))
        # Sort by count desc, then alphabetically
        return sorted(rows, key=lambda x: (-x[1], x[0]))

    def _drill_into_tag(self, tag: str) -> None:
        """Switch to level-2: load conversations for *tag*."""
        self._selected_tag = tag
        try:
            convs = self._db.list_conversations_by_tag(tag)
        except Exception:
            convs = []
        # Fetch branch counts
        conv_ids = [str(getattr(c, "id", "")) for c in convs]
        branch_counts: Dict[str, int] = {}
        try:
            branch_counts = self._db.batch_branch_counts(conv_ids)
        except Exception:
            pass

        self._conversations = []
        self._table.clear()
        # Back row at the top
        self._table.add_row("←", "back to tags", "", key=_KEY_BACK)
        for conv in convs:
            conv_id = str(getattr(conv, "id", ""))
            conv.branch_count = branch_counts.get(conv_id, 0)
            updated = getattr(conv, "updated_at", None)
            updated_str = updated.strftime("%Y-%m-%d") if updated else ""
            self._table.add_row(
                _flags(conv), _title(conv), updated_str, key=conv_id
            )
            self._conversations.append(conv)
        self._has_more = False
        self._next_cursor = None
        if self._conversations:
            self._table.move_cursor(row=1)  # skip back row
        self._update_title()

    # ------------------------------------------------------------------
    # Internal: cursor-driven fetch + table rendering
    # ------------------------------------------------------------------

    def _reset_and_fetch(self) -> None:
        """Wipe the table and load the first page from the current filter."""
        self._next_cursor = ""
        self._has_more = False
        self._conversations = []
        self._table.clear()

        if self._is_tag_browser() and self._selected_tag is None:
            # Level-1: populate tag list directly (no cursor pagination)
            self._load_tag_list()
            return

        page = self._fetch_page(cursor="")
        self._merge_page(page)
        if self._conversations:
            self._table.move_cursor(row=0)
        self._update_title()

    def _load_tag_list(self) -> None:
        """Populate the table with the tag-name / count rows (level 1)."""
        rows = self._get_tag_rows()
        for tag_name, count in rows:
            icon = "🗂" if self._mode == "projects" else "🏷"
            self._table.add_row(icon, tag_name, str(count), key=tag_name)
        self._has_more = False
        if rows:
            self._table.move_cursor(row=0)
        self._update_title()

    # Mode -> extra filter kwargs for ``list_conversations``.
    _MODE_FILTERS: Dict[str, Dict[str, Any]] = {
        "all":      {},
        "starred":  {"starred": True},
        "pinned":   {"pinned": True},
        "archived": {"archived": True, "include_archived": True},
    }

    def _fetch_page(self, cursor: str) -> PaginatedResult:
        """Run the right cursor-mode DB query for the current mode + search."""
        ps = self.DEFAULT_PAGE_SIZE
        if self._search:
            return cast(
                PaginatedResult,
                self._db.search_conversations(
                    self._search, cursor=cursor, page_size=ps
                ),
            )

        if self._mode == "recent":
            recent = cast(
                PaginatedResult,
                self._db.list_conversations(cursor="", page_size=20),
            )
            return PaginatedResult(items=recent.items, next_cursor=None, has_more=False)

        filters = self._MODE_FILTERS.get(self._mode, {})
        return cast(
            PaginatedResult,
            self._db.list_conversations(cursor=cursor, page_size=ps, **filters),
        )

    def _merge_page(self, page: PaginatedResult) -> int:
        """Append page items to the table; update cursor / has_more."""
        conv_ids = [str(getattr(c, "id", "")) for c in page.items]
        branch_counts: Dict[str, int] = {}
        try:
            branch_counts = self._db.batch_branch_counts(conv_ids)
        except Exception:
            pass

        added = 0
        for conv in page.items:
            conv_id = str(getattr(conv, "id", ""))
            conv.branch_count = branch_counts.get(conv_id, 0)
            updated = getattr(conv, "updated_at", None)
            updated_str = updated.strftime("%Y-%m-%d") if updated else ""
            self._table.add_row(
                _flags(conv),
                _title(conv),
                updated_str,
                key=conv_id,
            )
            self._conversations.append(conv)
            added += 1
        self._next_cursor = page.next_cursor
        self._has_more = page.has_more
        return added

    def _update_title(self) -> None:
        """Reflect current state in the sidebar header label."""
        if self._is_tag_browser():
            if self._selected_tag is None:
                label = "projects" if self._mode == "projects" else "tags"
                n = self._table.row_count
                self._title_label.update(f"{label} · {n} available")
            else:
                n = len(self._conversations)
                self._title_label.update(
                    f"🏷 {self._selected_tag} · {n} conversation{'s' if n != 1 else ''}"
                )
            return

        n = len(self._conversations)
        if self._has_more:
            self._title_label.update(f"conversations · {n} loaded · more (Ctrl+L)")
        elif n > 0:
            self._title_label.update(f"conversations · {n}")
        else:
            self._title_label.update("conversations")

    # ------------------------------------------------------------------
    # Tab activation
    # ------------------------------------------------------------------

    def on_tabs_tab_activated(self, event: Tabs.TabActivated) -> None:
        if event.tab is None:
            return
        new_mode = event.tab.id or "all"
        if new_mode != self._mode:
            self.set_mode(new_mode)
